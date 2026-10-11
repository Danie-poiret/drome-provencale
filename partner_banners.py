"""One shared advertising component; does not scrape or call an email/AI service."""
from functools import wraps
from pathlib import Path
import re

SCRIPT = '<script data-nyons-partner-script src="https://www.vivreanyons.fr/assets/nyons-partner-banners.js?v=20261011-v2" defer></script>'
SLOT = '<div data-nyons-partner-slot></div>'

def add_partner_banners(source):
    if not re.search(r'<main(?:\s|>)', source, re.I):
        return source
    def replace_ad(match):
        block=match.group(0)
        return SLOT if '/banniere-livres/' in block or 'banniere-livres-drome-ardeche.png' in block else block
    source=re.sub(r'<aside\b[^>]*class=["\']top["\'][^>]*>.*?</aside>',replace_ad,source,flags=re.S|re.I)
    source=re.sub(r'<div\b[^>]*class=["\']banner["\'][^>]*>\s*<iframe\b.*?</iframe>\s*</div>',replace_ad,source,flags=re.S|re.I)
    # Keep only one mount if an old generator adds a second book advertisement.
    if source.count(SLOT)>1:
        first=source.index(SLOT)+len(SLOT)
        source=source[:first]+source[first:].replace(SLOT,'')
    if SLOT not in source:
        source=re.sub(r'(<main(?:\s[^>]*)?>)',lambda m:m[0]+SLOT,source,count=1,flags=re.I)
    if 'data-nyons-partner-script' not in source:
        source=re.sub(r'</head>',lambda m:SCRIPT+m[0],source,count=1,flags=re.I)
    return source

def with_partner_banners(renderer):
    @wraps(renderer)
    def decorated(*args,**kwargs):
        return add_partner_banners(renderer(*args,**kwargs))
    return decorated

def install_partner_banners(root):
    changed=0
    for path in sorted(Path(root).rglob('*.html')):
        if any(part.startswith('.') for part in path.relative_to(root).parts):
            continue
        old=path.read_text(encoding='utf8');new=add_partner_banners(old)
        if new!=old:
            path.write_text(new,encoding='utf8');changed+=1
    return changed
