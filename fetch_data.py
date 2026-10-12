#!/usr/bin/env python3
"""ProIklan: ambil peraduan dan promosi daripada RSS/Atom, simpan ke Firestore.

Guna:
  python fetch_data.py                  -> ambil dan tulis ke Firestore (perlu FIREBASE_SERVICE_ACCOUNT)
  python fetch_data.py --dry            -> ambil dan PAPAR sahaja, tiada tulis (uji sumber di komputer anda)
  python fetch_data.py --test fail.xml  -> uji parsing satu fail RSS/Atom tanpa internet

Sumber diset dalam sources.json ("type": "peraduan" atau "iklan"; "format": "sitemap" untuk laman tanpa RSS).
Facebook/Instagram TIDAK disokong (dilarang scrape).
Hanya tajuk, ringkasan pendek (maks 220 aksara), imej dan pautan asal disimpan.
Status keputusan admin (lulus/tolak) tidak akan ditimpa bila item dikemas kini.
"""
import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).parent
SOURCES = ROOT / "sources.json"
UA = "ProIklanBot/0.2 (peribadi; hormati robots.txt)"
MYT = timezone(timedelta(hours=8))
KEEP_DAYS = 30  # buang item lama dari Firestore supaya kekal dalam kuota percuma

KEYWORDS = {
    "peraduan": ("peraduan", "giveaway", "cabutan bertuah", "contest"),
    "iklan": ("promosi", "promo ", "diskaun", "jualan murah", "tawaran", "sale "),
}
COLLECTION = {"peraduan": "peraduan", "iklan": "iklan"}
# Frasa lazim dalam penipuan: item ini masuk senarai semakan admin, tidak terus tayang.
SCAM = (
    "yuran penyertaan", "bayar untuk sertai", "bayar untuk menang", "wang pendahuluan",
    "deposit dahulu", "bank in", "bank-in", "pos ke akaun", "cukai kemenangan",
)
MONTHS = {
    "januari": 1, "februari": 2, "mac": 3, "april": 4, "mei": 5, "jun": 6, "julai": 7,
    "ogos": 8, "september": 9, "oktober": 10, "november": 11, "disember": 12,
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "august": 8, "october": 10, "december": 12,
}
DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b\s*(\d{4})?", re.I)


def today():
    return datetime.now(MYT).date()


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child_text(e, name):
    for c in e:
        if local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def clean(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def find_image(e, raw):
    for c in e.iter():
        n = local(c.tag)
        url = c.get("url")
        if n in ("content", "thumbnail", "enclosure") and url:
            typ = c.get("type") or ""
            medium = c.get("medium") or ""
            if n == "thumbnail" or "image" in typ or medium == "image":
                return url
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', html.unescape(raw))
    return m.group(1) if m else ""


def find_end_date(text, ref):
    """Tarikh paling lewat yang >= hari ini dalam teks, atau None."""
    found = []
    for d, mon, yr in DATE_RE.findall(text):
        try:
            found.append(date(int(yr) if yr else ref.year, MONTHS[mon.lower()], int(d)))
        except ValueError:
            continue
    future = [x for x in found if x >= ref]
    return max(future) if future else None


def parse_feed(xml_bytes, source_name, ref=None, kind="peraduan"):
    ref = ref or today()
    root = ET.fromstring(xml_bytes)
    items = []
    for e in root.iter():
        if local(e.tag) not in ("item", "entry"):
            continue
        title = clean(child_text(e, "title"))
        link = child_text(e, "link")
        if not link:
            for c in e:
                if local(c.tag) == "link" and c.get("href"):
                    link = c.get("href")
                    break
        raw = " ".join(child_text(e, n) for n in ("description", "summary", "content", "encoded"))
        summary = clean(raw)
        text = (title + " " + summary).lower() + " "
        if not link.startswith(("http://", "https://")) or not any(k in text for k in KEYWORDS[kind]):
            continue
        end = find_end_date(title + " " + summary, ref)
        # Ada tarikh dalam teks tetapi semuanya sudah lepas -> tamat, langkau.
        if end is None and DATE_RE.search(title + " " + summary):
            continue
        hit = [k for k in SCAM if k in text]
        items.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:20],
            "type": kind,
            "title": title[:140],
            "summary": summary[:220],
            "link": link,
            "source": source_name,
            "image": find_image(e, raw),
            "end": end.isoformat() if end else None,
            "flag": bool(hit),
            "why": ("Mengandungi frasa mencurigakan: " + ", ".join(hit)) if hit else "",
            "found": ref.isoformat(),
        })
    return items


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- robots.txt (ikut semantik Google: padanan terpanjang menang) ----------
_ROBOTS = {}


