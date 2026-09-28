#!/usr/bin/env python3
"""
V8 — Agenda Baronnies hors Nyons via La Drôme Tourisme.

Cette version abandonne dromeprovencale.fr pour l'automatisation, car ses
pages secondaires et son PDF bloquent les runners GitHub Actions en 403.
La source départementale drome-cestmanature.com est accessible depuis GitHub
Actions et publie les mêmes données touristiques Apidae.

Principe :
- parcourt les pages de l'agenda départemental ;
- ne présélectionne un événement que si le libellé de SON lien se termine par
  une commune officielle des Baronnies ;
- exclut Nyons ;
- ouvre ensuite la fiche La Drôme Tourisme pour confirmer la vraie commune à
  partir de « code postal + commune », avant la zone de recommandations ;
- récupère titre, dates, description, adresse, ouverture et contact ;
- supprime les doublons ;
- protège agenda.json si le résultat semble anormalement faible.

Aucun appel OpenAI dans cette version.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
CACHE_FILE = ROOT / "_detail_cache_v8.json"

SOURCE = "https://www.drome-cestmanature.com/preparez-votre-sejour/l-agenda/"
TIMEOUT = 35
REQUEST_DELAY = 0.12
MAX_LIST_PAGES = 60
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


def commune_at_end(label: str) -> str:
    """Commune uniquement si elle termine le libellé DU lien événement."""
    n = norm(label)
    for key in COMMUNE_KEYS:
        if n == key or n.endswith(" " + key):
            return COMMUNE_BY_NORM[key]
    return ""


def commune_after_postal(text: str) -> str:
    """Commune confirmée par une adresse 26xxx + commune."""
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
    return p.netloc.lower() in {"www.drome-cestmanature.com", "drome-cestmanature.com"} and "/fiches/" in p.path.lower()


def page_url(page_no: int) -> str:
    return SOURCE if page_no <= 1 else urljoin(SOURCE, f"page/{page_no}/")


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
    """Sélection stricte depuis le texte propre de chaque lien /fiches/."""
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    by_url = {}

    for a in root.find_all("a", href=True):
        href = normalize_url(urljoin(current_url, a["href"]))
        if not is_detail_url(href):
            continue

        label = clean(a.get_text(" ", strip=True))
        if not label:
            continue
        commune = commune_at_end(label)
        if not commune:
            continue

        old = by_url.get(href)
        item = {"url": href, "list_label": label, "list_commune": commune}
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

    # Si la première date n'a pas d'année mais la seconde en a une, elles
    # appartiennent généralement à la même période (« Du 01/04 au 31/08/2026 »).
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

    # On coupe avant les recommandations : elles contiennent d'autres communes
    # et ne doivent jamais servir à déterminer le lieu de l'événement.
    primary = text
    for marker in ("Ça peut vous intéresser", "Ca peut vous intéresser"):
        p = primary.find(marker)
        if p >= 0:
            primary = primary[:p]
            break

    h1 = main.find("h1") or soup.find("h1")
    title = clean(h1.get_text(" ", strip=True)) if h1 else item.get("list_label", "")

    commune = commune_after_postal(primary)
    address_text = section(primary, "Accès", ("Informations complémentaires", "Ouverture", "Tarifs", "Contact"))
    if not commune and address_text:
        commune = commune_after_postal(address_text)

    description = section(primary, "Description", ("Accès", "Informations complémentaires", "Ouverture", "Tarifs", "Contact"))
    opening = section(primary, "Ouverture", ("Tarifs", "Contact"))
    tariffs = section(primary, "Tarifs", ("Contact",))
    contact = section(primary, "Contact", ())

    date_source = opening
    if not date_source:
        # Les fiches affichent également la période juste avant le H1.
        pos = primary.find(title) if title else -1
        date_source = primary[max(0, pos - 220):pos] if pos >= 0 else primary[:450]
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
        "list_commune": item.get("list_commune", ""),
        "source_format": "drome_tourisme_detail",
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
    raw = "|".join([
        norm(event.get("title")),
        event.get("start_date", ""),
        event.get("end_date", ""),
        norm(event.get("commune")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def main() -> None:
    print("V8 LA DROME TOURISME : extraction stricte Baronnies hors Nyons, sans GPT.")
    session = requests.Session()
    cache = load_json(CACHE_FILE)

    collected: dict[str, dict] = {}
    detected_pages = None
    list_errors = 0
    scanned = 0

    for page_no in range(1, MAX_LIST_PAGES + 1):
        if detected_pages is not None and page_no > min(detected_pages, MAX_LIST_PAGES):
            break

        url = page_url(page_no)
        try:
            r = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            print(f"LISTE {page_no:02d}: HTTP {r.status_code} | {len(r.content)} octets")
            if r.status_code == 404:
                break
            r.raise_for_status()
        except Exception as exc:
            list_errors += 1
            print(f"LISTE {page_no:02d}: ERREUR {exc}")
            if page_no == 1:
                raise
            break

        soup = BeautifulSoup(r.text, "html.parser")
        if page_no == 1:
            detected_pages = detect_total_pages(soup)
            print(f"PAGES DETECTEES: {detected_pages or '?'} (plafond {MAX_LIST_PAGES})")

        candidates = extract_candidates(r.text, r.url)
        new_count = 0
        nyons_on_page = 0
        for item in candidates:
            if norm(item.get("list_commune")) == "nyons":
                nyons_on_page += 1
            if item["url"] not in collected:
                collected[item["url"]] = item
                new_count += 1
        scanned += 1
        print(
            f"  candidats Baronnies={len(candidates)} | nouveaux={new_count} | "
            f"Nyons={nyons_on_page} | total={len(collected)}"
        )
        time.sleep(REQUEST_DELAY)

    print(f"CANDIDATS UNIQUES AVANT DETAIL: {len(collected)}")

    detailed = []
    detail_errors = 0
    nyons_excluded = 0
    outside_rejected = 0
    geo_mismatch = 0

    for idx, item in enumerate(collected.values(), 1):
        if norm(item.get("list_commune")) == "nyons":
            nyons_excluded += 1
            continue
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
        if norm(commune) != norm(item.get("list_commune")):
            geo_mismatch += 1
            print(
                f"GEO MISMATCH: liste={item.get('list_commune')} détail={commune} | "
                f"{event.get('title')}"
            )
            # La fiche détaillée fait foi si elle donne code postal + commune.

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

    print("=== BILAN V8 AVANT CONTROLE ===")
    print(f"Pages liste parcourues      : {scanned}")
    print(f"Erreurs liste               : {list_errors}")
    print(f"Liens Baronnies candidats   : {len(collected)}")
    print(f"Nyons exclus                : {nyons_excluded}")
    print(f"Hors territoire/inconnus    : {outside_rejected}")
    print(f"Divergences liste/détail    : {geo_mismatch}")
    print(f"Erreurs détail              : {detail_errors}")
    print(f"Doublons supprimés          : {duplicates}")
    print(f"Dates manquantes            : {missing_date}")
    print(f"Événements conservés        : {len(unique)}")

    if len(unique) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Contrôle qualité V8: seulement {len(unique)} événement(s) hors Nyons. "
            "agenda.json reste inchangé."
        )

    payload = {
        "source": SOURCE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "count": len(unique),
        "diagnostics": {
            "list_pages_scanned": scanned,
            "detected_list_pages": detected_pages,
            "candidate_urls": len(collected),
            "nyons_excluded": nyons_excluded,
            "outside_or_unrecognized_rejected": outside_rejected,
            "geo_mismatches": geo_mismatch,
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
