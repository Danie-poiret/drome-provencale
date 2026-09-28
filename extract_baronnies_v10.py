#!/usr/bin/env python3
"""Agenda simple : 100 prochains événements autour de Nyons, Nyons exclu.

Principe :
- recherche La Drôme Tourisme autour de Nyons dans un rayon de 100 km ;
- lit seulement les pages de LISTE ;
- récupère tous les vrais liens /fiches/ sans filtre de territoire ;
- enlève uniquement les événements dont le bloc local mentionne Nyons ;
- dédoublonne par URL ;
- trie par date ;
- conserve les 100 prochains événements.

Aucune fiche détail, aucune IA, aucun filtre Baronnies.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
import extract_baronnies_v9 as v9

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"

CENTER = "nyons"
RADIUS_KM = 100
HORIZON_DAYS = 180
MAX_LIST_PAGES = 160
MAX_FINAL_EVENTS = 100
TIMEOUT = 18

v9.RADIUS_KM = RADIUS_KM
v9.TIMEOUT = TIMEOUT

MONTH_RE = "|".join(sorted((re.escape(x) for x in v9.MONTHS), key=len, reverse=True))
LIST_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE})\.?\b", re.I)
POSTAL_COMMUNE_RE = re.compile(r"\b(?:0[1-9]|[1-8]\d|9[0-5])\d{3}\s+([A-Za-zÀ-ÿ'’\- ]{2,60})")


def today_paris() -> date:
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def list_url(page_no: int, start: date, end: date) -> str:
    return v9.search_url(CENTER, page_no, start, end)


def get_page(page_no: int, start: date, end: date) -> dict:
    url = list_url(page_no, start, end)
    last_error = ""
    for attempt in range(2):
        try:
            r = requests.get(url, headers=v9.HEADERS, timeout=TIMEOUT, allow_redirects=True)
            if r.status_code == 404:
                return {"page": page_no, "status": 404, "url": url, "html": "", "error": ""}
            r.raise_for_status()
            return {"page": page_no, "status": r.status_code, "url": r.url, "html": r.text, "error": ""}
        except Exception as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(0.4)
    return {"page": page_no, "status": 0, "url": url, "html": "", "error": last_error}


def true_links_in_order(html: str, current_url: str) -> list[tuple[str, str]]:
    soup = v9.BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    seen = set()
    items: list[tuple[str, str]] = []

    for a in root.find_all("a", href=True):
        href = v9.normalize_url(urljoin(current_url, a.get("href", "")))
        if not v9.is_detail_url(href) or href in seen:
            continue
        title = v9.clean(a.get_text(" ", strip=True))
        if len(title) < 3:
            continue
        seen.add(href)
        items.append((title, href))
    return items


def find_positions(page_text: str, items: list[tuple[str, str]]) -> list[tuple[str, str, int]]:
    located = []
    cursor = 0
    for title, href in items:
        pos = page_text.find(title, cursor)
        if pos < 0:
            pos = page_text.find(title)
        if pos < 0:
            continue
        located.append((title, href, pos))
        cursor = pos + len(title)
    return located


def date_from_before(text: str, today: date) -> str:
    matches = list(LIST_DATE_RE.finditer(text))
    if not matches:
        return ""
    m = matches[-1]
    day = int(m.group(1))
    month = v9.MONTHS.get(m.group(2).lower().rstrip("."))
    if not month:
        return ""

    year = today.year
    try:
        candidate = date(year, month, day)
        if candidate < today - timedelta(days=7):
            candidate = date(year + 1, month, day)
        return candidate.isoformat()
    except ValueError:
        return ""


def commune_from_segment(segment: str) -> str:
    """Essaie d'afficher une commune, sans jamais l'utiliser comme filtre."""
    m = POSTAL_COMMUNE_RE.search(segment)
    if m:
        value = v9.clean(m.group(1))
        value = re.split(r"\b(?:Tél|Tel|Téléphone|Contact|Ouverture|Tarifs|Description)\b", value, maxsplit=1, flags=re.I)[0]
        return v9.clean(value)[:60]
    return ""


def parse_list_page(result: dict, today: date) -> tuple[list[dict], int]:
    if not result.get("html"):
        return [], 0

    soup = v9.BeautifulSoup(result["html"], "html.parser")
    root = soup.find("main") or soup
    page_text = v9.clean(root.get_text(" ", strip=True))
    items = true_links_in_order(result["html"], result["url"])
    located = find_positions(page_text, items)

    events = []
    nyons_removed = 0

    for i, (title, href, pos) in enumerate(located):
        prev = 0 if i == 0 else located[i - 1][2] + len(located[i - 1][0])
        nxt = len(page_text) if i + 1 == len(located) else located[i + 1][2]

        before = page_text[max(prev, pos - 700):pos]
        after = page_text[pos + len(title):nxt]
        local_block = v9.clean(before[-250:] + " " + title + " " + after[:700])

        # SEUL filtre territorial demandé : Nyons.
        if re.search(r"\bnyons\b", v9.norm(local_block)):
            nyons_removed += 1
            continue

        start = date_from_before(before, today)
        if not start:
            continue

        commune = commune_from_segment(after[:700])
        summary = v9.clean(after)

        events.append({
            "title": title,
            "start_date": start,
            "end_date": start,
            "commune": commune,
            "summary": summary[:420],
            "categories": [],
            "url": href,
            "source_url": href,
            "source_format": "drome_tourisme_list",
            "list_page": result["page"],
        })

    return events, nyons_removed


def main() -> None:
    started = time.monotonic()
    today = today_paris()
    horizon = today + timedelta(days=HORIZON_DAYS)

    print("AGENDA SIMPLE : tous les événements, sauf Nyons, puis 100 prochains")
    print(f"Période {today} -> {horizon} | centre={CENTER} | rayon={RADIUS_KM} km")

    first = get_page(1, today, horizon)
    if not first["html"]:
        raise RuntimeError(f"Page 1 inaccessible: {first['error'] or first['status']}")

    detected = v9.detect_total_pages(v9.BeautifulSoup(first["html"], "html.parser")) or 1
    total_pages = min(detected, MAX_LIST_PAGES)
    print(f"Pages détectées={detected}")

    by_url: dict[str, dict] = {}
    errors = 0
    nyons_removed = 0
    pages_read = 0

    # Les résultats sont paginés par l'agenda. On lit dans l'ordre et on s'arrête
    # dès qu'on a une marge suffisante au-dessus des 100 événements demandés.
    for page_no in range(1, total_pages + 1):
        result = first if page_no == 1 else get_page(page_no, today, horizon)
        pages_read += 1

        if not result["html"]:
            errors += 1
            continue

        events, removed = parse_list_page(result, today)
        nyons_removed += removed
        added = 0

        for event in events:
            if event["url"] not in by_url:
                by_url[event["url"]] = event
                added += 1

        print(
            f"Page {page_no:03d}/{total_pages}: gardés={len(events)} | "
            f"Nyons retirés={removed} | +{added} | total={len(by_url)}"
        )

        # Petite marge pour pouvoir trier proprement par date ensuite.
        if len(by_url) >= 130:
            break

    events = sorted(
        by_url.values(),
        key=lambda e: (e["start_date"], v9.norm(e.get("title"))),
    )
    events = [e for e in events if e["end_date"] >= today.isoformat()]
    events = events[:MAX_FINAL_EVENTS]

    if not events:
        raise RuntimeError("Aucun événement hors Nyons trouvé. agenda.json reste inchangé.")

    elapsed = round(time.monotonic() - started, 2)
    payload = {
        "source": v9.BASE,
        "source_mode": "next_100_all_except_nyons",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "100 prochains événements dans un rayon de 100 km autour de Nyons, Nyons exclu",
        "search": {
            "date_du": today.strftime("%d/%m/%Y"),
            "date_au": horizon.strftime("%d/%m/%Y"),
            "center": CENTER,
            "radius_km": RADIUS_KM,
            "max_events": MAX_FINAL_EVENTS,
        },
        "count": len(events),
        "diagnostics": {
            "detected_pages": detected,
            "pages_read": pages_read,
            "list_errors": errors,
            "nyons_removed": nyons_removed,
            "unique_urls_before_limit": len(by_url),
            "elapsed_seconds": elapsed,
            "detail_pages_opened": 0,
            "territory_filter": "none_except_nyons",
        },
        "events": events,
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)

    print("=== BILAN ===")
    print(f"Pages lues            : {pages_read}")
    print(f"Erreurs liste         : {errors}")
    print(f"Nyons retirés         : {nyons_removed}")
    print(f"Événements candidats  : {len(by_url)}")
    print(f"Événements publiés    : {len(events)}")
    print("Filtre territoire      : AUCUN, sauf Nyons")
    print("Fiches détail ouvertes : 0")
    print(f"Durée script          : {elapsed} s")
    print("OK: agenda.json prêt.")


if __name__ == "__main__":
    main()
