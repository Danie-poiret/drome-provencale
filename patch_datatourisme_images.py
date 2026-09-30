#!/usr/bin/env python3
from pathlib import Path

PATH = Path("extract_datatourisme_drome.py")
text = PATH.read_text(encoding="utf-8")

old_preserve = '''        # Les textes déjà payés restent attachés à leurs données d'origine.\n        # On ajoute seulement les coordonnées, qui n'invalident pas le cache\n        # éditorial, afin d'éviter de repayer une fiche déjà rédigée.\n        event = dict(old_event)\n        for field in ("latitude", "longitude"):\n            if field in fresh_event:\n                event[field] = fresh_event[field]\n'''
new_preserve = '''        # Les textes déjà payés restent attachés à leurs données d'origine.\n        # On rafraîchit les données techniques qui n'invalident pas le cache\n        # éditorial : coordonnées et, lorsqu'elle existe, image DATAtourisme.\n        event = dict(old_event)\n        for field in ("latitude", "longitude"):\n            if field in fresh_event:\n                event[field] = fresh_event[field]\n        if clean(fresh_event.get("image_url")):\n            for field in ("image_url", "image_credit", "image_rights"):\n                event[field] = fresh_event.get(field, "")\n'''

if old_preserve in text:
    text = text.replace(old_preserve, new_preserve, 1)
elif new_preserve not in text:
    raise SystemExit("Bloc de conservation des anciennes fiches introuvable")

old_sort = '''    events.sort(\n        key=lambda e: (\n            max(parse_date(e["start_date"]) or today, today),\n            e.get("commune", "").lower(),\n            e.get("title", "").lower(),\n        )\n    )\n\n    if EVENT_LIMIT > 0:\n'''
new_sort = '''    events.sort(\n        key=lambda e: (\n            max(parse_date(e["start_date"]) or today, today),\n            e.get("commune", "").lower(),\n            e.get("title", "").lower(),\n        )\n    )\n\n    raw_with_image = sum(1 for poi in raw if image_data(poi)[0])\n    normalized_count = len(events)\n    normalized_with_image = sum(1 for event in events if clean(event.get("image_url")))\n\n    if EVENT_LIMIT > 0:\n'''

if old_sort in text:
    text = text.replace(old_sort, new_sort, 1)
elif new_sort not in text:
    raise SystemExit("Bloc avant sélection introuvable")

old_summary = '''    print("=== DATATOURISME DRÔME ===")\n    print(f"Objets API reçus          : {len(raw)}")\n    print(f"Événements en cours/à venir: {len(events)}")\n    print(f"Fichier                   : {OUT.name}")\n'''
new_summary = '''    selected_with_image = sum(1 for event in events if clean(event.get("image_url")))\n\n    print("=== DATATOURISME DRÔME ===")\n    print(f"Objets API reçus                 : {len(raw)}")\n    print(f"Objets API avec image            : {raw_with_image}")\n    print(f"Événements normalisés            : {normalized_count}")\n    print(f"Événements normalisés avec image : {normalized_with_image}")\n    print(f"Événements retenus               : {len(events)}")\n    print(f"Événements retenus avec image    : {selected_with_image}")\n    print(f"Fichier                          : {OUT.name}")\n'''

if old_summary in text:
    text = text.replace(old_summary, new_summary, 1)
elif new_summary not in text:
    raise SystemExit("Bloc de résumé introuvable")

PATH.write_text(text, encoding="utf-8")
print("Patch photos DATAtourisme appliqué à extract_datatourisme_drome.py")
