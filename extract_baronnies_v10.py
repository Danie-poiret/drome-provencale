#!/usr/bin/env python3
"""
V10 optimisée — Nyons + rayon 100 km, mais sans ouvrir des centaines de
fiches hors Baronnies.

Principe :
- une seule recherche autour de Nyons, rayon 100 km ;
- parcourt toutes les pages détectées (jusqu'à 160) ;
- sur chaque page, repère le plus petit bloc/carte autour de chaque lien
  /fiches/ et ne garde que les cartes mentionnant réellement une commune des
  Baronnies ;
- Nyons est éliminé dès la liste, avant l'ouverture des fiches ;
- ouvre ensuite seulement les fiches candidates utiles ;
- réutilise le cache V9 pendant 60 h ;
- enlève les événements terminés ;
- conserve au maximum les 50 prochains événements.

Aucun appel OpenAI.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import extract_baronnies_v9 as v9

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
MAX_FINAL_EVENTS = 50

# Une seule zone large demandée : Nyons + 100 km.
v9.SEARCH_CENTERS = ("nyons",)
v9.RADIUS_KM = 100
v9.MAX_PAGES_PER_CENTER = 160
v9.TIMEOUT = 15
v9.REQUEST_DELAY = 0
v9.DETAIL_TTL_HOURS = 60
v9.MIN_FINAL_EVENTS = 10


def commune_in_text(text: str) -> str:
    """Trouve une commune Baronnies dans le texte d'UNE carte locale."""
    n = " " + v9.norm(text) + " "
    for key in v9.COMMUNE_KEYS:
        if re.search(rf"\b{re.escape(key)}\b", n):
            return v9.COMMUNE_BY_NORM[key]
    return ""


def local_card_for_link(link) -> object | None:
    """
    Remonte seulement quelques niveaux et choisit le plus petit ancêtre qui
    semble représenter une seule fiche événement. Cela évite le bug des
    anciennes versions qui lisaient un grand bloc contenant plusieurs cartes.
    """
    parent = link.parent
    best = None

    for _ in range(7):
        if parent is None:
            break

        text = v9.clean(parent.get_text(" ", strip=True))
        if text and len(text) <= 1800:
            detail_urls = set()
            for a in parent.find_all("a", href=True):
                href = v9.normalize_url(urljoin("https://www.drome-cestmanature.com/", a.get("href", "")))
                if v9.is_detail_url(href):
                    detail_urls.add(href)

            # La carte doit correspondre à une seule fiche événement.
            if len(detail_urls) == 1:
                best = parent
                # Le premier petit bloc valable est le plus précis.
                break

        parent = parent.parent

    return best


def extract_candidates_100km(html: str, current_url: str) -> list[dict]:
    """
    Préfiltre les résultats AVANT ouverture des fiches : uniquement une carte
    locale qui mentionne une commune des Baronnies. Nyons est supprimé ici.
    """
    soup = v9.BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    by_url: dict[str, dict] = {}

    for a in root.find_all("a", href=True):
        href = v9.normalize_url(urljoin(current_url, a["href"]))
        if not v9.is_detail_url(href):
            continue

        card = local_card_for_link(a)
        if card is None:
            continue

        card_text = v9.clean(card.get_text(" ", strip=True))
        commune = commune_in_text(card_text)
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


# On garde toute la logique robuste de V9 mais on remplace uniquement la
# détection des candidats par ce préfiltrage carte-local.
v9.extract_candidates = extract_candidates_100km


def reparsed_dates(opening: str, today: date) -> tuple[str, str]:
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
            candidates.append((day, month, int(match.group(3)) if match.group(3) else None))

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
    elif candidates[0][2] is None:
        year = explicit_years[0]

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


def postprocess() -> None:
    today = datetime.now(ZoneInfo("Europe/Paris")).date()
    horizon = today + timedelta(days=v9.HORIZON_DAYS)

    data = json.loads(OUT.read_text(encoding="utf-8"))
    cleaned = []
    past_removed = 0
    after_horizon_removed = 0

    for event in data.get("events", []):
        event = dict(event)
        opening = event.get("opening", "")
        start, end = reparsed_dates(opening, today)
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
        cleaned.append(event)

    cleaned.sort(
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            v9.norm(e.get("commune")),
            v9.norm(e.get("title")),
        )
    )
    cleaned = cleaned[:MAX_FINAL_EVENTS]

    if len(cleaned) < 10:
        raise RuntimeError(
            f"Contrôle qualité V10-100km: seulement {len(cleaned)} événement(s) après nettoyage."
        )

    data["source_mode"] = "filtered_radius_100km_cardprefilter_v10"
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["count"] = len(cleaned)
    data.setdefault("search", {})["centers"] = ["nyons"]
    data["search"]["radius_km"] = 100
    data["search"]["max_final_events"] = MAX_FINAL_EVENTS
    data["search"]["max_pages_per_center"] = v9.MAX_PAGES_PER_CENTER
    data.setdefault("diagnostics", {})["v10_past_removed"] = past_removed
    data["diagnostics"]["v10_after_horizon_removed"] = after_horizon_removed
    data["diagnostics"]["v10_cache_ttl_hours"] = v9.DETAIL_TTL_HOURS
    data["diagnostics"]["v10_prefilter"] = "nearest_single-detail-card + Baronnies commune"
    data["events"] = cleaned

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)

    print("=== V10 100 KM POST-TRAITEMENT ===")
    print(f"Événements terminés retirés : {past_removed}")
    print(f"Après horizon retirés        : {after_horizon_removed}")
    print(f"Événements finaux            : {len(cleaned)}")


def main() -> None:
    print("V10 OPTIMISEE : Nyons + rayon 100 km + préfiltrage cartes Baronnies.")
    v9.main()
    postprocess()
    print("OK V10 100 km: agenda.json prêt.")


if __name__ == "__main__":
    main()
