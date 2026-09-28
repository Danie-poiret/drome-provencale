#!/usr/bin/env python3
"""Agenda Baronnies simple : 100 prochains événements, Nyons exclu.

Principe calqué sur Nyons :
- recherche autour de Nyons sur 100 km et 180 jours ;
- lit uniquement les pages de LISTE, pas les fiches détail ;
- repère les vrais liens /fiches/ et leur position dans le texte ;
- récupère date + commune directement dans la liste ;
- garde uniquement les communes des Baronnies ;
- enlève Nyons ;
- dédoublonne par URL ;
- trie par date et conserve les 100 prochains.

Aucune IA et aucune ouverture de fiche détail : c'est volontairement simple.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
LIST_WORKERS = 24
TIMEOUT = 18

v9.RADIUS_KM = RADIUS_KM
v9.TIMEOUT = TIMEOUT

MONTH_RE = "|".join(sorted((re.escape(x) for x in v9.MONTHS), key=len, reverse=True))
LIST_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE})\.?\b", re.I)


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
    """Vrais liens événement, dans l'ordre visuel, une seule fois par URL."""
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


def commune_from_segment(segment: str) -> str:
    """Cherche une commune connue dans le texte qui suit le titre."""
    n = " " + v9.norm(segment) + " "
    for key in v9.COMMUNE_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", n):
            return v9.COMMUNE_BY_NORM[key]
    return ""


def date_from_before(text: str, today: date) -> str:
    """Prend la dernière date visible avant le titre et déduit l'année."""
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


def parse_list_page(result: dict, today: date) -> list[dict]:
    if not result.get("html"):
        return []

    soup = v9.BeautifulSoup(result["html"], "html.parser")
    root = soup.find("main") or soup
    page_text = v9.clean(root.get_text(" ", strip=True))
    items = true_links_in_order(result["html"], result["url"])
    located = find_positions(page_text, items)

    events = []
    for i, (title, href, pos) in enumerate(located):
        prev = 0 if i == 0 else located[i - 1][2] + len(located[i - 1][0])
        nxt = len(page_text) if i + 1 == len(located) else located[i + 1][2]

        before = page_text[max(prev, pos - 700):pos]
        after = page_text[pos + len(title):nxt]

        commune = commune_from_segment(after[:700])
        if not commune:
            continue
        if v9.norm(commune) == "nyons":
            continue
        if v9.norm(commune) not in v9.COMMUNE_BY_NORM:
            continue

        start = date_from_before(before, today)
        if not start:
            continue

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

    return events


def main() -> None:
    started = time.monotonic()
    today = today_paris()
    horizon = today + timedelta(days=HORIZON_DAYS)

    print("BARONNIES SIMPLE : pages de liste -> Baronnies -> enlève Nyons -> 100 prochains")
    print(f"Période {today} -> {horizon} | centre={CENTER} | rayon={RADIUS_KM} km")

    first = get_page(1, today, horizon)
    if not first["html"]:
        raise RuntimeError(f"Page 1 inaccessible: {first['error'] or first['status']}")

    detected = v9.detect_total_pages(v9.BeautifulSoup(first["html"], "html.parser")) or 1
    total_pages = min(detected, MAX_LIST_PAGES)
    results = [first]

    print(f"Pages détectées={detected} | pages lues={total_pages}")

    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            futures = [pool.submit(get_page, p, today, horizon) for p in range(2, total_pages + 1)]
            for future in as_completed(futures):
                results.append(future.result())

    by_url: dict[str, dict] = {}
    errors = 0
    nyons_seen = 0

    for result in sorted(results, key=lambda x: x["page"]):
        if not result["html"]:
            errors += 1
            continue

        # Comptage diagnostic Nyons sur le texte de la page.
        text_norm = v9.norm(v9.BeautifulSoup(result["html"], "html.parser").get_text(" ", strip=True))
        nyons_seen += len(re.findall(r"\bnyons\b", text_norm))

        events = parse_list_page(result, today)
        added = 0
        for event in events:
            if event["url"] not in by_url:
                by_url[event["url"]] = event
                added += 1

        p = result["page"]
        if p == 1 or p % 10 == 0 or added:
            print(f"Page {p:03d}/{total_pages}: Baronnies hors Nyons={len(events)} | +{added} | total={len(by_url)}")

    events = sorted(
        by_url.values(),
        key=lambda e: (e["start_date"], v9.norm(e["commune"]), v9.norm(e["title"])),
    )
    events = [e for e in events if e["end_date"] >= today.isoformat()]
    events = events[:MAX_FINAL_EVENTS]

    if not events:
        raise RuntimeError("Aucun événement Baronnies hors Nyons trouvé. agenda.json reste inchangé.")

    elapsed = round(time.monotonic() - started, 2)
    payload = {
        "source": v9.BASE,
        "source_mode": "list_only_next_100_exclude_nyons",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
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
            "pages_scanned": total_pages,
            "list_errors": errors,
            "nyons_mentions_seen": nyons_seen,
            "unique_baronnies_urls_before_limit": len(by_url),
            "elapsed_seconds": elapsed,
            "detail_pages_opened": 0,
        },
        "events": events,
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)

    print("=== BILAN ===")
    print(f"Pages liste          : {total_pages}")
    print(f"Erreurs liste        : {errors}")
    print(f"Baronnies hors Nyons : {len(by_url)}")
    print(f"Événements publiés   : {len(events)}")
    print(f"Fiches détail ouvertes: 0")
    print(f"Durée script         : {elapsed} s")
    print("OK: agenda.json prêt.")


if __name__ == "__main__":
    main()