def parse_robots(txt):
    groups, agents, rules, last_agent = [], [], [], False
    for raw in txt.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        k, v = [x.strip() for x in line.split(":", 1)]
        k = k.lower()
        if k == "user-agent":
            if not last_agent and agents:
                groups.append((agents, rules))
                agents, rules = [], []
            agents.append(v.lower())
            last_agent = True
        elif k in ("allow", "disallow"):
            last_agent = False
            rules.append((k == "allow", v))
    if agents:
        groups.append((agents, rules))
    me = UA.split("/")[0].lower()
    chosen = [r for a, r in groups if any(x != "*" and x in me for x in a)] or [r for a, r in groups if "*" in a]
    return [rule for g in chosen for rule in g]


def path_allowed(rules, path):
    best_len, best_allow = -1, True
    for allow, pat in rules:
        if not pat:
            continue
        rx = re.escape(pat).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        if re.match(rx, path) and (len(pat) > best_len or (len(pat) == best_len and allow)):
            best_len, best_allow = len(pat), allow
    return best_allow


def robots_ok(url):
    u = urlparse(url)
    root = f"{u.scheme}://{u.netloc}"
    if root not in _ROBOTS:
        try:
            _ROBOTS[root] = parse_robots(fetch(root + "/robots.txt").decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            _ROBOTS[root] = [] if e.code == 404 else None
        except Exception:
            _ROBOTS[root] = None
    rules = _ROBOTS[root]
    if rules is None:  # tidak dapat baca robots.txt -> jangan ambil
        return False
    return path_allowed(rules, (u.path or "/") + ("?" + u.query if u.query else ""))


# ---------- sumber jenis sitemap (laman tanpa RSS) ----------
DEFAULT_EXCLUDE = re.compile(
    r"/(go|api|admin|dashboard|auth|login|register|tag|tags|category|kategori|page|about|privacy|terms|contact)(/|$)", re.I)


def meta(h, prop):
    for pat in (r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*?content=["\']([^"\']*)["\']',
                r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']%s["\']'):
        m = re.search(pat % re.escape(prop), h, re.I)
        if m:
            return clean(m.group(1))
    return ""


def parse_sitemap(xml_bytes):
    root = ET.fromstring(xml_bytes)
    out = []
    for e in root:
        loc = child_text(e, "loc")
        if loc:
            out.append((loc, child_text(e, "lastmod")))
    return local(root.tag), out


def parse_page(url, h, source_name, ref, kind):
    m = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
    title = meta(h, "og:title") or (clean(m.group(1)) if m else "")
    if not title:
        return None
    summary = meta(h, "og:description") or meta(h, "description")
    image = meta(h, "og:image")
    image = urljoin(url, image) if image else ""
    body = clean(re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I))[:3000]
    head = title + " " + summary
    # Semua tarikh dalam tajuk/ringkasan sudah lepas -> peraduan tamat.
    if find_end_date(head, ref) is None and DATE_RE.search(head):
        return None
    end = find_end_date(head + " " + body, ref)
    hit = [k for k in SCAM if k in (head + " " + body).lower() + " "]
    return {
        "id": hashlib.sha1(url.encode()).hexdigest()[:20],
        "type": kind,
        "title": title[:140],
        "summary": summary[:220],
        "link": url,
        "source": source_name,
        "image": image,
        "end": end.isoformat() if end else None,
        "flag": bool(hit),
        "why": ("Mengandungi frasa mencurigakan: " + ", ".join(hit)) if hit else "",
        "found": ref.isoformat(),
    }


def collect_sitemap(src, ref, kind, delay=1.0):
    kindname, entries = parse_sitemap(fetch(src["url"]))
    if kindname == "sitemapindex":  # satu aras sub-sitemap
        sub = []
        for loc, _ in entries[:5]:
            sub += parse_sitemap(fetch(loc))[1]
        entries = sub
    inc = re.compile(src["include"]) if src.get("include") else None
    exc = re.compile(src["exclude"]) if src.get("exclude") else None
    urls = []
    for loc, last in entries:
        path = urlparse(loc).path
        if path in ("", "/") or DEFAULT_EXCLUDE.search(path):
            continue
        if (inc and not inc.search(loc)) or (exc and exc.search(loc)):
            continue
        if not robots_ok(loc):
            continue
        urls.append((last, loc))
    urls.sort(reverse=True)  # lastmod terbaru dahulu
    items = []
    for last, loc in urls[: int(src.get("limit", 15))]:
        try:
            it = parse_page(loc, fetch(loc).decode("utf-8", "ignore"), src["name"], ref, kind)
            if it:
                items.append(it)
        except Exception as ex:
            print("  langkau", loc, "-", ex)
        time.sleep(delay)
    return items


def collect(ref):
    merged = {}
    for src in json.loads(SOURCES.read_text(encoding="utf-8")):
        kind = src.get("type", "peraduan")
        try:
            if src.get("format") == "sitemap":
                found = collect_sitemap(src, ref, kind)
            else:
                found = parse_feed(fetch(src["url"]), src["name"], ref, kind)
            for it in found:
                merged[(kind, it["id"])] = it
            print("OK   ", src["name"], len(found))
        except Exception as ex:  # satu sumber gagal tidak patut hentikan yang lain
            print("GAGAL", src["name"], "-", ex)
    return list(merged.values())


def sync(db, items, ref):
    """Tulis ke Firestore. Item baharu: status 'pending' jika mencurigakan, kalau tidak 'live'.
    Item sedia ada: kemas kini kandungan sahaja, status (keputusan admin) kekal."""
    added = updated = 0
    for it in items:
        col = COLLECTION[it["type"]]
        data = {k: v for k, v in it.items() if k not in ("id", "found")}
        ref_doc = db.collection(col).document(it["id"])
        snap = ref_doc.get()
        if snap.exists:
            ref_doc.update(data)
            updated += 1
        else:
            data["found"] = it["found"]
            data["status"] = "pending" if it["flag"] else "live"
            ref_doc.set(data)
            added += 1
    cutoff = (ref - timedelta(days=KEEP_DAYS)).isoformat()
    removed = 0
    for col in COLLECTION.values():
        for d in db.collection(col).where("found", "<", cutoff).stream():
            if (d.to_dict() or {}).get("source") != "Admin":  # jangan buang entri manual admin
                d.reference.delete()
                removed += 1
    print(f"Firestore: {added} baharu, {updated} dikemas kini, {removed} lama dibuang")


def firestore_client():
    import firebase_admin
    from firebase_admin import credentials, firestore
    cred = credentials.Certificate(json.loads(os.environ["FIREBASE_SERVICE_ACCOUNT"]))
    firebase_admin.initialize_app(cred)
    return firestore.client()


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        for it in parse_feed(Path(sys.argv[2]).read_bytes(), "ujian"):
            print(json.dumps(it, ensure_ascii=False))
        return

    ref = today()
    items = collect(ref)
    print(len(items), "item dijumpai")
    if "--dry" in sys.argv:
        for it in items[:20]:
            print("-", it["type"], "|", it["title"], "|", it["end"] or "tiada tarikh", "|", "SEMAK" if it["flag"] else "ok")
        return
    if not os.environ.get("FIREBASE_SERVICE_ACCOUNT"):
        sys.exit("FIREBASE_SERVICE_ACCOUNT tidak ditetapkan. Guna --dry untuk uji tanpa menulis.")
    sync(firestore_client(), items, ref)


if __name__ == "__main__":
    main()
