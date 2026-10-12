#!/usr/bin/env python3
"""ProIklan: ambil peraduan dan promosi daripada RSS/Atom, simpan ke Firestore.

Guna:
  python fetch_data.py                  -> ambil dan tulis ke Firestore (perlu FIREBASE_SERVICE_ACCOUNT)
  python fetch_data.py --dry            -> ambil dan PAPAR sahaja, tiada tulis (uji sumber di komputer anda)
  python fetch_data.py --test fail.xml  -> uji parsing satu fail RSS/Atom tanpa internet

Sumber diset dalam sources.json ("type": "peraduan" atau "iklan").
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
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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


def collect(ref):
    merged = {}
    for src in json.loads(SOURCES.read_text(encoding="utf-8")):
        kind = src.get("type", "peraduan")
        try:
            for it in parse_feed(fetch(src["url"]), src["name"], ref, kind):
                merged[(kind, it["id"])] = it
            print("OK   ", src["name"])
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
