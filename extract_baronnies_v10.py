#!/usr/bin/env python3
"""Agenda autour de Nyons : 100 prochains événements, Nyons retiré ensuite.

Cette version conserve la collecte rapide qui fonctionne déjà, puis fiabilise
les données pratiques AVANT publication :
- tous les vrais liens /fiches/ sont collectés sans filtre territorial ;
- dédoublonnage par URL ;
- commune relue depuis la fiche, avec secours générique via code postal + ville ;
- Nyons est retiré seulement après cette lecture ;
- dates début/fin normalisées depuis la période principale d'ouverture ;
- les événements terminés sont supprimés ;
- jusqu'à 100 événements sont publiés.
"""
from __future__ import annotations

import json
import re
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

POSTCODE_CITY_RE = re.compile(
    r"\b\d{5}\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’\- ]{1,80})",
    re.I,
)
PHONE_RE = re.compile(r"\s+0[1-9](?:[ .-]?\d{2}){4}\b")
CITY_STOP_RE = re.compile(
    r"\s+(?:Nous contacter|Mise à jour|Mise a jour|Visiter le site|Galerie d.images|"
    r"Ouverture|Tarifs|Contact|Accès|Acces)\b",
    re.I,
)

NUM_RANGE_RE = re.compile(
    r"\bdu\s+(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\s+au\s+"
    r"(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?\b",
    re.I,
)
NAMED_RANGE_RE = re.compile(
    rf"\bdu\s+(\d{{1,2}})\s+({v9.MONTH_RE})\.?(?:\s+(20\d{{2}}))?\s+au\s+"
    rf"(\d{{1,2}})\s+({v9.MONTH_RE})\.?(?:\s+(20\d{{2}}))?\b",
    re.I,
)
SINGLE_NUMERIC_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")


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


