#!/usr/bin/env python3
"""Agenda autour de Nyons : prendre les 100 prochains, retirer Nyons ensuite.

Logique simple et robuste :
1) lire les pages de résultats dans l'ordre chronologique ;
2) récupérer TOUS les vrais liens /fiches/ avec le parseur V9 déjà validé ;
3) dédoublonner uniquement par URL ;
4) seulement ensuite lire le cache / la fiche pour connaître la commune ;
5) retirer uniquement Nyons ;
6) garder tout le reste, sans filtre Baronnies ;
7) s'arrêter dès que 100 événements valides hors Nyons sont obtenus.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import extract_baronnies_v9 as v9

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
CACHE_FILE = ROOT / "_detail_cache_v9.json"

CENTER = "nyons"
RADIUS_KM = 100
HORIZON_DAYS = 180
TARGET_EVENTS = 100
MAX_LIST_PAGES = 160
LIST_BATCH = 5
LIST_WORKERS = 5
DETAIL_WORKERS = 16
TIMEOUT = 18

v9.RADIUS_KM = RADIUS_KM
v9.TIMEOUT = TIMEOUT


def today_paris() -> date:
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def list_url(page_no: int, start: date, end: date) -> str:
    return v9.search_url(CENTER, page_no, start, end)


def get_page(page_no: int, start: date, end: date) -> dict:
    url = list_url(page_no, start, end)
    last_error = ""
    for attempt in range(2):
        try:
            r = requests.get(
                url,
                headers=v9.HEADERS,
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 404:
                return {"page": page_no, "status": 404, "url": url, "html": "", "error": ""}
            r.raise_for_status()
            return {
                "page": page_no,
                "status": r.status_code,
                "url": r.url,
                "html": r.text,
                "error": "",
            }
        except Exception as exc:
            last_error = str(exc)
            if attempt == 0:
                time.sleep(0.4)

    return {"page": page_no, "status": 0, "url": url, "html": "", "error": last_error}


def get_detail(item: dict) -> tuple[dict | None, str]:
    try:
        r = requests.get(
            item["url"],
            headers=v9.HEADERS,
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        r.raise_for_status()
        return v9.parse_detail(r.text, item, r.url), ""
    except Exception as exc:
        return None, str(exc)


def reparse_dates(opening: str, today: date) -> tuple[str, str]:
    """Corrige les dates sans année, ex. janvier prochain vu en septembre."""
    opening = v9.clean(opening)
    if not opening:
        return "", ""

    candidates: list[tuple[int, int, int | None]] = []

    for d, m, y in v9.NUMERIC_DATE_RE.findall(opening):
        candidates.append((int(d), int(m), int(y) if y else None))

    for match in v9.NAMED_DATE_RE.finditer(opening):
        day = int(match.group(1))
        month = v9.MONTHS.get(match.group(2).lower().rstrip("."))
        if month:
            candidates.append(
                (day, month, int(match.group(3)) if match.group(3) else None)
            )

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


def resolve_details(items: list[dict], cache: dict) -> tuple[dict[str, dict | None], dict]:
    """Cache d'abord, réseau seulement pour les fiches jamais connues."""
    details: dict[str, dict | None] = {}
    pending: list[dict] = []
    cache_hits = 0
    network_fetches = 0
    errors = 0

    for item in items:
        entry = cache.get(item["url"])
        # Pour déterminer la commune, même un cache ancien reste utile.
        if isinstance(entry, dict) and isinstance(entry.get("data"), dict):
            details[item["url"]] = entry["data"]
            cache_hits += 1
        else:
            pending.append(item)

    if pending:
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            futures = {pool.submit(get_detail, item): item for item in pending}
            for future in as_completed(futures):
                item = futures[future]
                detail, error = future.result()
                if detail is None:
                    details[item["url"]] = None
                    errors += 1
                    print(f"DETAIL ERREUR: {item['url']} | {error}")
                    continue

                details[item["url"]] = detail
                network_fetches += 1
                cache[item["url"]] = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "data": detail,
                }

    return details, {
        "cache_hits": cache_hits,
        "network_fetches": network_fetches,
        "detail_errors": errors,
    }


