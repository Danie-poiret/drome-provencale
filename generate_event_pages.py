#!/usr/bin/env python3
"""Génère les fiches événements HTML dans le même esprit que agenda.vivreanyons.fr.

- lit agenda.json produit par extract_baronnies_v10.py ;
- génère une page /evenements/<slug>/index.html par événement ;
- reprend la même présentation visuelle et éditoriale que l'agenda Nyons ;
- utilise OpenAI uniquement pour les événements nouveaux ou modifiés ;
- conserve un cache éditorial pour ne pas repayer les textes inchangés ;
- génère aussi l'index des événements, sitemap.xml et robots.txt.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus, urljoin

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

ROOT = Path(__file__).resolve().parent
AGENDA = ROOT / "agenda.json"
EVENTS_DIR = ROOT / "evenements"
CACHE_FILE = ROOT / "_event_seo_cache.json"
SITEMAP = ROOT / "sitemap.xml"
ROBOTS = ROOT / "robots.txt"
ANECDOTES_FILE = ROOT / "village_anecdotes.json"

SITE = os.getenv("SITE_URL", "https://drome.vivreanyons.fr/").rstrip("/") + "/"
BANNER_URL = "https://danie-poiret.github.io/banniere-nyons/"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")
MAX_EVENT_AI_CALLS = int(os.getenv("MAX_EVENT_AI_CALLS", "100"))
PROMPT_VERSION = 1

MONTHS = {
    1: "janvier", 2: "février", 3: "mars", 4: "avril", 5: "mai", 6: "juin",
    7: "juillet", 8: "août", 9: "septembre", 10: "octobre", 11: "novembre", 12: "décembre",
}


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def esc(value) -> str:
    return html.escape(clean(value), quote=True)


def slugify(text: str, max_len: int = 90) -> str:
    raw = unicodedata.normalize("NFKD", clean(text))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.lower().replace("’", "-").replace("'", "-")
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    return raw[:max_len].rstrip("-") or "evenement"


def iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(clean(value))
    except Exception:
        return None


def fr_date(value: str) -> str:
    d = iso_date(value)
    if not d:
        return clean(value)
    return f"{d.day} {MONTHS[d.month]} {d.year}"


def date_summary(event: dict) -> str:
    """Date courte, toujours sûre pour le bandeau, les listes et les cartes."""
    start = iso_date(event.get("start_date", ""))
    end = iso_date(event.get("end_date", ""))
    if not start:
        return "Dates à vérifier sur la fiche source"
    if not end or end == start:
        return fr_date(start.isoformat())
    return f"Du {fr_date(start.isoformat())} au {fr_date(end.isoformat())}"


def _cut_clean_text(text: str, limit: int = 280) -> str:
    text = clean(text).strip(" -:;,.|")
    if len(text) <= limit:
        return text
    chunk = text[:limit]
    cuts = [chunk.rfind(". "), chunk.rfind("; "), chunk.rfind(" - ")]
    cut = max(cuts)
    if cut >= 80:
        chunk = chunk[: cut + 1]
    else:
        chunk = chunk.rsplit(" ", 1)[0]
    return chunk.rstrip(" -:;,.|")


def date_label(event: dict) -> str:
    """Horaires utiles, sans laisser une description entière envahir la fiche."""
    opening = clean(event.get("opening"))
    if not opening:
        return date_summary(event)

    # Une vraie ligne d'ouverture courte est conservée telle quelle.
    has_time_or_date = bool(re.search(
        r"(?:\b\d{1,2}[h:]\d{0,2}\b|\b20\d{2}\b|\b(?:lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche)\b|\bdu\s+\d{1,2}[\s/])",
        opening,
        re.I,
    ))
    if len(opening) <= 240 and has_time_or_date:
        return opening

    # Certains champs de la source contiennent le programme complet puis une vraie rubrique "Ouverture".
    low = opening.lower()
    pos = low.rfind("ouverture")
    if pos >= 0:
        tail = clean(opening[pos + len("ouverture"):]).strip(" -:;")
        for marker in (
            " informations complémentaires", " langues parlées", " tarifs", " réservation",
            " contact", " accès", " equipements", " équipements", " services",
        ):
            idx = tail.lower().find(marker)
            if idx > 0:
                tail = tail[:idx]
        tail = _cut_clean_text(tail, 300)
        if len(tail) >= 8:
            return tail

    return date_summary(event)


def concise_address(event: dict) -> str:
    """Évite d'afficher un paragraphe complet lorsque le scraper a avalé le texte autour de l'adresse."""
    raw = clean(event.get("address"))
    commune = clean(event.get("commune"))
    if not raw:
        return commune
    if len(raw) <= 190:
        return raw

    postcodes = list(re.finditer(r"\b\d{5}\b", raw))
    if postcodes:
        pc = postcodes[-1]
        end = pc.end()
        after = raw[end:end + 100]
        if commune:
            cm = re.search(re.escape(commune), after, re.I)
            if cm:
                end += cm.end()
        start = max(0, pc.start() - 150)
        segment = clean(raw[start:end]).strip(" -:;,.|")
        low = segment.lower()
        starts = [
            low.rfind("accès "), low.rfind("adresse "), low.rfind("rue "),
            low.rfind("avenue "), low.rfind("place "), low.rfind("chemin "),
            low.rfind("route "), low.rfind("boulevard "),
        ]
        good = max(starts)
        if good >= 0:
            segment = segment[good:]
        segment = _cut_clean_text(segment, 190)
        if segment:
            return segment

    return commune or _cut_clean_text(raw, 190)


def event_status(event: dict) -> str:
    today = date.today()
    start = iso_date(event.get("start_date", ""))
    end = iso_date(event.get("end_date", "")) or start
    if start and end and start <= today <= end:
        return "En cours"
    if start and start > today:
        return "À venir"
    return "Agenda"


def extract_email(text: str) -> str:
    m = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text or "", re.I)
    return m.group(0) if m else ""


def extract_phone(text: str) -> str:
    m = re.search(r"(?<!\d)(?:\+33\s?\(0\)\s?|\+33\s?|0)[1-9](?:[ .-]?\d{2}){4}(?!\d)", text or "")
    return clean(m.group(0)) if m else ""


def compact_description(text: str, limit: int = 3600) -> str:
    return clean(text)[:limit]


def schema_offer(event: dict, source_url: str, canonical: str) -> dict | None:
    """Construit une Offer seulement à partir d'un tarif réellement annoncé."""
    tariffs = clean(event.get("tariffs"))
    desc = clean(event.get("description"))
    evidence = tariffs or ("Gratuit" if re.search(r"\bgratuit(?:e|ement)?\b", desc, re.I) else "")
    if not evidence:
        return None

    offer = {
        "@type": "Offer",
        "url": source_url or canonical,
        "priceCurrency": "EUR",
        "availability": "https://schema.org/InStock",
    }
    low = evidence.lower()
    if re.search(r"\bgratuit(?:e|ement)?\b", low):
        offer["price"] = "0"
    else:
        match = re.search(r"(?<!\d)(\d+(?:[,.]\d{1,2})?)\s*(?:€|euros?\b)", evidence, re.I)
        if not match:
            match = re.search(r"\b(?:à partir de|dès|de)\s+(\d+(?:[,.]\d{1,2})?)", evidence, re.I)
        if match:
            offer["price"] = match.group(1).replace(",", ".")
    offer["description"] = _cut_clean_text(evidence, 280)
    return offer


