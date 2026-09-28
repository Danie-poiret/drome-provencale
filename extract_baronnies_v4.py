#!/usr/bin/env python3
"""
V4 - Agenda Baronnies hors Nyons depuis le PDF officiel dynamique.

Principe :
1. Chromium/Playwright ouvre le site comme un vrai navigateur.
2. Le script récupère le PDF officiel de la page déjà filtrée sur
   "Baronnies en Drôme Provençale" via ?sitpdf=1.
3. Le PDF est lu colonne par colonne (Nom / Adresse / Code postal-Ville /
   Contact / Périodes).
4. Nyons est supprimé.
5. Les doublons exacts sont supprimés.
6. agenda.json n'est remplacé que si le contrôle qualité est satisfaisant.

Aucun appel OpenAI dans cette V4.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import unicodedata
from collections import OrderedDict
from datetime import date, datetime, timezone
from pathlib import Path

import pdfplumber
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "agenda.json"
TMP_PDF = ROOT / "_latest_agenda.pdf"

FILTERED_PAGE = (
    "https://www.dromeprovencale.fr/agenda/tout-lagenda/"
    "recherche/territoire/baronnies-en-drome-provencale/"
)
PDF_URL = FILTERED_PAGE + "?sitpdf=1"

TIMEOUT_MS = 90_000
MIN_FINAL_EVENTS = 20
MIN_PDF_ROWS = 30
ROW_GAP_THRESHOLD = 18.0

# Colonnes fixes du PDF TCPDF officiel (A4, en points PDF).
COL_BOUNDS = [31.0, 111.0, 191.0, 271.0, 377.5, 565.0]

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

DATE_RE = re.compile(r"\b(\d{2}/\d{2}/20\d{2})\b")
RANGE_RE = re.compile(
    r"Du\s+(\d{2}/\d{2}/20\d{2})\s+au\s+(\d{2}/\d{2}/20\d{2})",
    re.I,
)
SINGLE_DATE_RE = re.compile(r"Le\s+(\d{2}/\d{2}/20\d{2})", re.I)
PHONE_RE = re.compile(r"(?:(?:\+33|0)[1-9](?:[ .-]?\d{2}){4})")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://.+", re.I)


def clean(value: object) -> str:
    text = str(value or "")
    text = text.replace("\u00ad", "").replace("\ufffe", "")
    return re.sub(r"[ \t]+", " ", text).strip()


def norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", clean(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("’", "'")
    return re.sub(r"[^a-z0-9]+", "", text)


COMMUNE_BY_NORM = {norm(c): c for c in BARONNIES_COMMUNES}


def join_wrapped(text: str) -> str:
    """Recompose un texte PDF en gardant les vrais mots composés."""
    lines = [clean(line) for line in (text or "").splitlines() if clean(line)]
    out = ""
    for line in lines:
        if not out:
            out = line
        elif out.endswith("-"):
            out += line
        else:
            out += " " + line
    return re.sub(r"\s+", " ", out).strip()


def iso_date(fr_date: str) -> str:
    return datetime.strptime(fr_date, "%d/%m/%Y").date().isoformat()


def parse_periods(periods: str) -> tuple[str, str, list[str]]:
    ranges = [(iso_date(a), iso_date(b)) for a, b in RANGE_RE.findall(periods or "")]
    all_dates = [iso_date(x) for x in DATE_RE.findall(periods or "")]
    singles = sorted({iso_date(x) for x in SINGLE_DATE_RE.findall(periods or "")})

    if ranges:
        start = min([a for a, _ in ranges] + all_dates)
        end = max([b for _, b in ranges] + all_dates)
    elif all_dates:
        start = min(all_dates)
        end = max(all_dates)
    else:
        start = ""
        end = ""

    return start, end, singles


def parse_city(city_cell: str) -> tuple[str, str, str]:
    """Retourne code postal, commune canonique, libellé brut recomposé."""
    raw = city_cell or ""
    m = re.search(r"(\d{5})", raw)
    postal = m.group(1) if m else ""
    rest = raw[m.end():] if m else raw

    # Dans cette colonne, les retours ligne peuvent couper un mot :
    # Montbru\nn-les-Bains, Saint-M\naurice-sur-\nEygues, etc.
    city_raw = rest.replace("\n", "").strip()
    commune = COMMUNE_BY_NORM.get(norm(city_raw), "")
    return postal, commune, city_raw


def parse_contact(contact_raw: str) -> dict:
    raw = clean(contact_raw.replace("Powered by TCPDF (www.tcpdf.org)", ""))

    # Le PDF coupe souvent les e-mails et URL au milieu d'un mot.
    compact = (contact_raw or "").replace("\n", "").replace(" ", "")
    url_match = re.search(r"https?://.+", compact, re.I)
    website = url_match.group(0).strip() if url_match else ""

    before_url_compact = compact[: url_match.start()] if url_match else compact
    email_match = EMAIL_RE.search(before_url_compact)
    email = email_match.group(0) if email_match else ""

    before_url_raw = contact_raw or ""
    if "http://" in before_url_raw:
        before_url_raw = before_url_raw.split("http://", 1)[0]
    if "https://" in before_url_raw:
        before_url_raw = before_url_raw.split("https://", 1)[0]
    phone_match = PHONE_RE.search(before_url_raw.replace("\n", " "))
    phone = clean(phone_match.group(0)) if phone_match else ""

    return {
        "contact_raw": raw,
        "phone": phone,
        "email": email,
        "website": website,
    }


def text_from_words(words: list[dict]) -> str:
    lines: OrderedDict[float, list[dict]] = OrderedDict()
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        top = round(float(word["top"]), 1)
        lines.setdefault(top, []).append(word)

    rendered = []
    for line_words in lines.values():
        line_words = sorted(line_words, key=lambda w: w["x0"])
        rendered.append(" ".join(str(w["text"]) for w in line_words))
    return "\n".join(rendered).strip()


def extract_page_rows(page, page_no: int) -> list[dict]:
    words = page.extract_words(use_text_flow=True, keep_blank_chars=False) or []

    # Chaque page du PDF répète l'en-tête du tableau.
    header = [
        w for w in words
        if str(w.get("text", "")) == "Nom"
        and COL_BOUNDS[0] <= float(w["x0"]) < COL_BOUNDS[1]
    ]
    if not header:
        print(f"PDF PAGE {page_no:02d}: en-tête 'Nom' introuvable")
        return []

    header_top = min(float(w["top"]) for w in header)
    title_words = [
        w for w in words
        if COL_BOUNDS[0] <= float(w["x0"]) < COL_BOUNDS[1]
        and float(w["top"]) >= header_top + 20
    ]

    title_tops = sorted({round(float(w["top"]), 1) for w in title_words})
    groups: list[list[float]] = []
    current: list[float] = []
    for top in title_tops:
        if not current or top - current[-1] < ROW_GAP_THRESHOLD:
            current.append(top)
        else:
            groups.append(current)
            current = [top]
    if current:
        groups.append(current)

    starts = [g[0] for g in groups]
    rows = []

    for idx, start in enumerate(starts):
        end = starts[idx + 1] - 1 if idx + 1 < len(starts) else float(page.height)
        row_words = [w for w in words if start - 1 <= float(w["top"]) < end]

        cols = []
        for col in range(5):
            col_words = [
                w for w in row_words
                if COL_BOUNDS[col] <= float(w["x0"]) < COL_BOUNDS[col + 1]
            ]
            cols.append(text_from_words(col_words))

        if not clean(cols[0]):
            continue

        rows.append(
            {
                "page": page_no,
                "title_raw": cols[0],
                "address_raw": cols[1],
                "city_raw": cols[2],
                "contact_raw": cols[3],
                "periods_raw": cols[4].replace("Powered by TCPDF (www.tcpdf.org)", "").strip(),
            }
        )

    print(f"PDF PAGE {page_no:02d}: {len(rows)} ligne(s) événement")
    return rows


def extract_pdf(pdf_bytes: bytes) -> tuple[list[dict], int, str]:
    rows: list[dict] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page_count = len(pdf.pages)
        first_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
        for page_no, page in enumerate(pdf.pages, 1):
            rows.extend(extract_page_rows(page, page_no))
    return rows, page_count, first_text


def build_event(row: dict) -> dict:
    title = join_wrapped(row["title_raw"])
    address = join_wrapped(row["address_raw"])
    postal, commune, city_original = parse_city(row["city_raw"])
    periods = row["periods_raw"].strip()
    start, end, dates = parse_periods(periods)
    contact = parse_contact(row["contact_raw"])

    event = {
        "title": title,
        "start_date": start,
        "end_date": end or start,
        "dates": dates,
        "periods": periods,
        "address": address,
        "postal_code": postal,
        "commune": commune,
        "commune_source": city_original,
        "phone": contact["phone"],
        "email": contact["email"],
        "website": contact["website"],
        "contact_raw": contact["contact_raw"],
        "source_page": row["page"],
        "source_format": "official_dynamic_pdf",
    }
    return event


def dedupe_key(event: dict) -> str:
    raw = "|".join(
        [
            norm(event.get("title")),
            event.get("start_date", ""),
            event.get("end_date", ""),
            norm(event.get("commune")),
            norm(event.get("address")),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def fetch_pdf_with_browser() -> tuple[str, bytes, dict]:
    print("NAVIGATEUR: démarrage Chromium/Playwright")
    diagnostics = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            locale="fr-FR",
            timezone_id="Europe/Paris",
            viewport={"width": 1365, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
                "DNT": "1",
            },
        )
        page = await context.new_page()

        landing_status = 0
        try:
            landing = await page.goto(
                FILTERED_PAGE,
                wait_until="domcontentloaded",
                timeout=TIMEOUT_MS,
            )
            landing_status = landing.status if landing else 0
            print(f"PAGE FILTREE: HTTP {landing_status} | {page.url}")
            await page.wait_for_timeout(1500)
        except Exception as exc:
            print(f"PAGE FILTREE: avertissement navigation: {exc}")

        diagnostics["landing_status"] = landing_status

        # 1) Requête Playwright dans le contexte navigateur (cookies partagés).
        try:
            response = await context.request.get(
                PDF_URL,
                headers={
                    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8",
                    "Referer": FILTERED_PAGE,
                },
                timeout=TIMEOUT_MS,
                fail_on_status_code=False,
            )
            body = await response.body()
            ctype = response.headers.get("content-type", "")
            print(
                f"PDF CONTEXT REQUEST: HTTP {response.status} | "
                f"{len(body)} octets | {ctype}"
            )
            diagnostics["context_request_status"] = response.status
            if response.status == 200 and body.startswith(b"%PDF-"):
                await browser.close()
                return PDF_URL, body, diagnostics
        except Exception as exc:
            print(f"PDF CONTEXT REQUEST: avertissement: {exc}")

        # 2) Secours : navigation Chromium directement vers la ressource PDF.
        try:
            pdf_response = await page.goto(
                PDF_URL,
                wait_until="commit",
                timeout=TIMEOUT_MS,
            )
            if pdf_response:
                body = await pdf_response.body()
                ctype = pdf_response.headers.get("content-type", "")
                print(
                    f"PDF PAGE GOTO: HTTP {pdf_response.status} | "
                    f"{len(body)} octets | {ctype}"
                )
                diagnostics["page_goto_status"] = pdf_response.status
                if pdf_response.status == 200 and body.startswith(b"%PDF-"):
                    await browser.close()
                    return PDF_URL, body, diagnostics
        except Exception as exc:
            print(f"PDF PAGE GOTO: avertissement: {exc}")

        await browser.close()

    raise RuntimeError(
        "Le navigateur n'a pas pu récupérer le PDF officiel. "
        "agenda.json reste inchangé."
    )


def main() -> None:
    source_url, pdf_bytes, browser_diag = asyncio.run(fetch_pdf_with_browser())

    if not pdf_bytes.startswith(b"%PDF-"):
        raise RuntimeError("La réponse reçue n'est pas un PDF.")

    TMP_PDF.write_bytes(pdf_bytes)
    pdf_sha = hashlib.sha256(pdf_bytes).hexdigest()
    print(f"PDF TELECHARGE: {len(pdf_bytes)} octets | sha256={pdf_sha[:16]}...")

    raw_rows, page_count, first_page_text = extract_pdf(pdf_bytes)
    print(f"PDF: {page_count} page(s) | {len(raw_rows)} ligne(s) extraite(s)")

    # Garde-fou : le PDF doit bien être celui du territoire filtré.
    if "baronniesendromeprovencale" not in norm(first_page_text):
        raise RuntimeError(
            "Le PDF reçu ne confirme pas le filtre 'Baronnies en Drôme Provençale'. "
            "agenda.json reste inchangé."
        )

    if len(raw_rows) < MIN_PDF_ROWS:
        raise RuntimeError(
            f"PDF suspect : seulement {len(raw_rows)} ligne(s) événement. "
            "agenda.json reste inchangé."
        )

    today = date.today()
    excluded_nyons = 0
    excluded_unknown_city = 0
    excluded_past = 0
    missing_date = 0
    events = []

    for row in raw_rows:
        event = build_event(row)
        commune = event.get("commune", "")

        if norm(commune) == norm("Nyons"):
            excluded_nyons += 1
            continue
        if not commune:
            excluded_unknown_city += 1
            continue

        end_date = event.get("end_date", "")
        if end_date:
            try:
                if date.fromisoformat(end_date) < today:
                    excluded_past += 1
                    continue
            except ValueError:
                pass
        else:
            missing_date += 1

        events.append(event)

    unique = {}
    duplicates = 0
    for event in events:
        key = dedupe_key(event)
        if key in unique:
            duplicates += 1
            continue
        event["dedupe_id"] = key[:16]
        unique[key] = event

    final_events = sorted(
        unique.values(),
        key=lambda e: (
            e.get("start_date") or "9999-12-31",
            norm(e.get("commune")),
            norm(e.get("title")),
        ),
    )

    if len(final_events) < MIN_FINAL_EVENTS:
        raise RuntimeError(
            f"Extraction finale suspecte : seulement {len(final_events)} événement(s). "
            "agenda.json reste inchangé."
        )

    payload = {
        "source": source_url,
        "source_filter": FILTERED_PAGE,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Baronnies en Drôme Provençale hors Nyons",
        "count": len(final_events),
        "diagnostics": {
            "browser": browser_diag,
            "pdf_pages": page_count,
            "pdf_bytes": len(pdf_bytes),
            "pdf_sha256": pdf_sha,
            "pdf_rows": len(raw_rows),
            "excluded_nyons": excluded_nyons,
            "excluded_unknown_city": excluded_unknown_city,
            "excluded_past": excluded_past,
            "duplicates_removed": duplicates,
            "missing_date": missing_date,
        },
        "events": final_events,
    }

    tmp_json = OUT.with_suffix(".json.tmp")
    tmp_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp_json.replace(OUT)

    print("=== BILAN V4 PDF DYNAMIQUE ===")
    print(f"Pages PDF                : {page_count}")
    print(f"Lignes PDF               : {len(raw_rows)}")
    print(f"Nyons exclus             : {excluded_nyons}")
    print(f"Communes non reconnues   : {excluded_unknown_city}")
    print(f"Evénements expirés       : {excluded_past}")
    print(f"Doublons supprimés       : {duplicates}")
    print(f"Dates manquantes         : {missing_date}")
    print(f"Evénements conservés     : {len(final_events)}")
    print("OK: agenda.json écrit depuis le PDF officiel du jour.")


if __name__ == "__main__":
    main()
