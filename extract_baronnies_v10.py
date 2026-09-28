#!/usr/bin/env python3
"""Agenda Baronnies — moteur inspiré de Nyons.

Principe :
- recherche Nyons + 100 km sur 180 jours ;
- récupère TOUS les vrais liens /fiches/ des pages de résultats ;
- dédoublonne immédiatement par URL ;
- ne tente plus de deviner la commune dans la carte de liste ;
- utilise d'abord le cache des fiches ;
- ouvre ensuite les fiches nouvelles en parallèle ;
- APRÈS lecture de la fiche, garde seulement les communes des Baronnies ;
- exclut Nyons à ce moment-là ;
- enlève les événements terminés et garde les 50 prochains.

Ainsi, une carte qui n'affiche pas sa commune n'est plus perdue.
"""
from __future__ import annotations

import hashlib
import json
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
CACHE_FILE = ROOT / "_detail_cache_v9.json"

CENTER = "nyons"
RADIUS_KM = 100
HORIZON_DAYS = 180
MAX_LIST_PAGES = 160
MAX_FINAL_EVENTS = 50
MIN_FINAL_EVENTS = 10
LIST_WORKERS = 24
DETAIL_WORKERS = 16
DETAIL_BATCH_SIZE = 120
TIMEOUT = 18
DETAIL_TTL_HOURS = 60

v9.RADIUS_KM = RADIUS_KM
v9.TIMEOUT = TIMEOUT
v9.DETAIL_TTL_HOURS = DETAIL_TTL_HOURS


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
                time.sleep(0.5)
    return {"page": page_no, "status": 0, "url": url, "html": "", "error": last_error}


def extract_true_links(html: str, current_url: str, page_no: int) -> list[dict]:
    """Récupère tous les vrais liens /fiches/, sans filtre de commune."""
    soup = v9.BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    by_url: dict[str, dict] = {}

    for a in root.find_all("a", href=True):
        href = v9.normalize_url(urljoin(current_url, a.get("href", "")))
        if not v9.is_detail_url(href):
            continue
        label = v9.clean(a.get_text(" ", strip=True))
        item = {"url": href, "list_label": label, "list_page": page_no}
        old = by_url.get(href)
        if old is None or len(label) > len(old.get("list_label", "")):
            by_url[href] = item
    return list(by_url.values())


def collect_candidates(start: date, end: date) -> tuple[dict[str, dict], dict]:
    first = get_page(1, start, end)
    if not first["html"]:
        raise RuntimeError(f"Page 1 agenda inaccessible: {first['error'] or first['status']}")

    detected = v9.detect_total_pages(v9.BeautifulSoup(first["html"], "html.parser")) or 1
    total_pages = min(detected, MAX_LIST_PAGES)
    results = [first]

    print(f"PAGE 1: HTTP {first['status']} | pages détectées={detected} | plafond={MAX_LIST_PAGES}")

    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            futures = [pool.submit(get_page, p, start, end) for p in range(2, total_pages + 1)]
            for future in as_completed(futures):
                results.append(future.result())

    collected: dict[str, dict] = {}
    errors = 0
    page_stats = {}

    for result in sorted(results, key=lambda x: x["page"]):
        p = result["page"]
        if not result["html"]:
            errors += 1
            page_stats[str(p)] = {"status": result["status"], "links": 0, "error": result["error"]}
            continue

        items = extract_true_links(result["html"], result["url"], p)
        new_count = 0
        for item in items:
            old = collected.get(item["url"])
            if old is None:
                collected[item["url"]] = item
                new_count += 1
            else:
                old["list_page"] = min(old.get("list_page", p), p)
                if len(item.get("list_label", "")) > len(old.get("list_label", "")):
                    old["list_label"] = item.get("list_label", "")

        page_stats[str(p)] = {"status": result["status"], "links": len(items), "new": new_count}
        if p == 1 or p % 10 == 0 or new_count:
            print(f"Page {p:03d}/{total_pages}: liens={len(items)} | +{new_count} | total={len(collected)}")

    return collected, {
        "detected_pages": detected,
        "pages_scanned": total_pages,
        "list_errors": errors,
        "list_workers": LIST_WORKERS,
        "candidate_urls": len(collected),
        "page_stats": page_stats,
    }


