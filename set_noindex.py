#!/usr/bin/env python3
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent


def set_noindex(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    original = text

    robots_re = re.compile(
        r'<meta\s+name=["\']robots["\']\s+content=["\'][^"\']*["\']\s*/?>',
        re.I,
    )
    meta = '<meta name="robots" content="noindex,follow">'

    if robots_re.search(text):
        text = robots_re.sub(meta, text, count=1)
    elif "</head>" in text.lower():
        text = re.sub(r"</head>", f"  {meta}\n</head>", text, count=1, flags=re.I)

    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


def main() -> None:
    files = []
    root_index = ROOT / "index.html"
    if root_index.exists():
        files.append(root_index)
    events_dir = ROOT / "evenements"
    if events_dir.exists():
        files.extend(events_dir.rglob("*.html"))

    changed = 0
    for path in files:
        if set_noindex(path):
            changed += 1

    print(f"NOINDEX: {changed} fichier(s) HTML modifié(s) sur {len(files)} contrôlé(s).")


if __name__ == "__main__":
    main()
