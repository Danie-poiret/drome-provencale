#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path

import requests

from extract_datatourisme_drome import clean, image_data

ROOT = Path(__file__).resolve().parent
AGENDA = ROOT / "agenda.json"
DETAIL_URL = "https://api.datatourisme.fr/v1/catalog/{uuid}"
TIMEOUT = 45
TEST_LIMIT = 5


def main() -> None:
    api_key = os.getenv("DATATOURISME_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Secret DATATOURISME_API_KEY absent")

    payload = json.loads(AGENDA.read_text(encoding="utf-8"))
    events = [e for e in payload.get("events", []) if clean(e.get("datatourisme_uuid"))]

    # Priorité aux fiches où une photo serait particulièrement plausible,
    # puis complétion avec les premières fiches de l'agenda.
    preferred_terms = ("exposition", "photo", "forêt", "vehicule", "véhicule")
    chosen = []
    used = set()
    for event in events:
        haystack = f"{event.get('title', '')} {event.get('description', '')}".lower()
        if any(term in haystack for term in preferred_terms):
            uuid = clean(event.get("datatourisme_uuid"))
            if uuid not in used:
                chosen.append(event)
                used.add(uuid)
        if len(chosen) >= TEST_LIMIT:
            break
    for event in events:
        if len(chosen) >= TEST_LIMIT:
            break
        uuid = clean(event.get("datatourisme_uuid"))
        if uuid and uuid not in used:
            chosen.append(event)
            used.add(uuid)

    headers = {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "User-Agent": "agenda-drome-vivreanyons-detail-audit/1.0",
    }

    found = 0
    print("=== AUDIT DETAIL PHOTOS DATATOURISME ===")
    for event in chosen:
        uuid = clean(event.get("datatourisme_uuid"))
        response = requests.get(
            DETAIL_URL.format(uuid=uuid),
            headers=headers,
            params={"lang": "fr"},
            timeout=TIMEOUT,
        )
        print(f"HTTP {response.status_code} | {event.get('title')} | {uuid}")
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("object"), dict):
            poi = data["object"]
        else:
            poi = data
        if not isinstance(poi, dict):
            print("  Réponse détail non exploitable")
            continue

        main_rep = bool(poi.get("hasMainRepresentation"))
        any_rep = bool(poi.get("hasRepresentation"))
        image_url, credit, rights = image_data(poi)
        if image_url:
            found += 1
        print(f"  hasMainRepresentation : {main_rep}")
        print(f"  hasRepresentation     : {any_rep}")
        print(f"  image détectée         : {'OUI' if image_url else 'NON'}")
        if image_url:
            print(f"  image_url              : {image_url}")
            print(f"  crédit                 : {credit or '-'}")
            print(f"  droits                 : {rights or '-'}")

    print(f"DETAIL_IMAGES_FOUND={found}/{len(chosen)}")


if __name__ == "__main__":
    main()
