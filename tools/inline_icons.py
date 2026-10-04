"""Fill the page's icon, logo and copy-button placeholders with inline SVG.

The page is one static file with no build step, so the SVGs live in the HTML.
They are never drawn by hand: icons come from Phosphor, provider logos from
Simple Icons and Lobe Icons, and this script only pastes them in. Run it after
editing a ``{{...}}`` placeholder back into ``index.html``:

    .venv\\Scripts\\python.exe landing\\tools\\inline_icons.py

Placeholders:
    {{icon:NAME}} / {{icon:NAME:CLASS}}   a Phosphor icon (regular weight)
    {{logo:SLUG:LABEL}}                   a provider logo from assets/logos/
    {{copy:TEXT}}                         a copy button for TEXT
"""

from __future__ import annotations

import html
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "index.html"
LOGOS = ROOT / "assets" / "logos"
PHOSPHOR = "https://cdn.jsdelivr.net/npm/@phosphor-icons/core@2/assets/regular/{name}.svg"
SITES = {
    "openai": "https://openai.com",
    "anthropic": "https://www.anthropic.com",
    "googlegemini": "https://gemini.google.com",
    "nvidia": "https://build.nvidia.com",
    "openrouter": "https://openrouter.ai",
    "opencode": "https://opencode.ai",
    "ollama": "https://ollama.com",
}
_cache: dict[str, str] = {}


def _inner(svg: str) -> tuple[str, str]:
    """``(viewBox, inner markup)`` of an SVG file, minus its <title>."""
    view = re.search(r'viewBox="([^"]+)"', svg).group(1)  # type: ignore[union-attr]
    inner = re.search(r"<svg[^>]*>(.*)</svg>", svg, re.S).group(1)  # type: ignore[union-attr]
    return view, re.sub(r"<title>.*?</title>", "", inner, flags=re.S).strip()


def icon(name: str, cls: str = "") -> str:
    if name not in _cache:
        with urllib.request.urlopen(PHOSPHOR.format(name=name), timeout=30) as r:  # noqa: S310
            _cache[name] = r.read().decode("utf-8")
    view, inner = _inner(_cache[name])
    klass = f' class="{cls}"' if cls else ""
    return f'<svg{klass} viewBox="{view}" fill="currentColor" aria-hidden="true">{inner}</svg>'


def logo(slug: str, label: str) -> str:
    view, inner = _inner((LOGOS / f"{slug}.svg").read_text(encoding="utf-8"))
    svg = f'<svg viewBox="{view}" fill="currentColor" role="img" aria-label="{html.escape(label)}">{inner}</svg>'
    return f'<a href="{SITES[slug]}" title="{html.escape(label)}">{svg}</a>'


def copy_button(text: str) -> str:
    return (
        f'<button class="copy" type="button" data-copy="{html.escape(text, quote=True)}">'
        f'{icon("copy", "i-copy")}{icon("check", "i-check")}<span>Copy</span></button>'
    )


def main() -> None:
    page = PAGE.read_text(encoding="utf-8")
    page = re.sub(r"\{\{icon:([a-z-]+)(?::([a-z-]+))?\}\}", lambda m: icon(m.group(1), m.group(2) or ""), page)
    page = re.sub(r"\{\{logo:([a-z]+):([^}]+)\}\}", lambda m: logo(m.group(1), m.group(2)), page)
    page = re.sub(r"\{\{copy:(.+?)\}\}", lambda m: copy_button(m.group(1)), page)
    assert "{{" not in page, "an unknown placeholder is left in the page"
    PAGE.write_text(page, encoding="utf-8", newline="\n")
    print(f"{PAGE.name}: {len(page) // 1024} KB")


if __name__ == "__main__":
    main()
