#!/usr/bin/env python3
"""
Moteur Baronnies calqué sur le fonctionnement qui marche pour Nyons.

Principe :
- une seule recherche La Drôme Tourisme autour de Nyons, rayon 100 km ;
- page 1 lue d'abord pour connaître le nombre réel de pages ;
- pages de liste téléchargées en parallèle pour éviter les runs de 10 minutes ;
- sur chaque carte, on ne garde que les communes des Baronnies et on exclut Nyons ;
- dédoublonnage immédiat par URL, comme sur l'agenda Nyons ;
- ouverture uniquement des fiches utiles ;
- cache des fiches pendant 60 h ;
- contrôle final sur la commune réelle de la fiche ;
- correction des dates sans année et suppression des événements terminés ;
- maximum 50 prochains événements.

Aucun appel OpenAI ici : comme pour Nyons, l'IA viendra après la collecte,
uniquement sur les événements nouveaux ou modifiés.
"""

from __future__ import annotations

import hashlib
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
MAX_LIST_PAGES = 160
MAX_FINAL_EVENTS = 50
LIST_WORKERS = 10
DETAIL_WORKERS = 8
TIMEOUT = 20
DETAIL_TTL_HOURS = 60
MIN_FINAL_EVENTS = 10

# On réutilise les parseurs robustes déjà validés en V9.
v9.RADIUS_KM = RADIUS_KM
v9.TIMEOUT = TIMEOUT
v9.DETAIL_TTL_HOURS = DETAIL_TTL_HOURS


def today_paris() -> date:
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def list_url(page_no: int, start: date, end: date) -> str:
    return v9.search_url(CENTER, page_no, start, end)


def commune_in_text(text: str) -> str:
    """Repère une commune Baronnies dans le texte local d'une carte."""
    n = " " + v9.norm(text) + " "
    for key in v9.COMMUNE_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", n):
            return v9.COMMUNE_BY_NORM[key]
    return ""


def local_card_for_link(link):
    """
    Cherche le plus petit ancêtre qui semble être UNE carte événement.
    On évite ainsi de lire une grosse zone contenant plusieurs communes.
    """
    parent = link.parent

    for _ in range(8):
        if parent is None:
            break

        text = v9.clean(parent.get_text(" ", strip=True))
        if text and len(text) <= 2200:
            urls = set()
            for a in parent.find_all("a", href=True):
                href = v9.normalize_url(
                    urljoin("https://www.drome-cestmanature.com/", a.get("href", ""))
                )
                if v9.is_detail_url(href):
                    urls.add(href)

            if len(urls) == 1:
                return parent

        parent = parent.parent

    return None


def extract_list_candidates(html: str, current_url: str) -> list[dict]:
    """Même logique que Nyons : vrais liens événement + une seule entrée par URL."""
    soup = v9.BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    by_url: dict[str, dict] = {}

    for a in root.find_all("a", href=True):
        href = v9.normalize_url(urljoin(current_url, a.get("href", "")))
        if not v9.is_detail_url(href):
            continue

        card = local_card_for_link(a)
        if card is None:
            continue

        card_text = v9.clean(card.get_text(" ", strip=True))
        commune = commune_in_text(card_text)

        # Si la carte ne donne pas de commune fiable, on ne l'ouvre pas.
        if not commune:
            continue
        if v9.norm(commune) == "nyons":
            continue

        label = v9.clean(a.get_text(" ", strip=True))
        item = {
            "url": href,
            "list_label": label,
            "list_commune": commune,
        }

        old = by_url.get(href)
        if not old or len(label) > len(old.get("list_label", "")):
            by_url[href] = item

    return list(by_url.values())


def get_page(page_no: int, start: date, end: date) -> dict:
    """Télécharge une page de résultats avec une petite relance en cas d'erreur."""
    url = list_url(page_no, start, end)
    last_error = None

    for attempt in range(2):
        try:
            r = requests.get(
                url,
                headers=v9.HEADERS,
                timeout=TIMEOUT,
                allow_redirects=True,
            )
            if r.status_code == 404:
                return {
                    "page": page_no,
                    "status": 404,
                    "url": url,
                    "html": "",
                    "error": "",
                }
            r.raise_for_status()
            return {
                "page": page_no,
                "status": r.status_code,
                "url": r.url,
                "html": r.text,
                "error": "",
            }
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.8)

    return {
        "page": page_no,
        "status": 0,
        "url": url,
        "html": "",
        "error": str(last_error or "erreur inconnue"),
    }


