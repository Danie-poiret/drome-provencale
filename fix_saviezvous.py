#!/usr/bin/env python3
"""Crée un « Le savais-tu ? » unique et lié au sujet de chaque événement.

Les faits sont conservés dans _saviezvous_cache.json. Une fiche dont le sujet
n'a pas changé réutilise son texte et n'occasionne aucun nouvel appel à l'API.
Le script ne modifie que le bloc « Le savais-tu ? » des fiches HTML.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


ROOT = Path(__file__).resolve().parent
AGENDA = ROOT / "agenda.json"
EVENT_CACHE = ROOT / "_event_seo_cache.json"
FACT_CACHE = ROOT / "_saviezvous_cache.json"
MANUAL_FACTS = ROOT / "saviezvous_drome.json"
EVENTS_DIR = ROOT / "evenements"
FACT_VERSION = 1
BATCH_SIZE = int(os.getenv("SAVIEZVOUS_BATCH_SIZE", "25"))
MAX_API_CALLS = int(os.getenv("MAX_SAVIEZVOUS_API_CALLS", "8"))
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-terra")


STOPWORDS = {
    "administratif", "ancien", "ancienne", "apres", "atelier", "avec",
    "cadre", "cette", "dans", "depuis", "drome", "enfance", "entre",
    "evenement", "faire", "fete", "histoire", "journee", "jours", "leur",
    "leurs", "loisirs", "maison", "marche", "pendant", "place", "pour",
    "propose", "rendez", "sortie", "ville", "village", "vous", "envie",
    "decouvrir", "hebdomadaire", "visite", "guidee", "exposition",
}

THEME_GROUPS = (
    {"photo", "photographie", "photographe", "image", "objectif", "faune"},
    {"exposition", "art", "oeuvre", "portrait", "peinture", "dessin", "sculpture"},
    {"marche", "producteur", "etal", "commerce", "forain", "terroir"},
    {"jeu", "piste", "enquete", "enigme", "indice", "patrimoine"},
    {"automobile", "vehicule", "voiture", "mecanique", "moteur", "restauration"},
    {"chocolat", "chocolaterie", "cacao", "ganache", "confiserie"},
    {"noix", "noyer", "lavande", "agriculture", "culture", "ferme"},
    {"brocante", "grenier", "chine", "occasion", "reemploi", "ressourcerie"},
    {"chevre", "chevrerie", "fromage", "traite", "elevage", "miel"},
    {"huile", "olive", "moulin", "distillerie", "spiritueux", "alambic"},
    {"geologie", "roche", "gorge", "fossile", "sediment", "calcaire"},
    {"gravure", "linogravure", "tampon", "encre", "matrice", "impression"},
    {"libellule", "insecte", "zone", "humide", "odonate"},
    {"sport", "mental", "sante", "activite", "physique", "senior"},
    {"vin", "vigne", "vendange", "vinification", "cave", "cepage"},
    {"piano", "recital", "masterclass", "clavier", "musique", "concert"},
    {"velo", "bicyclette", "cyclisme", "deux", "roues"},
    {"riviere", "souterraine", "eau", "legende", "tresor"},
    {"fleur", "ceramique", "dessin", "peinture", "bronze", "art"},
    {"pompier", "incendie", "secours", "sapeur"},
    {"sevigne", "marquise", "epistolaire", "lettre", "carrosse"},
    {"patisserie", "gateau", "dessert", "cuisine", "gourmand"},
    {"parfum", "hongrie", "fragonard", "arome", "olfactif"},
    {"foret", "arbre", "sensoriel", "ecoute", "biodiversite"},
    {"biscuit", "biscuiterie", "four", "farine", "cuisson"},
    {"vautour", "rapace", "ornithologie", "oiseau", "reintroduction"},
    {"chateau", "medieval", "moyen", "age", "architecture"},
    {"animal", "garenne", "gibier", "parc", "nature"},
    {"soie", "textile", "tissu", "vetement", "fibre"},
    {"danse", "tango", "rock", "salsa", "valse", "chacha"},
    {"golf", "club", "balle", "parcours", "green"},
    {"livre", "bibliotheque", "lecture", "ecrivain", "litterature"},
    {"cirque", "baignoire", "clown", "acrobatie", "spectacle"},
    {"bien", "etre", "relaxation", "detente"},
    {"randonnee", "marche", "sentier", "nordique", "hiking"},
    {"train", "locomotive", "vapeur", "modelisme", "ferroviaire"},
    {"societe", "plateau", "ludique", "jeu", "cartes"},
    {"saveur", "degustation", "gastronomie", "gourmande", "aliment"},
    {"jazz", "improvisation", "rythme", "swing", "musique"},
    {"trail", "course", "denivele", "sentier", "endurance"},
    {"chambre", "romantique", "trio", "instrument", "musique"},
    {"automne", "coing", "courge", "fruit", "saison"},
)


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def norm(value) -> str:
    text = unicodedata.normalize("NFKD", clean(value).lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", text))


def terms(value) -> set[str]:
    result = set()
    for word in norm(value).split():
        if len(word) < 4 or word in STOPWORDS:
            continue
        result.add(word)
        if len(word) > 5 and word.endswith("s"):
            result.add(word[:-1])
    return result


def fact_key(text) -> str:
    return re.sub(r"\s+", " ", norm(text)).strip()


def slugify(text: str, max_len: int = 90) -> str:
    raw = unicodedata.normalize("NFKD", clean(text))
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
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
        used.add(slug)
        result[key] = slug
    return result


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def event_context(item: dict) -> str:
    event = item.get("event") or {}
    editorial = item.get("editorial") or {}
    return " ".join([
        clean(event.get("title", "")),
        clean(editorial.get("question", "")),
        clean(event.get("description", "")),
        clean(editorial.get("seo_title", "")),
        clean(editorial.get("meta_description", "")),
        clean(editorial.get("lead", "")),
        clean(editorial.get("discover_title", "")),
        clean(editorial.get("discover_text", "")),
        clean(editorial.get("why_text", "")),
    ])


def context_hash(item: dict) -> str:
    return hashlib.sha256(event_context(item).encode("utf-8")).hexdigest()


def valid_fact(text) -> bool:
    text = clean(text)
    count = len(text.split())
    return (
        18 <= count <= 75
        and not re.search(r"https?://|www\.", text, re.I)
        and not re.search(r"\b(peut-être|probablement|il semble|on peut supposer)\b", text, re.I)
    )


def topical_fact(item: dict, text: str) -> bool:
    context = event_context(item)
    if terms(context) & terms(text):
        return True
    context_words = set(norm(context).split())
    fact_words = set(norm(text).split())
    return any(context_words & group and fact_words & group for group in THEME_GROUPS)


def batch_payload(batch):
    result = []
    for _url, slug, item in batch:
        event = item.get("event") or {}
        editorial = item.get("editorial") or {}
        result.append({
            "event_key": slug,
            "title": clean(event.get("title", "")),
            "reader_question": clean(editorial.get("question", "")),
            "summary": clean(event.get("description", ""))[:900],
            "editorial_context": clean(" ".join([
                editorial.get("seo_title", ""),
                editorial.get("meta_description", ""),
                editorial.get("lead", ""),
                editorial.get("discover_title", ""),
                editorial.get("discover_text", ""),
            ]))[:1200],
        })
    return result


def generate_batch(batch, used_texts):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or OpenAI is None:
        return {}

    schema = {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "event_key": {"type": "string"},
                        "text": {"type": "string"},
                    },
                    "required": ["event_key", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["facts"],
        "additionalProperties": False,
    }
    system = (
        "Tu es fact-checkeur et médiateur culturel. Pour chaque événement, écris un fait "
        "pédagogique en français, réel, prudent et directement lié à son sujet principal. "
        "N'invente jamais un fait sur l'événement, son programme, son lieu ou une personne nommée. "
        "Choisis seulement un fait général solidement établi. Pour un titre artistique obscur, "
        "utilise un fait certain sur la discipline clairement indiquée par le résumé. "
        "N'attribue aucun bienfait médical non démontré."
    )
    prompt = (
        "Produis exactement un texte pour chaque event_key.\n"
        "Le vrai sujet doit être déduit en priorité de title et reader_question, puis confirmé "
        "par summary et editorial_context. Reader_question est un indice : ne la recopie pas.\n\n"
        "Règles absolues :\n"
        "- environ 25 à 55 mots ;\n"
        "- fait général vérifiable et lien évident avec le sujet ;\n"
        "- aucune invention sur l'événement, le village ou l'artiste ;\n"
        "- pas de statistique récente, de conseil médical, d'URL ni de source inventée ;\n"
        "- tous les textes doivent être différents, y compris pour plusieurs marchés, expositions "
        "ou jeux de piste : choisis alors des angles factuels distincts ;\n"
        "- ne parle jamais du village par défaut lorsqu'un sujet plus précis existe.\n\n"
        f"TEXTES DÉJÀ UTILISÉS :\n{json.dumps(used_texts[-160:], ensure_ascii=False)}\n\n"
        f"FICHES :\n{json.dumps(batch_payload(batch), ensure_ascii=False, indent=2)}"
    )
    response = OpenAI(api_key=api_key).responses.create(
        model=OPENAI_MODEL,
        store=False,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "agenda_drome_saviezvous",
                "strict": True,
                "schema": schema,
            }
        },
    )
    data = json.loads(response.output_text)
    expected = {slug: item for _url, slug, item in batch}
    used_keys = {fact_key(text) for text in used_texts}
    result = {}
    for value in data.get("facts", []):
        slug = clean(value.get("event_key", ""))
        text = clean(value.get("text", ""))
        key = fact_key(text)
        if (
            slug in expected
            and slug not in result
            and valid_fact(text)
            and topical_fact(expected[slug], text)
            and key
            and key not in used_keys
        ):
            result[slug] = {"text": text, "source_label": ""}
            used_keys.add(key)
    return result


def load_fact_cache():
    data = load_json(FACT_CACHE, {})
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return {"version": FACT_VERSION, "entries": {}}
    return data


def stable_record(fact, item, old=None, model=None):
    old = old if isinstance(old, dict) else {}
    wanted_hash = context_hash(item)
    if (
        clean(old.get("text")) == clean(fact.get("text"))
        and clean(old.get("context_hash")) == wanted_hash
    ):
        return old
    return {
        "text": clean(fact.get("text", "")),
        "source_label": clean(fact.get("source_label", "")),
        "model": model or OPENAI_MODEL,
        "version": FACT_VERSION,
        "context_hash": wanted_hash,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def render_fact(page: Path, fact: dict) -> bool:
    section_re = re.compile(
        r'<section class="section anecdote"><h2>💡 Le savais-tu(?: sur .*?)? \?</h2>.*?</section>',
        re.S,
    )
    source = clean(fact.get("source_label", ""))
    source_html = (
        f'<p class="source-note">Repère documentaire : {html.escape(source, quote=True)}</p>'
        if source else ""
    )
    block = (
        '<section class="section anecdote"><h2>💡 Le savais-tu ?</h2>'
        f'<p>{html.escape(clean(fact["text"]), quote=True)}</p>'
        f'{source_html}</section>'
    )
    old = page.read_text(encoding="utf-8")
    new, count = section_re.subn(block, old, count=1)
    if count != 1:
        raise RuntimeError(f"Bloc Le savais-tu introuvable : {page}")
    if new != old:
        page.write_text(new, encoding="utf-8")
        return True
    return False


def main():
    payload = load_json(AGENDA, {})
    events = payload.get("events", []) if isinstance(payload, dict) else []
    if not events:
        raise RuntimeError("agenda.json ne contient aucun événement")
    editorial_cache = load_json(EVENT_CACHE, {})
    if not isinstance(editorial_cache, dict):
        editorial_cache = {}
    slugs = event_slugs(events)

    entries = []
    for event in events:
        url = clean(event.get("url"))
        cached = editorial_cache.get(url) if isinstance(editorial_cache.get(url), dict) else {}
        editorial = cached.get("editorial") if isinstance(cached.get("editorial"), dict) else {}
        entries.append((url, slugs[url], {"event": event, "editorial": editorial}))
    entries.sort(key=lambda row: (clean(row[2]["event"].get("start_date", "")), row[1]))

    fact_cache = load_fact_cache()
    cached_facts = fact_cache.get("entries", {})
    manual_facts = load_json(MANUAL_FACTS, {})
    if not isinstance(manual_facts, dict):
        manual_facts = {}
    assigned = {}
    used_keys = set()
    for url, slug, item in entries:
        manual = manual_facts.get(slug)
        if isinstance(manual, str):
            candidate = {"text": manual, "source_label": ""}
        elif isinstance(manual, dict):
            candidate = manual
        else:
            candidate = cached_facts.get(url) if isinstance(cached_facts, dict) else None
        if not isinstance(candidate, dict):
            continue
        text = clean(candidate.get("text", ""))
        key = fact_key(text)
        if (
            (manual is not None or clean(candidate.get("context_hash")) == context_hash(item))
            and valid_fact(text)
            and topical_fact(item, text)
            and key
            and key not in used_keys
        ):
            assigned[url] = {
                "text": text,
                "source_label": clean(candidate.get("source_label", "")),
            }
            used_keys.add(key)

    missing = [(url, slug, item) for url, slug, item in entries if url not in assigned]
    calls = 0
    while missing and calls < MAX_API_CALLS:
        batch = missing[:BATCH_SIZE]
        try:
            generated = generate_batch(batch, [fact["text"] for fact in assigned.values()])
        except Exception as exc:
            print(f"Erreur génération Le savais-tu : {exc}", file=sys.stderr)
            generated = {}
        calls += 1
        if not generated:
            break
        slug_to_url = {slug: url for url, slug, _item in batch}
        for slug, fact in generated.items():
            url = slug_to_url.get(slug)
            key = fact_key(fact.get("text"))
            if url and url not in assigned and key and key not in used_keys:
                assigned[url] = fact
                used_keys.add(key)
        missing = [(url, slug, item) for url, slug, item in entries if url not in assigned]

    if missing:
        sample = ", ".join(slug for _url, slug, _item in missing[:6])
        raise RuntimeError(
            f"Correction interrompue : {len(missing)} fiche(s) sans fait thématique, dont {sample}"
        )
    if len(used_keys) != len(entries):
        raise RuntimeError("Des textes Le savais-tu sont encore dupliqués")

    updated_cache = dict(cached_facts) if isinstance(cached_facts, dict) else {}
    changed = 0
    for url, slug, item in entries:
        fact = assigned[url]
        old = cached_facts.get(url) if isinstance(cached_facts, dict) else None
        model = "manual" if slug in manual_facts else OPENAI_MODEL
        record = stable_record(fact, item, old, model=model)
        updated_cache[url] = record
        page = EVENTS_DIR / slug / "index.html"
        if not page.exists():
            raise RuntimeError(f"Fiche HTML absente : {page}")
        if render_fact(page, record):
            changed += 1

    FACT_CACHE.write_text(
        json.dumps({"version": FACT_VERSION, "entries": updated_cache}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Le savais-tu : {len(entries)} faits thématiques uniques contrôlés.")
    print(f"Pages modifiées : {changed}. Appels API groupés : {calls}.")


if __name__ == "__main__":
    main()