def cache_fresh(entry: dict) -> bool:
    stamp = v9.clean(entry.get("fetched_at"))
    if not stamp:
        return False
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
        return age < DETAIL_TTL_HOURS * 3600
    except Exception:
        return False


def get_detail(item: dict) -> tuple[dict | None, str]:
    try:
        r = requests.get(item["url"], headers=v9.HEADERS, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        return v9.parse_detail(r.text, item, r.url), ""
    except Exception as exc:
        return None, str(exc)


def reparse_dates(opening: str, today: date) -> tuple[str, str]:
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
            candidates.append((day, month, int(match.group(3)) if match.group(3) else None))
    if not candidates:
        return "", ""

    explicit = [y for _, _, y in candidates if y]
    year = explicit[0] if explicit else today.year
    if not explicit:
        d0, m0, _ = candidates[0]
        try:
            if date(year, m0, d0) < today - timedelta(days=7):
                year += 1
        except ValueError:
            pass

    values: list[date] = []
    prev_month = None
    for d, m, y in candidates:
        if y:
            year = y
        elif prev_month is not None and m < prev_month - 6:
            year += 1
        prev_month = m
        try:
            values.append(date(year, m, d))
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


def accept_event(data: dict, item: dict, today: date, horizon: date, stats: dict) -> dict | None:
    event = dict(data)
    commune = event.get("commune", "")

    # Le filtre est volontairement ici, APRÈS lecture de la fiche.
    if v9.norm(commune) == "nyons":
        stats["nyons_excluded"] += 1
        return None
    if not commune or v9.norm(commune) not in v9.COMMUNE_BY_NORM:
        stats["outside_rejected"] += 1
        return None

    start, end = reparse_dates(event.get("opening", ""), today)
    if start:
        event["start_date"] = start
        event["end_date"] = end or start

    start_d = iso(event.get("start_date", ""))
    end_d = iso(event.get("end_date", ""))
    if end_d and end_d < today:
        stats["past_removed"] += 1
        return None
    if start_d and start_d > horizon:
        stats["after_horizon_removed"] += 1
        return None

    event["list_page"] = item.get("list_page")
    return event


def dedupe_and_sort(events: list[dict]) -> tuple[list[dict], int]:
    unique = []
    seen_urls = set()
    seen_fp = set()
    duplicates = 0

    for event in sorted(events, key=lambda e: (
        e.get("start_date") or "9999-12-31",
        v9.norm(e.get("commune")),
        v9.norm(e.get("title")),
    )):
        url_key = v9.normalize_url(event.get("url", ""))
        raw = "|".join([
            v9.norm(event.get("title")),
            event.get("start_date", ""),
            event.get("end_date", ""),
            v9.norm(event.get("commune")),
        ])
        fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if (url_key and url_key in seen_urls) or fp in seen_fp:
            duplicates += 1
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_fp.add(fp)
        event["dedupe_id"] = fp[:16]
        unique.append(event)
    return unique, duplicates


def fetch_details(collected: dict[str, dict], cache: dict, today: date, horizon: date) -> tuple[list[dict], dict]:
    stats = {
        "cache_hits": 0,
        "network_attempts": 0,
        "network_fetches": 0,
        "detail_errors": 0,
        "nyons_excluded": 0,
        "outside_rejected": 0,
        "past_removed": 0,
        "after_horizon_removed": 0,
        "batches": 0,
    }
    accepted: list[dict] = []
    pending: list[dict] = []

    ordered = sorted(collected.values(), key=lambda x: (x.get("list_page", 9999), x.get("url", "")))

    # D'abord toutes les fiches déjà connues du cache.
    for item in ordered:
        entry = cache.get(item["url"])
        if isinstance(entry, dict) and cache_fresh(entry) and isinstance(entry.get("data"), dict):
            stats["cache_hits"] += 1
            event = accept_event(entry["data"], item, today, horizon, stats)
            if event:
                accepted.append(event)
        else:
            pending.append(item)

    current_unique, _ = dedupe_and_sort(accepted)
    print(
        f"FICHES: {len(collected)} URLs | cache={stats['cache_hits']} | "
        f"valides cache={len(current_unique)} | réseau restant={len(pending)}"
    )

    # Les nouvelles fiches sont ouvertes par lots. Dès que 50 événements valides
    # sont obtenus, il est inutile d'ouvrir les milliers de fiches restantes.
    for offset in range(0, len(pending), DETAIL_BATCH_SIZE):
        if len(current_unique) >= MAX_FINAL_EVENTS:
            break

        batch = pending[offset:offset + DETAIL_BATCH_SIZE]
        stats["batches"] += 1
        stats["network_attempts"] += len(batch)

        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            futures = {pool.submit(get_detail, item): item for item in batch}
            for future in as_completed(futures):
                item = futures[future]
                data, error = future.result()
                if not data:
                    stats["detail_errors"] += 1
                    continue
                stats["network_fetches"] += 1
                cache[item["url"]] = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "data": data,
                }
                event = accept_event(data, item, today, horizon, stats)
                if event:
                    accepted.append(event)

        current_unique, _ = dedupe_and_sort(accepted)
        print(
            f"Lot {stats['batches']}: {len(batch)} fiches testées | "
            f"Baronnies valides cumulées={len(current_unique)}"
        )

    final_unique, duplicates = dedupe_and_sort(accepted)
    stats["duplicates_removed"] = duplicates
    return final_unique[:MAX_FINAL_EVENTS], stats