def collect_candidates(start: date, end: date) -> tuple[dict[str, dict], dict]:
    """
    Page 1 d'abord, puis le reste en parallèle.
    C'est le point qui remplace le long parcours séquentiel des essais précédents.
    """
    first = get_page(1, start, end)
    if not first["html"]:
        raise RuntimeError(f"Page 1 agenda inaccessible: {first['error'] or first['status']}")

    first_soup = v9.BeautifulSoup(first["html"], "html.parser")
    detected = v9.detect_total_pages(first_soup) or 1
    total_pages = min(detected, MAX_LIST_PAGES)

    collected: dict[str, dict] = {}
    page_stats = {}
    errors = 0

    def absorb(result: dict) -> None:
        nonlocal errors
        page_no = result["page"]
        if not result["html"]:
            errors += 1
            page_stats[str(page_no)] = {
                "status": result["status"],
                "candidates": 0,
                "error": result["error"],
            }
            print(f"Page {page_no:03d}: ERREUR {result['error'] or result['status']}")
            return

        candidates = extract_list_candidates(result["html"], result["url"])
        new_count = 0

        for item in candidates:
            old = collected.get(item["url"])
            if old is None:
                collected[item["url"]] = item
                new_count += 1
            elif len(item.get("list_label", "")) > len(old.get("list_label", "")):
                old["list_label"] = item.get("list_label", "")
                if item.get("list_commune"):
                    old["list_commune"] = item["list_commune"]

        page_stats[str(page_no)] = {
            "status": result["status"],
            "candidates": len(candidates),
            "new": new_count,
        }
        print(
            f"Page {page_no:03d}/{total_pages}: "
            f"{len(candidates)} Baronnies | +{new_count} | total={len(collected)}"
        )

    print(
        f"PAGE 1: HTTP {first['status']} | pages détectées={detected} | "
        f"plafond={MAX_LIST_PAGES}"
    )
    absorb(first)

    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            futures = {
                pool.submit(get_page, page_no, start, end): page_no
                for page_no in range(2, total_pages + 1)
            }
            for future in as_completed(futures):
                absorb(future.result())

    diagnostics = {
        "detected_pages": detected,
        "pages_scanned": total_pages,
        "list_errors": errors,
        "list_workers": LIST_WORKERS,
        "candidate_urls_after_card_filter": len(collected),
        "page_stats": page_stats,
    }
    return collected, diagnostics


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


def get_detail_from_network(item: dict) -> tuple[dict | None, str]:
    try:
        r = requests.get(
            item["url"],
            headers=v9.HEADERS,
            timeout=TIMEOUT,
            allow_redirects=True,
        )
        r.raise_for_status()
        data = v9.parse_detail(r.text, item, r.url)
        return data, ""
    except Exception as exc:
        return None, str(exc)


def reparse_dates(opening: str, today: date) -> tuple[str, str]:
    """Corrige notamment '18 janvier' sans année rencontré en septembre."""
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


