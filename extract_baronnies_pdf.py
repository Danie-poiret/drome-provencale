#!/usr/bin/env python3
"""V1 robuste : agenda Baronnies hors Nyons depuis le PDF officiel."""

from __future__ import annotations

import hashlib
import io
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

import pdfplumber
import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"

FILTERED_PAGE = (
    "https://www.dromeprovencale.fr/agenda/tout-lagenda/"
    "recherche/territoire/baronnies-en-drome-provencale/"
)
PDF_CANDIDATES = [
    FILTERED_PAGE + "?sitpdf=1",
    FILTERED_PAGE + "?pdf=1",
    "https://www.baronnies-tourisme.com/votre-sejour/agenda/tout-lagenda/?pdf=1",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
    "Referer": "https://www.dromeprovencale.fr/",
}
TIMEOUT = 40
MIN_VALID_EVENTS = 5

MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}
MONTH_RE = "|".join(MONTHS)
DATE_RE = re.compile(rf"(\d{{1,2}})\s+({MONTH_RE})(?:\s+(20\d{{2}}))?", re.I)

# Liste officielle des 67 communes de la CCBDP ; Nyons sera exclu ensuite.
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


def clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "").replace("\u00ad", "")).strip()


def norm(v) -> str:
    s = unicodedata.normalize("NFKD", clean(v))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


COMMUNE_BY_NORM = {norm(x): x for x in BARONNIES_COMMUNES}


def canonical_commune(value: str) -> str:
    n = norm(value)
    if n in COMMUNE_BY_NORM:
        return COMMUNE_BY_NORM[n]
    # PDF : césures et retours à la ligne peuvent casser le nom.
    compact = n.replace(" ", "")
    for key, proper in COMMUNE_BY_NORM.items():
        if key.replace(" ", "") == compact:
            return proper
    return ""


def fetch_pdf() -> tuple[str, bytes]:
    s = requests.Session()
    errors = []
    for url in PDF_CANDIDATES:
        try:
            r = s.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            ctype = r.headers.get("content-type", "")
            print(f"SOURCE TEST: {r.status_code} | {len(r.content)} octets | {ctype} | {url}")
            if r.status_code == 200 and r.content[:5] == b"%PDF-":
                return url, r.content
            errors.append(f"{url} -> HTTP {r.status_code} {ctype}")
        except Exception as exc:
            errors.append(f"{url} -> {exc}")
    raise RuntimeError("Aucune source PDF accessible : " + " ; ".join(errors))


def header_map(row: list) -> dict[str, int]:
    found = {}
    for i, cell in enumerate(row or []):
        n = norm(cell)
        if "animation" in n:
            found["title"] = i
        elif n == "date" or n.startswith("date "):
            found["date"] = i
        elif n == "lieu" or n.startswith("lieu "):
            found["location"] = i
        elif "code postal" in n or n == "code":
            found["postal"] = i
        elif n == "ville" or n.startswith("ville "):
            found["city"] = i
        elif "tarif" in n:
            found["price"] = i
        elif "organisation" in n:
            found["organisation"] = i
        elif "contact" in n:
            found["contact"] = i
    return found


def parse_dates(raw: str) -> tuple[str, str]:
    matches = list(DATE_RE.finditer(clean(raw)))
    if not matches:
        return "", ""
    explicit_years = [int(m.group(3)) for m in matches if m.group(3)]
    default_year = explicit_years[0] if explicit_years else date.today().year
    dates = []
    for m in matches:
        d = int(m.group(1))
        mo = MONTHS[m.group(2).lower()]
        y = int(m.group(3)) if m.group(3) else default_year
        try:
            dates.append(date(y, mo, d))
        except ValueError:
            pass
    if not dates:
        return "", ""
    return dates[0].isoformat(), dates[-1].isoformat()


def cell(row: list, idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return clean(row[idx])


def extract_rows(pdf_bytes: bytes) -> list[dict]:
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        print(f"PDF: {len(pdf.pages)} page(s)")
        for page_no, page in enumerate(pdf.pages, 1):
            tables = page.extract_tables() or []
            print(f"PDF PAGE {page_no:02d}: {len(tables)} table(s)")
            for table in tables:
                hmap = None
                for row in table:
                    if not row:
                        continue
                    hm = header_map(row)
                    if "title" in hm and "date" in hm and "city" in hm:
                        hmap = hm
                        continue
                    if not hmap:
                        continue
                    title = cell(row, hmap.get("title"))
                    raw_date = cell(row, hmap.get("date"))
                    city_raw = cell(row, hmap.get("city"))
                    if not title or not raw_date or not city_raw:
                        continue
                    if norm(title) in {"animation", "animations"}:
                        continue
                    commune = canonical_commune(city_raw)
                    start, end = parse_dates(raw_date)
                    out.append({
                        "title": title,
                        "start_date": start,
                        "end_date": end or start,
                        "date_text": raw_date,
                        "location": cell(row, hmap.get("location")),
                        "postal_code": cell(row, hmap.get("postal")),
                        "commune_raw": city_raw,
                        "commune": commune,
                        "price": cell(row, hmap.get("price")),
                        "organisation": cell(row, hmap.get("organisation")),
                        "contact": cell(row, hmap.get("contact")),
                        "source_format": "official_pdf_table",
                    })
    return out


def dedupe_key(e: dict) -> str:
    raw = "|".join([
        norm(e.get("title")),
        e.get("start_date") or norm(e.get("date_text")),
        e.get("end_date") or "",
        norm(e.get("commune")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def main() -> None:
    source_url, pdf_bytes = fetch_pdf()
    rows = extract_rows(pdf_bytes)
    print(f"LIGNES EVENEMENTS EXTRAITES: {len(rows)}")
    if len(rows) < MIN_VALID_EVENTS:
        raise RuntimeError(f"Extraction PDF suspecte : seulement {len(rows)} ligne(s)")

    excluded_nyons = 0
    excluded_outside = 0
    missing_date = 0
    candidates = []

    for e in rows:
        commune = e.get("commune", "")
        if norm(commune) == "nyons":
            excluded_nyons += 1
            continue
        if not commune:
            excluded_outside += 1
            continue
        if not e.get("start_date"):
            missing_date += 1
        candidates.append(e)

    unique = {}
    duplicates = 0
    for e in candidates:
        k = dedupe_key(e)
        if k in unique:
            duplicates += 1
            continue
        e["dedupe_id"] = k[:16]
        unique[k] = e

    events = sorted(
        unique.values(),
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            norm(e.get("commune")),
            norm(e.get("title")),
        ),
    )

    if len(events) < MIN_VALID_EVENTS:
        raise RuntimeError(f"Extraction finale suspecte : seulement {len(events)} événement(s)")

    payload = {
        "source": source_url,
        "source_filter": FILTERED_PAGE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "count": len(events),
        "diagnostics": {
            "pdf_rows": len(rows),
            "excluded_nyons": excluded_nyons,
            "excluded_outside_or_unrecognized": excluded_outside,
            "duplicates_removed": duplicates,
            "missing_date": missing_date,
        },
        "events": events,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=== BILAN V1 PDF ===")
    print(f"Source utilisée          : {source_url}")
    print(f"Lignes PDF               : {len(rows)}")
    print(f"Nyons exclus             : {excluded_nyons}")
    print(f"Hors territoire/inconnus : {excluded_outside}")
    print(f"Doublons supprimés       : {duplicates}")
    print(f"Dates manquantes         : {missing_date}")
    print(f"Événements conservés     : {len(events)}")
    print("OK: agenda.json écrit.")


if __name__ == "__main__":
    main()
