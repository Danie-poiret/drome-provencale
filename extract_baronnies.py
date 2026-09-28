#!/usr/bin/env python3
"""
V1 — Extraction agenda des Baronnies en Drôme Provençale.

Objectif volontairement limité :
- lire l'agenda filtré "Baronnies en Drôme Provençale" ;
- récupérer les fiches officielles ;
- exclure les événements dont la commune est Nyons ;
- supprimer les doublons proprement ;
- écrire agenda.json ;
- aucun appel OpenAI / aucune génération SEO dans cette V1.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SOURCE = (
    "https://www.dromeprovencale.fr/agenda/tout-lagenda/"
    "recherche/territoire/baronnies-en-drome-provencale/"
)
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
CACHE = ROOT / "_detail_cache.json"

TIMEOUT = 30
MAX_LIST_PAGES = 100
DETAIL_TTL_HOURS = 24
REQUEST_DELAY = 0.25
MIN_VALID_EVENTS = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}

DATE_RE = re.compile(
    r"(?:(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+)?"
    r"(\d{1,2})\s+"
    r"(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    r"septembre|octobre|novembre|décembre|decembre)"
    r"(?:\s+(20\d{2}))?",
    re.I,
)
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])h([0-5]\d)?\b", re.I)

EVENT_PATH_PREFIXES = (
    "/fete-manifestation/",
    "/agenda/",
)


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize(value: object) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    scheme = parsed.scheme or "https"
    host = parsed.netloc.lower()
    path = re.sub(r"/+", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/") + "/"
    return f"{scheme}://{host}{path}"


def is_nyons(commune: str) -> bool:
    return normalize(commune) == "nyons"


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def parse_iso_date(value: object) -> str:
    text = clean(value)
    if not text:
        return ""
    m = re.match(r"^(20\d{2}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    return ""


def parse_french_date(text: str, default_year: int | None = None) -> str:
    m = DATE_RE.search(clean(text))
    if not m:
        return ""
    day = int(m.group(1))
    month = MONTHS[m.group(2).lower()]
    year = int(m.group(3)) if m.group(3) else (default_year or date.today().year)
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def iter_jsonld_objects(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from iter_jsonld_objects(v)
    elif isinstance(value, list):
        for item in value:
            yield from iter_jsonld_objects(item)


def is_event_jsonld(obj: dict) -> bool:
    typ = obj.get("@type")
    if isinstance(typ, list):
        return any(str(x).lower() == "event" for x in typ)
    return str(typ or "").lower() == "event"


def score_event_jsonld(obj: dict, expected_title: str) -> int:
    name = clean(obj.get("name"))
    if not name:
        return 0
    a = normalize(name)
    b = normalize(expected_title)
    if a == b:
        return 100
    if a and b and (a in b or b in a):
        return 80
    common = set(a.split()) & set(b.split())
    return min(60, len(common) * 8)


def best_event_jsonld(soup: BeautifulSoup, expected_title: str) -> dict:
    candidates = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue

        for obj in iter_jsonld_objects(payload):
            if isinstance(obj, dict) and is_event_jsonld(obj):
                candidates.append(obj)

    if not candidates:
        return {}

    candidates.sort(
        key=lambda obj: score_event_jsonld(obj, expected_title),
        reverse=True,
    )
    return candidates[0]


def extract_location_from_jsonld(event_obj: dict) -> tuple[str, str, str]:
    loc = event_obj.get("location")
    if isinstance(loc, list):
        loc = next((x for x in loc if isinstance(x, dict)), {})
    if not isinstance(loc, dict):
        return "", "", ""

    location_name = clean(loc.get("name"))
    address = loc.get("address")
    if isinstance(address, str):
        return location_name, clean(address), ""
    if not isinstance(address, dict):
        return location_name, "", ""

    locality = clean(address.get("addressLocality"))
    street = clean(address.get("streetAddress"))
    postal = clean(address.get("postalCode"))
    full = ", ".join(x for x in (street, postal, locality) if x)
    return location_name, full, locality


def extract_categories_from_jsonld(event_obj: dict) -> list[str]:
    values = []
    for key in ("eventType", "keywords", "category"):
        val = event_obj.get(key)
        if isinstance(val, str):
            values.extend(re.split(r"[,;|]", val))
        elif isinstance(val, list):
            values.extend(str(x) for x in val if x)
    out = []
    seen = set()
    for item in values:
        item = clean(item)
        n = normalize(item)
        if item and n not in seen:
            out.append(item)
            seen.add(n)
    return out


def canonical_url(soup: BeautifulSoup, fallback: str) -> str:
    link = soup.find("link", rel=lambda x: x and "canonical" in x)
    if link and link.get("href"):
        return normalize_url(urljoin(fallback, link["href"]))
    return normalize_url(fallback)


def extract_apidae_id(html_text: str) -> str:
    patterns = [
        r"(?i)\bapidae\b.{0,80}?\b(\d{5,12})\b",
        r"(?i)\bsitra\b.{0,80}?\b(\d{5,12})\b",
        r"(?i)\b(\d{5,12})\b.{0,80}?\bapidae\b",
        r"(?i)\b(\d{5,12})\b.{0,80}?\bsitra\b",
    ]
    for pattern in patterns:
        m = re.search(pattern, html_text, re.S)
        if m:
            return m.group(1)
    return ""


def event_url_from_heading(h2, base_url: str) -> str:
    a = h2.find("a", href=True)
    if not a:
        a = h2.find_parent("a", href=True)
    if not a:
        return ""
    url = normalize_url(urljoin(base_url, a["href"]))
    path = urlparse(url).path
    if not any(path.startswith(prefix) for prefix in EVENT_PATH_PREFIXES):
        return ""
    if "/tout-lagenda/" in path:
        return ""
    return url


def nearest_card(h2):
    current = h2
    best = h2.parent
    for _ in range(8):
        current = current.parent
        if current is None:
            break
        if current.name in ("article", "li"):
            return current
        text = clean(current.get_text(" ", strip=True))
        if 20 <= len(text) <= 1800:
            best = current
    return best


def guess_commune_from_card(card, title: str) -> str:
    if card is None:
        return ""
    candidates = []
    for node in card.find_all(["li", "span", "p", "div"], recursive=True):
        t = clean(node.get_text(" ", strip=True))
        if not t or t == title or len(t) > 80:
            continue
        if TIME_RE.fullmatch(t):
            continue
        if DATE_RE.search(t):
            continue
        if re.search(r"\d{1,2}\s*€", t):
            continue
        candidates.append(t)

    ignored = {
        "gratuit",
        "culture",
        "spectacle",
        "sports",
        "theatre",
        "oenologie",
        "nature et detente",
        "distractions et loisirs",
        "manifestations commerciales",
        "traditions et folklore",
    }
    for t in reversed(candidates):
        n = normalize(t)
        if not n or n in ignored:
            continue
        if 2 <= len(t) <= 60 and not re.search(r"\d", t):
            return t
    return ""


def extract_list_page(html_text: str, page_url: str) -> tuple[list[dict], str]:
    soup = BeautifulSoup(html_text, "html.parser")
    events = []
    seen = set()

    for h2 in soup.find_all("h2"):
        title = clean(h2.get_text(" ", strip=True))
        if len(title) < 3:
            continue

        url = event_url_from_heading(h2, page_url)
        if not url or url in seen:
            continue

        card = nearest_card(h2)
        card_text = clean(card.get_text(" ", strip=True)) if card else title
        commune = guess_commune_from_card(card, title)

        events.append(
            {
                "title": title,
                "url": url,
                "commune_hint": commune,
                "card_text": card_text[:1000],
            }
        )
        seen.add(url)

    next_url = ""
    next_link = soup.find("a", rel=lambda x: x and "next" in x)
    if next_link and next_link.get("href"):
        next_url = urljoin(page_url, next_link["href"])
    else:
        for a in soup.find_all("a", href=True):
            label = normalize(a.get_text(" ", strip=True))
            aria = normalize(a.get("aria-label", ""))
            cls = " ".join(a.get("class", []))
            if (
                label in {"suivant", "suivante", "next", "›", "»"}
                or "suivant" in aria
                or "next" in cls.lower()
            ):
                next_url = urljoin(page_url, a["href"])
                break

    return events, normalize_url(next_url) if next_url else ""


def cache_fresh(entry: dict) -> bool:
    stamp = clean(entry.get("fetched_at"))
    if not stamp:
        return False
    try:
        fetched = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - fetched.astimezone(timezone.utc)
        return age.total_seconds() < DETAIL_TTL_HOURS * 3600
    except Exception:
        return False


def fallback_detail_from_text(soup: BeautifulSoup, list_item: dict) -> dict:
    text = clean(soup.get_text(" ", strip=True))
    start = parse_french_date(text)
    return {
        "title": list_item["title"],
        "start_date": start,
        "end_date": start,
        "commune": clean(list_item.get("commune_hint")),
        "location": "",
        "address": "",
        "categories": [],
    }


def fetch_detail(session: requests.Session, item: dict, cache: dict) -> dict:
    url = item["url"]
    cached = cache.get(url)
    if isinstance(cached, dict) and cache_fresh(cached):
        result = cached.get("data")
        if isinstance(result, dict):
            return result

    r = session.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    event_obj = best_event_jsonld(soup, item["title"])
    if event_obj:
        title = clean(event_obj.get("name")) or item["title"]
        start = parse_iso_date(event_obj.get("startDate"))
        end = parse_iso_date(event_obj.get("endDate")) or start
        location_name, address, locality = extract_location_from_jsonld(event_obj)
        categories = extract_categories_from_jsonld(event_obj)
    else:
        fallback = fallback_detail_from_text(soup, item)
        title = fallback["title"]
        start = fallback["start_date"]
        end = fallback["end_date"]
        locality = fallback["commune"]
        location_name = fallback["location"]
        address = fallback["address"]
        categories = fallback["categories"]

    commune = clean(locality) or clean(item.get("commune_hint"))

    data = {
        "title": title,
        "start_date": start,
        "end_date": end,
        "commune": commune,
        "location": location_name,
        "address": address,
        "categories": categories,
        "url": canonical_url(soup, url),
        "apidae_id": extract_apidae_id(r.text),
        "source_url": url,
        "jsonld_event": bool(event_obj),
    }

    cache[url] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    return data


def merge_unique(values: list[str], additions: list[str]) -> list[str]:
    out = []
    seen = set()
    for value in list(values) + list(additions):
        value = clean(value)
        key = normalize(value)
        if value and key not in seen:
            out.append(value)
            seen.add(key)
    return out


def dedupe_key(event: dict) -> tuple[str, str]:
    apidae_id = clean(event.get("apidae_id"))
    if apidae_id:
        return "apidae", apidae_id

    canonical = normalize_url(clean(event.get("url")))
    if canonical:
        return "url", canonical

    fallback = "|".join(
        [
            normalize(event.get("title")),
            clean(event.get("start_date")),
            clean(event.get("end_date")),
            normalize(event.get("commune")),
        ]
    )
    return "fingerprint", hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def event_sort_key(event: dict):
    return (
        event.get("start_date") or "9999-12-31",
        normalize(event.get("commune")),
        normalize(event.get("title")),
    )


def main() -> None:
    session = requests.Session()
    cache = load_json(CACHE)

    print("SOURCE:", SOURCE)
    print("V1: extraction seule, sans GPT.")
    print("Règle commune: Nyons est exclu.")

    page_url = SOURCE
    visited_pages = set()
    listed = []
    listed_urls = set()

    for page_no in range(1, MAX_LIST_PAGES + 1):
        if not page_url or page_url in visited_pages:
            break
        visited_pages.add(page_url)

        r = session.get(page_url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        page_items, next_url = extract_list_page(r.text, page_url)

        new_count = 0
        for item in page_items:
            if item["url"] not in listed_urls:
                listed.append(item)
                listed_urls.add(item["url"])
                new_count += 1

        print(
            f"LISTE {page_no:02d}: HTTP {r.status_code} | "
            f"{len(page_items)} fiche(s) trouvée(s) | {new_count} nouvelle(s)"
        )

        if not next_url:
            break
        page_url = next_url
        time.sleep(REQUEST_DELAY)

    if len(listed) < MIN_VALID_EVENTS:
        raise RuntimeError(
            f"Extraction liste suspecte: seulement {len(listed)} fiche(s). "
            "agenda.json n'est pas remplacé."
        )

    print(f"TOTAL LIENS UNIQUES: {len(listed)}")

    detailed = []
    detail_errors = 0
    excluded_nyons = 0
    missing_commune = 0
    missing_date = 0

    for idx, item in enumerate(listed, 1):
        try:
            event = fetch_detail(session, item, cache)
        except Exception as exc:
            detail_errors += 1
            print(
                f"DETAIL {idx:03d}/{len(listed)} ERREUR — "
                f"{item['title']}: {exc}",
                file=sys.stderr,
            )
            continue

        commune = clean(event.get("commune"))
        if is_nyons(commune):
            excluded_nyons += 1
            print(
                f"DETAIL {idx:03d}/{len(listed)} EXCLU NYONS — "
                f"{event.get('title')}"
            )
            continue

        if not commune:
            missing_commune += 1
        if not event.get("start_date"):
            missing_date += 1

        detailed.append(event)
        print(
            f"DETAIL {idx:03d}/{len(listed)} OK — "
            f"{event.get('start_date') or '?'} | "
            f"{commune or 'COMMUNE ?'} | {event.get('title')}"
        )
        time.sleep(REQUEST_DELAY)

    save_json(CACHE, cache)

    unique = {}
    duplicate_count = 0

    for event in detailed:
        key_type, key_value = dedupe_key(event)
        key = f"{key_type}:{key_value}"

        if key not in unique:
            event["dedupe_method"] = key_type
            event["categories"] = merge_unique([], event.get("categories") or [])
            event["source_urls"] = merge_unique(
                [],
                [event.get("source_url", ""), event.get("url", "")],
            )
            unique[key] = event
            continue

        duplicate_count += 1
        existing = unique[key]
        existing["categories"] = merge_unique(
            existing.get("categories") or [],
            event.get("categories") or [],
        )
        existing["source_urls"] = merge_unique(
            existing.get("source_urls") or [],
            [event.get("source_url", ""), event.get("url", "")],
        )

        for field in (
            "commune",
            "location",
            "address",
            "start_date",
            "end_date",
            "apidae_id",
        ):
            if not clean(existing.get(field)) and clean(event.get(field)):
                existing[field] = event[field]

    events = sorted(unique.values(), key=event_sort_key)

    if len(events) < MIN_VALID_EVENTS:
        raise RuntimeError(
            f"Extraction finale suspecte: seulement {len(events)} événement(s). "
            "agenda.json n'est pas remplacé."
        )

    payload = {
        "source": SOURCE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "count": len(events),
        "diagnostics": {
            "list_pages": len(visited_pages),
            "list_unique_urls": len(listed),
            "detail_errors": detail_errors,
            "excluded_nyons": excluded_nyons,
            "duplicates_removed": duplicate_count,
            "missing_commune": missing_commune,
            "missing_date": missing_date,
        },
        "events": events,
    }
    save_json(OUT, payload)

    print()
    print("=== BILAN V1 ===")
    print(f"Pages liste             : {len(visited_pages)}")
    print(f"Liens uniques           : {len(listed)}")
    print(f"Nyons exclus            : {excluded_nyons}")
    print(f"Doublons supprimés      : {duplicate_count}")
    print(f"Erreurs fiches          : {detail_errors}")
    print(f"Commune manquante       : {missing_commune}")
    print(f"Date manquante          : {missing_date}")
    print(f"Événements conservés    : {len(events)}")
    print(f"OK: {OUT.name} écrit.")


if __name__ == "__main__":
    main()