def fetch_details(
    collected: dict[str, dict],
    cache: dict,
    today: date,
    horizon: date,
) -> tuple[list[dict], dict]:
    """
    Comme Nyons : cache d'abord.
    Seules les fiches nouvelles/périmées partent sur le réseau.
    """
    events = []
    pending = []
    cache_hits = 0
    network_fetches = 0
    detail_errors = 0
    nyons_excluded = 0
    outside_rejected = 0
    past_removed = 0
    after_horizon_removed = 0

    for item in collected.values():
        entry = cache.get(item["url"])
        if isinstance(entry, dict) and cache_fresh(entry):
            data = entry.get("data")
            if isinstance(data, dict):
                cache_hits += 1
                pending_data = dict(data)
                pending_data["_from_cache"] = True
                pending_data["_item"] = item
                events.append(pending_data)
                continue
        pending.append(item)

    print(
        f"FICHES: {len(collected)} candidates | cache={cache_hits} | "
        f"réseau={len(pending)}"
    )

    if pending:
        with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
            futures = {
                pool.submit(get_detail_from_network, item): item
                for item in pending
            }
            for future in as_completed(futures):
                item = futures[future]
                data, error = future.result()
                if not data:
                    detail_errors += 1
                    print(f"DETAIL ERREUR: {item['url']} | {error}")
                    continue

                network_fetches += 1
                cache[item["url"]] = {
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "data": data,
                }
                data = dict(data)
                data["_from_cache"] = False
                data["_item"] = item
                events.append(data)

    cleaned = []

    for event in events:
        item = event.pop("_item", {})
        event.pop("_from_cache", None)

        commune = event.get("commune", "")
        if v9.norm(commune) == "nyons":
            nyons_excluded += 1
            continue
        if not commune or v9.norm(commune) not in v9.COMMUNE_BY_NORM:
            outside_rejected += 1
            continue

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

        event["list_commune"] = item.get("list_commune", "")
        cleaned.append(event)

    diagnostics = {
        "detail_cache_hits": cache_hits,
        "detail_network_fetches": network_fetches,
        "detail_errors": detail_errors,
        "nyons_excluded_after_detail": nyons_excluded,
        "outside_or_unrecognized_after_detail": outside_rejected,
        "past_removed": past_removed,
        "after_horizon_removed": after_horizon_removed,
        "detail_workers": DETAIL_WORKERS,
    }
    return cleaned, diagnostics


def dedupe_and_sort(events: list[dict]) -> tuple[list[dict], int]:
    unique = []
    seen_urls = set()
    seen_fp = set()
    duplicates = 0

    for event in sorted(
        events,
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            v9.norm(e.get("commune")),
            v9.norm(e.get("title")),
        ),
    ):
        url_key = v9.normalize_url(event.get("url", ""))
        raw = "|".join(
            [
                v9.norm(event.get("title")),
                event.get("start_date", ""),
                event.get("end_date", ""),
                v9.norm(event.get("commune")),
            ]
        )
        fp = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        if (url_key and url_key in seen_urls) or fp in seen_fp:
            duplicates += 1
            continue

        if url_key:
            seen_urls.add(url_key)
        seen_fp.add(fp)

        event["dedupe_id"] = fp[:16]
        unique.append(event)

    return unique[:MAX_FINAL_EVENTS], duplicates


def save_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def main() -> None:
    started_at = time.monotonic()
    today = today_paris()
    horizon = today + timedelta(days=HORIZON_DAYS)

    print("MOTEUR NYONS ADAPTE AUX BARONNIES")
    print(
        f"Période {today} -> {horizon} | centre={CENTER} | "
        f"rayon={RADIUS_KM} km | max={MAX_FINAL_EVENTS}"
    )

    collected, list_diag = collect_candidates(today, horizon)

    if not collected:
        raise RuntimeError(
            "Aucune fiche Baronnies trouvée dans les cartes de résultats. "
            "agenda.json reste inchangé."
        )

    cache = v9.load_json(CACHE_FILE)
    events, detail_diag = fetch_details(collected, cache, today, horizon)
    save_json(CACHE_FILE, cache)

    final_events, duplicates = dedupe_and_sort(events)

    if len(final_events) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Contrôle qualité: seulement {len(final_events)} événement(s) "
            "Baronnies hors Nyons. agenda.json reste inchangé."
        )

    elapsed = round(time.monotonic() - started_at, 2)

    payload = {
        "source": v9.BASE,
        "source_mode": "nyons_engine_parallel_100km",
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
        "diagnostics": {
            **list_diag,
            **detail_diag,
            "duplicates_removed": duplicates,
            "elapsed_seconds": elapsed,
        },
        "events": final_events,
    }

    save_json(OUT, payload)

    print("=== BILAN ===")
    print(f"Pages liste             : {list_diag['pages_scanned']}")
    print(f"Candidates Baronnies    : {len(collected)}")
    print(f"Cache détail            : {detail_diag['detail_cache_hits']}")
    print(f"Fiches réseau           : {detail_diag['detail_network_fetches']}")
    print(f"Doublons supprimés      : {duplicates}")
    print(f"Événements finaux       : {len(final_events)}")
    print(f"Durée script            : {elapsed} s")
    print("OK: agenda.json prêt.")


if __name__ == "__main__":
    main()
