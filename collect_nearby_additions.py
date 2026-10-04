#!/usr/bin/env python3
"""Collect exactly 50 additional current events near Nyons without changing the live agenda."""
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
import extract_datatourisme_drome as dt

def main():
    previous = json.loads(Path("agenda.json").read_text(encoding="utf-8"))["events"]
    urls = {dt.clean(e.get("url")) for e in previous}
    uuids = {dt.clean(e.get("datatourisme_uuid")) for e in previous}
    signatures = {dt.event_signature(e) for e in previous}
    today = date.today()
    candidates = []
    for poi in dt.fetch_all(os.environ["DATATOURISME_API_KEY"]):
        event = dt.normalize_poi(poi, today)
        if not event or not dt.clean(event.get("commune")) or dt.commune_key(event["commune"]) == "nyons":
            continue
        url, uuid, signature = event["url"], dt.clean(event.get("datatourisme_uuid")), dt.event_signature(event)
        if url in urls or (uuid and uuid in uuids) or signature in signatures:
            continue
        distance = dt.distance_from_nyons(event)
        if distance is None or distance > 45:
            continue
        event["distance_from_nyons_km"] = distance
        candidates.append(event)
        urls.add(url)
        if uuid:
            uuids.add(uuid)
        signatures.add(signature)
    candidates.sort(key=lambda e: (e["distance_from_nyons_km"], max(e["start_date"], today.isoformat()), e["title"]))
    selected = candidates[:50]
    if len(selected) != 50:
        raise RuntimeError(f"Only {len(selected)} new events within 45 km; no agenda changes published")
    payload = {"source": dt.API_URL, "collected_at": datetime.now(timezone.utc).isoformat(), "reference_count": len(previous), "count": 50, "events": selected}
    Path("_nearby_additions.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(f"Collected 50 new events in {len({e['commune'] for e in selected})} communes")
    print(f"Distance range: {selected[0]['distance_from_nyons_km']} to {selected[-1]['distance_from_nyons_km']} km straight-line")
if __name__ == "__main__":
    main()
