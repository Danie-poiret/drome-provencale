#!/usr/bin/env python3
"""
V1 Baronnies hors Nyons via La Drôme Tourisme.

Pourquoi cette source ?
Le site dromeprovencale.fr bloque les runners GitHub Actions (HTTP 403),
y compris ses variantes PDF. La Drôme Tourisme reprend les données
Apidae du territoire et reste une source institutionnelle départementale.

Cette V1 :
- parcourt l'agenda départemental ;
- ne garde que les 67 communes officielles de la CCBDP ;
- exclut Nyons ;
- récupère les fiches détaillées quand possible ;
- supprime les doublons par URL canonique puis empreinte titre/date/commune ;
- n'utilise pas OpenAI.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
CACHE_FILE = ROOT / "_detail_cache.json"

SOURCE = "https://www.drome-cestmanature.com/preparez-votre-sejour/l-agenda/"
TIMEOUT = 30
REQUEST_DELAY = 0.20
MAX_LIST_PAGES = 40
DETAIL_TTL_HOURS = 24
MIN_VALID_EVENTS = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

# Liste officielle des 67 communes de la Communauté de communes des
# Baronnies en Drôme Provençale. Nyons est volontairement exclu ensuite.
COMMUNES = {
    "Arpavon", "Aubres", "Aulan", "Ballons", "Barret-de-Lioure",
    "Beauvoisin", "Bellecombe-Tarendol", "Bénivay-Ollon", "Bésignan",
    "Buis-les-Baronnies", "Châteauneuf-de-Bordette", "Chaudebonne",
    "Chauvac-Laux-Montaux", "Condorcet", "Cornillac", "Cornillon-sur-l’Oule",
    "Curnier", "Eygalayes", "Eygaliers", "Eyroles", "Izon-la-Bruisse",
    "La Charce", "La Penne-sur-l’Ouvèze", "La Roche-sur-le-Buis",
    "La Rochette-du-Buis", "Le Poët-en-Percip", "Le Poët-Sigillat", "Lemps",
    "Les Pilles", "Mérindol-les-Oliviers", "Mévouillon",
    "Mirabel-aux-Baronnies", "Montauban-sur-l’Ouvèze", "Montaulieu",
    "Montbrun-les-Bains", "Montferrand-la-Fare", "Montguers",
    "Montréal-les-Sources", "Nyons", "Pelonne", "Piégon", "Pierrelongue",
    "Plaisians", "Pommerol", "Propiac", "Reilhanette", "Rémuzat", "Rioms",
    "Rochebrune", "Roussieux", "Sahune", "Saint-Auban-sur-l’Ouvèze",
    "Saint-Ferréol-Trente-Pas", "Saint-Maurice-sur-Eygues", "Saint-May",
    "Saint-Sauveur-Gouvernet", "Sainte-Euphémie-sur-Ouvèze", "Sainte-Jalle",
    "Séderon", "Valouse", "Venterol", "Verclause", "Vercoiran",
    "Vers-sur-Méouge", "Villefranche-le-Château", "Villeperdrix", "Vinsobres",
}

MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}
MONTH_RE = "|".join(MONTHS)
DATE_RE = re.compile(
    rf"(?:(?:lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?)\s+)?"
    rf"(\d{{1,2}})\s+({MONTH_RE})(?:\s+(20\d{{2}}))?",
    re.I,
)
RESULTS_RE = re.compile(r"sur\s+([0-9\s]+)\s+r[ée]sultat", re.I)

GENERIC_LINK_TEXT = {
    "en savoir plus", "voir la fiche", "ouvrir le lien", "réserver", "reserver",
    "découvrir", "decouvrir", "plus d'informations", "plus d informations",
}


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


COMMUNE_BY_NORM = {norm(c): c for c in COMMUNES}
COMMUNE_KEYS = sorted(COMMUNE_BY_NORM, key=len, reverse=True)


def find_commune(text: str) -> str:
    n = f" {norm(text)} "
    for key in COMMUNE_KEYS:
        if f" {key} " in n:
            return COMMUNE_BY_NORM[key]
    return ""


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


def is_detail_url(url: str) -> bool:
    p = urlparse(url)
    path = p.path.lower()
    return "/fiches/" in path or bool(re.fullmatch(r"/\?p=\d+", path + ("?" + p.query if p.query else "")))


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
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def nearest_card(anchor):
    best = anchor.parent
    current = anchor
    for _ in range(8):
        current = current.parent
        if current is None:
            break
        text = clean(current.get_text(" ", strip=True))
        if current.name in ("article", "li") and 15 <= len(text) <= 2200:
            return current
        if 20 <= len(text) <= 1400:
            best = current
    return best


def title_from_card(card, anchor) -> str:
    if card:
        for tag in ("h2", "h3", "h4"):
            node = card.find(tag)
            if node:
                t = clean(node.get_text(" ", strip=True))
                if len(t) >= 3:
                    return t
    t = clean(anchor.get_text(" ", strip=True))
    if norm(t) in {norm(x) for x in GENERIC_LINK_TEXT}:
        return ""
    return t if len(t) >= 3 else ""


def parse_french_dates(text: str) -> tuple[str, str]:
    matches = list(DATE_RE.finditer(clean(text)))
    if not matches:
        return "", ""

    explicit_years = [int(m.group(3)) for m in matches if m.group(3)]
    fallback_year = explicit_years[0] if explicit_years else date.today().year
    values = []
    previous_month = None
    current_year = fallback_year

    for m in matches:
        day = int(m.group(1))
        month = MONTHS[m.group(2).lower()]
        if m.group(3):
            current_year = int(m.group(3))
        elif previous_month is not None and month < previous_month - 6:
            current_year += 1
        previous_month = month
        try:
            values.append(date(current_year, month, day))
        except ValueError:
            continue

    if not values:
        return "", ""
    return values[0].isoformat(), values[-1].isoformat()


def list_page_url(page_no: int) -> str:
    if page_no <= 1:
        return SOURCE
    return urljoin(SOURCE, f"page/{page_no}/")


def detect_total_pages(soup: BeautifulSoup) -> int | None:
    text = clean(soup.get_text(" ", strip=True))
    m = RESULTS_RE.search(text)
    if m:
        try:
            total = int(re.sub(r"\s+", "", m.group(1)))
            if total > 0:
                # Le site affiche actuellement 36 résultats par page.
                return max(1, math.ceil(total / 36))
        except ValueError:
            pass

    nums = []
    for a in soup.find_all("a", href=True):
        mm = re.search(r"/page/(\d+)/?", a["href"])
        if mm:
            nums.append(int(mm.group(1)))
    return max(nums) if nums else None


def extract_list_items(html_text: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = normalize_url(urljoin(page_url, a["href"]))
        if not is_detail_url(href) or href in seen:
            continue

        card = nearest_card(a)
        if not card:
            continue
        card_text = clean(card.get_text(" ", strip=True))
        if len(card_text) < 10:
            continue

        commune = find_commune(card_text)
        if not commune:
            continue

        title = title_from_card(card, a)
        if not title:
            continue

        # On ne garde déjà que notre territoire, puis on enlève Nyons.
        if norm(commune) == "nyons":
            items.append({
                "title": title,
                "url": href,
                "commune": commune,
                "card_text": card_text[:1200],
                "excluded_nyons": True,
            })
            seen.add(href)
            continue

        start, end = parse_french_dates(card_text)
        items.append({
            "title": title,
            "url": href,
            "commune": commune,
            "start_date": start,
            "end_date": end,
            "card_text": card_text[:1200],
            "excluded_nyons": False,
        })
        seen.add(href)

    return items


def iter_jsonld(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from iter_jsonld(v)
    elif isinstance(value, list):
        for item in value:
            yield from iter_jsonld(item)


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
        for obj in iter_jsonld(payload):
            if not isinstance(obj, dict):
                continue
            typ = obj.get("@type")
            types = [typ] if isinstance(typ, str) else (typ or [])
            if any(str(x).lower() == "event" for x in types):
                candidates.append(obj)

    if not candidates:
        return {}

    target = norm(expected_title)
    def score(obj):
        n = norm(obj.get("name"))
        if n == target:
            return 100
        if n and target and (n in target or target in n):
            return 80
        return len(set(n.split()) & set(target.split())) * 5

    return max(candidates, key=score)


def parse_iso_date(value: object) -> str:
    m = re.match(r"^(20\d{2}-\d{2}-\d{2})", clean(value))
    return m.group(1) if m else ""


def location_from_jsonld(obj: dict) -> tuple[str, str, str]:
    loc = obj.get("location")
    if isinstance(loc, list):
        loc = next((x for x in loc if isinstance(x, dict)), {})
    if not isinstance(loc, dict):
        return "", "", ""
    name = clean(loc.get("name"))
    addr = loc.get("address")
    if isinstance(addr, str):
        return name, clean(addr), find_commune(addr)
    if not isinstance(addr, dict):
        return name, "", ""
    locality = clean(addr.get("addressLocality"))
    street = clean(addr.get("streetAddress"))
    postal = clean(addr.get("postalCode"))
    full = ", ".join(x for x in (street, postal, locality) if x)
    return name, full, locality


def canonical_url(soup: BeautifulSoup, fallback: str) -> str:
    node = soup.find("link", rel=lambda x: x and "canonical" in x)
    if node and node.get("href"):
        return normalize_url(urljoin(fallback, node["href"]))
    return normalize_url(fallback)


def cache_fresh(entry: dict) -> bool:
    stamp = clean(entry.get("fetched_at"))
    if not stamp:
        return False
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() < DETAIL_TTL_HOURS * 3600
    except Exception:
        return False


def fetch_detail(session: requests.Session, item: dict, cache: dict) -> dict:
    url = item["url"]
    cached = cache.get(url)
    if isinstance(cached, dict) and cache_fresh(cached):
        data = cached.get("data")
        if isinstance(data, dict):
            return data

    r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    event_obj = best_event_jsonld(soup, item["title"])

    title = item["title"]
    start = item.get("start_date", "")
    end = item.get("end_date", "") or start
    commune = item["commune"]
    location = ""
    address = ""

    if event_obj:
        title = clean(event_obj.get("name")) or title
        start = parse_iso_date(event_obj.get("startDate")) or start
        end = parse_iso_date(event_obj.get("endDate")) or end or start
        location, address, locality = location_from_jsonld(event_obj)
        detailed_commune = find_commune(locality) or find_commune(address)
        if detailed_commune:
            commune = detailed_commune
    else:
        h1 = soup.find("h1")
        if h1:
            title = clean(h1.get_text(" ", strip=True)) or title
        page_text = clean(soup.get_text(" ", strip=True))
        pstart, pend = parse_french_dates(page_text[:5000])
        start = pstart or start
        end = pend or end or start
        detailed_commune = find_commune(page_text[:6000])
        if detailed_commune:
            commune = detailed_commune

    data = {
        "title": title,
        "start_date": start,
        "end_date": end or start,
        "commune": commune,
        "location": location,
        "address": address,
        "url": canonical_url(soup, r.url),
        "source_url": url,
        "jsonld_event": bool(event_obj),
    }
    cache[url] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    return data


def fingerprint(event: dict) -> str:
    raw = "|".join([
        norm(event.get("title")),
        clean(event.get("start_date")),
        clean(event.get("end_date")),
        norm(event.get("commune")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def main() -> None:
    session = requests.Session()
    cache = load_json(CACHE_FILE)

    print("SOURCE:", SOURCE)
    print("V1: La Drôme Tourisme / Apidae, hors Nyons, sans GPT.")

    collected = {}
    nyons_seen = 0
    total_pages = None

    for page_no in range(1, MAX_LIST_PAGES + 1):
        if total_pages is not None and page_no > total_pages:
            break

        url = list_page_url(page_no)
        r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        print(f"LISTE {page_no:02d}: HTTP {r.status_code} | {len(r.text)} octets | {r.url}")
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")
        if total_pages is None:
            detected = detect_total_pages(soup)
            if detected:
                total_pages = min(detected, MAX_LIST_PAGES)
                print(f"PAGES DETECTEES: {detected} (plafond utilisé: {total_pages})")

        items = extract_list_items(r.text, r.url)
        page_new = 0
        page_nyons = 0
        for item in items:
            if item.get("excluded_nyons"):
                nyons_seen += 1
                page_nyons += 1
                continue
            if item["url"] not in collected:
                collected[item["url"]] = item
                page_new += 1

        print(
            f"  Baronnies hors Nyons: {page_new} nouvelle(s) | "
            f"Nyons écarté(s): {page_nyons}"
        )
        time.sleep(REQUEST_DELAY)

    if len(collected) < MIN_VALID_EVENTS:
        raise RuntimeError(
            f"Extraction liste suspecte: seulement {len(collected)} événement(s) "
            "Baronnies hors Nyons. agenda.json n'est pas remplacé."
        )

    print(f"LIENS BARRONNIES HORS NYONS: {len(collected)}")

    detailed = []
    detail_errors = 0
    excluded_nyons_detail = 0

    items = list(collected.values())
    for idx, item in enumerate(items, 1):
        try:
            event = fetch_detail(session, item, cache)
        except Exception as exc:
            detail_errors += 1
            event = {
                "title": item["title"],
                "start_date": item.get("start_date", ""),
                "end_date": item.get("end_date", ""),
                "commune": item["commune"],
                "location": "",
                "address": "",
                "url": item["url"],
                "source_url": item["url"],
                "jsonld_event": False,
                "detail_error": clean(exc),
            }
            print(f"DETAIL {idx:03d}/{len(items)} AVERTISSEMENT — {item['title']}: {exc}", file=sys.stderr)

        if norm(event.get("commune")) == "nyons":
            excluded_nyons_detail += 1
            continue
        if not find_commune(event.get("commune", "")):
            # Si une fiche détaillée renvoie une commune hors territoire, on la retire.
            continue

        detailed.append(event)
        print(
            f"DETAIL {idx:03d}/{len(items)} OK — "
            f"{event.get('start_date') or '?'} | {event.get('commune') or '?'} | "
            f"{event.get('title')}"
        )
        time.sleep(REQUEST_DELAY)

    save_json(CACHE_FILE, cache)

    unique = []
    seen_urls = set()
    seen_fp = set()
    duplicates = 0
    missing_date = 0

    for event in sorted(
        detailed,
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            norm(e.get("commune")),
            norm(e.get("title")),
        ),
    ):
        url_key = normalize_url(event.get("url", ""))
        fp = fingerprint(event)
        if (url_key and url_key in seen_urls) or fp in seen_fp:
            duplicates += 1
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_fp.add(fp)
        event["dedupe_id"] = fp[:16]
        if not event.get("start_date"):
            missing_date += 1
        unique.append(event)

    if len(unique) < MIN_VALID_EVENTS:
        raise RuntimeError(
            f"Extraction finale suspecte: seulement {len(unique)} événement(s). "
            "agenda.json n'est pas remplacé."
        )

    payload = {
        "source": SOURCE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "67 communes CCBDP, hors Nyons",
        "count": len(unique),
        "diagnostics": {
            "list_pages_scanned": total_pages or MAX_LIST_PAGES,
            "nyons_excluded_on_list": nyons_seen,
            "nyons_excluded_on_detail": excluded_nyons_detail,
            "detail_errors": detail_errors,
            "duplicates_removed": duplicates,
            "missing_date": missing_date,
        },
        "events": unique,
    }
    save_json(OUT, payload)

    print("=== BILAN V1 ===")
    print(f"Nyons exclus (liste)    : {nyons_seen}")
    print(f"Nyons exclus (détail)   : {excluded_nyons_detail}")
    print(f"Erreurs fiches          : {detail_errors}")
    print(f"Doublons supprimés      : {duplicates}")
    print(f"Dates manquantes        : {missing_date}")
    print(f"Événements conservés    : {len(unique)}")
    print("OK: agenda.json écrit.")


if __name__ == "__main__":
    main()
