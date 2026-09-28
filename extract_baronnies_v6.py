#!/usr/bin/env python3
"""
V6 — Agenda Baronnies hors Nyons depuis le HTML officiel rendu par Chromium.

Corrections par rapport à la V5 :
- on reconnaît directement les vraies fiches événement via /fete-manifestation/ ;
- on ne demande plus qu'une date/commune soit déjà visible dans la carte ;
- on parcourt directement /page/2/, /page/3/, etc. au lieu de dépendre du bouton Suivant ;
- la commune finale est validée uniquement par JSON-LD ou par code postal + commune
  dans le contenu principal de la fiche (pas de recherche libre dans le menu).

Aucun appel OpenAI.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

import extract_baronnies_v5 as base

MAX_LIST_PAGES = 30
MIN_FINAL_EVENTS = 15
DETAIL_DELAY_MS = 80


def is_real_event_url(url: str) -> bool:
    if not url:
        return False
    p = urlparse(url)
    if p.netloc.lower() not in {base.DOMAIN, "dromeprovencale.fr"}:
        return False
    path = p.path.lower()
    return path.startswith("/fete-manifestation/") and len(path.strip("/").split("/")) >= 2


def extract_list_items_v6(html: str, page_url: str) -> tuple[list[dict], int]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    found = {}
    raw_event_links = 0

    for a in root.find_all("a", href=True):
        href = base.normalize_url(urljoin(page_url, a["href"]))
        if not is_real_event_url(href):
            continue
        raw_event_links += 1

        card = base.nearest_card(a)
        card_text = base.clean(card.get_text(" ", strip=True)) if card else base.clean(a.get_text(" ", strip=True))
        title = base.title_from_card(card, a)

        # Si le lien lui-même porte un vrai libellé, il est souvent plus fiable
        # qu'un h2 générique trouvé trop haut dans le DOM.
        anchor_text = base.clean(a.get_text(" ", strip=True))
        if (not title or len(title) < 3) and 3 <= len(anchor_text) <= 240:
            title = anchor_text
        if not title or len(title) > 240:
            continue

        start, end = base.parse_dates(card_text)
        commune_hint = base.find_commune(card_text)

        item = {
            "title": title,
            "url": href,
            "commune": commune_hint,
            "start_date": start,
            "end_date": end or start,
            "card_text": card_text[:2200],
        }
        old = found.get(href)
        if not old or len(item["card_text"]) > len(old["card_text"]):
            found[href] = item

    return list(found.values()), raw_event_links


def detail_from_html_v6(html: str, item: dict, final_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup.find("article") or soup
    main_text = base.clean(main.get_text(" ", strip=True))
    main_norm = base.norm(main_text)

    title = item["title"]
    start = item.get("start_date", "")
    end = item.get("end_date", "") or start
    commune = ""
    address = ""
    geo_source = ""

    obj = base.event_jsonld_for_title(soup, title)
    if obj:
        title = base.clean(obj.get("name")) or title
        start = base.clean(obj.get("startDate"))[:10] or start
        end = base.clean(obj.get("endDate"))[:10] or end or start

        loc = obj.get("location")
        if isinstance(loc, list):
            loc = next((x for x in loc if isinstance(x, dict)), {})
        if isinstance(loc, dict):
            addr = loc.get("address")
            if isinstance(addr, dict):
                locality = base.clean(addr.get("addressLocality"))
                street = base.clean(addr.get("streetAddress"))
                postal = base.clean(addr.get("postalCode"))
                explicit = base.canonical_commune(locality)
                if explicit:
                    commune = explicit
                    geo_source = "jsonld_addressLocality"
                address = ", ".join(x for x in (street, postal, locality) if x)

    # Validation très stricte dans le contenu principal : CP + nom de commune.
    if not commune:
        for key in base.COMMUNE_KEYS:
            proper = base.COMMUNE_BY_NORM[key]
            # base.norm transforme les tirets/apostrophes en espaces.
            if re.search(rf"\b\d{{5}}\s+{re.escape(key)}\b", main_norm):
                commune = proper
                geo_source = "main_postal_city"
                break

    if not start:
        start, end2 = base.parse_dates(main_text[:9000])
        end = end2 or start

    return {
        "title": title,
        "start_date": start,
        "end_date": end or start,
        "commune": commune,
        "address": address,
        "url": base.normalize_url(final_url),
        "source_url": item["url"],
        "geo_source": geo_source or "unconfirmed",
    }


def dedupe_key(event: dict) -> str:
    raw = "|".join([
        base.norm(event.get("title")),
        event.get("start_date", ""),
        event.get("end_date", ""),
        base.norm(event.get("commune")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def main_async() -> None:
    print("V6 HTML CHROMIUM : liens /fete-manifestation/ + pagination directe, hors Nyons, sans GPT.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        collected = {}
        pages_scanned = 0
        stagnant = 0
        previous_signature = None

        for page_no in range(1, MAX_LIST_PAGES + 1):
            target = base.FILTERED_PAGE if page_no == 1 else urljoin(base.FILTERED_PAGE, f"page/{page_no}/")
            try:
                response = await page.goto(target, wait_until="domcontentloaded", timeout=base.TIMEOUT_MS)
            except Exception as exc:
                print(f"LISTE {page_no:02d}: erreur navigation {exc}")
                if page_no == 1:
                    raise
                break

            status = response.status if response else 0
            if status >= 400:
                print(f"LISTE {page_no:02d}: HTTP {status}, arrêt pagination.")
                if page_no == 1:
                    raise RuntimeError(f"Page filtrée inaccessible: HTTP {status}")
                break

            try:
                await page.wait_for_load_state("networkidle", timeout=8_000)
            except Exception:
                pass
            await page.wait_for_timeout(1800)
            await base.auto_scroll(page)
            await page.wait_for_timeout(500)

            html = await page.content()
            items, raw_links = extract_list_items_v6(html, page.url)
            pages_scanned += 1

            urls = sorted(x["url"] for x in items)
            signature = hashlib.sha256("|".join(urls).encode("utf-8")).hexdigest() if urls else "empty"

            new_count = 0
            for item in items:
                if item["url"] not in collected:
                    collected[item["url"]] = item
                    new_count += 1

            print(
                f"LISTE {page_no:02d}: HTTP {status} | liens événement bruts={raw_links} | "
                f"candidats={len(items)} | nouveaux={new_count} | total={len(collected)} | {page.url}"
            )

            if new_count == 0:
                stagnant += 1
            else:
                stagnant = 0

            if previous_signature == signature and page_no >= 2:
                print("PAGINATION: même série d'événements que la page précédente.")
            previous_signature = signature

            # Deux pages consécutives sans aucun nouvel événement suffisent à conclure
            # qu'on a dépassé la pagination utile ou que le site répète la dernière page.
            if stagnant >= 2 and page_no >= 3:
                print("PAGINATION: 2 pages sans nouveau résultat, arrêt.")
                break

        if not collected:
            raise RuntimeError("Aucun lien /fete-manifestation/ détecté sur l'agenda filtré.")

        print(f"CANDIDATS UNIQUES AVANT DETAIL: {len(collected)}")

        detail_page = await context.new_page()
        detailed = []
        detail_errors = 0
        nyons_excluded = 0
        outside_rejected = 0

        for idx, item in enumerate(collected.values(), 1):
            try:
                resp = await detail_page.goto(item["url"], wait_until="domcontentloaded", timeout=base.TIMEOUT_MS)
                dstatus = resp.status if resp else 0
                if dstatus >= 400:
                    raise RuntimeError(f"HTTP {dstatus}")
                try:
                    await detail_page.wait_for_load_state("networkidle", timeout=5_000)
                except Exception:
                    pass
                await detail_page.wait_for_timeout(150)
                event = detail_from_html_v6(await detail_page.content(), item, detail_page.url)
            except Exception as exc:
                detail_errors += 1
                print(f"DETAIL {idx:03d}/{len(collected)} ERREUR — {item['title']}: {base.clean(exc)}")
                continue

            commune = base.canonical_commune(event.get("commune", ""))
            event["commune"] = commune
            if base.norm(commune) == "nyons":
                nyons_excluded += 1
                continue
            if not commune:
                outside_rejected += 1
                continue

            detailed.append(event)
            print(
                f"DETAIL {idx:03d}/{len(collected)} OK — "
                f"{event.get('start_date') or '?'} | {commune} | {event.get('title')}"
            )
            await detail_page.wait_for_timeout(DETAIL_DELAY_MS)

        await detail_page.close()
        await browser.close()

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
        fp = dedupe_key(event)
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

    print("=== BILAN V6 AVANT CONTROLE ===")
    print(f"Pages liste             : {pages_scanned}")
    print(f"Liens candidats         : {len(collected)}")
    print(f"Nyons exclus            : {nyons_excluded}")
    print(f"Commune rejetée/inconnue: {outside_rejected}")
    print(f"Erreurs détail          : {detail_errors}")
    print(f"Doublons supprimés      : {duplicates}")
    print(f"Dates manquantes        : {missing_date}")
    print(f"Événements conservés    : {len(unique)}")

    if len(unique) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Contrôle qualité V6: seulement {len(unique)} événement(s) hors Nyons. "
            "agenda.json reste inchangé."
        )

    payload = {
        "source": base.FILTERED_PAGE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "count": len(unique),
        "diagnostics": {
            "version": 6,
            "list_pages_scanned": pages_scanned,
            "candidate_urls": len(collected),
            "nyons_excluded": nyons_excluded,
            "outside_or_unrecognized_rejected": outside_rejected,
            "detail_errors": detail_errors,
            "duplicates_removed": duplicates,
            "missing_date": missing_date,
        },
        "events": unique,
    }
    tmp = base.OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(base.OUT)
    print("OK: agenda.json écrit.")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
