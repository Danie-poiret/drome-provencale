#!/usr/bin/env python3
"""
V2 propre des Baronnies hors Nyons.

On réutilise le moteur V1 mais on corrige le point qui contaminait les communes :
- la commune est lue UNIQUEMENT dans le lien propre à l'événement sur la liste ;
- elle doit être placée en fin de libellé du lien (comme sur La Drôme Tourisme) ;
- on ne cherche plus une commune dans un gros bloc HTML ni dans le menu de la fiche ;
- les anciennes données de cache erronées sont ignorées grâce au préfixe v2:.
"""

from __future__ import annotations

from datetime import datetime, timezone

from bs4 import BeautifulSoup

import extract_baronnies_drome as base

CACHE_PREFIX = "v2:"


def commune_at_end(text: str) -> str:
    """Retourne une commune CCBDP seulement si elle termine le libellé du lien."""
    n = base.norm(text)
    for key in base.COMMUNE_KEYS:
        if n == key or n.endswith(" " + key):
            return base.COMMUNE_BY_NORM[key]
    return ""


def extract_list_items_clean(html_text: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html_text, "html.parser")
    items = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = base.normalize_url(base.urljoin(page_url, a["href"]))
        if not base.is_detail_url(href) or href in seen:
            continue

        # IMPORTANT : uniquement le texte DU LIEN événement.
        # Sur La Drôme Tourisme, le libellé se termine par la commune réelle.
        anchor_text = base.clean(a.get_text(" ", strip=True))
        commune = commune_at_end(anchor_text)
        if not commune:
            continue

        card = base.nearest_card(a)
        title = base.title_from_card(card, a)
        if not title:
            continue

        # Le bloc est encore utile pour la date affichée, mais JAMAIS pour la commune.
        card_text = base.clean(card.get_text(" ", strip=True)) if card else anchor_text
        start, end = base.parse_french_dates(card_text)

        items.append({
            "title": title,
            "url": href,
            "commune": commune,
            "start_date": start,
            "end_date": end,
            "card_text": card_text[:1200],
            "anchor_text": anchor_text[:1000],
            "excluded_nyons": base.norm(commune) == "nyons",
        })
        seen.add(href)

    return items


def fetch_detail_clean(session, item: dict, cache: dict) -> dict:
    """Détail sans jamais déduire la commune depuis le menu ou le texte global."""
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

    if event_obj:
        title = base.clean(event_obj.get("name")) or title
        start = base.parse_iso_date(event_obj.get("startDate")) or start
        end = base.parse_iso_date(event_obj.get("endDate")) or end or start
        location, address, locality = base.location_from_jsonld(event_obj)
        detail_locality = base.clean(locality)

        # Si la fiche fournit explicitement une localité structurée, elle prime.
        # Si cette localité n'appartient pas aux Baronnies, la fiche sera rejetée.
        if detail_locality:
            confirmed = base.find_commune(detail_locality)
            commune = confirmed if confirmed else ""
    else:
        h1 = soup.find("h1")
        if h1:
            title = base.clean(h1.get_text(" ", strip=True)) or title
        # PAS de recherche de commune dans page_text : le menu contient des noms
        # comme Montbrun-les-Bains et provoquait les faux positifs de la V1.
        # PAS non plus de réécriture des dates à partir du texte global.

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
        "geo_source": "event_link_city" if not detail_locality else "jsonld_addressLocality",
    }
    cache[cache_key] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    return data


def main() -> None:
    # Remplace uniquement les deux points fragiles de la V1.
    base.extract_list_items = extract_list_items_clean
    base.fetch_detail = fetch_detail_clean
    print("V2 GEO STRICTE : commune lue dans le lien événement, jamais dans le menu.")
    base.main()


if __name__ == "__main__":
    main()
