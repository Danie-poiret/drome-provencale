#!/usr/bin/env python3
"""Contrôles bloquants avant publication de l'agenda Drôme."""
from __future__ import annotations

import html
import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path


ROOT = Path(__file__).resolve().parent
AGENDA = ROOT / "agenda.json"
EVENTS_DIR = ROOT / "evenements"
SITEMAP = ROOT / "sitemap.xml"


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value) -> str:
    text = unicodedata.normalize("NFKD", clean(value).lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "", text)


def slugify(text: str, max_len: int = 90) -> str:
    raw = unicodedata.normalize("NFKD", clean(text))
    raw = "".join(char for char in raw if not unicodedata.combining(char))
    raw = raw.lower().replace("’", "-").replace("'", "-")
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return raw[:max_len].rstrip("-") or "evenement"


def event_slugs(events: list[dict]) -> dict[str, str]:
    result = {}
    used = set()
    for event in events:
        key = clean(event.get("url"))
        base = f"{slugify(event.get('title', 'evenement'))}-{clean(event.get('start_date')) or 'date'}"
        slug = base
        if slug in used:
            slug = f"{base}-{slugify(event.get('commune', 'lieu'), 35)}"
        if slug in used:
            identity = clean(event.get("datatourisme_uuid")) or key
            suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
            slug = f"{base}-{slugify(event.get('commune', 'lieu'), 24)}-{suffix}"
        if slug in used:
            raise RuntimeError(f"Collision de slug impossible à résoudre : {slug}")
        used.add(slug)
        result[key] = slug
    return result


def event_signature(event: dict) -> tuple[str, str, str, str, str]:
    return (
        norm(event.get("title")),
        clean(event.get("start_date")),
        clean(event.get("end_date")),
        norm(event.get("commune")),
        norm(event.get("opening")),
    )


def fail(message: str) -> None:
    raise SystemExit(message)


def main() -> None:
    payload = json.loads(AGENDA.read_text(encoding="utf-8"))
    events = payload.get("events", [])
    expected = int(os.getenv("EXPECTED_EVENT_COUNT", str(len(events))))
    if len(events) != expected or payload.get("count") != expected:
        fail(f"Nombre d'événements invalide : agenda={len(events)}, déclaré={payload.get('count')}, attendu={expected}")

    urls = [clean(event.get("url")) for event in events]
    uuids = [clean(event.get("datatourisme_uuid")) for event in events if clean(event.get("datatourisme_uuid"))]
    signatures = [event_signature(event) for event in events]
    if not all(urls) or len(urls) != len(set(urls)):
        fail("URL DATAtourisme absente ou dupliquée")
    if len(uuids) != len(set(uuids)):
        fail("UUID DATAtourisme dupliqué")
    if len(signatures) != len(set(signatures)):
        fail("Même titre/date/commune/horaire détecté plusieurs fois")
    if any(norm(event.get("commune")) == "nyons" for event in events):
        fail("Nyons a été détecté dans la sélection")
    if any(not clean(event.get("commune")) for event in events):
        fail("Une fiche ne possède pas de commune")

    slugs = event_slugs(events)
    page_dirs = sorted(path for path in EVENTS_DIR.iterdir() if path.is_dir())
    if len(page_dirs) != expected:
        fail(f"Dossiers de fiches invalides : {len(page_dirs)} au lieu de {expected}")

    titles = []
    canonicals = []
    facts = []
    for event in events:
        slug = slugs[clean(event.get("url"))]
        path = EVENTS_DIR / slug / "index.html"
        if not path.exists():
            fail(f"Fiche absente : {path}")
        source = path.read_text(encoding="utf-8")

        title_match = re.search(r"<title>(.*?)</title>", source, re.S)
        canonical_match = re.search(r'<link rel="canonical" href="([^"]+)">', source)
        if not title_match or not canonical_match:
            fail(f"Title ou canonical absent : {path}")
        title = html.unescape(clean(title_match.group(1))).casefold()
        canonical = html.unescape(clean(canonical_match.group(1)))
        if len(title) > 70:
            fail(f"Title trop long ({len(title)}) : {path}")
        titles.append(title)
        canonicals.append(canonical)

        section = re.search(
            r'<section class="section anecdote"><h2>💡 Le savais-tu \?</h2>'
            r'<p>(.*?)</p>(.*?)</section>',
            source,
            re.S,
        )
        if not section:
            fail(f"Le savais-tu thématique absent : {path}")
        if "href=" in section.group(0):
            fail(f"Lien interdit dans Le savais-tu : {path}")
        fact = html.unescape(re.sub(r"<[^>]+>", "", section.group(1)))
        fact = clean(fact).casefold()
        if "en cours de contrôle" in fact:
            fail(f"Fait thématique provisoire détecté : {path}")
        if not 18 <= len(fact.split()) <= 75:
            fail(f"Longueur du fait thématique invalide : {path}")
        facts.append(fact)

        if "Ajouter à mon calendrier" not in source or "Voir l’itinéraire" not in source:
            fail(f"Bouton calendrier ou itinéraire absent : {path}")
        if "DATATOURISME_ATTRIBUTION_START" not in source:
            fail(f"Attribution DATAtourisme absente : {path}")

        scripts = re.findall(
            r'<script type="application/ld\+json">(.*?)</script>', source, re.S
        )
        event_ld = None
        for raw in scripts:
            data = json.loads(html.unescape(raw))
            if data.get("@type") == "Event":
                event_ld = data
                break
        if not event_ld:
            fail(f"Schema.org Event absent : {path}")
        location = event_ld.get("location") or {}
        address = location.get("address") or {}
        if (
            location.get("@type") != "Place"
            or not clean(location.get("name"))
            or address.get("@type") != "PostalAddress"
            or not clean(address.get("addressLocality"))
            or address.get("addressCountry") != "FR"
        ):
            fail(f"Schema.org location incomplet : {path}")

    if len(titles) != len(set(titles)):
        fail(f"Titles dupliqués : {len(set(titles))}/{expected}")
    if len(canonicals) != len(set(canonicals)):
        fail("Canonical dupliqué")
    if len(facts) != len(set(facts)):
        fail(f"Le savais-tu dupliqués : {len(set(facts))}/{expected}")

    index_source = (EVENTS_DIR / "index.html").read_text(encoding="utf-8")
    if f"<h1>{expected} événements autour de Nyons</h1>" not in index_source:
        fail("Compteur de la page événements incorrect")
    sitemap_source = SITEMAP.read_text(encoding="utf-8")
    sitemap_urls = re.findall(r"<loc>(.*?)</loc>", sitemap_source)
    if len(sitemap_urls) != expected + 2:
        fail(f"Sitemap incomplet : {len(sitemap_urls)} URL au lieu de {expected + 2}")

    communes = {clean(event.get("commune")) for event in events}
    known_distances = [
        event.get("distance_from_nyons_km")
        for event in events
        if isinstance(event.get("distance_from_nyons_km"), (int, float))
    ]
    print(f"VALIDATION OK : {expected} fiches, {len(communes)} villages, Nyons exclu")
    print(f"{expected} titles uniques, {expected} faits thématiques uniques, aucun lien")
    print(f"{expected} Schema.org Event avec location, calendrier et itinéraire")
    print(f"Distances DATAtourisme disponibles : {len(known_distances)}/{expected}")


if __name__ == "__main__":
    main()
