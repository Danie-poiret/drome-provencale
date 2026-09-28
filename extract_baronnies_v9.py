#!/usr/bin/env python3
"""
V9 — Agenda Baronnies hors Nyons via La Drôme Tourisme filtrée.

Principe :
- utilise le filtre officiel La Drôme Tourisme « commune + rayon » ;
- lance plusieurs recherches de 30 km autour de communes repères des Baronnies
  pour couvrir le territoire sans scanner toute la Drôme ;
- dates calculées automatiquement : aujourd'hui -> +180 jours ;
- collecte les liens /fiches/ de toutes les pages de résultats ;
- ouvre chaque fiche une seule fois grâce au dédoublonnage/cache ;
- garde uniquement les 67 communes des Baronnies et exclut Nyons ;
- récupère titre, dates, description, adresse, ouverture, tarifs et contact ;
- protège agenda.json si le résultat final paraît anormalement faible.

Aucun appel OpenAI dans cette version.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
CACHE_FILE = ROOT / "_detail_cache_v9.json"

BASE = "https://www.drome-cestmanature.com/preparez-votre-sejour/l-agenda/"
SEARCH_CENTERS = (
    "nyons",
    "buis-les-baronnies",
    "remuzat",
    "montbrun-les-bains",
    "sederon",
)
RADIUS_KM = 30
HORIZON_DAYS = 180
TIMEOUT = 35
REQUEST_DELAY = 0.10
MAX_PAGES_PER_CENTER = 30
DETAIL_TTL_HOURS = 20
MIN_FINAL_EVENTS = 15

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
}

BARONNIES_COMMUNES = {
    "Arpavon", "Aubres", "Aulan", "Ballons", "Barret-de-Lioure",
    "Beauvoisin", "Bellecombe-Tarendol", "Bénivay-Ollon", "Bésignan",
    "Buis-les-Baronnies", "La Charce", "Châteauneuf-de-Bordette",
    "Chaudebonne", "Chauvac-Laux-Montaux", "Condorcet", "Cornillac",
    "Cornillon-sur-l’Oule", "Curnier", "Eygalayes", "Eygaliers", "Eyroles",
    "Izon-la-Bruisse", "Lemps", "Mérindol-les-Oliviers", "Mévouillon",
    "Mirabel-aux-Baronnies", "Montauban-sur-l’Ouvèze", "Montaulieu",
    "Montbrun-les-Bains", "Montferrand-la-Fare", "Montguers",
    "Montréal-les-Sources", "Nyons", "Pelonne", "La Penne-sur-l’Ouvèze",
    "Piégon", "Pierrelongue", "Les Pilles", "Plaisians", "Le Poët-en-Percip",
    "Le Poët-Sigillat", "Pommerol", "Propiac", "Reilhanette", "Rémuzat",
    "Rioms", "Rochebrune", "La Roche-sur-le-Buis", "La Rochette-du-Buis",
    "Roussieux", "Sahune", "Saint-Auban-sur-l’Ouvèze",
    "Sainte-Euphémie-sur-Ouvèze", "Saint-Ferréol-Trente-Pas", "Sainte-Jalle",
    "Saint-Maurice-sur-Eygues", "Saint-May", "Saint-Sauveur-Gouvernet",
    "Séderon", "Valouse", "Venterol", "Verclause", "Vercoiran",
    "Vers-sur-Méouge", "Villefranche-le-Château", "Villeperdrix", "Vinsobres",
}

MONTHS = {
    "janvier": 1, "janv": 1,
    "février": 2, "fevrier": 2, "févr": 2, "fevr": 2,
    "mars": 3,
    "avril": 4, "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7, "juil": 7,
    "août": 8, "aout": 8,
    "septembre": 9, "sept": 9,
    "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11,
    "décembre": 12, "decembre": 12, "déc": 12, "dec": 12,
}
MONTH_RE = "|".join(sorted((re.escape(x) for x in MONTHS), key=len, reverse=True))
NAMED_DATE_RE = re.compile(
    rf"\b(\d{{1,2}})\s+({MONTH_RE})\.?(?:\s+(20\d{{2}}))?\b", re.I
)
NUMERIC_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b")
RESULTS_RE = re.compile(r"sur\s+([0-9\s]+)\s+r[ée]sultat", re.I)


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


COMMUNE_BY_NORM = {norm(c): c for c in BARONNIES_COMMUNES}
COMMUNE_KEYS = sorted(COMMUNE_BY_NORM, key=len, reverse=True)


def commune_after_postal(text: str) -> str:
    """Commune confirmée uniquement par une adresse 26xxx + commune connue."""
    n = " " + norm(text) + " "
    for key in COMMUNE_KEYS:
        if re.search(rf"\b26\d{{3}}\s+{re.escape(key)}\b", n):
            return COMMUNE_BY_NORM[key]
    return ""


def normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    scheme = p.scheme or "https"
    host = p.netloc.lower()
    path = re.sub(r"/+", "/", p.path or "/")
    if path != "/":
        path = path.rstrip("/") + "/"
    return f"{scheme}://{host}{path}"


def is_detail_url(url: str) -> bool:
    p = urlparse(url)
    return (
        p.netloc.lower() in {"www.drome-cestmanature.com", "drome-cestmanature.com"}
        and "/fiches/" in p.path.lower()
    )


def search_url(center: str, page_no: int, start: date, end: date) -> str:
    params = urlencode(
        {
            "date_du": start.strftime("%d/%m/%Y"),
            "date_au": end.strftime("%d/%m/%Y"),
            "communes": center,
            "rayon": str(RADIUS_KM),
        }
    )
    return f"{BASE}page/{page_no}/?{params}"


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


def cache_fresh(entry: dict) -> bool:
    stamp = clean(entry.get("fetched_at"))
    if not stamp:
        return False
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
        return age < DETAIL_TTL_HOURS * 3600
    except Exception:
        return False


def detect_total_pages(soup: BeautifulSoup) -> int | None:
    text = clean(soup.get_text(" ", strip=True))
    m = RESULTS_RE.search(text)
    if m:
        try:
            total = int(re.sub(r"\s+", "", m.group(1)))
            if total > 0:
                return max(1, math.ceil(total / 36))
        except ValueError:
            pass

    nums = []
    for a in soup.find_all("a", href=True):
        mm = re.search(r"/page/(\d+)/?", a.get("href", ""))
        if mm:
            nums.append(int(mm.group(1)))
    return max(nums) if nums else None


def extract_candidates(html: str, current_url: str) -> list[dict]:
    """Tous les vrais liens /fiches/ de la zone principale des résultats."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    by_url: dict[str, dict] = {}

    for a in root.find_all("a", href=True):
        href = normalize_url(urljoin(current_url, a["href"]))
        if not is_detail_url(href):
            continue

        label = clean(a.get_text(" ", strip=True))
        item = {"url": href, "list_label": label}
        old = by_url.get(href)
        if not old or len(label) > len(old.get("list_label", "")):
            by_url[href] = item

    return list(by_url.values())