def main() -> None:
    started = time.monotonic()
    today = today_paris()
    horizon = today + timedelta(days=HORIZON_DAYS)

    print("AGENDA SIMPLE : tous les événements d'abord, Nyons retiré ensuite.")
    print(
        f"Période {today} -> {horizon} | centre={CENTER} | "
        f"rayon={RADIUS_KM} km | objectif={TARGET_EVENTS}"
    )

    first = get_page(1, today, horizon)
    if not first["html"]:
        raise RuntimeError(f"Page 1 inaccessible: {first['error'] or first['status']}")

    detected = v9.detect_total_pages(v9.BeautifulSoup(first["html"], "html.parser")) or 1
    total_pages = min(detected, MAX_LIST_PAGES)
    print(f"Pages détectées={detected} | plafond={total_pages}")

    cache = v9.load_json(CACHE_FILE)
    seen_urls: set[str] = set()
    kept: list[dict] = []

    pages_read = 0
    list_errors = 0
    raw_links = 0
    unique_candidates = 0
    nyons_removed = 0
    past_removed = 0
    after_horizon_removed = 0
    detail_errors = 0
    cache_hits = 0
    network_fetches = 0

    next_page = 1

    while next_page <= total_pages and len(kept) < TARGET_EVENTS:
        batch_pages = list(range(next_page, min(next_page + LIST_BATCH, total_pages + 1)))

        if 1 in batch_pages:
            results = [first]
            remaining = [p for p in batch_pages if p != 1]
        else:
            results = []
            remaining = batch_pages

        if remaining:
            with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
                futures = [pool.submit(get_page, p, today, horizon) for p in remaining]
                results.extend(f.result() for f in as_completed(futures))

        new_items: list[dict] = []

        for result in sorted(results, key=lambda x: x["page"]):
            pages_read += 1
            page_no = result["page"]

            if not result["html"]:
                list_errors += 1
                print(f"Page {page_no:03d}: ERREUR {result['error'] or result['status']}")
                continue

            # IMPORTANT : on réutilise le parseur qui trouvait bien ~36 liens/page.
            items = v9.extract_candidates(result["html"], result["url"])
            raw_links += len(items)
            added = 0

            for item in items:
                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                item = dict(item)
                item["list_page"] = page_no
                new_items.append(item)
                added += 1

            unique_candidates += added
            print(
                f"Page {page_no:03d}/{total_pages}: liens={len(items)} | "
                f"+{added} | candidats uniques={unique_candidates}"
            )

        if new_items:
            # Les pages sont déjà chronologiques. On vérifie les nouvelles fiches
            # seulement après les avoir collectées, puis on enlève Nyons.
            details, stats = resolve_details(new_items, cache)
            cache_hits += stats["cache_hits"]
            network_fetches += stats["network_fetches"]
            detail_errors += stats["detail_errors"]

            for item in new_items:
                if len(kept) >= TARGET_EVENTS:
                    break

                detail = details.get(item["url"])

                if isinstance(detail, dict):
                    commune = v9.clean(detail.get("commune", ""))
                    if v9.norm(commune) == "nyons":
                        nyons_removed += 1
                        continue

                    event = dict(detail)
                    event["list_page"] = item.get("list_page")

                    start, end = reparse_dates(event.get("opening", ""), today)
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

                    kept.append(event)
                else:
                    # Une fiche impossible à lire n'est pas supprimée au hasard.
                    # On la garde avec les informations de liste.
                    kept.append({
                        "title": item.get("list_label", "Événement"),
                        "start_date": "",
                        "end_date": "",
                        "commune": "",
                        "description": "",
                        "opening": "",
                        "tariffs": "",
                        "contact": "",
                        "url": item["url"],
                        "source_url": item["url"],
                        "source_format": "drome_tourisme_list_fallback",
                        "list_page": item.get("list_page"),
                    })

        print(
            f"APRÈS LOT : gardés hors Nyons={len(kept)} | "
            f"Nyons retirés={nyons_removed}"
        )
        next_page += LIST_BATCH

    save_tmp = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
    save_tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    save_tmp.replace(CACHE_FILE)

    final_events = kept[:TARGET_EVENTS]

    if len(final_events) < 10:
        raise RuntimeError(
            f"Contrôle qualité: seulement {len(final_events)} événement(s) hors Nyons. "
            "agenda.json reste inchangé."
        )

    elapsed = round(time.monotonic() - started, 2)

    payload = {
        "source": v9.BASE,
        "source_mode": "all_links_then_remove_nyons",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "100 prochains événements dans un rayon de 100 km autour de Nyons, Nyons retiré après collecte",
        "search": {
            "date_du": today.strftime("%d/%m/%Y"),
            "date_au": horizon.strftime("%d/%m/%Y"),
            "center": CENTER,
            "radius_km": RADIUS_KM,
            "target_events": TARGET_EVENTS,
        },
        "count": len(final_events),
        "diagnostics": {
            "detected_pages": detected,
            "pages_read": pages_read,
            "list_errors": list_errors,
            "raw_links_seen": raw_links,
            "unique_candidates_seen": unique_candidates,
            "nyons_removed_second_pass": nyons_removed,
            "past_removed": past_removed,
            "after_horizon_removed": after_horizon_removed,
            "detail_cache_hits": cache_hits,
            "detail_network_fetches": network_fetches,
            "detail_errors": detail_errors,
            "elapsed_seconds": elapsed,
            "territory_filter": "none",
            "only_exclusion": "Nyons",
        },
        "events": final_events,
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)

    print("=== BILAN ===")
    print(f"Pages lues              : {pages_read}")
    print(f"Liens bruts vus         : {raw_links}")
    print(f"Candidats uniques       : {unique_candidates}")
    print(f"Nyons retirés ensuite   : {nyons_removed}")
    print(f"Cache détail            : {cache_hits}")
    print(f"Fiches réseau           : {network_fetches}")
    print(f"Événements publiés      : {len(final_events)}")
    print(f"Durée                   : {elapsed} s")
    print("OK: agenda.json prêt.")


if __name__ == "__main__":
    main()
