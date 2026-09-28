#!/usr/bin/env python3
"""
V11 — Agenda Baronnies hors Nyons, recherche par petites périodes.

Objectif : garder la vitesse de la V10 tout en retrouvant davantage
d'événements. Au lieu d'une seule recherche sur 6 mois, V11 découpe les
180 prochains jours en fenêtres d'environ 30 jours et interroge seulement
3 centres utiles : Nyons, Buis-les-Baronnies et Montbrun-les-Bains.

Pour chaque fenêtre :
- quelques pages seulement par centre ;
- arrêt anticipé si les pages n'apportent plus rien ;
- fusion des URLs avant ouverture des fiches ;
- réutilisation du cache V9 ;
- validation de la commune sur la fiche ;
- Nyons exclu ; événements terminés exclus ;
- arrêt global dès que 50 événements valides sont obtenus.

Aucun appel OpenAI.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import requests

import extract_baronnies_v9 as v9

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
CACHE_FILE = ROOT / "_detail_cache_v9.json"

SEARCH_CENTERS = (
    "nyons",
    "buis-les-baronnies",
    "montbrun-les-bains",
)
RADIUS_KM = 30
HORIZON_DAYS = 180
WINDOW_DAYS = 30
MAX_PAGES_PER_SEARCH = 6
MAX_FINAL_EVENTS = 50
MIN_FINAL_EVENTS = 12
TIMEOUT = 15
CACHE_TTL_HOURS = 72

v9.RADIUS_KM = RADIUS_KM
v9.TIMEOUT = TIMEOUT
v9.REQUEST_DELAY = 0
v9.DETAIL_TTL_HOURS = CACHE_TTL_HOURS


def iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except Exception:
        return None


def repair_dates(opening: str, today: date) -> tuple[str, str]:
    """Réinterprète les dates sans année par rapport à aujourd'hui."""
    opening = v9.clean(opening)
    if not opening:
        return "", ""

    candidates: list[tuple[int, int, int | None]] = []
    for d, m, y in v9.NUMERIC_DATE_RE.findall(opening):
        candidates.append((int(d), int(m), int(y) if y else None))

    for match in v9.NAMED_DATE_RE.finditer(opening):
        day = int(match.group(1))
        key = match.group(2).lower().rstrip(".")
        month = v9.MONTHS.get(key)
        if month:
            candidates.append((day, month, int(match.group(3)) if match.group(3) else None))

    if not candidates:
        return "", ""

    explicit_years = [y for _, _, y in candidates if y]
    year = explicit_years[0] if explicit_years else today.year

    if not explicit_years:
        d0, m0, _ = candidates[0]
        try:
            if date(year, m0, d0) < today - timedelta(days=7):
                year += 1
        except ValueError:
            pass
    elif candidates[0][2] is None:
        year = explicit_years[0]

    values: list[date] = []
    previous_month = None
    for day, month, explicit_year in candidates:
        if explicit_year:
            year = explicit_year
        elif previous_month is not None and month < previous_month - 6:
            year += 1
        previous_month = month
        try:
            values.append(date(year, month, day))
        except ValueError:
            pass

    if not values:
        return "", ""
    return min(values).isoformat(), max(values).isoformat()


