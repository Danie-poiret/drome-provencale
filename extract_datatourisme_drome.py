#!/usr/bin/env python3
"""Collecte tous les événements DATAtourisme du département de la Drôme (26).

Le script :
- utilise l'API REST officielle DATAtourisme ;
- filtre le département INSEE 26 ;
- parcourt toutes les pages ;
- conserve les événements encore en cours ou à venir ;
- normalise les données vers le format agenda.json déjà utilisé par le site.

La clé API doit être fournie dans DATATOURISME_API_KEY.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
ANECDOTES_FILE = ROOT / "village_anecdotes.json"
API_URL = "https://api.datatourisme.fr/v1/entertainmentAndEvent"
DEPARTMENT_INSEE = "26"
PAGE_SIZE = 250
MAX_PAGES = 100
TIMEOUT = 45
EVENT_LIMIT = int(os.getenv("DATATOURISME_EVENT_LIMIT", "0"))

# Communes retenues pour les 50 fiches supplémentaires autour de Nyons.
# L'ordre va globalement du Nyonsais vers le reste de la Drôme provençale.
# Nyons est volontairement absent et fait aussi l'objet d'un filtre explicite.
NEARBY_NYONS_COMMUNES = (
    "Buis-les-Baronnies", "Venterol", "Vinsobres", "Mirabel-aux-Baronnies",
    "Saint-Maurice-sur-Eygues", "Tulette", "Bouchet", "Suze-la-Rousse",
    "La Baume-de-Transit", "Rochegude", "Montbrun-les-Bains", "Sahune",
    "Rémuzat", "Verclause", "Les Pilles", "Saint-May", "Taulignan",
    "Grignan", "Colonzelle", "Réauville", "Montségur-sur-Lauzon",
    "Saint-Paul-Trois-Châteaux", "Valaurie", "Clansayes", "La Garde-Adhémar",
    "Le Poët-Laval", "Dieulefit", "Bourdeaux", "Comps", "Pont-de-Barret",
    "Rochebaudin", "La Bégude-de-Mazenc", "La Touche", "Allan",
    "Malataverne", "Donzère", "Pierrelatte",
)
PRESERVED_EVENT_TARGET = 50

# Les champs parents permettent de récupérer leurs sous-propriétés sans faire
# un appel de détail pour chaque événement.
FIELDS = ",".join(
    [
        "uuid",
        "uri",
        "label",
        "type",
        "isLocatedAt",
        "hasDescription",
        "hasContact",
        "hasBeenCreatedBy",
        "takesPlaceAt",
        "offers",
        "lastUpdate",
        "lastUpdateDatatourisme",
        "hasMainRepresentation",
    ]
)


def clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def commune_key(value: Any) -> str:
    """Clé tolérante aux accents, apostrophes et traits d'union."""
    import unicodedata

    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def select_events(events: list[dict], previous_urls: set[str], limit: int) -> list[dict]:
    """Conserve les 50 fiches actuelles puis ajoute les villages proches de Nyons.

    Les ajouts sont effectués en plusieurs tours (une fiche par commune à chaque
    tour) afin d'éviter qu'une grande ville occupe toute la sélection.
    """
    events = [e for e in events if commune_key(e.get("commune")) != "nyons"]
    if limit <= 0:
        return events

    by_url = {clean(e.get("url")): e for e in events}
    preserved = [by_url[url] for url in previous_urls if url in by_url]
    preserved.sort(
        key=lambda e: (
            max(parse_date(e["start_date"]) or date.today(), date.today()),
            clean(e.get("commune")).lower(),
            clean(e.get("title")).lower(),
        )
    )
    selected = preserved[: min(PRESERVED_EVENT_TARGET, limit)]
    used = {clean(e.get("url")) for e in selected}
    used_by_commune = Counter(clean(e.get("commune")) for e in selected)

    anecdote_catalog = {}
    if ANECDOTES_FILE.exists():
        try:
            anecdote_catalog = json.loads(ANECDOTES_FILE.read_text(encoding="utf-8"))
        except Exception:
            anecdote_catalog = {}
    anecdote_capacity = {
        clean(commune): len(items)
        for commune, items in anecdote_catalog.items()
        if isinstance(items, list)
    }

    rank = {commune_key(name): pos for pos, name in enumerate(NEARBY_NYONS_COMMUNES)}
    grouped: dict[str, list[dict]] = {}
    for event in events:
        key = commune_key(event.get("commune"))
        url = clean(event.get("url"))
        if url in used or key not in rank:
            continue
        grouped.setdefault(key, []).append(event)

    for candidates in grouped.values():
        candidates.sort(
            key=lambda e: (
                max(parse_date(e["start_date"]) or date.today(), date.today()),
                clean(e.get("title")).lower(),
            )
        )

    depth = 0
    while len(selected) < limit:
        added_this_round = 0
        for commune in NEARBY_NYONS_COMMUNES:
            candidates = grouped.get(commune_key(commune), [])
            if depth >= len(candidates):
                continue
            event = candidates[depth]
            event_commune = clean(event.get("commune"))
            if used_by_commune[event_commune] >= anecdote_capacity.get(event_commune, 0):
                continue
            selected.append(event)
            used.add(clean(event.get("url")))
            used_by_commune[event_commune] += 1
            added_this_round += 1
            if len(selected) >= limit:
                break
        if not added_this_round:
            break
        depth += 1

    # Sécurité : un complément ne peut utiliser qu'une commune disposant encore
    # d'une anecdote sourcée et non attribuée.
    if len(selected) < limit:
        for event in events:
            url = clean(event.get("url"))
            event_commune = clean(event.get("commune"))
            if url in used:
                continue
            if used_by_commune[event_commune] >= anecdote_capacity.get(event_commune, 0):
                continue
            selected.append(event)
            used.add(url)
            used_by_commune[event_commune] += 1
            if len(selected) >= limit:
                break

    if len(selected) < limit:
        raise RuntimeError(
            f"Seulement {len(selected)} fiches peuvent être produites avec une anecdote "
            f"réelle et unique ; objectif demandé : {limit}."
        )

    selected.sort(
        key=lambda e: (
            max(parse_date(e["start_date"]) or date.today(), date.today()),
            clean(e.get("commune")).lower(),
            clean(e.get("title")).lower(),
        )
    )
    return selected