def iso(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except Exception:
        return None


def clean_city_candidate(value: str) -> str:
    value = v9.clean(value)
    if not value:
        return ""
    value = PHONE_RE.split(value, maxsplit=1)[0]
    value = CITY_STOP_RE.split(value, maxsplit=1)[0]
    value = re.split(r"\s+[|•·]\s+", value, maxsplit=1)[0]
    value = value.strip(" ,.;:-")
    # Une commune française raisonnable ne doit pas absorber tout le texte suivant.
    words = value.split()
    if len(words) > 7:
        value = " ".join(words[:7])
    return v9.clean(value)[:70]


def commune_from_detail(detail: dict) -> str:
    """Commune fiable, sans liste blanche de territoire."""
    current = v9.clean(detail.get("commune", ""))
    if current:
        return current

    # L'adresse est prioritaire ; le contact est un bon secours.
    for field in ("address", "contact"):
        text = v9.clean(detail.get(field, ""))
        if not text:
            continue
        matches = list(POSTCODE_CITY_RE.finditer(text))
        if not matches:
            continue
        # La dernière adresse postale est généralement celle du lieu de rendez-vous.
        candidate = clean_city_candidate(matches[-1].group(1))
        if candidate:
            return candidate
    return ""


def infer_range_years(
    sd: int,
    sm: int,
    sy: int | None,
    ed: int,
    em: int,
    ey: int | None,
    today: date,
) -> tuple[date, date] | None:
    """Construit une plage en gérant les années omises et les passages d'année."""
    if sy is None and ey is not None:
        sy = ey if (sm, sd) <= (em, ed) else ey - 1
    elif sy is not None and ey is None:
        ey = sy if (em, ed) >= (sm, sd) else sy + 1
    elif sy is None and ey is None:
        sy = today.year
        ey = sy if (em, ed) >= (sm, sd) else sy + 1
        try:
            if date(ey, em, ed) < today - timedelta(days=7):
                sy += 1
                ey += 1
        except ValueError:
            return None

    try:
        return date(int(sy), sm, sd), date(int(ey), em, ed)
    except (TypeError, ValueError):
        return None


def normalized_dates(detail: dict, today: date) -> tuple[str, str]:
    """
    Extrait d'abord la période PRINCIPALE d'ouverture.
    Cela évite que des dates secondaires ('sauf le 25 décembre', horaires saisonniers,
    etc.) deviennent par erreur la date de début ou de fin de l'événement.
    """
    opening = v9.clean(detail.get("opening", ""))

    if opening:
        m = NUM_RANGE_RE.search(opening)
        if m:
            rng = infer_range_years(
                int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None,
                int(m.group(4)), int(m.group(5)), int(m.group(6)) if m.group(6) else None,
                today,
            )
            if rng:
                return rng[0].isoformat(), rng[1].isoformat()

        m = NAMED_RANGE_RE.search(opening)
        if m:
            sm = v9.MONTHS.get(m.group(2).lower().rstrip("."))
            em = v9.MONTHS.get(m.group(5).lower().rstrip("."))
            if sm and em:
                rng = infer_range_years(
                    int(m.group(1)), sm, int(m.group(3)) if m.group(3) else None,
                    int(m.group(4)), em, int(m.group(6)) if m.group(6) else None,
                    today,
                )
                if rng:
                    return rng[0].isoformat(), rng[1].isoformat()

        low = v9.norm(opening)
        if "toute l annee" in low or "toute l'année" in opening.lower():
            return date(today.year, 1, 1).isoformat(), date(today.year, 12, 31).isoformat()

        m = SINGLE_NUMERIC_RE.search(opening)
        if m:
            try:
                d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                return d.isoformat(), d.isoformat()
            except ValueError:
                pass

    # Secours : conserver les dates déjà extraites si elles sont cohérentes.
    start = iso(detail.get("start_date", ""))
    end = iso(detail.get("end_date", ""))
    if start and end and end >= start:
        return start.isoformat(), end.isoformat()
    if start:
        return start.isoformat(), start.isoformat()
    return "", ""


def resolve_details(items: list[dict], cache: dict) -> tuple[dict[str, dict | None], dict]:
    """Cache d'abord, réseau seulement pour les fiches jamais connues."""
    details: dict[str, dict | None] = {}
    pending: list[dict] = []
    cache_hits = 0
    network_fetches = 0
    errors = 0

    for item in items:
        entry = cache.get(item["url"])
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

    print("AGENDA : collecte rapide -> nettoyage commune/date -> retrait Nyons -> 100.")
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
    commune_filled = 0
    commune_missing = 0
    dates_normalized = 0

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
            details, stats = resolve_details(new_items, cache)
            cache_hits += stats["cache_hits"]
            network_fetches += stats["network_fetches"]
            detail_errors += stats["detail_errors"]

            for item in new_items:
                if len(kept) >= TARGET_EVENTS:
                    break

                detail = details.get(item["url"])
                if not isinstance(detail, dict):
                    # On garde la fiche en secours, sans inventer commune/date.
                    commune_missing += 1
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
                    continue

                event = dict(detail)
                event["list_page"] = item.get("list_page")

                old_commune = v9.clean(event.get("commune", ""))
                commune = commune_from_detail(event)
                event["commune"] = commune
                if commune and not old_commune:
                    commune_filled += 1
                if not commune:
                    commune_missing += 1

                # Nyons est le SEUL territoire retiré, et seulement ici.
                if v9.norm(commune) == "nyons":
                    nyons_removed += 1
                    continue

                old_start = v9.clean(event.get("start_date", ""))
                old_end = v9.clean(event.get("end_date", ""))
                start, end = normalized_dates(event, today)
                if start:
                    event["start_date"] = start
                    event["end_date"] = end or start
                if (event.get("start_date", ""), event.get("end_date", "")) != (old_start, old_end):
                    dates_normalized += 1

                start_d = iso(event.get("start_date", ""))
                end_d = iso(event.get("end_date", ""))
                if end_d and end_d < today:
                    past_removed += 1
                    continue
                if start_d and start_d > horizon:
                    after_horizon_removed += 1
                    continue

                # Met aussi le cache à niveau, sans nouvelle requête réseau.
                cached = cache.get(item["url"])
                if isinstance(cached, dict) and isinstance(cached.get("data"), dict):
                    cached["data"]["commune"] = event.get("commune", "")
                    cached["data"]["start_date"] = event.get("start_date", "")
                    cached["data"]["end_date"] = event.get("end_date", "")

                kept.append(event)

        print(
            f"APRÈS LOT : gardés hors Nyons={len(kept)} | "
            f"Nyons retirés={nyons_removed} | communes complétées={commune_filled}"
        )
        next_page += LIST_BATCH

    cache_tmp = CACHE_FILE.with_suffix(CACHE_FILE.suffix + ".tmp")
    cache_tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cache_tmp.replace(CACHE_FILE)

    final_events = kept[:TARGET_EVENTS]
    if len(final_events) < 10:
        raise RuntimeError(
            f"Contrôle qualité: seulement {len(final_events)} événement(s) hors Nyons. "
            "agenda.json reste inchangé."
        )

    elapsed = round(time.monotonic() - started, 2)
    payload = {
        "source": v9.BASE,
        "source_mode": "all_links_clean_fields_then_remove_nyons",
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
            "communes_filled_from_address": commune_filled,
            "communes_still_missing": commune_missing,
            "dates_normalized": dates_normalized,
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
    print(f"Pages lues                : {pages_read}")
    print(f"Liens bruts vus           : {raw_links}")
    print(f"Candidats uniques         : {unique_candidates}")
    print(f"Nyons retirés ensuite     : {nyons_removed}")
    print(f"Communes complétées       : {commune_filled}")
    print(f"Communes encore manquantes: {commune_missing}")
    print(f"Dates normalisées         : {dates_normalized}")
    print(f"Cache détail              : {cache_hits}")
    print(f"Fiches réseau             : {network_fetches}")
    print(f"Événements publiés        : {len(final_events)}")
    print(f"Durée                     : {elapsed} s")
    print("OK: agenda.json prêt.")


if __name__ == "__main__":
    main()
