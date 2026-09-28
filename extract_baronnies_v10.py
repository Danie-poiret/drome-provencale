#!/usr/bin/env python3
"""
V10 rapide — s'appuie sur la V9 validée, mais réduit fortement le travail.

Le run V9 a montré que :
- Nyons, Buis-les-Baronnies et Montbrun-les-Bains suffisent à retrouver les
  événements valides conservés ;
- Rémuzat n'apporte presque rien de nouveau ;
- Séderon déclenche énormément de pages/bruit hors territoire.

V10 garde donc seulement 3 centres, limite Nyons à 15 pages, réutilise le
cache V9 pendant 60 h, raccourcit les timeouts, puis nettoie le résultat :
- dates sans année réinterprétées par rapport à aujourd'hui ;
- événements réellement terminés supprimés ;
- maximum 50 prochains événements.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import extract_baronnies_v9 as v9

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
MAX_FINAL_EVENTS = 50

# Réduction basée sur le run V9 réussi du 28/09/2026.
v9.SEARCH_CENTERS = (
    "nyons",
    "buis-les-baronnies",
    "montbrun-les-bains",
)
v9.MAX_PAGES_PER_CENTER = 15
v9.TIMEOUT = 15
v9.REQUEST_DELAY = 0
v9.DETAIL_TTL_HOURS = 60
v9.MIN_FINAL_EVENTS = 10


def reparsed_dates(opening: str, today: date) -> tuple[str, str]:
    """Corrige surtout les dates sans année, ex. '18 janvier' vu en septembre."""
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


def iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except Exception:
        return None


def postprocess() -> None:
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    horizon = today + timedelta(days=v9.HORIZON_DAYS)

    data = json.loads(OUT.read_text(encoding="utf-8"))
    cleaned = []
    past_removed = 0
    after_horizon_removed = 0

    for event in data.get("events", []):
        event = dict(event)
        opening = event.get("opening", "")
        start, end = reparsed_dates(opening, today)
        if start:
            event["start_date"] = start
            event["end_date"] = end or start

        start_d = iso(event.get("start_date", ""))
        end_d = iso(event.get("end_date", ""))

        if end_d and end_d < today:
            past_removed += 1
            continue
        if start_d and start_d > horizon:
            after_horizon_removed += 1
            continue
        cleaned.append(event)

    cleaned.sort(
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            v9.norm(e.get("commune")),
            v9.norm(e.get("title")),
        )
    )
    cleaned = cleaned[:MAX_FINAL_EVENTS]

    if len(cleaned) < 10:
        raise RuntimeError(
            f"Contrôle qualité V10: seulement {len(cleaned)} événement(s) après nettoyage."
        )

    data["source_mode"] = "filtered_radius_fast_v10"
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["count"] = len(cleaned)
    data.setdefault("search", {})["centers"] = list(v9.SEARCH_CENTERS)
    data["search"]["max_final_events"] = MAX_FINAL_EVENTS
    data["search"]["max_pages_per_center"] = v9.MAX_PAGES_PER_CENTER
    data.setdefault("diagnostics", {})["v10_past_removed"] = past_removed
    data["diagnostics"]["v10_after_horizon_removed"] = after_horizon_removed
    data["diagnostics"]["v10_cache_ttl_hours"] = v9.DETAIL_TTL_HOURS
    data["events"] = cleaned

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)

    print("=== V10 POST-TRAITEMENT ===")
    print(f"Événements terminés retirés : {past_removed}")
    print(f"Après horizon retirés        : {after_horizon_removed}")
    print(f"Événements finaux            : {len(cleaned)}")


def main() -> None:
    print("V10 RAPIDE : 3 centres utiles + cache V9 + nettoyage rolling 50.")
    v9.main()
    postprocess()
    print("OK V10: agenda.json prêt.")


if __name__ == "__main__":
    main()
