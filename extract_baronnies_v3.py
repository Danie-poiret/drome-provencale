#!/usr/bin/env python3
"""
V3 Baronnies hors Nyons.

Objectifs :
- conserver le filtrage géographique strict de la V2 ;
- récupérer les dates depuis le groupe de date de la liste ;
- compléter/valider les dates via la section "Ouverture" de la fiche ;
- tenter de détecter automatiquement le filtre "Communes" du site La Drôme
  afin d'interroger directement les communes des Baronnies et éviter de balayer
  inutilement tout le département ;
- exclure Nyons ;
- supprimer les doublons ;
- aucun appel OpenAI.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

import extract_baronnies_drome as base

CACHE_PREFIX = "v3:"
MAX_FILTERED_PAGES = 80
DATE_GROUP_RE = re.compile(
    rf"^(?:lun\.?|mar\.?|mer\.?|jeu\.?|ven\.?|sam\.?|dim\.?|"
    rf"lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\s+"
    rf"(\d{{1,2}})\s+({base.MONTH_RE})(?:\s+(20\d{{2}}))?$",
    re.I,
)
SECTION_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def commune_at_end(text: str) -> str:
    n = base.norm(text)
    for key in base.COMMUNE_KEYS:
        if n == key or n.endswith(" " + key):
            return base.COMMUNE_BY_NORM[key]
    return ""


def exact_commune_label(text: str) -> str:
    """Reconnaît une commune dans un libellé de filtre, même avec un compteur."""
    n = base.norm(text)
    for key in base.COMMUNE_KEYS:
        if n == key:
            return base.COMMUNE_BY_NORM[key]
        if n.startswith(key + " "):
            rest = n[len(key):].strip()
            if not rest or re.fullmatch(r"\d+", rest) or re.fullmatch(r"\( ?\d+ ?\)", rest):
                return base.COMMUNE_BY_NORM[key]
    return ""


def infer_group_date(day: int, month: int, explicit_year: int | None = None) -> str:
    if explicit_year:
        try:
            return date(explicit_year, month, day).isoformat()
        except ValueError:
            return ""

    today = date.today()
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return ""

    # L'agenda est tourné vers les événements courants/à venir. En fin d'année,
    # janvier/février appartiennent donc généralement à l'année suivante.
    if candidate < today - timedelta(days=7):
        try:
            candidate = date(today.year + 1, month, day)
        except ValueError:
            return ""
    return candidate.isoformat()


def parse_group_date(text: str) -> str:
    text = base.clean(text)
    if len(text) > 60:
        return ""
    m = DATE_GROUP_RE.match(text)
    if not m:
        return ""
    day = int(m.group(1))
    month = base.MONTHS[m.group(2).lower()]
    year = int(m.group(3)) if m.group(3) else None
    return infer_group_date(day, month, year)


def date_from_previous_group(anchor) -> str:
    """Cherche uniquement un petit titre de date situé avant la fiche."""
    for node in anchor.find_all_previous(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "span"],
        limit=100,
    ):
        text = base.clean(node.get_text(" ", strip=True))
        d = parse_group_date(text)
        if d:
            return d
    return ""


def section_text(soup: BeautifulSoup, wanted: str, max_chars: int = 1800) -> str:
    """Lit une section précise de fiche sans aspirer menus/pied de page."""
    target = base.norm(wanted)
    heading = None
    for h in soup.find_all(list(SECTION_HEADINGS)):
        if base.norm(h.get_text(" ", strip=True)) == target:
            heading = h
            break
    if heading is None:
        return ""

    pieces = []
    total = 0
    seen = set()
    for node in heading.find_all_next():
        if node is heading:
            continue
        if node.name in SECTION_HEADINGS:
            break
        if node.name in {"script", "style", "svg", "noscript"}:
            continue
        # Prendre surtout les feuilles / petits blocs évite les répétitions massives.
        text = base.clean(node.get_text(" ", strip=True))
        if not text or len(text) > 900:
            continue
        key = base.norm(text)
        if key in seen:
            continue
        seen.add(key)
        pieces.append(text)
        total += len(text) + 1
        if total >= max_chars:
            break
    return base.clean(" ".join(pieces))[:max_chars]


def find_filter_input_for_label(form, label):
    inp = label.find("input")
    if inp is not None:
        return inp
    target_id = label.get("for")
    if target_id:
        return form.find("input", id=target_id)
    parent = label.parent
    if parent is not None:
        return parent.find("input")
    return None


def discover_commune_filter(soup: BeautifulSoup, page_url: str):
    """
    Cherche un formulaire contenant les communes et prépare une requête GET.
    Retourne (url, params, nb_communes) ou None.
    """
    best = None

    for form in soup.find_all("form"):
        matched = []
        for label in form.find_all("label"):
            commune = exact_commune_label(label.get_text(" ", strip=True))
            if not commune:
                continue
            inp = find_filter_input_for_label(form, label)
            if inp is None:
                continue
            name = base.clean(inp.get("name"))
            value = base.clean(inp.get("value"))
            if not name or not value:
                continue
            matched.append((commune, name, value))

        if not matched:
            # Certains thèmes n'utilisent pas de <label> explicite.
            for inp in form.find_all("input"):
                name = base.clean(inp.get("name"))
                value = base.clean(inp.get("value"))
                if not name or not value:
                    continue
                parent_text = base.clean(inp.parent.get_text(" ", strip=True)) if inp.parent else ""
                commune = exact_commune_label(parent_text)
                if commune:
                    matched.append((commune, name, value))

        unique_communes = {c for c, _, _ in matched}
        if len(unique_communes) < 3:
            continue

        method = base.clean(form.get("method", "get")).lower() or "get"
        action = urljoin(page_url, form.get("action") or page_url)
        candidate = (len(unique_communes), method, action, matched, form)
        if best is None or candidate[0] > best[0]:
            best = candidate

    if best is None:
        return None

    count, method, action, matched, form = best
    names = sorted({name for _, name, _ in matched})
    print(
        f"FILTRE COMMUNES DETECTE: {count} commune(s) reconnue(s) | "
        f"method={method} | champ(s)={names}"
    )

    if method != "get":
        print("FILTRE AUTO: formulaire non-GET, repli sur l'agenda général.")
        return None

    params = []
    # Conserver les champs cachés utiles du formulaire.
    for inp in form.find_all("input", attrs={"type": "hidden"}):
        name = base.clean(inp.get("name"))
        value = base.clean(inp.get("value"))
        if name and value:
            params.append((name, value))

    selected = 0
    seen_pairs = set()
    for commune, name, value in matched:
        if base.norm(commune) == "nyons":
            continue
        pair = (name, value)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        params.append(pair)
        selected += 1

    if selected < 3:
        return None

    print(f"FILTRE AUTO: {selected} commune(s) Baronnies sélectionnée(s), Nyons exclu.")
    return action, params, selected


def page_url_with_number(first_url: str, page_no: int) -> str:
    if page_no <= 1:
        return first_url
    parts = urlsplit(first_url)
    path = parts.path
    path = re.sub(r"/page/\d+/?$", "/", path)
    if not path.endswith("/"):
        path += "/"
    path = path + f"page/{page_no}/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def extract_list_items(html_text: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = base.normalize_url(urljoin(page_url, a["href"]))
        if not base.is_detail_url(href) or href in seen:
            continue

        anchor_text = base.clean(a.get_text(" ", strip=True))
        commune = commune_at_end(anchor_text)
        if not commune:
            continue

        card = base.nearest_card(a)
        title = base.title_from_card(card, a)
        if not title:
            continue

        start = date_from_previous_group(a)
        if not start and card is not None:
            start, _ = base.parse_french_dates(base.clean(card.get_text(" ", strip=True)))

        items.append({
            "title": title,
            "url": href,
            "commune": commune,
            "start_date": start,
            "end_date": start,
            "anchor_text": anchor_text[:1000],
            "excluded_nyons": base.norm(commune) == "nyons",
        })
        seen.add(href)

    return items


def fetch_detail(session: requests.Session, item: dict, cache: dict) -> dict:
    url = item["url"]
    cache_key = CACHE_PREFIX + url
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and base.cache_fresh(cached):
        data = cached.get("data")
        if isinstance(data, dict):
            return data

    r = session.get(url, headers=base.HEADERS, timeout=base.TIMEOUT, allow_redirects=True)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    event_obj = base.best_event_jsonld(soup, item["title"])

    title = item["title"]
    start = item.get("start_date", "")
    end = item.get("end_date", "") or start
    commune = item["commune"]
    location = ""
    address = ""
    detail_locality = ""
    geo_source = "event_link_city"

    if event_obj:
        title = base.clean(event_obj.get("name")) or title
        start = base.parse_iso_date(event_obj.get("startDate")) or start
        end = base.parse_iso_date(event_obj.get("endDate")) or end or start
        location, address, locality = base.location_from_jsonld(event_obj)
        detail_locality = base.clean(locality)
        if detail_locality:
            confirmed = base.find_commune(detail_locality)
            if confirmed:
                commune = confirmed
                geo_source = "jsonld_addressLocality"
            else:
                # Une localité structurée explicite hors Baronnies doit faire rejeter la fiche.
                commune = ""
                geo_source = "jsonld_outside_scope"
    else:
        h1 = soup.find("h1")
        if h1:
            title = base.clean(h1.get_text(" ", strip=True)) or title

        opening = section_text(soup, "Ouverture")
        if opening:
            pstart, pend = base.parse_french_dates(opening)
            start = pstart or start
            end = pend or end or start

        access = section_text(soup, "Accès")
        contact = section_text(soup, "Contact")
        precise_geo = base.clean(access + " " + contact)
        confirmed = base.find_commune(precise_geo)
        if confirmed:
            commune = confirmed
            detail_locality = confirmed
            geo_source = "detail_access_contact"

    data = {
        "title": title,
        "start_date": start,
        "end_date": end or start,
        "commune": commune,
        "location": location,
        "address": address,
        "detail_locality": detail_locality,
        "url": base.canonical_url(soup, r.url),
        "source_url": url,
        "jsonld_event": bool(event_obj),
        "geo_source": geo_source,
    }
    cache[cache_key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    return data


def fingerprint(event: dict) -> str:
    raw = "|".join([
        base.norm(event.get("title")),
        base.clean(event.get("start_date")),
        base.clean(event.get("end_date")),
        base.norm(event.get("commune")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def main() -> None:
    session = requests.Session()
    cache = base.load_json(base.CACHE_FILE)

    print("V3: filtre communes auto + dates précises, hors Nyons, sans GPT.")
    r0 = session.get(base.SOURCE, headers=base.HEADERS, timeout=base.TIMEOUT, allow_redirects=True)
    print(f"SOURCE INITIALE: HTTP {r0.status_code} | {len(r0.text)} octets | {r0.url}")
    r0.raise_for_status()
    initial_soup = BeautifulSoup(r0.text, "html.parser")

    filter_info = discover_commune_filter(initial_soup, r0.url)
    filter_used = False
    filter_communes = 0

    if filter_info:
        action, params, filter_communes = filter_info
        rf = session.get(action, params=params, headers=base.HEADERS, timeout=base.TIMEOUT, allow_redirects=True)
        print(f"SOURCE FILTREE: HTTP {rf.status_code} | {len(rf.text)} octets | {rf.url}")
        rf.raise_for_status()
        first_url = rf.url
        first_html = rf.text
        filter_used = True
    else:
        first_url = r0.url
        first_html = r0.text

    first_soup = BeautifulSoup(first_html, "html.parser")
    detected_pages = base.detect_total_pages(first_soup) or 1
    max_pages = min(detected_pages, MAX_FILTERED_PAGES if filter_used else base.MAX_LIST_PAGES)
    print(f"PAGES DETECTEES: {detected_pages} | pages parcourues max: {max_pages}")

    collected = {}
    nyons_seen = 0
    pages_scanned = 0

    for page_no in range(1, max_pages + 1):
        if page_no == 1:
            html_text = first_html
            page_url = first_url
            status = 200
        else:
            page_url = page_url_with_number(first_url, page_no)
            rr = session.get(page_url, headers=base.HEADERS, timeout=base.TIMEOUT, allow_redirects=True)
            status = rr.status_code
            rr.raise_for_status()
            html_text = rr.text
            page_url = rr.url

        pages_scanned += 1
        items = extract_list_items(html_text, page_url)
        page_new = 0
        page_nyons = 0
        for item in items:
            if item.get("excluded_nyons"):
                nyons_seen += 1
                page_nyons += 1
                continue
            if item["url"] not in collected:
                collected[item["url"]] = item
                page_new += 1

        print(
            f"LISTE {page_no:02d}: HTTP {status} | "
            f"Baronnies +{page_new} | Nyons -{page_nyons} | total={len(collected)}"
        )
        time.sleep(base.REQUEST_DELAY)

    if len(collected) < base.MIN_VALID_EVENTS:
        raise RuntimeError(
            f"Extraction liste suspecte: seulement {len(collected)} événement(s) hors Nyons."
        )

    print(f"LIENS BARRONNIES HORS NYONS: {len(collected)}")

    detailed = []
    detail_errors = 0
    excluded_detail = 0

    items = list(collected.values())
    for idx, item in enumerate(items, 1):
        try:
            event = fetch_detail(session, item, cache)
        except Exception as exc:
            detail_errors += 1
            event = {
                "title": item["title"],
                "start_date": item.get("start_date", ""),
                "end_date": item.get("end_date", ""),
                "commune": item["commune"],
                "location": "",
                "address": "",
                "detail_locality": "",
                "url": item["url"],
                "source_url": item["url"],
                "jsonld_event": False,
                "geo_source": "event_link_city_detail_error",
                "detail_error": base.clean(exc),
            }
            print(f"DETAIL {idx:03d}/{len(items)} AVERTISSEMENT — {item['title']}: {exc}")

        if not event.get("commune") or base.norm(event.get("commune")) == "nyons":
            excluded_detail += 1
            continue
        if not base.find_commune(event.get("commune", "")):
            excluded_detail += 1
            continue

        detailed.append(event)
        print(
            f"DETAIL {idx:03d}/{len(items)} OK — "
            f"{event.get('start_date') or '?'} -> {event.get('end_date') or '?'} | "
            f"{event.get('commune')} | {event.get('title')}"
        )
        time.sleep(base.REQUEST_DELAY)

    base.save_json(base.CACHE_FILE, cache)

    unique = []
    seen_urls = set()
    seen_fp = set()
    duplicates = 0
    missing_date = 0

    for event in sorted(
        detailed,
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            base.norm(e.get("commune")),
            base.norm(e.get("title")),
        ),
    ):
        url_key = base.normalize_url(event.get("url", ""))
        fp = fingerprint(event)
        if (url_key and url_key in seen_urls) or fp in seen_fp:
            duplicates += 1
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_fp.add(fp)
        event["dedupe_id"] = fp[:16]
        if not event.get("start_date"):
            missing_date += 1
        unique.append(event)

    if len(unique) < base.MIN_VALID_EVENTS:
        raise RuntimeError(
            f"Extraction finale suspecte: seulement {len(unique)} événement(s)."
        )

    payload = {
        "source": base.SOURCE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "67 communes CCBDP, hors Nyons",
        "version": "v3",
        "count": len(unique),
        "diagnostics": {
            "commune_filter_auto_used": filter_used,
            "commune_filter_values_selected": filter_communes,
            "list_pages_scanned": pages_scanned,
            "nyons_excluded_on_list": nyons_seen,
            "excluded_on_detail": excluded_detail,
            "detail_errors": detail_errors,
            "duplicates_removed": duplicates,
            "missing_date": missing_date,
        },
        "events": unique,
    }
    base.save_json(base.OUT, payload)

    print("=== BILAN V3 ===")
    print(f"Filtre communes auto     : {'OUI' if filter_used else 'NON'}")
    print(f"Valeurs communes filtre  : {filter_communes}")
    print(f"Pages parcourues         : {pages_scanned}")
    print(f"Nyons exclus liste       : {nyons_seen}")
    print(f"Rejets après détail      : {excluded_detail}")
    print(f"Erreurs fiches           : {detail_errors}")
    print(f"Doublons supprimés       : {duplicates}")
    print(f"Dates manquantes         : {missing_date}")
    print(f"Événements conservés     : {len(unique)}")
    print("OK: agenda.json écrit.")


if __name__ == "__main__":
    main()
