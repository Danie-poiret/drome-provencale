#!/usr/bin/env python3
"""Habillage du test DATAtourisme autour de Nyons après génération des fiches.

- adapte la page /evenements/ aux villages autour de Nyons ;
- ajoute une attribution discrète DATAtourisme/producteur/date de mise à jour ;
- ne touche pas au contenu éditorial lui-même.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AGENDA = ROOT / "agenda.json"
EVENTS_DIR = ROOT / "evenements"


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def slugify(text: str, max_len: int = 90) -> str:
    raw = unicodedata.normalize("NFKD", clean(text))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.lower().replace("’", "-").replace("'", "-")
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return raw[:max_len].rstrip("-") or "evenement"


def event_slug(event: dict) -> str:
    return f"{slugify(event.get('title', 'evenement'))}-{clean(event.get('start_date')) or 'date'}"


def format_update(value: str) -> str:
    text = clean(value)
    if not text:
        return ""
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return d.strftime("%d/%m/%Y")
    except Exception:
        return text[:10]


def attribution_block(event: dict) -> str:
    producer = clean(event.get("data_provider")) or "DATAtourisme"
    updated = format_update(event.get("last_update", ""))
    when = f" · mise à jour le {html.escape(updated)}" if updated else ""
    return (
        '\n<!-- DATATOURISME_ATTRIBUTION_START -->\n'
        '<div style="font-size:12px;color:#686b63;margin-top:22px;padding:12px 14px;'
        'border:1px solid #e4dccd;border-radius:12px;background:rgba(255,255,255,.65)">'
        f'Données : {html.escape(producer)} via DATAtourisme{when} · Licence Ouverte 2.0'
        '</div>\n'
        '<!-- DATATOURISME_ATTRIBUTION_END -->\n'
    )


def patch_event_page(path: Path, event: dict) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    original = text
    text = re.sub(
        r"\s*<!-- DATATOURISME_ATTRIBUTION_START -->.*?<!-- DATATOURISME_ATTRIBUTION_END -->\s*",
        "\n",
        text,
        flags=re.S,
    )
    block = attribution_block(event)
    if "</main>" in text:
        text = text.replace("</main>", block + "  </main>", 1)
    else:
        text = text.replace("</body>", block + "</body>", 1)
    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def patch_index(count: int) -> bool:
    path = EVENTS_DIR / "index.html"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    original = text

    title = f"Agenda autour de Nyons : {count} événements en cours et à venir"
    desc = (
        "Agenda des événements dans les villages autour de Nyons : sorties, culture, fêtes, "
        "spectacles et loisirs."
    )

    text = re.sub(r"<title>.*?</title>", f"<title>{html.escape(title)}</title>", text, count=1, flags=re.S)
    text = re.sub(
        r'<meta name="description" content="[^"]*">',
        f'<meta name="description" content="{html.escape(desc, quote=True)}">',
        text,
        count=1,
    )
    text = re.sub(
        r"<h1>100 prochains événements autour de Nyons</h1>",
        f"<h1>{count} événements autour de Nyons</h1>",
        text,
        count=1,
    )
    text = re.sub(
        r'<p class="date">Nyons est retiré de cette sélection pour éviter les doublons avec l’agenda local\.</p>',
        f'<p class="date">Les villages alentour sont à l’honneur.</p>',
        text,
        count=1,
    )

    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def main() -> None:
    payload = json.loads(AGENDA.read_text(encoding="utf-8"))
    events = payload.get("events", [])

    slug_map = {}
    used = set()
    for event in events:
        key = clean(event.get("url"))
        base = event_slug(event)
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
        slug_map[key] = slug

    changed = 0
    for event in events:
        slug = slug_map.get(clean(event.get("url")))
        if slug and patch_event_page(EVENTS_DIR / slug / "index.html", event):
            changed += 1

    if patch_index(len(events)):
        changed += 1

    print(f"DATAtourisme habillage: {changed} fichier(s) modifié(s).")
    print(f"Événements Drôme : {len(events)}")


if __name__ == "__main__":
    main()
