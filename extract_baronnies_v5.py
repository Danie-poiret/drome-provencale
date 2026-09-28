#!/usr/bin/env python3
"""
V5 — Agenda Baronnies hors Nyons depuis la page HTML officielle rendue par Chromium.

Pourquoi cette V5 ?
- la page filtrée officielle répond HTTP 200 dans Chromium ;
- le point d'export PDF (?sitpdf=1) répond 403 depuis GitHub Actions ;
- on abandonne donc le PDF automatique et on lit directement les cartes
  événements rendues par le navigateur, puis les fiches lorsque nécessaire.

Aucun appel OpenAI dans cette version.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"

FILTERED_PAGE = (
    "https://www.dromeprovencale.fr/agenda/tout-lagenda/"
    "recherche/territoire/baronnies-en-drome-provencale/"
)
DOMAIN = "www.dromeprovencale.fr"
TIMEOUT_MS = 90_000
MAX_LIST_PAGES = 30
MIN_FINAL_EVENTS = 15
DETAIL_DELAY_MS = 120

BARONNIES_COMMUNES = {
    "Arpavon", "Aubres", "Aulan", "Ballons", "Barret-de-Lioure",
    "Beauvoisin", "Bellecombe-Tarendol", "Bénivay-Ollon", "Bésignan",
    "Buis-les-Baronnies", "La Charce", "Châteauneuf-de-Bordette",
    "Chaudebonne", "Chauvac-Laux-Montaux", "Condorcet", "Cornillac",
    "Cornillon-sur-l’Oule", "Curnier", "Eygalayes", "Eygaliers", "Eyroles",
    "Izon-la-Bruisse", "Lemps", "Mérindol-les-Oliviers", "Mévouillon",
    "Mirabel-aux-Baronnies", "Montauban-sur-l’Ouvèze", "Montaulieu",
    "Montbrun-les-Bains", "Montferrand-la-Fare", "Montguers",
    "Montréal-les-Sources", "Nyons", "Pelonne", "La Penne-sur-l’Ouvèze",
    "Piégon", "Pierrelongue", "Les Pilles", "Plaisians", "Le Poët-en-Percip",
    "Le Poët-Sigillat", "Pommerol", "Propiac", "Reilhanette", "Rémuzat",
    "Rioms", "Rochebrune", "La Roche-sur-le-Buis", "La Rochette-du-Buis",
    "Roussieux", "Sahune", "Saint-Auban-sur-l’Ouvèze",
    "Sainte-Euphémie-sur-Ouvèze", "Saint-Ferréol-Trente-Pas", "Sainte-Jalle",
    "Saint-Maurice-sur-Eygues", "Saint-May", "Saint-Sauveur-Gouvernet",
    "Séderon", "Valouse", "Venterol", "Verclause", "Vercoiran",
    "Vers-sur-Méouge", "Villefranche-le-Château", "Villeperdrix", "Vinsobres",
}

MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}
MONTH_RE = "|".join(MONTHS)
FR_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE})(?:\s+(20\d{{2}}))?\b", re.I)
SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


def clean(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


COMMUNE_BY_NORM = {norm(c): c for c in BARONNIES_COMMUNES}
COMMUNE_KEYS = sorted(COMMUNE_BY_NORM, key=len, reverse=True)


def find_commune(text: str) -> str:
    n = f" {norm(text)} "
    for key in COMMUNE_KEYS:
        if f" {key} " in n:
            return COMMUNE_BY_NORM[key]
    return ""


def canonical_commune(value: str) -> str:
    n = norm(value)
    if n in COMMUNE_BY_NORM:
        return COMMUNE_BY_NORM[n]
    return find_commune(value)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    p = urlparse(url)
    scheme = p.scheme or "https"
    host = p.netloc.lower()
    path = re.sub(r"/+", "/", p.path or "/")
    if path != "/":
        path = path.rstrip("/") + "/"
    return f"{scheme}://{host}{path}"


def parse_dates(text: str) -> tuple[str, str]:
    values: list[date] = []

    for d, m, y in SLASH_DATE_RE.findall(text or ""):
        try:
            values.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass

    for y, m, d in ISO_DATE_RE.findall(text or ""):
        try:
            values.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass

    fr_matches = list(FR_DATE_RE.finditer(text or ""))
    explicit_years = [int(m.group(3)) for m in fr_matches if m.group(3)]
    fallback_year = explicit_years[0] if explicit_years else datetime.now().year
    last_month = None
    current_year = fallback_year
    for m in fr_matches:
        day = int(m.group(1))
        month = MONTHS[m.group(2).lower()]
        if m.group(3):
            current_year = int(m.group(3))
        elif last_month is not None and month < last_month - 6:
            current_year += 1
        last_month = month
        try:
            values.append(date(current_year, month, day))
        except ValueError:
            pass

    if not values:
        return "", ""
    values = sorted(set(values))
    return values[0].isoformat(), values[-1].isoformat()


def has_date(text: str) -> bool:
    return bool(SLASH_DATE_RE.search(text or "") or ISO_DATE_RE.search(text or "") or FR_DATE_RE.search(text or ""))


def looks_like_event_detail(url: str) -> bool:
    if not url:
        return False
    p = urlparse(url)
    if p.netloc.lower() not in {DOMAIN, "dromeprovencale.fr"}:
        return False
    path = p.path.lower()
    if not path or path == "/":
        return False
    banned = (
        "/agenda/tout-lagenda/",
        "/recherche/",
        "/contact/",
        "/mentions-legales/",
        "/politique-de-confidentialite/",
        "/wp-content/",
    )
    if any(x in path for x in banned):
        return False
    return True


def nearest_card(anchor):
    current = anchor
    best = anchor.parent
    for _ in range(9):
        current = current.parent
        if current is None:
            break
        text = clean(current.get_text(" ", strip=True))
        if current.name in ("article", "li") and 20 <= len(text) <= 2500:
            return current
        classes = " ".join(current.get("class", [])) if hasattr(current, "get") else ""
        if re.search(r"card|item|result|event|agenda|fiche", classes, re.I) and 20 <= len(text) <= 2500:
            return current
        if 20 <= len(text) <= 1400:
            best = current
    return best


def title_from_card(card, anchor) -> str:
    if card:
        for tag in ("h2", "h3", "h4", "h5"):
            node = card.find(tag)
            if node:
                t = clean(node.get_text(" ", strip=True))
                if 3 <= len(t) <= 220:
                    return t
    t = clean(anchor.get_text(" ", strip=True))
    generic = {
        "en savoir plus", "voir plus", "voir la fiche", "découvrir", "decouvrir",
        "lire la suite", "plus d'informations", "plus d informations",
    }
    return "" if norm(t) in {norm(x) for x in generic} else t


def extract_list_items(html: str, page_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    root = soup.find("main") or soup
    found = {}

    for a in root.find_all("a", href=True):
        href = normalize_url(urljoin(page_url, a["href"]))
        if not looks_like_event_detail(href):
            continue

        card = nearest_card(a)
        if not card:
            continue
        card_text = clean(card.get_text(" ", strip=True))

        # Un lien événement doit vivre dans un bloc qui ressemble réellement
        # à une fiche agenda : date ou commune du territoire.
        commune = find_commune(card_text)
        if not commune and not has_date(card_text):
            continue

        title = title_from_card(card, a)
        if not title or len(title) > 240:
            continue

        start, end = parse_dates(card_text)
        item = {
            "title": title,
            "url": href,
            "commune": commune,
            "start_date": start,
            "end_date": end or start,
            "card_text": card_text[:1800],
        }

        # Même URL rencontrée via plusieurs liens internes du même bloc :
        # on conserve la version la plus informative.
        old = found.get(href)
        if not old or len(item["card_text"]) > len(old["card_text"]):
            found[href] = item

    return list(found.values())


def jsonld_events(soup: BeautifulSoup) -> list[dict]:
    out = []

    def walk(value):
        if isinstance(value, dict):
            typ = value.get("@type")
            types = [typ] if isinstance(typ, str) else (typ or [])
            if any(str(x).lower() == "event" for x in types):
                out.append(value)
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            walk(json.loads(raw))
        except Exception:
            continue
    return out


def event_jsonld_for_title(soup: BeautifulSoup, title: str) -> dict:
    candidates = jsonld_events(soup)
    if not candidates:
        return {}
    target = norm(title)

    def score(obj):
        name = norm(obj.get("name"))
        if name == target:
            return 100
        if name and target and (name in target or target in name):
            return 80
        return len(set(name.split()) & set(target.split())) * 7

    return max(candidates, key=score)


def detail_from_html(html: str, item: dict, final_url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup.find("article") or soup
    main_text = clean(main.get_text(" ", strip=True))

    title = item["title"]
    commune = item.get("commune", "")
    start = item.get("start_date", "")
    end = item.get("end_date", "") or start
    address = ""
    source = "list_card"

    obj = event_jsonld_for_title(soup, title)
    if obj:
        title = clean(obj.get("name")) or title
        start = clean(obj.get("startDate"))[:10] or start
        end = clean(obj.get("endDate"))[:10] or end or start
        loc = obj.get("location")
        if isinstance(loc, list):
            loc = next((x for x in loc if isinstance(x, dict)), {})
        if isinstance(loc, dict):
            addr = loc.get("address")
            if isinstance(addr, dict):
                locality = clean(addr.get("addressLocality"))
                street = clean(addr.get("streetAddress"))
                postal = clean(addr.get("postalCode"))
                explicit = canonical_commune(locality)
                if explicit:
                    commune = explicit
                    source = "jsonld"
                address = ", ".join(x for x in (street, postal, locality) if x)

    if not commune:
        # Recherche uniquement dans le contenu principal de la fiche, jamais
        # dans le menu/footer. On privilégie une commune accolée à un CP.
        for proper in BARONNIES_COMMUNES:
            pat = rf"\b\d{{5}}\s+{re.escape(proper)}\b"
            if re.search(pat, main_text, re.I):
                commune = proper
                source = "main_postal_city"
                break

    if not commune:
        commune = find_commune(main_text)
        if commune:
            source = "main_content"

    if not start:
        start, end2 = parse_dates(main_text[:7000])
        end = end2 or start

    return {
        "title": title,
        "start_date": start,
        "end_date": end or start,
        "commune": commune,
        "address": address,
        "url": normalize_url(final_url),
        "source_url": item["url"],
        "geo_source": source,
    }


def dedupe_key(event: dict) -> str:
    raw = "|".join([
        norm(event.get("title")),
        event.get("start_date", ""),
        event.get("end_date", ""),
        norm(event.get("commune")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def auto_scroll(page) -> None:
    last_height = 0
    stable = 0
    for _ in range(8):
        height = await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(350)
        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == height == last_height:
            stable += 1
        else:
            stable = 0
        last_height = new_height
        if stable >= 2:
            break


async def find_next(page):
    selectors = [
        'a[rel="next"]',
        '.pagination a.next',
        '.pagination-next a',
        'a[aria-label*="suivant" i]',
        'a[title*="suivant" i]',
    ]
    for selector in selectors:
        loc = page.locator(selector)
        if await loc.count():
            for i in range(min(await loc.count(), 4)):
                one = loc.nth(i)
                if await one.is_visible():
                    return one

    # Fallback sur le libellé visible.
    for label in ("Suivant", "Suivante", "›", "»"):
        loc = page.get_by_role("link", name=label, exact=True)
        if await loc.count():
            for i in range(min(await loc.count(), 4)):
                one = loc.nth(i)
                if await one.is_visible():
                    return one
    return None


async def main_async() -> None:
    print("V5 HTML CHROMIUM : page officielle filtrée, sans PDF, sans GPT.")

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
        response = await page.goto(FILTERED_PAGE, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        status = response.status if response else 0
        print(f"PAGE FILTREE: HTTP {status} | {page.url}")
        if status != 200:
            raise RuntimeError(f"Page filtrée inaccessible dans Chromium: HTTP {status}")
        await page.wait_for_timeout(1200)

        collected = {}
        visited_pages = set()

        for page_no in range(1, MAX_LIST_PAGES + 1):
            await auto_scroll(page)
            current_url = page.url
            if current_url in visited_pages:
                break
            visited_pages.add(current_url)

            html = await page.content()
            items = extract_list_items(html, current_url)
            new_count = 0
            for item in items:
                if item["url"] not in collected:
                    collected[item["url"]] = item
                    new_count += 1
            print(
                f"LISTE {page_no:02d}: {len(items)} candidat(s), "
                f"{new_count} nouveau(x), total={len(collected)} | {current_url}"
            )

            nxt = await find_next(page)
            if nxt is None:
                print("PAGINATION: pas de page suivante détectée.")
                break
            before = page.url
            try:
                await nxt.click(timeout=15_000)
                await page.wait_for_timeout(900)
                await page.wait_for_load_state("domcontentloaded", timeout=30_000)
            except Exception as exc:
                print(f"PAGINATION: arrêt ({exc})")
                break
            if page.url == before:
                print("PAGINATION: URL inchangée après clic, arrêt.")
                break

        if not collected:
            raise RuntimeError("Aucun lien événement détecté sur la page filtrée.")

        print(f"CANDIDATS UNIQUES AVANT DETAIL: {len(collected)}")

        detail_page = await context.new_page()
        detailed = []
        detail_errors = 0
        nyons_excluded = 0
        outside_rejected = 0

        for idx, item in enumerate(collected.values(), 1):
            # Si la carte identifie déjà Nyons sans ambiguïté, inutile d'ouvrir la fiche.
            if norm(item.get("commune")) == "nyons":
                nyons_excluded += 1
                continue

            try:
                resp = await detail_page.goto(item["url"], wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                dstatus = resp.status if resp else 0
                if dstatus >= 400:
                    raise RuntimeError(f"HTTP {dstatus}")
                await detail_page.wait_for_timeout(180)
                event = detail_from_html(await detail_page.content(), item, detail_page.url)
            except Exception as exc:
                detail_errors += 1
                event = {
                    "title": item["title"],
                    "start_date": item.get("start_date", ""),
                    "end_date": item.get("end_date", ""),
                    "commune": item.get("commune", ""),
                    "address": "",
                    "url": item["url"],
                    "source_url": item["url"],
                    "geo_source": "list_card_after_detail_error",
                    "detail_error": clean(exc),
                }

            commune = canonical_commune(event.get("commune", ""))
            event["commune"] = commune
            if norm(commune) == "nyons":
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
            norm(e.get("commune")),
            norm(e.get("title")),
        ),
    ):
        url_key = normalize_url(event.get("url", ""))
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

    if len(unique) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Contrôle qualité V5: seulement {len(unique)} événement(s) hors Nyons. "
            "agenda.json reste inchangé."
        )

    payload = {
        "source": FILTERED_PAGE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "count": len(unique),
        "diagnostics": {
            "list_pages_scanned": len(visited_pages),
            "candidate_urls": len(collected),
            "nyons_excluded": nyons_excluded,
            "outside_or_unrecognized_rejected": outside_rejected,
            "detail_errors": detail_errors,
            "duplicates_removed": duplicates,
            "missing_date": missing_date,
        },
        "events": unique,
    }
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)

    print("=== BILAN V5 ===")
    print(f"Pages liste             : {len(visited_pages)}")
    print(f"Liens candidats         : {len(collected)}")
    print(f"Nyons exclus            : {nyons_excluded}")
    print(f"Commune rejetée/inconnue: {outside_rejected}")
    print(f"Erreurs détail          : {detail_errors}")
    print(f"Doublons supprimés      : {duplicates}")
    print(f"Dates manquantes        : {missing_date}")
    print(f"Événements conservés    : {len(unique)}")
    print("OK: agenda.json écrit.")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