def save_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    started = time.monotonic()
    today = today_paris()
    horizon = today + timedelta(days=HORIZON_DAYS)

    print("MOTEUR NYONS BARONNIES : tous les liens d'abord, filtre commune après la fiche")
    print(f"Période {today} -> {horizon} | centre={CENTER} | rayon={RADIUS_KM} km | max={MAX_FINAL_EVENTS}")

    collected, list_diag = collect_candidates(today, horizon)
    if not collected:
        raise RuntimeError("Aucun lien /fiches/ trouvé. agenda.json reste inchangé.")

    cache = v9.load_json(CACHE_FILE)
    final_events, detail_diag = fetch_details(collected, cache, today, horizon)
    save_json(CACHE_FILE, cache)

    if len(final_events) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Contrôle qualité: seulement {len(final_events)} événement(s) Baronnies hors Nyons. "
            "agenda.json reste inchangé."
        )

    elapsed = round(time.monotonic() - started, 2)
    payload = {
        "source": v9.BASE,
        "source_mode": "nyons_engine_all_links_filter_after_detail",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "search": {
            "date_du": today.strftime("%d/%m/%Y"),
            "date_au": horizon.strftime("%d/%m/%Y"),
            "center": CENTER,
            "radius_km": RADIUS_KM,
            "max_final_events": MAX_FINAL_EVENTS,
        },
        "count": len(final_events),
        "diagnostics": {**list_diag, **detail_diag, "elapsed_seconds": elapsed},
        "events": final_events,
    }
    save_json(OUT, payload)

    print("=== BILAN ===")
    print(f"Pages liste             : {list_diag['pages_scanned']}")
    print(f"URLs candidates         : {list_diag['candidate_urls']}")
    print(f"Cache utilisé           : {detail_diag['cache_hits']}")
    print(f"Fiches réseau testées   : {detail_diag['network_attempts']}")
    print(f"Nyons exclus après fiche: {detail_diag['nyons_excluded']}")
    print(f"Hors Baronnies          : {detail_diag['outside_rejected']}")
    print(f"Événements finaux       : {len(final_events)}")
    print(f"Durée script            : {elapsed} s")
    print("OK: agenda.json prêt.")


if __name__ == "__main__":
    main()
