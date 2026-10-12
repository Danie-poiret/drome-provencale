"""Normalize links to static pages; leave routing and existing 301 redirects intact."""
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, urljoin
import html
import re

HOSTS = {'www.vivreanyons.fr', 'vivreanyons.fr', 'agenda.vivreanyons.fr', 'drome.vivreanyons.fr'}
TAG = re.compile(r'<(?:a|link)\b[^>]*>', re.I)
HREF = re.compile(r'(\bhref\s*=\s*)([\"\'])(.*?)(\2)', re.I | re.S)

def normalize_url(raw, base):
    value = html.unescape(raw)
    if not value or value.startswith(('#', 'mailto:', 'tel:', 'javascript:', 'data:')):
        return raw
    if any(c in value for c in '{}$`<>\'"') or re.search(r'\s', value):
        return raw
    try:
        part = urlsplit(value)
        resolved = urlsplit(urljoin(base, value))
    except ValueError:
        return raw
    if resolved.scheme not in {'http','https'} or resolved.hostname not in HOSTS:
        return raw
    path = part.path
    if not path and not part.netloc:
        return raw
    if path.endswith('/') or re.search(r'\.[a-z0-9]{1,12}$', path.rsplit('/',1)[-1], re.I):
        return raw
    if any(resolved.path.startswith(p) for p in ['/assets/', '/api/', '/.well-known/']):
        return raw
    updated = urlunsplit((part.scheme, part.netloc, path+'/', part.query, part.fragment))
    return html.escape(updated, quote=True)

def normalize_html(source, base):
    def replace_tag(tag):
        return HREF.sub(lambda m:m[1]+m[2]+normalize_url(m[3],base)+m[4],tag[0])
    return TAG.sub(replace_tag,source)

def install(root=None, site=None):
    root = Path(root or __file__).resolve()
    if root.is_file(): root = root.parent
    if site is None:
        cname = root/'CNAME'
        if not cname.is_file(): raise RuntimeError('CNAME absent : définir le domaine explicitement.')
        host=cname.read_text(encoding='utf8').strip()
        if host not in HOSTS: raise RuntimeError('Domaine inattendu : '+host)
        site='https://'+host+'/'
    count=0;links=0
    for path in sorted(root.rglob('*.html')):
        if any(p.startswith('.') for p in path.relative_to(root).parts): continue
        source=path.read_text(encoding='utf8');updated=normalize_html(source,site)
        if source!=updated:
            assert normalize_html(updated,site)==updated, path
            before=TAG.sub(lambda m:HREF.sub('href="[LINK]"',m[0]),source)
            after=TAG.sub(lambda m:HREF.sub('href="[LINK]"',m[0]),updated)
            assert before==after, 'Seuls les href peuvent changer : '+str(path)
            links+=sum(a!=b for a,b in zip(HREF.findall(source),HREF.findall(updated)))
            path.write_text(updated,encoding='utf8');count+=1
    print(f'{count} pages harmonisées, {links} liens corrigés. Aucune URL supprimée ; redirections inchangées.')
    return count

if __name__=='__main__': install()