def schema_organizer(event: dict) -> str:
    """Organisateur seulement lorsqu'il est fourni ou explicitement nommé dans la source."""
    explicit = clean(event.get("organizer"))
    if explicit:
        return explicit

    text = " ".join((clean(event.get("description")), clean(event.get("contact"))))
    patterns = (
        r"(?i:\borganis(?:é|ée) par\s+(?:la\s+|le\s+|les\s+|l['’]\s*)?)([A-ZÀ-ÖØ-Þ][^,.;]{2,80})",
        r"(?i:\bpropos(?:é|ée) par\s+(?:la\s+|le\s+|les\s+|l['’]\s*)?)([A-ZÀ-ÖØ-Þ][^,.;]{2,80})",
        r"(?i:\borganisateur(?:rice)?\s*[:\-]\s*)([A-ZÀ-ÖØ-Þ][^,.;]{2,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = _cut_clean_text(match.group(1), 80).strip(" .,:;-")
            value = re.split(r"(?i)\s+(?:et|avec)\s+(?:le|la|les|l['’])?\s*soutien\b", value, maxsplit=1)[0]
            return value.strip(" .,:;-")
    return ""


def schema_performer(event: dict) -> str:
    """Artiste/intervenant uniquement lorsqu'un nom propre est explicitement présent."""
    explicit = clean(event.get("performer"))
    if explicit:
        return explicit

    text = " . ".join((clean(event.get("title")), clean(event.get("description"))))
    token = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÿ'’.-]{1,}"
    name = rf"({token}(?:\s+{token}){{1,3}})"
    patterns = (
        rf"(?i:\b(?:avec|anim(?:é|ée) par|interpr(?:été|étée) par)\s+){name}",
        rf"(?:^|[.!?]\s+)(?i:par)\s+{name}",
        rf"(?i:\b(?:concert|récital|spectacle|exposition|conférence))(?:\s+[a-zà-ÿœ'’\-]+){{0,3}}\s+(?i:de|par)\s+{name}",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return clean(match.group(1)).strip(" .,:;-")
    return ""


def event_facts(event: dict) -> dict:
    return {
        "title": clean(event.get("title")),
        "commune": clean(event.get("commune")),
        "start_date": clean(event.get("start_date")),
        "end_date": clean(event.get("end_date")),
        "dates_horaires_source": clean(event.get("opening")),
        "adresse": clean(event.get("address")),
        "description_source": compact_description(event.get("description", "")),
        "tarifs": clean(event.get("tariffs")),
        "contact": clean(event.get("contact"))[:1200],
        "source_url": clean(event.get("source_url") or event.get("url")),
    }


def event_hash(event: dict) -> str:
    payload = {
        "facts": event_facts(event),
        "prompt_version": PROMPT_VERSION,
        "model": OPENAI_MODEL,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fallback_editorial(event: dict) -> dict:
    title = clean(event.get("title"))
    commune = clean(event.get("commune"))
    desc = clean(event.get("description"))
    place = f" à {commune}" if commune else ""
    lead = desc[:560].rstrip(" .") if desc else f"{title} est annoncé{place}. Retrouvez ci-dessous les informations pratiques publiées par la source de l’événement."
    if lead and not lead.endswith("."):
        lead += "."
    address = concise_address(event)
    return {
        "seo_title": f"{title}{' à ' + commune if commune else ''}"[:68],
        "meta_description": (f"{title}{place} : dates, lieu et informations pratiques pour préparer votre sortie.")[:158],
        "lead": lead,
        "discover_title": "Ce que vous pourrez découvrir",
        "discover_text": desc[:950] if desc else lead,
        "why_text": f"Ce rendez-vous peut être une idée de sortie{place}. Les informations utiles sont regroupées ici pour vérifier rapidement la date, le lieu et les conditions annoncées.",
        "practical_text": date_label(event) + (f". {address}" if address else ""),
        "question": f"Irez-vous découvrir {title}{place} ?",
    }


def generate_editorial(event: dict) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or OpenAI is None:
        return fallback_editorial(event)

    schema = {
        "type": "object",
        "properties": {
            "seo_title": {"type": "string"},
            "meta_description": {"type": "string"},
            "lead": {"type": "string"},
            "discover_title": {"type": "string"},
            "discover_text": {"type": "string"},
            "why_text": {"type": "string"},
            "practical_text": {"type": "string"},
            "question": {"type": "string"},
        },
        "required": [
            "seo_title", "meta_description", "lead", "discover_title",
            "discover_text", "why_text", "practical_text", "question"
        ],
        "additionalProperties": False,
    }

    facts = event_facts(event)
    system = (
        "Tu es rédacteur local francophone spécialisé dans les sorties, le tourisme et le SEO utile. "
        "Tu écris une fiche événement dans le même esprit éditorial que l'agenda de Nyons de Vivre à Nyons : "
        "chaleureux, clair, local, naturel, sans ton publicitaire ni phrases creuses. "
        "RÈGLES ABSOLUES : utilise uniquement les faits fournis ; n'invente jamais un horaire, un tarif, "
        "une activité, un public, une réservation, un programme, un lieu ou une caractéristique absente. "
        "Ne recopie pas mot pour mot la description source : reformule réellement. "
        "Si l'information est pauvre, fais plus court au lieu d'inventer. "
        "Le texte doit ressembler aux fiches de agenda.vivreanyons.fr : un chapeau, une partie découverte, "
        "une partie 'pourquoi cela peut valoir le détour', puis des informations pratiques. "
        "Évite les expressions typiques d'IA, les superlatifs gratuits et les répétitions. "
        "SEO : le titre doit naturellement contenir le nom de l'événement et la commune quand elle est connue. "
        "La meta description doit faire idéalement 145 à 160 caractères. "
        "Longueur totale éditoriale souhaitée : environ 250 à 430 mots seulement si les faits le permettent."
    )
    user = (
        "Rédige la fiche à partir de ces faits structurés. Le champ practical_text doit synthétiser uniquement "
        "les informations pratiques présentes. Le champ question doit être une vraie question simple au lecteur.\n\n"
        + json.dumps(facts, ensure_ascii=False, indent=2)
    )

    client = OpenAI(api_key=api_key)
    response = client.responses.create(
        model=OPENAI_MODEL,
        store=False,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "agenda_drome_event_page",
                "strict": True,
                "schema": schema,
            }
        },
    )
    return json.loads(response.output_text)


STYLE = """
*{box-sizing:border-box}:root{--olive:#566b3a;--olive-dark:#354622;--terracotta:#a94f35;--cream:#f6f1e8;--paper:#fffdf9;--ink:#262722;--muted:#686b63;--line:#e4dccd;--shadow:0 12px 34px rgba(52,48,38,.10)}
body{margin:0;font-family:Arial,Helvetica,sans-serif;background:var(--cream);color:var(--ink);line-height:1.72}a{color:var(--olive-dark)}
.top{max-width:1180px;margin:auto;padding:15px 18px 0}.ad-shell{background:#fff;border:1px solid var(--line);border-radius:16px;overflow:hidden;box-shadow:var(--shadow)}.ad-frame{display:block;width:100%;aspect-ratio:3/1;border:0}.ad-note{font-size:11px;text-align:right;color:#777;margin:6px 4px 0}
.wrap{max-width:980px;margin:auto;padding:18px 18px 64px}.nav{display:flex;gap:9px;flex-wrap:wrap;margin:8px 0 18px}.nav a{padding:9px 13px;background:#fff;border:1px solid var(--line);border-radius:999px;text-decoration:none;font-weight:800;font-size:13px}
.hero{background:linear-gradient(125deg,var(--olive-dark),var(--olive) 62%,#788d58);color:#fff;border-radius:24px;padding:clamp(27px,5vw,52px);box-shadow:var(--shadow)}
.status{display:inline-block;padding:6px 10px;border-radius:999px;background:rgba(255,255,255,.15);font-size:12px;font-weight:900;text-transform:uppercase;letter-spacing:.04em}h1{font-size:clamp(31px,5vw,50px);line-height:1.08;margin:.35em 0 .3em}.date{font-size:18px;font-weight:800;margin:0 0 8px}.cats{opacity:.9;font-size:14px}
.event-photo{margin:22px 0 0;background:#fff;border:1px solid var(--line);border-radius:18px;overflow:hidden;box-shadow:var(--shadow)}.event-photo img{display:block;width:100%;max-height:620px;object-fit:cover}.event-photo figcaption{padding:9px 14px;font-size:12px;color:var(--muted)}
.lead{font-size:19px;background:var(--paper);border-left:5px solid var(--terracotta);padding:22px 24px;border-radius:16px;margin:24px 0;box-shadow:0 6px 22px rgba(52,48,38,.055)}
.section{background:var(--paper);border:1px solid var(--line);border-radius:18px;padding:22px 24px;margin:18px 0}.section h2{margin:0 0 9px;font-size:25px;line-height:1.2}.section p{margin:0 0 10px}.section p:last-child{margin-bottom:0}.practical-box{border-top:5px solid var(--terracotta)}.info-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:14px}.info-row{display:flex;gap:11px;align-items:flex-start;background:#fff;border:1px solid var(--line);border-radius:14px;padding:14px;min-width:0}.info-row span{flex:0 0 24px;font-size:19px}.info-row a{overflow-wrap:anywhere}.info-date{grid-column:1/-1;background:#f6f0e4}
.event-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}.event-action{display:inline-flex;align-items:center;justify-content:center;min-height:46px;padding:11px 16px;border-radius:12px;background:var(--olive-dark);color:#fff;text-decoration:none;font-weight:900}.event-action.route{background:var(--terracotta)}
.question{background:#efe7cf;border-radius:18px;padding:22px 24px;margin:20px 0;font-weight:800;font-size:18px}
.anecdote{border-left:5px solid var(--terracotta);background:#fff8ec}.source-note{font-size:12px;color:var(--muted);margin-top:14px!important}.source-note a{font-weight:700}
.around-intro{color:var(--muted);margin-bottom:14px!important}
.related{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-top:14px}.related-card{display:flex;flex-direction:column;gap:6px;padding:16px;background:#fff;border:1px solid var(--line);border-radius:14px;text-decoration:none}.related-card span{font-size:13px;color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:18px;text-decoration:none}.card strong{display:block;font-size:18px}.card span{font-size:13px;color:var(--muted)}
@media(max-width:720px){.related,.info-grid,.cards{grid-template-columns:1fr}.info-date{grid-column:auto}.top{padding:9px 9px 0}.wrap{padding:10px 11px 45px}.hero{border-radius:18px;padding:24px 20px}.lead,.section{padding:18px}}
"""

INDEX_STYLE = STYLE + """
.village-picker{margin-top:18px}.village-picker h2{margin-bottom:7px}.village-picker-intro{color:var(--muted);margin-bottom:15px!important}
.village-select-label{display:block;font-weight:900;margin-bottom:7px}.village-select{width:100%;padding:13px 14px;border:1px solid var(--line);border-radius:12px;background:#fff;color:var(--ink);font:inherit;font-weight:800}
.village-links{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}.village-filter{display:inline-flex;align-items:center;gap:6px;padding:8px 11px;border:1px solid var(--line);border-radius:999px;background:#fff;text-decoration:none;font-size:13px;font-weight:800}.village-filter span{display:inline-grid;place-items:center;min-width:24px;height:24px;padding:0 6px;border-radius:999px;background:#eef1e8;color:var(--olive-dark);font-size:12px}.village-filter[aria-pressed="true"]{background:var(--olive-dark);border-color:var(--olive-dark);color:#fff}.village-filter[aria-pressed="true"] span{background:rgba(255,255,255,.18);color:#fff}
.filter-status{margin:14px 0 0!important;font-size:14px;font-weight:800;color:var(--olive-dark)}.village-section{scroll-margin-top:14px}.village-section h2{display:flex;align-items:center;justify-content:space-between;gap:12px}.village-count{white-space:nowrap;font-size:13px;color:var(--muted);font-weight:800}.village-section[hidden]{display:none}
@media(max-width:720px){.village-links{display:none}.village-picker{position:sticky;top:0;z-index:5;box-shadow:0 8px 24px rgba(52,48,38,.10)}.village-section h2{align-items:flex-start;flex-direction:column;gap:4px}}
"""


def event_slug(event: dict) -> str:
    return f"{slugify(event.get('title', 'evenement'))}-{clean(event.get('start_date')) or 'date'}"


def related_events(current: dict, events: list[dict], slug_map: dict[str, str]) -> list[dict]:
    current_date = iso_date(current.get("start_date", "")) or date.max
    others = [e for e in events if e is not current and clean(e.get("url")) != clean(current.get("url"))]

    def score(e):
        d = iso_date(e.get("start_date", "")) or date.max
        delta = abs((d - current_date).days) if d != date.max and current_date != date.max else 99999
        same_commune = 0 if clean(e.get("commune")).lower() == clean(current.get("commune")).lower() else 1
        return (same_commune, delta, clean(e.get("title")).lower())

    return sorted(others, key=score)[:3]


def assign_anecdotes(events: list[dict], catalog: dict) -> dict[str, dict]:
    """Attribue une anecdote réelle et différente à chaque fiche, sans recyclage."""
    positions: dict[str, int] = {}
    assigned: dict[str, dict] = {}
    seen_texts: set[str] = set()

    for event in events:
        commune = clean(event.get("commune"))
        key = clean(event.get("url"))
        choices = catalog.get(commune)
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(f"Aucune anecdote sourcée disponible pour {commune or key}")

        index = positions.get(commune, 0)
        if index >= len(choices):
            raise RuntimeError(
                f"Pas assez d'anecdotes uniques pour {commune}: "
                f"{index + 1} fiches mais seulement {len(choices)} anecdotes"
            )

        anecdote = choices[index]
        if not isinstance(anecdote, dict):
            raise RuntimeError(f"Anecdote invalide pour {commune}, position {index + 1}")
        text = clean(anecdote.get("text"))
        source_url = clean(anecdote.get("source_url"))
        if not text or not source_url:
            raise RuntimeError(f"Texte ou source manquant pour {commune}, position {index + 1}")
        if text in seen_texts:
            raise RuntimeError(f"Anecdote dupliquée détectée pour {commune}")

        assigned[key] = anecdote
        seen_texts.add(text)
        positions[commune] = index + 1

    if len(assigned) != len(events):
        raise RuntimeError("Chaque fiche doit recevoir exactement une anecdote")
    return assigned


def ics_escape(value: str) -> str:
    return clean(value).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")


def calendar_href(event: dict, canonical: str) -> str:
    """Construit un fichier calendrier universel sans rien stocker sur le site."""
    start = iso_date(event.get("start_date", ""))
    end = iso_date(event.get("end_date", "")) or start
    if not start or not end:
        return ""

    stamp = iso_date(clean(event.get("last_update"))[:10]) or start
    location = concise_address(event) or clean(event.get("commune"))
    uid_seed = clean(event.get("url")) or canonical
    uid = hashlib.sha256(uid_seed.encode("utf-8")).hexdigest()[:24]
    description = f"Fiche : {canonical}"
    source_url = clean(event.get("source_url") or event.get("url"))
    if source_url:
        description += f" | Source : {source_url}"

    content = "\r\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Vivre à Nyons//Agenda Drôme//FR",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}@drome.vivreanyons.fr",
        f"DTSTAMP:{stamp.strftime('%Y%m%d')}T000000Z",
        f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(end + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{ics_escape(event.get('title'))}",
        f"LOCATION:{ics_escape(location)}",
        f"DESCRIPTION:{ics_escape(description)}",
        f"URL:{canonical}",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    return "data:text/calendar;charset=utf-8," + quote(content, safe="")


def render_page(
    event: dict,
    editorial: dict,
    events: list[dict],
    slug_map: dict[str, str],
    anecdote: dict,
) -> str:
    slug = slug_map[clean(event.get("url"))]
    canonical = urljoin(SITE, f"evenements/{slug}/")
    source_url = clean(event.get("source_url") or event.get("url"))
    title = clean(event.get("title"))
    commune = clean(event.get("commune"))
    address = concise_address(event)
    tariffs = clean(event.get("tariffs"))
    contact = clean(event.get("contact"))
    phone = extract_phone(contact)
    email = extract_email(contact)
    date_text = date_label(event)
    header_date = date_summary(event)
    seo_title = clean(editorial.get("seo_title")) or f"{title} à {commune}".strip()
    meta = clean(editorial.get("meta_description")) or fallback_editorial(event)["meta_description"]
    status = event_status(event)
    image_url = clean(event.get("image_url"))
    image_credit = clean(event.get("image_credit"))
    image_rights = clean(event.get("image_rights"))

    photo_html = ""
    if image_url:
        credit_bits = [bit for bit in (image_credit, image_rights) if bit]
        caption = f'<figcaption>Photo : {esc(" · ".join(credit_bits))}</figcaption>' if credit_bits else ""
        photo_html = (
            '<figure class="event-photo">'
            f'<img src="{esc(image_url)}" alt="{esc(title + (" à " + commune if commune else ""))}" '
            'loading="lazy" decoding="async">'
            f'{caption}</figure>'
        )

    actions = []
    cal_href = calendar_href(event, canonical)
    if cal_href:
        actions.append(
            f'<a class="event-action" href="{esc(cal_href)}" download="{esc(slug)}.ics">'
            '📅 Ajouter à mon calendrier</a>'
        )
    destination = address or (f"{commune}, Drôme, France" if commune else "")
    if destination:
        route_url = "https://www.google.com/maps/dir/?api=1&destination=" + quote_plus(destination)
        actions.append(
            f'<a class="event-action route" href="{esc(route_url)}" target="_blank" '
            'rel="noopener noreferrer">🧭 Voir l’itinéraire</a>'
        )
    actions_html = f'<div class="event-actions">{"".join(actions)}</div>' if actions else ""

    anecdote_html = (
        '<section class="section anecdote"><h2>💡 Le savais-tu sur '
        f'{esc(commune or "ce village")} ?</h2>'
        f'<p>{esc(anecdote.get("text"))}</p>'
        '<p class="source-note">Source : '
        f'<a href="{esc(anecdote.get("source_url"))}" target="_blank" rel="noopener noreferrer">'
        f'{esc(anecdote.get("source_label") or "référence historique")}</a></p></section>'
    )

    info = [f'<div class="info-row info-date"><span>📅</span><div><strong>Dates et horaires</strong><br>{esc(date_text)}</div></div>']
    if address:
        info.append(f'<div class="info-row"><span>🗺️</span><div><strong>Adresse</strong><br>{esc(address)}</div></div>')
    elif commune:
        info.append(f'<div class="info-row"><span>📍</span><div><strong>Commune</strong><br>{esc(commune)}</div></div>')
    if phone:
        tel_href = re.sub(r"[^+\d]", "", phone)
        info.append(f'<div class="info-row"><span>☎️</span><div><strong>Téléphone</strong><br><a href="tel:{esc(tel_href)}">{esc(phone)}</a></div></div>')
    if email:
        info.append(f'<div class="info-row"><span>✉️</span><div><strong>Email</strong><br><a href="mailto:{esc(email)}">{esc(email)}</a></div></div>')
    if tariffs:
        info.append(f'<div class="info-row"><span>💶</span><div><strong>Tarifs</strong><br>{esc(_cut_clean_text(tariffs, 360))}</div></div>')

    rel_html = []
    for e in related_events(event, events, slug_map):
        eurl = clean(e.get("url"))
        eslug = slug_map.get(eurl)
        if not eslug:
            continue
        rel_html.append(
            f'<a class="related-card" href="{esc(urljoin(SITE, f"evenements/{eslug}/"))}">'
            f'<strong>{esc(e.get("title"))}</strong>'
            f'<span>📍 {esc(e.get("commune") or "Drôme")}</span>'
            f'<span>📅 {esc(date_summary(e))}</span>'
            f'</a>'
        )

    around_intro = (
        f"Après « {title} » à {commune or 'la Drôme'}, annoncé {date_text}, voici trois autres idées prises dans cet agenda. "
        "La commune et la date sont indiquées sur chaque proposition pour choisir facilement."
    )

    organizer = schema_organizer(event)
    performer = schema_performer(event)
    offer = schema_offer(event, source_url, canonical)

    event_ld = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": title,
        "startDate": clean(event.get("start_date")),
        "endDate": clean(event.get("end_date")) or clean(event.get("start_date")),
        "url": canonical,
        "sameAs": source_url,
        "eventStatus": "https://schema.org/EventScheduled",
        "description": meta,
        "location": {
            "@type": "Place",
            "name": commune or address or "Drôme Provençale",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": address,
                "addressLocality": commune,
                "addressCountry": "FR",
            },
        },
    }
    if image_url:
        event_ld["image"] = [image_url]
    if organizer:
        event_ld["organizer"] = {"@type": "Organization", "name": organizer}
    if performer:
        event_ld["performer"] = {"@type": "Person", "name": performer}
    if offer:
        event_ld["offers"] = offer
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Agenda", "item": SITE},
            {"@type": "ListItem", "position": 2, "name": "Événements", "item": urljoin(SITE, "evenements/")},
            {"@type": "ListItem", "position": 3, "name": title, "item": canonical},
        ],
    }
    event_json = json.dumps(event_ld, ensure_ascii=False).replace("</", "<\\/")
    breadcrumb_json = json.dumps(breadcrumb, ensure_ascii=False).replace("</", "<\\/")

    return f'''<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{esc(seo_title)}</title>
  <meta name="description" content="{esc(meta)}">
  <link rel="canonical" href="{esc(canonical)}">
  <meta name="robots" content="index,follow">
  <meta property="og:type" content="article">
  <meta property="og:title" content="{esc(seo_title)}">
  <meta property="og:description" content="{esc(meta)}">
  <meta property="og:url" content="{esc(canonical)}">
  {f'<meta property="og:image" content="{esc(image_url)}">' if image_url else ''}
  <script type="application/ld+json">{event_json}</script>
  <script type="application/ld+json">{breadcrumb_json}</script>
  <style>{STYLE}</style>
</head>
<body>
  <aside class="top" aria-label="Sélection de livres sur Nyons"><div class="ad-shell"><iframe class="ad-frame" src="{esc(BANNER_URL)}" loading="eager" title="Voir ma sélection de vieux livres sur Nyons"></iframe></div><div class="ad-note">Publicité · lien affilié</div></aside>
  <main class="wrap">
    <nav class="nav"><a href="{esc(SITE)}">← Agenda</a><a href="{esc(urljoin(SITE, 'evenements/'))}">📌 Tous les événements</a></nav>
    <header class="hero"><span class="status">{esc(status)}</span><h1>{esc(title)}</h1><p class="date">📅 {esc(header_date)}</p><div class="cats">{esc(commune or 'Drôme et alentours')}</div></header>
    {photo_html}

    <section class="section practical-box"><h2>📌 Infos pratiques</h2><div class="info-grid">{''.join(info)}</div>{actions_html}</section>
    <div class="lead">{esc(editorial.get('lead'))}</div>
    <section class="section"><h2>🖼️ {esc(editorial.get('discover_title') or 'Ce que vous pourrez découvrir')}</h2><p>{esc(editorial.get('discover_text'))}</p></section>
    <section class="section"><h2>👀 Pourquoi cette sortie peut valoir le détour</h2><p>{esc(editorial.get('why_text'))}</p></section>
    {anecdote_html}
    <section class="section"><h2>ℹ️ Informations pratiques</h2><p>{esc(editorial.get('practical_text'))}</p></section>
    <div class="question">💬 {esc(editorial.get('question'))}</div>
    <section class="section"><h2>🧭 À découvrir autour</h2><p class="around-intro">{esc(around_intro)}</p><div class="related">{''.join(rel_html)}</div></section>
  </main>
</body>
</html>'''