def fingerprint(event: dict) -> str:
    raw = "|".join(
        [
            v9.norm(event.get("title")),
            event.get("start_date", ""),
            event.get("end_date", ""),
            v9.norm(event.get("commune")),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def windows(today: date, horizon: date):
    start = today
    while start <= horizon:
        end = min(start + timedelta(days=WINDOW_DAYS - 1), horizon)
        yield start, end
        start = end + timedelta(days=1)


def main() -> None:
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    horizon = today + timedelta(days=HORIZON_DAYS)

    print("V11 : périodes courtes + 3 centres + cache + arrêt à 50 événements.")
    print(f"PERIODE GLOBALE: {today} -> {horizon}")

    session = requests.Session()
    cache = v9.load_json(CACHE_FILE)

    seen_candidate_urls: set[str] = set()
    seen_event_urls: set[str] = set()
    seen_fingerprints: set[str] = set()
    valid_events: list[dict] = []

    diagnostics = {
        "windows_scanned": 0,
        "list_pages_scanned": 0,
        "list_errors": 0,
        "candidate_urls": 0,
        "detail_errors": 0,
        "nyons_excluded": 0,
        "outside_or_unrecognized_rejected": 0,
        "past_removed": 0,
        "after_horizon_removed": 0,
        "duplicates_removed": 0,
        "missing_date": 0,
        "center_pages": {c: 0 for c in SEARCH_CENTERS},
    }

    for window_no, (start, end) in enumerate(windows(today, horizon), 1):
        diagnostics["windows_scanned"] += 1
        print(f"=== FENETRE {window_no}: {start} -> {end} ===")
        window_new: dict[str, dict] = {}

        for center in SEARCH_CENTERS:
            detected_pages = None
            no_new_streak = 0

            for page_no in range(1, MAX_PAGES_PER_SEARCH + 1):
                if detected_pages is not None and page_no > min(detected_pages, MAX_PAGES_PER_SEARCH):
                    break

                url = v9.search_url(center, page_no, start, end)
                try:
                    r = session.get(url, headers=v9.HEADERS, timeout=TIMEOUT, allow_redirects=True)
                    if r.status_code == 404:
                        break
                    r.raise_for_status()
                except Exception as exc:
                    diagnostics["list_errors"] += 1
                    print(f"LISTE {center} p{page_no:02d}: ERREUR {exc}")
                    break

                diagnostics["list_pages_scanned"] += 1
                diagnostics["center_pages"][center] += 1

                soup = BeautifulSoup(r.text, "html.parser")
                if page_no == 1:
                    detected_pages = v9.detect_total_pages(soup)

                candidates = v9.extract_candidates(r.text, r.url)
                new_count = 0
                for item in candidates:
                    item.setdefault("search_centers", [])
                    if center not in item["search_centers"]:
                        item["search_centers"].append(center)
                    item.setdefault("search_windows", [])
                    win_label = f"{start.isoformat()}_{end.isoformat()}"
                    if win_label not in item["search_windows"]:
                        item["search_windows"].append(win_label)

                    if item["url"] in seen_candidate_urls:
                        continue
                    if item["url"] not in window_new:
                        window_new[item["url"]] = item
                        new_count += 1
                    else:
                        old = window_new[item["url"]]
                        if center not in old.setdefault("search_centers", []):
                            old["search_centers"].append(center)

                print(
                    f"LISTE {center:22s} p{page_no:02d}: "
                    f"fiches={len(candidates)} | nouvelles={new_count} | "
                    f"pages détectées={detected_pages or '?'}"
                )

                if new_count == 0:
                    no_new_streak += 1
                else:
                    no_new_streak = 0
                if no_new_streak >= 2:
                    print(f"  {center}: 2 pages sans nouveauté, arrêt anticipé.")
                    break

        print(f"NOUVELLES URLS FENETRE: {len(window_new)}")

        # Marque les URLs maintenant, avant le détail, pour ne jamais les retraiter.
        seen_candidate_urls.update(window_new)
        diagnostics["candidate_urls"] = len(seen_candidate_urls)

        for idx, item in enumerate(window_new.values(), 1):
            try:
                event = v9.fetch_detail(session, item, cache)
            except Exception as exc:
                diagnostics["detail_errors"] += 1
                print(f"DETAIL ERREUR: {item['url']} | {exc}")
                continue

            commune = event.get("commune", "")
            if v9.norm(commune) == "nyons":
                diagnostics["nyons_excluded"] += 1
                continue
            if not commune or v9.norm(commune) not in v9.COMMUNE_BY_NORM:
                diagnostics["outside_or_unrecognized_rejected"] += 1
                continue

            repaired_start, repaired_end = repair_dates(event.get("opening", ""), today)
            if repaired_start:
                event["start_date"] = repaired_start
                event["end_date"] = repaired_end or repaired_start

            start_d = iso(event.get("start_date", ""))
            end_d = iso(event.get("end_date", ""))
            if end_d and end_d < today:
                diagnostics["past_removed"] += 1
                continue
            if start_d and start_d > horizon:
                diagnostics["after_horizon_removed"] += 1
                continue
            if not start_d:
                diagnostics["missing_date"] += 1

            url_key = v9.normalize_url(event.get("url", ""))
            fp = fingerprint(event)
            if (url_key and url_key in seen_event_urls) or fp in seen_fingerprints:
                diagnostics["duplicates_removed"] += 1
                continue

            if url_key:
                seen_event_urls.add(url_key)
            seen_fingerprints.add(fp)
            event["dedupe_id"] = fp[:16]
            event["search_centers"] = item.get("search_centers", [])
            event["search_windows"] = item.get("search_windows", [])
            valid_events.append(event)

        print(f"VALIDES CUMULES: {len(valid_events)}")
        if len(valid_events) >= MAX_FINAL_EVENTS:
            print("Objectif 50 atteint : arrêt des fenêtres suivantes.")
            break

    v9.save_json(CACHE_FILE, cache)

    valid_events.sort(
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            v9.norm(e.get("commune")),
            v9.norm(e.get("title")),
        )
    )
    valid_events = valid_events[:MAX_FINAL_EVENTS]

    if len(valid_events) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Contrôle qualité V11: seulement {len(valid_events)} événement(s). "
            "agenda.json reste inchangé."
        )

    payload = {
        "source": v9.BASE,
        "source_mode": "segmented_radius_v11",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "search": {
            "date_du": today.strftime("%d/%m/%Y"),
            "date_au": horizon.strftime("%d/%m/%Y"),
            "radius_km": RADIUS_KM,
            "centers": list(SEARCH_CENTERS),
            "window_days": WINDOW_DAYS,
            "max_pages_per_search": MAX_PAGES_PER_SEARCH,
            "max_final_events": MAX_FINAL_EVENTS,
        },
        "count": len(valid_events),
        "diagnostics": diagnostics,
        "events": valid_events,
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)

    print("=== BILAN V11 ===")
    print(f"Fenêtres parcourues       : {diagnostics['windows_scanned']}")
    print(f"Pages liste parcourues    : {diagnostics['list_pages_scanned']}")
    print(f"URLs candidates uniques   : {diagnostics['candidate_urls']}")
    print(f"Nyons exclus              : {diagnostics['nyons_excluded']}")
    print(f"Hors Baronnies/inconnus   : {diagnostics['outside_or_unrecognized_rejected']}")
    print(f"Erreurs détail            : {diagnostics['detail_errors']}")
    print(f"Événements finaux         : {len(valid_events)}")
    print("OK V11: agenda.json prêt.")


if __name__ == "__main__":
    main()
