#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

GENERATOR = Path("generate_event_pages.py")
MARKER = "/* accueil-magazine-v1 */"

EXTRA_CSS = r'''/* accueil-magazine-v1 */
.index-hero{position:relative;overflow:hidden;padding:clamp(32px,6vw,62px);background:linear-gradient(135deg,#2f4120 0%,#526a37 58%,#7d925a 100%)}
.index-hero:after{content:"";position:absolute;right:-75px;top:-95px;width:280px;height:280px;border-radius:50%;background:rgba(255,255,255,.08)}
.index-hero>*{position:relative;z-index:1}.index-hero h1{max-width:780px;margin-bottom:14px}.hero-intro{max-width:720px;font-size:18px;opacity:.94;margin:0}
.hero-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}.hero-actions a{display:inline-flex;align-items:center;justify-content:center;min-height:46px;padding:11px 16px;border-radius:999px;background:#fff;color:var(--olive-dark);text-decoration:none;font-weight:900}.hero-actions a:last-child{background:rgba(255,255,255,.14);color:#fff;border:1px solid rgba(255,255,255,.30)}
.quick-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:18px 0}.quick-stat{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:17px 18px;box-shadow:0 7px 22px rgba(52,48,38,.05)}.quick-stat strong{display:block;font-size:25px;line-height:1;color:var(--olive-dark)}.quick-stat span{display:block;margin-top:7px;color:var(--muted);font-size:13px;font-weight:800}
.featured-section{padding:24px}.section-heading{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;margin-bottom:16px}.section-heading h2{margin:0}.section-heading p{margin:0!important;color:var(--muted);font-size:14px}
.featured-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:13px}.featured-card{display:grid;grid-template-columns:62px minmax(0,1fr);gap:14px;align-items:start;padding:16px;background:#fff;border:1px solid var(--line);border-radius:16px;text-decoration:none;transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}.featured-card:hover{transform:translateY(-2px);box-shadow:0 12px 28px rgba(52,48,38,.10);border-color:#d1c3ad}.featured-date{display:grid;place-items:center;align-content:center;min-height:62px;border-radius:13px;background:var(--terracotta);color:#fff;text-align:center}.featured-date strong{font-size:23px;line-height:1}.featured-date small{margin-top:4px;font-size:10px;font-weight:900;letter-spacing:.05em}.featured-info{min-width:0}.featured-info strong{display:block;color:var(--ink);font-size:17px;line-height:1.25;margin-bottom:8px}.featured-info span{display:block;color:var(--muted);font-size:13px}.featured-arrow{display:block!important;margin-top:8px!important;color:var(--olive-dark)!important;font-weight:900!important}
.village-picker{border-top:5px solid var(--olive)}.cards .card{transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}.cards .card:hover{transform:translateY(-2px);box-shadow:0 10px 24px rgba(52,48,38,.08);border-color:#d1c3ad}
@media(max-width:720px){.quick-stats{grid-template-columns:1fr 1fr}.quick-stat:last-child{grid-column:1/-1}.featured-grid{grid-template-columns:1fr}.section-heading{display:block}.section-heading p{margin-top:5px!important}.hero-actions a{width:100%}.village-picker{position:static!important}.index-hero{padding:29px 21px}}
'''