def parse_date(value: Any) -> date | None:
    text = clean(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        return None


def lang_text(value: Any) -> str:
    """Extrait au mieux une chaîne française depuis une structure multilingue."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return clean(value)
    if isinstance(value, list):
        for item in value:
            text = lang_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        for key in ("fr", "@fr", "value"):
            if key in value:
                text = lang_text(value[key])
                if text:
                    return text
        for key in ("label", "name", "shortDescription", "longDescription", "description"):
            if key in value:
                text = lang_text(value[key])
                if text:
                    return text
        for item in value.values():
            text = lang_text(item)
            if text:
                return text
    return ""


def values_for_key(obj: Any, wanted: set[str]) -> list[str]:
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in wanted:
                text = lang_text(value)
                if text:
                    found.append(text)
            if isinstance(value, (dict, list)):
                found.extend(values_for_key(value, wanted))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(values_for_key(item, wanted))
    # dédoublonnage en conservant l'ordre
    out: list[str] = []
    seen = set()
    for text in found:
        if text not in seen:
            seen.add(text)
            out.append(text)
    return out


def first_for_key(obj: Any, *keys: str) -> str:
    vals = values_for_key(obj, set(keys))
    return vals[0] if vals else ""


def all_periods(obj: Any) -> list[dict]:
    periods: list[dict] = []
    if isinstance(obj, dict):
        if "startDate" in obj or "endDate" in obj:
            periods.append(obj)
        else:
            for value in obj.values():
                periods.extend(all_periods(value))
    elif isinstance(obj, list):
        for item in obj:
            periods.extend(all_periods(item))
    return periods


def choose_period(poi: dict, today: date) -> dict | None:
    candidates = []
    for period in all_periods(poi.get("takesPlaceAt")):
        start = parse_date(period.get("startDate"))
        end = parse_date(period.get("endDate")) or start
        if not start and end:
            start = end
        if not start:
            continue
        if not end:
            end = start
        if end < today:
            continue
        sort_date = today if start <= today <= end else start
        candidates.append((sort_date, start, end, period))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3]


def format_time(value: Any) -> str:
    text = clean(value)
    if not text:
        return ""
    if "T" in text:
        text = text.split("T", 1)[1]
    text = text[:5]
    return text.replace(":", "h") if re.fullmatch(r"\d{2}:\d{2}", text) else text


def build_opening(period: dict, start: date, end: date) -> str:
    bits = []
    if start == end:
        bits.append(start.strftime("%d/%m/%Y"))
    else:
        bits.append(f"Du {start.strftime('%d/%m/%Y')} au {end.strftime('%d/%m/%Y')}")

    start_time = format_time(period.get("startTime"))
    end_time = format_time(period.get("endTime"))
    if start_time and end_time:
        bits.append(f"{start_time}–{end_time}")
    elif start_time:
        bits.append(f"à partir de {start_time}")
    elif end_time:
        bits.append(f"jusqu'à {end_time}")

    details = lang_text(period.get("openingDetails"))
    if details:
        bits.append(details[:500])
    return " · ".join(bits)


def description_text(poi: dict) -> str:
    descriptions = poi.get("hasDescription")
    parts = values_for_key(descriptions, {"shortDescription", "longDescription", "description"})
    if not parts:
        text = lang_text(descriptions)
        return text[:6000]
    # courte puis longue, sans répétition
    joined = " ".join(dict.fromkeys(parts))
    return clean(joined)[:6000]


def address_data(poi: dict) -> tuple[str, str, str]:
    located = poi.get("isLocatedAt") or {}
    commune = first_for_key(located, "addressLocality")
    if not commune:
        city = first_for_key(located, "hasAddressCity")
        commune = city
    street = first_for_key(located, "streetAddress")
    postal = first_for_key(located, "postalCode")
    department = first_for_key(located, "insee")

    # Le premier insee trouvé peut parfois être celui de la commune. On vérifie
    # aussi explicitement si le code département 26 existe dans l'arborescence.
    insee_values = values_for_key(located, {"insee"})
    if DEPARTMENT_INSEE in insee_values:
        department = DEPARTMENT_INSEE

    address = clean(" ".join(part for part in (street, postal, commune) if part))
    return commune, address, department


def contact_text(poi: dict) -> tuple[str, str]:
    contacts = poi.get("hasContact") or {}
    phones = values_for_key(contacts, {"telephone", "phone"})
    emails = values_for_key(contacts, {"email"})
    urls = values_for_key(contacts, {"homepage", "website", "url"})
    pieces = []
    if phones:
        pieces.append("Téléphone : " + " / ".join(phones[:3]))
    if emails:
        pieces.append("Email : " + " / ".join(emails[:3]))
    source_url = next((u for u in urls if u.startswith("http")), "")
    return " · ".join(pieces)[:1200], source_url


def tariff_text(poi: dict) -> str:
    offers = poi.get("offers") or {}
    texts = values_for_key(offers, {"textPriceSpecification"})
    if texts:
        return " · ".join(texts)[:1200]
    mins = values_for_key(offers, {"minPrice"})
    maxs = values_for_key(offers, {"maxPrice"})
    if mins and maxs:
        return f"De {mins[0]} à {maxs[0]} €"
    if mins:
        return f"À partir de {mins[0]} €"
    return ""


def image_data(poi: dict) -> tuple[str, str, str]:
    """Retourne uniquement l'image principale réellement fournie par DATAtourisme.

    L'image reste hébergée par sa source : seul son URL, son crédit et ses droits
    sont conservés dans agenda.json. Aucun fichier image n'est téléchargé.
    """
    media = poi.get("hasMainRepresentation")
    if not media:
        return "", "", ""

    def dictionaries(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from dictionaries(child)
        elif isinstance(value, list):
            for child in value:
                yield from dictionaries(child)

    credit = first_for_key(media, "credits")
    rights = first_for_key(media, "isCoveredBy")

    for resource in dictionaries(media):
        locator = clean(resource.get("locator"))
        if not re.match(r"^https?://", locator, re.I):
            continue

        mime = first_for_key(resource, "hasMimeType").lower()
        path = locator.split("?", 1)[0].lower()
        looks_like_image = bool(
            mime.startswith("image/")
            or re.search(r"\.(?:avif|gif|jpe?g|png|webp)$", path)
            or (not mime and not path.endswith(".pdf"))
        )
        if looks_like_image:
            return locator, credit, rights

    return "", "", ""


def normalize_poi(poi: dict, today: date) -> dict | None:
    period = choose_period(poi, today)
    if not period:
        return None

    start = parse_date(period.get("startDate"))
    end = parse_date(period.get("endDate")) or start
    if not start:
        return None
    if not end:
        end = start

    title = lang_text(poi.get("label"))
    if not title:
        return None

    commune, address, department = address_data(poi)
    # Le filtre API fait foi. Cette vérification supplémentaire ne rejette que
    # les objets explicitement rattachés à un autre département.
    if department and department != DEPARTMENT_INSEE and len(department) == 2:
        return None

    contact, website = contact_text(poi)
    uri = clean(poi.get("uri"))
    uuid = clean(poi.get("uuid"))
    stable_url = uri or (f"https://data.datatourisme.fr/{uuid}" if uuid else "")
    if not stable_url:
        return None

    producer = lang_text(poi.get("hasBeenCreatedBy")) or "DATAtourisme"
    last_update = clean(poi.get("lastUpdate") or poi.get("lastUpdateDatatourisme"))
    image_url, image_credit, image_rights = image_data(poi)

    return {
        "title": title,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "opening": build_opening(period, start, end),
        "commune": commune,
        "address": address,
        "description": description_text(poi),
        "tariffs": tariff_text(poi),
        "contact": contact,
        "url": stable_url,
        "source_url": website or stable_url,
        "datatourisme_uri": uri,
        "datatourisme_uuid": uuid,
        "data_provider": producer,
        "last_update": last_update,
        "image_url": image_url,
        "image_credit": image_credit,
        "image_rights": image_rights,
        "source_name": "DATAtourisme",
        "source_license": "Licence Ouverte 2.0",
    }


def fetch_all(api_key: str) -> list[dict]:
    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "agenda-drome-vivreanyons/1.0",
    }
    filters = (
        "isLocatedAt.address.hasAddressCity.isPartOfDepartment.insee[in]="
        + DEPARTMENT_INSEE
    )
    session = requests.Session()
    objects: list[dict] = []

    for page in range(1, MAX_PAGES + 1):
        params = {
            "filters": filters,
            "fields": FIELDS,
            "lang": "fr",
            "page": page,
            "page_size": PAGE_SIZE,
        }
        response = session.get(API_URL, headers=headers, params=params, timeout=TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("objects", []) if isinstance(payload, dict) else []
        meta = payload.get("meta", {}) if isinstance(payload, dict) else {}

        print(
            f"DATAtourisme page {page}: {len(batch)} objet(s) | "
            f"total annoncé: {meta.get('total', '?')}"
        )
        objects.extend(batch)

        if not batch or not meta.get("next"):
            break
    else:
        raise RuntimeError(f"Sécurité atteinte: plus de {MAX_PAGES * PAGE_SIZE} objets")

    return objects


def main() -> None:
    api_key = os.getenv("DATATOURISME_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Secret DATATOURISME_API_KEY absent")

    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    previous_urls = {
        clean(event.get("url"))
        for event in previous.get("events", [])
        if isinstance(event, dict) and clean(event.get("url"))
    }

    today = date.today()
    raw = fetch_all(api_key)
    events = []
    seen = set()

    for poi in raw:
        event = normalize_poi(poi, today)
        if not event:
            continue
        key = event["url"]
        if key in seen:
            continue
        seen.add(key)
        events.append(event)

    events.sort(
        key=lambda e: (
            max(parse_date(e["start_date"]) or today, today),
            e.get("commune", "").lower(),
            e.get("title", "").lower(),
        )
    )

    if EVENT_LIMIT > 0:
        events = select_events(events, previous_urls, EVENT_LIMIT)
        print(
            f"Sélection limitée à {len(events)} événements : "
            f"fiches existantes conservées, puis villages autour de Nyons (Nyons exclu)."
        )

    payload = {
        "source": API_URL,
        "source_name": "DATAtourisme",
        "license": "Licence Ouverte 2.0",
        "department": "Drôme",
        "department_insee": DEPARTMENT_INSEE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "raw_count": len(raw),
        "count": len(events),
        "events": events,
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)

    print("=== DATATOURISME DRÔME ===")
    print(f"Objets API reçus          : {len(raw)}")
    print(f"Événements en cours/à venir: {len(events)}")
    print(f"Fichier                   : {OUT.name}")
    if events:
        print("Premiers événements:")
        for event in events[:10]:
            print(f" - {event['start_date']} | {event['commune']} | {event['title']}")


if __name__ == "__main__":
    main()
