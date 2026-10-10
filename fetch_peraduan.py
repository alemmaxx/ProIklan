#!/usr/bin/env python3
"""ProIklan: ambil senarai peraduan daripada RSS/Atom (percuma, tanpa pustaka luar).

Guna:
  python fetch_peraduan.py                 -> kemas kini data/peraduan.json
  python fetch_peraduan.py --test fail.xml -> uji parsing satu fail RSS/Atom (tanpa internet)

Sumber diset dalam sources.json. Facebook/Instagram TIDAK disokong (dilarang scrape).
Hanya tajuk, ringkasan pendek (maks 220 aksara), imej dan pautan asal disimpan,
supaya pengunjung dihantar ke sumber asal.
"""
import hashlib
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
SOURCES = ROOT / "sources.json"
OUT = ROOT / "data" / "peraduan.json"
UA = "ProIklanBot/0.1 (peribadi; hormati robots.txt)"
MAX_ITEMS = 60
MYT = timezone(timedelta(hours=8))

KEYWORDS = ("peraduan", "giveaway", "cabutan bertuah", "contest")
# Frasa yang selalu muncul dalam penipuan: item ini diletakkan dalam senarai semakan, bukan slider.
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


def parse_feed(xml_bytes, source_name, ref=None):
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
        text = (title + " " + summary).lower()
        if not link or not any(k in text for k in KEYWORDS):
            continue
        end = find_end_date(title + " " + summary, ref)
        # Ada tarikh dalam teks tetapi semuanya sudah lepas -> peraduan tamat, langkau.
        if end is None and DATE_RE.search(title + " " + summary):
            continue
        hit = [k for k in SCAM if k in text]
        items.append({
            "id": hashlib.sha1(link.encode()).hexdigest()[:10],
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


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--test":
        for it in parse_feed(Path(sys.argv[2]).read_bytes(), "ujian"):
            print(json.dumps(it, ensure_ascii=False))
        return

    ref = today()
    old = {}
    if OUT.exists():
        try:
            for it in json.loads(OUT.read_text(encoding="utf-8")).get("items", []):
                if not it.get("end") or it["end"] >= ref.isoformat():
                    old[it["id"]] = it
        except (ValueError, KeyError):
            pass

    merged = dict(old)
    for src in json.loads(SOURCES.read_text(encoding="utf-8")):
        try:
            for it in parse_feed(fetch(src["url"]), src["name"], ref):
                merged[it["id"]] = it
            print("OK  ", src["name"])
        except Exception as ex:  # satu sumber gagal tidak patut hentikan yang lain
            print("GAGAL", src["name"], "-", ex)

    items = sorted(merged.values(), key=lambda x: (x["end"] is None, x["end"] or "", x["title"]))[:MAX_ITEMS]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps({"updated": datetime.now(MYT).isoformat(timespec="minutes"), "items": items},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    print(len(items), "item disimpan ke", OUT)


if __name__ == "__main__":
    main()
