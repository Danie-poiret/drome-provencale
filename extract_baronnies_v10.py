#!/usr/bin/env python3
"""Agenda autour de Nyons : on collecte d'abord, on enlève Nyons ensuite.

Logique volontairement simple :
1) on récupère les prochains événements autour de Nyons dans un rayon de 100 km ;
2) aucun filtre de territoire pendant la collecte ;
3) on trie chronologiquement ;
4) seulement ensuite, on vérifie la fiche et on retire Nyons ;
5) tout le reste est conservé, quelle que soit la commune ;
6) on publie jusqu'à 100 événements hors Nyons.
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
CACHE_FILE = ROOT / "_detail_cache_v9.json"

CENTER = "nyons"
RADIUS_KM = 100
HORIZON_DAYS = 180
TARGET_EVENTS = 100
CANDIDATE_TARGET = 180
MAX_LIST_PAGES = 40
LIST_WORKERS = 10
DETAIL_WORKERS = 12
TIMEOUT = 18
DETAIL_TTL_HOURS = 60

v9.RADIUS_KM = RADIUS_KM
v9.TIMEOUT = TIMEOUT
v9.DETAIL_TTL_HOURS = DETAIL_TTL_HOURS

MONTH_RE = "|".join(sorted((re.escape(x) for x in v9.MONTHS), key=len, reverse=True))
LIST_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE})\.?(?:\s+(20\d{{2}}))?\b", re.I)


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
    items = []

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
    explicit_year = int(m.group(3)) if m.group(3) else None
    if not month:
        return ""

    year = explicit_year or today.year
    try:
        candidate = date(year, month, day)
        if not explicit_year and candidate < today - timedelta(days=7):
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

        start = date_from_before(before, today)
        if not start:
            continue

        events.append({
            "title": title,
            "start_date": start,
            "end_date": start,
            "summary": v9.clean(after)[:420],
            "categories": [],
            "url": href,
            "source_url": href,
            "source_format": "drome_tourisme_list_then_detail_filter",
            "list_page": result["page"],
            "list_context": v9.clean(before[-250:] + " " + title + " " + after[:650]),
        })

    return events


def collect_candidates(today: date, horizon: date) -> tuple[list[dict], dict]:
    first = get_page(1, today, horizon)
    if not first["html"]:
        raise RuntimeError(f"Page 1 inaccessible: {first['error'] or first['status']}")

    detected = v9.detect_total_pages(v9.BeautifulSoup(first["html"], "html.parser")) or 1
    max_pages = min(detected, MAX_LIST_PAGES)
    by_url: dict[str, dict] = {}
    errors = 0
    pages_read = 0

    def absorb(result: dict) -> None:
        nonlocal errors, pages_read
        pages_read += 1
        if not result.get("html"):
            errors += 1
            return
        events = parse_list_page(result, today)
        added = 0
        for event in events:
            if event["url"] not in by_url:
                by_url[event["url"]] = event
                added += 1
        print(
            f"Page {result['page']:03d}/{max_pages}: événements={len(events)} | "
            f"+{added} | total={len(by_url)}"
        )

    absorb(first)

    next_page = 2
    while next_page <= max_pages and len(by_url) < CANDIDATE_TARGET:
        batch_pages = list(range(next_page, min(next_page + LIST_WORKERS, max_pages + 1)))
        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            futures = [pool.submit(get_page, p, today, horizon) for p in batch_pages]
            results = [f.result() for f in as_completed(futures)]
        for result in sorted(results, key=lambda x: x["page"]):
            absorb(result)
        next_page += LIST_WORKERS

    events = sorted(
        by_url.values(),
        key=lambda e: (e["start_date"], e.get("list_page", 9999), v9.norm(e["title"])),
    )
    events = [e for e in events if e["end_date"] >= today.isoformat()]

    return events, {
        "detected_pages": detected,
        "pages_read": pages_read,
        "list_errors": errors,
        "candidate_urls": len(events),
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


def fetch_detail(item: dict) -> tuple[dict | None, str]:
    try:
        r = requests.get(item["url"], headers=v9.HEADERS, timeout=TIMEOUT, allow_redirects=True)
        r.raise_for_status()
        return v9.parse_detail(r.text, item, r.url), ""
    except Exception as exc:
        return None, str(exc)


def remove_nyons_second_pass(candidates: list[dict], cache: dict) -> tuple[list[dict], dict]:
    """Aucun tri territorial ici : on retire seulement Nyons, après la collecte."""
    kept: list[dict] = []
    cache_hits = 0
    network = 0
    errors = 0
    nyons_removed = 0

    # On vérifie dans l'ordre chronologique et on s'arrête dès qu'on a 100 hors Nyons.
    index = 0
    while index < len(candidates) and len(kept) < TARGET_EVENTS:
        batch = candidates[index:index + DETAIL_WORKERS]
        index += DETAIL_WORKERS

        network_items = []
        details_by_url: dict[str, dict | None] = {}

        for item in batch:
            entry = cache.get(item["url"])
            if isinstance(entry, dict) and cache_fresh(entry) and isinstance(entry.get("data"), dict):
                cache_hits += 1
                details_by_url[item["url"]] = entry["data"]
            else:
                network_items.append(item)

        if network_items:
            with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
                futures = {pool.submit(fetch_detail, item): item for item in network_items}
                for future in as_completed(futures):
                    item = futures[future]
                    detail, error = future.result()
                    if detail is None:
                        errors += 1
                        details_by_url[item["url"]] = None
                        continue
                    network += 1
                    cache[item["url"]] = {
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                        "data": detail,
                    }
                    details_by_url[item["url"]] = detail

        for item in batch:
            if len(kept) >= TARGET_EVENTS:
                break

            detail = details_by_url.get(item["url"])
            confirmed_nyons = False

            if isinstance(detail, dict):
                commune = v9.norm(detail.get("commune", ""))
                confirmed_nyons = commune == "nyons"
                if detail.get("commune"):
                    item["commune"] = detail.get("commune")
                if detail.get("address"):
                    item["address"] = detail.get("address")
            else:
                # Secours : si la fiche n'a pas pu être lue, on ne supprime que si
                # le bloc de liste dit clairement Nyons.
                confirmed_nyons = bool(re.search(r"\bnyons\b", v9.norm(item.get("list_context", ""))))

            if confirmed_nyons:
                nyons_removed += 1
                continue

            item.pop("list_context", None)
            kept.append(item)

    return kept, {
        "detail_cache_hits": cache_hits,
        "detail_network_fetches": network,
        "detail_errors": errors,
        "nyons_removed_second_pass": nyons_removed,
        "candidates_checked_second_pass": index,
    }


def save_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> None:
    started = time.monotonic()
    today = today_paris()
    horizon = today + timedelta(days=HORIZON_DAYS)

    print("ETAPE 1 : je récupère les prochains événements, sans filtre territorial.")
    candidates, list_diag = collect_candidates(today, horizon)
    if not candidates:
        raise RuntimeError("Aucun événement trouvé. agenda.json reste inchangé.")

    print(f"ETAPE 1 OK : {len(candidates)} candidats triés.")
    print("ETAPE 2 : seulement maintenant, je retire Nyons.")

    cache = v9.load_json(CACHE_FILE)
    final_events, detail_diag = remove_nyons_second_pass(candidates, cache)
    save_json(CACHE_FILE, cache)

    if not final_events:
        raise RuntimeError("Tous les événements contrôlés ont été retirés. agenda.json reste inchangé.")

    elapsed = round(time.monotonic() - started, 2)
    payload = {
        "source": v9.BASE,
        "source_mode": "collect_first_remove_nyons_second",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Prochains événements dans un rayon de 100 km autour de Nyons, Nyons retiré en second passage",
        "search": {
            "date_du": today.strftime("%d/%m/%Y"),
            "date_au": horizon.strftime("%d/%m/%Y"),
            "center": CENTER,
            "radius_km": RADIUS_KM,
            "target_events": TARGET_EVENTS,
        },
        "count": len(final_events),
        "diagnostics": {
            **list_diag,
            **detail_diag,
            "elapsed_seconds": elapsed,
            "territory_filter": "none",
            "only_exclusion": "Nyons, second pass",
        },
        "events": final_events,
    }
    save_json(OUT, payload)

    print("=== BILAN ===")
    print(f"Candidats collectés       : {len(candidates)}")
    print(f"Nyons retirés ensuite     : {detail_diag['nyons_removed_second_pass']}")
    print(f"Événements publiés        : {len(final_events)}")
    print(f"Fiches réseau             : {detail_diag['detail_network_fetches']}")
    print(f"Cache                     : {detail_diag['detail_cache_hits']}")
    print(f"Durée                     : {elapsed} s")
    print("OK: agenda.json prêt.")


if __name__ == "__main__":
    main()