NEW_TAIL = r'''    count = len(events)
    village_total = len(villages)
    today = date.today()
    upcoming = [
        event for event in events
        if (iso_date(event.get("end_date", "")) or iso_date(event.get("start_date", "")) or date.min) >= today
    ]
    featured_events = sorted(
        upcoming or events,
        key=lambda event: (
            iso_date(event.get("start_date", "")) or date.max,
            alpha_key(event.get("title", "")),
        ),
    )[:6]

    featured_cards = []
    for event in featured_events:
        event_key = clean(event.get("url"))
        event_slug_value = slug_map.get(event_key)
        if not event_slug_value:
            continue
        start_date = iso_date(event.get("start_date", ""))
        if start_date:
            badge_day = f"{start_date.day:02d}"
            badge_month = MONTHS[start_date.month][:4].upper()
        else:
            badge_day = "•"
            badge_month = "DATE"
        featured_cards.append(
            f'<a class="featured-card" href="{esc(urljoin(SITE, f"evenements/{event_slug_value}/"))}">'
            f'<span class="featured-date"><strong>{esc(badge_day)}</strong><small>{esc(badge_month)}</small></span>'
            f'<span class="featured-info"><strong>{esc(event.get("title"))}</strong>'
            f'<span>📍 {esc(event.get("commune") or "Drôme")}</span>'
            f'<span>📅 {esc(date_summary(event))}</span>'
            f'<span class="featured-arrow">Découvrir →</span></span></a>'
        )

    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agenda autour de Nyons : {count} événements en cours et à venir</title><meta name="description" content="Agenda des événements dans {village_total} villages autour de Nyons : sorties, culture, fêtes, spectacles et loisirs."><link rel="canonical" href="{esc(urljoin(SITE, 'evenements/'))}"><meta name="robots" content="index,follow"><style>{INDEX_STYLE}</style></head><body><main class="wrap"><nav class="nav"><a href="{esc(SITE)}">← Accueil</a><a href="#prochaines-sorties">📅 Prochaines sorties</a><a href="#classement-villages">🏘️ Villages</a></nav><header class="hero index-hero"><span class="status">🌿 Sortir dans la Drôme</span><h1>{count} idées de sorties autour de Nyons</h1><p class="hero-intro">{village_total} villages à découvrir, des Baronnies au Diois et à la vallée du Rhône. Trouvez facilement une fête, un marché, une exposition ou une idée de balade.</p><div class="hero-actions"><a href="#prochaines-sorties">📅 Voir les prochaines sorties</a><a href="#classement-villages">🏘️ Choisir un village</a></div></header><section class="quick-stats" aria-label="Chiffres de l'agenda"><div class="quick-stat"><strong>{count}</strong><span>sorties dans l’agenda</span></div><div class="quick-stat"><strong>{village_total}</strong><span>villages représentés</span></div><div class="quick-stat"><strong>100 %</strong><span>agenda local à explorer</span></div></section><section class="section featured-section" id="prochaines-sorties"><div class="section-heading"><div><h2>✨ À découvrir prochainement</h2><p>Quelques idées parmi les prochaines dates de l’agenda.</p></div></div><div class="featured-grid">{''.join(featured_cards)}</div></section><section class="section village-picker" id="classement-villages"><h2>🏘️ Explorer les {village_total} villages</h2><p class="village-picker-intro">Choisissez un village pour afficher uniquement ses sorties.</p><label class="village-select-label" for="village-select">Choisir un village</label><select class="village-select" id="village-select"><option value="">Tous les villages ({count})</option>{''.join(options)}</select><div class="village-links" aria-label="Villages classés par ordre alphabétique">{''.join(filters)}</div><p class="filter-status" id="filter-status" aria-live="polite">{count} événements affichés</p></section><div id="liste-villages">{''.join(sections)}</div></main>{script}</body></html>"""
'''


def patch_generator() -> None:
    text = GENERATOR.read_text(encoding="utf-8")
    if MARKER not in text:
        style_anchor = 'INDEX_STYLE = STYLE + """\n'
        if style_anchor not in text:
            raise SystemExit("INDEX_STYLE introuvable")
        text = text.replace(style_anchor, style_anchor + EXTRA_CSS, 1)

    end_marker = "\n\n\ndef write_sitemap"
    end = text.find(end_marker)
    if end < 0:
        raise SystemExit("Fin de render_index introuvable")
    start = text.rfind("    count = len(events)", 0, end)
    if start < 0:
        if "village_total = len(villages)" not in text[:end]:
            raise SystemExit("Début du bloc final de render_index introuvable")
    elif "village_total = len(villages)" not in text[start:end]:
        text = text[:start] + NEW_TAIL + text[end:]

    GENERATOR.write_text(text, encoding="utf-8")


def load_generator_module():
    spec = importlib.util.spec_from_file_location("agenda_generator_beautified", GENERATOR)
    if spec is None or spec.loader is None:
        raise SystemExit("Impossible de charger generate_event_pages.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def rebuild_index_only() -> None:
    mod = load_generator_module()
    payload = mod.load_json(mod.AGENDA, {})
    events = payload.get("events", []) if isinstance(payload, dict) else []
    if not events:
        raise SystemExit("agenda.json ne contient aucun événement")

    slug_map = {}
    used = set()
    for event in events:
        key = mod.clean(event.get("url"))
        base = mod.event_slug(event)
        slug = base
        if slug in used:
            slug = f"{base}-{mod.slugify(event.get('commune', 'lieu'), 35)}"
        if slug in used:
            identity = mod.clean(event.get("datatourisme_uuid")) or key
            suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
            slug = f"{base}-{mod.slugify(event.get('commune', 'lieu'), 24)}-{suffix}"
        if slug in used:
            raise SystemExit(f"Collision de slug impossible à résoudre : {slug}")
        used.add(slug)
        slug_map[key] = slug

    (mod.EVENTS_DIR / "index.html").write_text(mod.render_index(events, slug_map), encoding="utf-8")
    print(f"Index régénéré : {len(events)} événements, aucune fiche individuelle modifiée.")


if __name__ == "__main__":
    patch_generator()
    rebuild_index_only()
