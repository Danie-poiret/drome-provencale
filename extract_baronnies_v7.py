#!/usr/bin/env python3
"""
V7 — Agenda Baronnies hors Nyons depuis la liste officielle rendue dans Chromium.

Principe :
- une seule ouverture initiale de la page filtrée ;
- pagination via clics dans la page (AJAX), même si l'URL ne change pas ;
- aucune ouverture des fiches détail, car elles sont bloquées en 403 depuis GitHub Actions ;
- extraction depuis chaque carte événement : titre, commune, date ;
- Nyons supprimé ; doublons supprimés ; contrôle qualité avant écriture.

Aucun appel OpenAI.
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

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"

SOURCE = (
    "https://www.dromeprovencale.fr/agenda/tout-lagenda/"
    "recherche/territoire/baronnies-en-drome-provencale/"
)
DOMAIN = "www.dromeprovencale.fr"
TIMEOUT_MS = 90_000
MAX_PAGES = 30
MIN_FINAL_EVENTS = 15

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
FR_DATE_RE = re.compile(
    rf"\b(\d{{1,2}})\s+({MONTH_RE})(?:\s+(20\d{{2}))?\b", re.I
)
SLASH_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")
ISO_DATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
POSTAL_RE = re.compile(r"\b(26\d{3})\b")


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

    fr = list(FR_DATE_RE.finditer(text or ""))
    explicit_years = [int(m.group(3)) for m in fr if m.group(3)]
    year = explicit_years[0] if explicit_years else datetime.now().year
    last_month = None
    for m in fr:
        day = int(m.group(1))
        month = MONTHS[m.group(2).lower()]
        if m.group(3):
            year = int(m.group(3))
        elif last_month is not None and month < last_month - 6:
            year += 1
        last_month = month
        try:
            values.append(date(year, month, day))
        except ValueError:
            pass

    if not values:
        return "", ""
    values = sorted(set(values))
    return values[0].isoformat(), values[-1].isoformat()


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


def dedupe_key(event: dict) -> str:
    raw = "|".join([
        norm(event.get("title")),
        event.get("start_date", ""),
        event.get("end_date", ""),
        norm(event.get("commune")),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def collect_cards(page) -> list[dict]:
    """Récupère les cartes depuis le DOM courant, sans requête supplémentaire."""
    raw = await page.locator('a[href*="/fete-manifestation/"]').evaluate_all(
        """
        (anchors) => {
          const out = [];
          for (const a of anchors) {
            let node = a;
            let chosen = null;
            let fallback = null;
            for (let i = 0; i < 9 && node; i++, node = node.parentElement) {
              const txt = (node.innerText || '').replace(/\s+/g, ' ').trim();
              if (!txt) continue;
              if (!fallback && txt.length <= 1800) fallback = node;
              const hasDate = /\b\d{1,2}[\/\-.]\d{1,2}[\/\-.]20\d{2}\b/.test(txt)
                || /\b\d{1,2}\s+(janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)\b/i.test(txt);
              const hasPostal = /\b26\d{3}\b/.test(txt);
              if ((hasDate || hasPostal) && txt.length <= 2200) {
                chosen = node;
                break;
              }
            }
            const card = chosen || fallback || a.parentElement;
            const text = ((card && card.innerText) || a.innerText || '').replace(/\s+/g, ' ').trim();
            let title = (a.innerText || '').replace(/\s+/g, ' ').trim();
            if (!title || title.length < 3 || /^(en savoir plus|voir|découvrir|decouvrir|lire la suite)$/i.test(title)) {
              const h = card && card.querySelector('h2,h3,h4,h5');
              if (h) title = (h.innerText || '').replace(/\s+/g, ' ').trim();
            }
            out.push({href: a.href, title, text});
          }
          return out;
        }
        """
    )

    by_url = {}
    for item in raw:
        url = normalize_url(item.get("href", ""))
        if not url:
            continue
        if urlparse(url).netloc.lower() not in {DOMAIN, "dromeprovencale.fr"}:
            continue
        title = clean(item.get("title"))
        text = clean(item.get("text"))
        if not title or len(title) > 260:
            continue

        commune = find_commune(text)
        start, end = parse_dates(text)
        event = {
            "title": title,
            "url": url,
            "commune": commune,
            "start_date": start,
            "end_date": end or start,
            "card_text": text[:1800],
        }
        old = by_url.get(url)
        if not old or len(event["card_text"]) > len(old["card_text"]):
            by_url[url] = event

    return list(by_url.values())


async def current_link_signature(page) -> tuple[str, ...]:
    hrefs = await page.locator('a[href*="/fete-manifestation/"]').evaluate_all(
        "els => els.map(e => e.href).filter(Boolean).sort()"
    )
    return tuple(hrefs)


async def find_next_control(page):
    selectors = [
        'a[rel="next"]',
        '.pagination a.next',
        '.pagination-next a',
        'button[aria-label*="suivant" i]',
        'a[aria-label*="suivant" i]',
        'button[title*="suivant" i]',
        'a[title*="suivant" i]',
        '.pagination button:last-child',
    ]
    for selector in selectors:
        loc = page.locator(selector)
        count = await loc.count()
        for i in range(min(count, 5)):
            one = loc.nth(i)
            try:
                if await one.is_visible() and await one.is_enabled():
                    return one
            except Exception:
                pass

    # Fallback texte visible.
    for label in ("Suivant", "Suivante", "›", "»"):
        for role in ("link", "button"):
            loc = page.get_by_role(role, name=label, exact=True)
            count = await loc.count()
            for i in range(min(count, 5)):
                one = loc.nth(i)
                try:
                    if await one.is_visible() and await one.is_enabled():
                        return one
                except Exception:
                    pass
    return None


async def wait_for_list_change(page, before_sig: tuple[str, ...]) -> bool:
    for _ in range(32):
        await page.wait_for_timeout(250)
        try:
            sig = await current_link_signature(page)
        except Exception:
            continue
        if sig and sig != before_sig:
            return True
    return False


async def main_async() -> None:
    print("V7 AJAX: page officielle filtrée, pagination par clic, sans fiches détail, sans GPT.")

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
        response = await page.goto(SOURCE, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
        status = response.status if response else 0
        print(f"PAGE FILTREE: HTTP {status} | {page.url}")
        if status != 200:
            raise RuntimeError(f"Page filtrée inaccessible: HTTP {status}")
        await page.wait_for_timeout(1400)

        collected = {}
        page_count = 0

        for page_no in range(1, MAX_PAGES + 1):
            # Met le pager à portée et laisse les éventuels contenus lazy-load se stabiliser.
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(500)

            cards = await collect_cards(page)
            page_count += 1
            new_count = 0
            for card in cards:
                url = card["url"]
                if url not in collected:
                    collected[url] = card
                    new_count += 1
                else:
                    old = collected[url]
                    if len(card.get("card_text", "")) > len(old.get("card_text", "")):
                        collected[url] = card

            known_communes = sum(1 for c in cards if c.get("commune"))
            known_dates = sum(1 for c in cards if c.get("start_date"))
            print(
                f"LISTE {page_no:02d}: cartes={len(cards)} | nouvelles={new_count} | "
                f"communes={known_communes} | dates={known_dates} | total={len(collected)}"
            )

            before_sig = await current_link_signature(page)
            nxt = await find_next_control(page)
            if nxt is None:
                print("PAGINATION: aucun contrôle suivant détecté, arrêt.")
                break

            try:
                await nxt.scroll_into_view_if_needed()
                await nxt.click(timeout=15_000)
            except Exception as exc:
                print(f"PAGINATION: clic impossible ({exc}), arrêt.")
                break

            changed = await wait_for_list_change(page, before_sig)
            if not changed:
                print("PAGINATION: aucun changement de liste après clic, arrêt.")
                break

        await browser.close()

    print(f"CANDIDATS UNIQUES: {len(collected)}")

    retained = []
    nyons_excluded = 0
    unknown_commune = 0
    missing_date = 0
    duplicates = 0
    seen_fp = set()

    for event in collected.values():
        commune = event.get("commune", "")
        if norm(commune) == "nyons":
            nyons_excluded += 1
            continue
        if not commune:
            unknown_commune += 1
            continue

        fp = dedupe_key(event)
        if fp in seen_fp:
            duplicates += 1
            continue
        seen_fp.add(fp)
        event["dedupe_id"] = fp[:16]
        event.pop("card_text", None)
        if not event.get("start_date"):
            missing_date += 1
        retained.append(event)

    retained.sort(
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            norm(e.get("commune")),
            norm(e.get("title")),
        )
    )

    print("=== BILAN V7 AVANT CONTROLE ===")
    print(f"Pages AJAX parcourues    : {page_count}")
    print(f"Liens candidats          : {len(collected)}")
    print(f"Nyons exclus             : {nyons_excluded}")
    print(f"Commune inconnue rejetée : {unknown_commune}")
    print(f"Doublons supprimés       : {duplicates}")
    print(f"Dates manquantes         : {missing_date}")
    print(f"Événements conservés     : {len(retained)}")

    if len(retained) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Contrôle qualité V7: seulement {len(retained)} événement(s) hors Nyons. "
            "agenda.json reste inchangé."
        )

    payload = {
        "source": SOURCE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "count": len(retained),
        "diagnostics": {
            "ajax_pages_scanned": page_count,
            "candidate_urls": len(collected),
            "nyons_excluded": nyons_excluded,
            "unknown_commune_rejected": unknown_commune,
            "duplicates_removed": duplicates,
            "missing_date": missing_date,
        },
        "events": retained,
    }

    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(OUT)
    print("OK: agenda.json écrit.")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