def section(text: str, start_label: str, end_labels: tuple[str, ...]) -> str:
    low = text.lower()
    start = low.find(start_label.lower())
    if start < 0:
        return ""
    start += len(start_label)
    end = len(text)
    for label in end_labels:
        p = low.find(label.lower(), start)
        if p >= 0:
            end = min(end, p)
    return clean(text[start:end])


def parse_dates(text: str) -> tuple[str, str]:
    raw = clean(text)
    candidates: list[tuple[int, int, int | None]] = []

    for d, m, y in NUMERIC_DATE_RE.findall(raw):
        candidates.append((int(d), int(m), int(y) if y else None))

    for match in NAMED_DATE_RE.finditer(raw):
        d = int(match.group(1))
        key = match.group(2).lower().rstrip(".")
        mo = MONTHS.get(key)
        if not mo:
            continue
        y = int(match.group(3)) if match.group(3) else None
        candidates.append((d, mo, y))

    if not candidates:
        return "", ""

    explicit_years = [y for _, _, y in candidates if y]
    default_year = explicit_years[0] if explicit_years else date.today().year
    values: list[date] = []
    current_year = default_year
    prev_month = None

    if candidates and candidates[0][2] is None and explicit_years:
        current_year = explicit_years[0]

    for d, mo, y in candidates:
        if y:
            current_year = y
        elif prev_month is not None and mo < prev_month - 6:
            current_year += 1
        prev_month = mo
        try:
            values.append(date(current_year, mo, d))
        except ValueError:
            pass

    if not values:
        return "", ""
    return min(values).isoformat(), max(values).isoformat()