def render_index(events: list[dict], slug_map: dict[str, str]) -> str:
    def alpha_key(value: str) -> str:
        raw = unicodedata.normalize("NFKD", clean(value))
        return "".join(ch for ch in raw if not unicodedata.combining(ch)).casefold()

    by_village: dict[str, list[dict]] = {}
    for event in events:
        village = clean(event.get("commune")) or "Village non précisé"
        by_village.setdefault(village, []).append(event)

    villages = sorted(by_village, key=alpha_key)
    options = []
    filters = [
        f'<a class="village-filter" href="#tous-les-villages" data-village-filter="" '
        f'aria-pressed="true">Tous les villages <span>{len(events)}</span></a>'
    ]
    sections = []

    for village in villages:
        village_slug = slugify(village, 60)
        village_events = sorted(
            by_village[village],
            key=lambda event: (
                clean(event.get("start_date")) or "9999-12-31",
                alpha_key(event.get("title")),
            ),
        )
        count = len(village_events)
        label = "1 sortie" if count == 1 else f"{count} sorties"
        options.append(
            f'<option value="{esc(village_slug)}">{esc(village)} ({count})</option>'
        )
        filters.append(
            f'<a class="village-filter" href="#village-{esc(village_slug)}" '
            f'data-village-filter="{esc(village_slug)}" aria-pressed="false">'
            f'{esc(village)} <span>{count}</span></a>'
        )

        cards = []
        for event in village_events:
            event_slug_value = slug_map[clean(event.get("url"))]
            cards.append(
                f'<a class="card" href="{esc(urljoin(SITE, f"evenements/{event_slug_value}/"))}">'
                f'<strong>{esc(event.get("title"))}</strong>'
                f'<span>📅 {esc(date_summary(event))}</span>'
                f'<span>📍 {esc(village)}</span></a>'
            )
        sections.append(
            f'<section class="section village-section" id="village-{esc(village_slug)}" '
            f'data-village="{esc(village_slug)}"><h2>📍 {esc(village)} '
            f'<span class="village-count">{esc(label)}</span></h2>'
            f'<div class="cards">{"".join(cards)}</div></section>'
        )

    script = """
<script>
(function () {
  const select = document.getElementById('village-select');
  const status = document.getElementById('filter-status');
  const sections = Array.from(document.querySelectorAll('.village-section'));
  const filters = Array.from(document.querySelectorAll('[data-village-filter]'));

  function showVillage(value, changeHash) {
    let shown = 0;
    sections.forEach(function (section) {
      const visible = !value || section.dataset.village === value;
      section.hidden = !visible;
      if (visible) shown += section.querySelectorAll('.card').length;
    });
    filters.forEach(function (filter) {
      filter.setAttribute('aria-pressed', filter.dataset.villageFilter === value ? 'true' : 'false');
    });
    select.value = value;
    status.textContent = shown + (shown > 1 ? ' événements affichés' : ' événement affiché');
    if (changeHash) {
      history.replaceState(null, '', value ? '#village-' + value : '#tous-les-villages');
    }
  }

  select.addEventListener('change', function () { showVillage(select.value, true); });
  filters.forEach(function (filter) {
    filter.addEventListener('click', function (event) {
      event.preventDefault();
      showVillage(filter.dataset.villageFilter, true);
      document.getElementById('classement-villages').scrollIntoView({behavior: 'smooth'});
    });
  });

  const initial = location.hash.indexOf('#village-') === 0
    ? location.hash.replace('#village-', '')
    : '';
  showVillage(sections.some(function (section) { return section.dataset.village === initial; }) ? initial : '', false);
})();
</script>
"""

    count = len(events)
    return f'''<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agenda autour de Nyons : {count} événements en cours et à venir</title><meta name="description" content="Agenda des événements classés par village autour de Nyons : sorties, culture, fêtes, spectacles et loisirs."><link rel="canonical" href="{esc(urljoin(SITE, 'evenements/'))}"><meta name="robots" content="index,follow"><style>{INDEX_STYLE}</style></head><body><aside class="top" aria-label="Sélection de livres sur Nyons"><div class="ad-shell"><iframe class="ad-frame" src="{esc(BANNER_URL)}" loading="eager" title="Voir ma sélection de vieux livres sur Nyons"></iframe></div><div class="ad-note">Publicité · lien affilié</div></aside><main class="wrap"><nav class="nav"><a href="{esc(SITE)}">← Accueil</a></nav><header class="hero"><span class="status">Agenda</span><h1>{count} événements autour de Nyons</h1><p class="date">Les villages alentour sont à l’honneur.</p></header><section class="section village-picker" id="classement-villages"><h2>🏘️ Classement par village</h2><p class="village-picker-intro">Choisissez un village pour afficher uniquement ses sorties.</p><label class="village-select-label" for="village-select">Choisir un village</label><select class="village-select" id="village-select"><option value="">Tous les villages ({count})</option>{''.join(options)}</select><div class="village-links" aria-label="Villages classés par ordre alphabétique">{''.join(filters)}</div><p class="filter-status" id="filter-status" aria-live="polite">{count} événements affichés</p></section><div id="liste-villages">{''.join(sections)}</div></main>{script}</body></html>'''


def write_sitemap(events: list[dict], slug_map: dict[str, str]) -> None:
    urls = [SITE, urljoin(SITE, "evenements/")]
    urls += [urljoin(SITE, f"evenements/{slug_map[clean(e.get('url'))]}/") for e in events]
    now = datetime.now(timezone.utc).date().isoformat()
    body = "\n".join(f"  <url><loc>{html.escape(u)}</loc><lastmod>{now}</lastmod></url>" for u in urls)
    SITEMAP.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}\n</urlset>\n', encoding="utf-8")
    ROBOTS.write_text(f"User-agent: *\nAllow: /\nSitemap: {urljoin(SITE, 'sitemap.xml')}\n", encoding="utf-8")


def main() -> None:
    payload = load_json(AGENDA, {})
    events = payload.get("events", []) if isinstance(payload, dict) else []
    if not events:
        raise RuntimeError("agenda.json ne contient aucun événement")

    anecdote_catalog = load_json(ANECDOTES_FILE, {})
    if not isinstance(anecdote_catalog, dict):
        raise RuntimeError("village_anecdotes.json est invalide")
    anecdotes = assign_anecdotes(events, anecdote_catalog)

    cache = load_json(CACHE_FILE, {})
    if not isinstance(cache, dict):
        cache = {}

    slug_map: dict[str, str] = {}
    used = set()
    for event in events:
        key = clean(event.get("url"))
        base = event_slug(event)
        slug = base
        if slug in used:
            slug = f"{base}-{slugify(event.get('commune', 'lieu'), 35)}"
        used.add(slug)
        slug_map[key] = slug

    EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    wanted_dirs = set(slug_map.values())
    for child in EVENTS_DIR.iterdir():
        if child.is_dir() and child.name not in wanted_dirs:
            shutil.rmtree(child)

    ai_calls = 0
    cache_hits = 0
    fallbacks = 0

    for i, event in enumerate(events, start=1):
        key = clean(event.get("url"))
        h = event_hash(event)
        entry = cache.get(key)
        editorial = None
        if isinstance(entry, dict) and entry.get("hash") == h and isinstance(entry.get("editorial"), dict):
            editorial = entry["editorial"]
            cache_hits += 1
        else:
            if ai_calls < MAX_EVENT_AI_CALLS:
                try:
                    editorial = generate_editorial(event)
                    if os.getenv("OPENAI_API_KEY", "").strip() and OpenAI is not None:
                        ai_calls += 1
                    else:
                        fallbacks += 1
                except Exception as exc:
                    print(f"IA ERREUR {key}: {exc}")
                    editorial = fallback_editorial(event)
                    fallbacks += 1
            else:
                editorial = fallback_editorial(event)
                fallbacks += 1
            cache[key] = {"hash": h, "editorial": editorial, "updated_at": datetime.now(timezone.utc).isoformat()}

        out_dir = EVENTS_DIR / slug_map[key]
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "index.html").write_text(
            render_page(event, editorial, events, slug_map, anecdotes[key]),
            encoding="utf-8",
        )
        print(f"FICHE {i:03d}/{len(events)}: {slug_map[key]}")

    (EVENTS_DIR / "index.html").write_text(render_index(events, slug_map), encoding="utf-8")
    save_json(CACHE_FILE, cache)
    write_sitemap(events, slug_map)

    print("=== FICHES ÉVÉNEMENTS ===")
    print(f"Fiches générées : {len(events)}")
    print(f"Cache éditorial : {cache_hits}")
    print(f"Appels OpenAI    : {ai_calls}")
    print(f"Textes secours   : {fallbacks}")
    print("OK: pages événements, sitemap.xml et robots.txt prêts.")


if __name__ == "__main__":
    main()