def parse_detail(html: str, item: dict, final_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    text = clean(main.get_text(" ", strip=True))

    primary = text
    for marker in ("Ça peut vous intéresser", "Ca peut vous intéresser"):
        p = primary.find(marker)
        if p >= 0:
            primary = primary[:p]
            break

    h1 = main.find("h1") or soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else item.get("list_label", "")

    commune = commune_after_postal(primary)
    address_text = section(
        primary,
        "Accès",
        ("Informations complémentaires", "Ouverture", "Tarifs", "Contact"),
    )
    if not commune and address_text:
        commune = commune_after_postal(address_text)

    description = section(
        primary,
        "Description",
        ("Accès", "Informations complémentaires", "Ouverture", "Tarifs", "Contact"),
    )
    opening = section(primary, "Ouverture", ("Tarifs", "Contact"))
    tariffs = section(primary, "Tarifs", ("Contact",))
    contact = section(primary, "Contact", ())

    date_source = opening
    if not date_source:
        pos = primary.find(title) if title else -1
        date_source = primary[max(0, pos - 260):pos] if pos >= 0 else primary[:550]
    start, end = parse_dates(date_source)

    return {
        "title": title,
        "start_date": start,
        "end_date": end or start,
        "commune": commune,
        "address": address_text,
        "description": description,
        "opening": opening,
        "tariffs": tariffs,
        "contact": contact,
        "url": normalize_url(final_url),
        "source_url": item["url"],
        "source_format": "drome_tourisme_filtered_detail",
    }


def fetch_detail(session: requests.Session, item: dict, cache: dict) -> dict:
    url = item["url"]
    cached = cache.get(url)
    if isinstance(cached, dict) and cache_fresh(cached):
        data = cached.get("data")
        if isinstance(data, dict):
            return data

    r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    data = parse_detail(r.text, item, r.url)
    cache[url] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    return data


def dedupe_key(event: dict) -> str:
    raw = "|".join(
        [
            norm(event.get("title")),
            event.get("start_date", ""),
            event.get("end_date", ""),
            norm(event.get("commune")),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def main() -> None:
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    until = today + timedelta(days=HORIZON_DAYS)
    print("V9 LA DROME TOURISME FILTREE : Baronnies hors Nyons, sans GPT.")
    print(
        f"PERIODE: {today.strftime('%d/%m/%Y')} -> {until.strftime('%d/%m/%Y')} | "
        f"rayon={RADIUS_KM} km | centres={', '.join(SEARCH_CENTERS)}"
    )

    session = requests.Session()
    cache = load_json(CACHE_FILE)

    collected: dict[str, dict] = {}
    list_errors = 0
    scanned_pages = 0
    center_diagnostics: dict[str, dict] = {}

    for center in SEARCH_CENTERS:
        detected_pages = None
        center_pages = 0
        center_candidates = 0

        for page_no in range(1, MAX_PAGES_PER_CENTER + 1):
            if detected_pages is not None and page_no > min(detected_pages, MAX_PAGES_PER_CENTER):
                break

            url = search_url(center, page_no, today, until)
            try:
                r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
                print(
                    f"LISTE {center:22s} p{page_no:02d}: "
                    f"HTTP {r.status_code} | {len(r.content)} octets"
                )
                if r.status_code == 404:
                    break
                r.raise_for_status()
            except Exception as exc:
                list_errors += 1
                print(f"LISTE {center} p{page_no:02d}: ERREUR {exc}")
                if page_no == 1:
                    break
                break

            soup = BeautifulSoup(r.text, "html.parser")
            if page_no == 1:
                detected_pages = detect_total_pages(soup)
                print(
                    f"  {center}: pages détectées={detected_pages or '?'} "
                    f"(plafond {MAX_PAGES_PER_CENTER})"
                )

            candidates = extract_candidates(r.text, r.url)
            new_count = 0
            for item in candidates:
                item.setdefault("search_centers", [])
                if center not in item["search_centers"]:
                    item["search_centers"].append(center)

                if item["url"] not in collected:
                    collected[item["url"]] = item
                    new_count += 1
                else:
                    existing = collected[item["url"]]
                    centers = existing.setdefault("search_centers", [])
                    if center not in centers:
                        centers.append(center)
                    if len(item.get("list_label", "")) > len(existing.get("list_label", "")):
                        existing["list_label"] = item.get("list_label", "")

            scanned_pages += 1
            center_pages += 1
            center_candidates += len(candidates)
            print(
                f"  fiches={len(candidates)} | nouvelles={new_count} | "
                f"total unique={len(collected)}"
            )
            time.sleep(REQUEST_DELAY)

        center_diagnostics[center] = {
            "pages_scanned": center_pages,
            "detected_pages": detected_pages,
            "raw_candidate_links": center_candidates,
        }

    print(f"CANDIDATS UNIQUES AVANT DETAIL: {len(collected)}")
    if not collected:
        raise RuntimeError("V9: aucun lien /fiches/ trouvé dans les recherches filtrées.")

    detailed = []
    detail_errors = 0
    nyons_excluded = 0
    outside_rejected = 0

    for idx, item in enumerate(collected.values(), 1):
        try:
            event = fetch_detail(session, item, cache)
        except Exception as exc:
            detail_errors += 1
            print(f"DETAIL {idx:03d}/{len(collected)} ERREUR: {item['url']} | {exc}")
            continue

        commune = event.get("commune", "")
        if norm(commune) == "nyons":
            nyons_excluded += 1
            continue
        if not commune or norm(commune) not in COMMUNE_BY_NORM:
            outside_rejected += 1
            continue

        event["search_centers"] = item.get("search_centers", [])
        detailed.append(event)
        print(
            f"DETAIL {idx:03d}/{len(collected)} OK — "
            f"{event.get('start_date') or '?'} | {commune} | {event.get('title')}"
        )
        time.sleep(REQUEST_DELAY)

    save_json(CACHE_FILE, cache)

    unique = []
    seen_url = set()
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
        fp = dedupe_key(event)
        if (url_key and url_key in seen_url) or fp in seen_fp:
            duplicates += 1
            continue
        if url_key:
            seen_url.add(url_key)
        seen_fp.add(fp)
        event["dedupe_id"] = fp[:16]
        if not event.get("start_date"):
            missing_date += 1
        unique.append(event)

    print("=== BILAN V9 AVANT CONTROLE ===")
    print(f"Période                    : {today} -> {until}")
    print(f"Centres                    : {len(SEARCH_CENTERS)}")
    print(f"Pages liste parcourues     : {scanned_pages}")
    print(f"Erreurs liste              : {list_errors}")
    print(f"Liens candidats uniques    : {len(collected)}")
    print(f"Nyons exclus               : {nyons_excluded}")
    print(f"Hors Baronnies/inconnus    : {outside_rejected}")
    print(f"Erreurs détail             : {detail_errors}")
    print(f"Doublons supprimés         : {duplicates}")
    print(f"Dates manquantes           : {missing_date}")
    print(f"Événements conservés       : {len(unique)}")

    if len(unique) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Contrôle qualité V9: seulement {len(unique)} événement(s) hors Nyons. "
            "agenda.json reste inchangé."
        )

    payload = {
        "source": BASE,
        "source_mode": "filtered_radius_multi_center",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "search": {
            "date_du": today.strftime("%d/%m/%Y"),
            "date_au": until.strftime("%d/%m/%Y"),
            "radius_km": RADIUS_KM,
            "centers": list(SEARCH_CENTERS),
        },
        "count": len(unique),
        "diagnostics": {
            "center_searches": center_diagnostics,
            "list_pages_scanned": scanned_pages,
            "list_errors": list_errors,
            "candidate_urls": len(collected),
            "nyons_excluded": nyons_excluded,
            "outside_or_unrecognized_rejected": outside_rejected,
            "detail_errors": detail_errors,
            "duplicates_removed": duplicates,
            "missing_date": missing_date,
        },
        "events": unique,
    }

    save_json(OUT, payload)
    print("OK: agenda.json écrit.")


if __name__ == "__main__":
    main()
