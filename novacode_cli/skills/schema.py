"""Canonical SKILL.md schema — one source of truth for structure + frontmatter.

Autonomous skill creation (Hermes review/evolution) and the CLI generator all
converge on this schema so generated skills are consistent and machine-checkable.

The design stays **lean** (procedures, not prose): ``version``/``tags`` are
optional metadata with sensible defaults, and the body sections formalize the
gotchas/verification the agent already cared about — without intros, theory, or
FAQs. ``normalize_skill_frontmatter`` is *tolerant*: it only fills gaps and never
raises, so it can sit on the hot write path without ever blocking a skill write.
"""

from __future__ import annotations

import json
import re

DEFAULT_VERSION = "1.0.0"

# Canonical body sections, in order. Lean by design — every section earns its
# tokens (no introductions, background, or theory).
CANONICAL_SECTIONS: tuple[str, ...] = (
    "When to Use",
    "Quick Reference",
    "Procedure",
    "Pitfalls",
    "Verification",
)

# Frontmatter keys the schema guarantees (others the author wrote are preserved).
_REQUIRED_KEYS = ("name", "description", "version", "tags")

_FM_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$")

# A delimited frontmatter splits into ["", frontmatter, body] on "---".
_FRONTMATTER_PARTS = 3


def _split_frontmatter(content: str) -> tuple[str | None, str]:
    """Return ``(frontmatter_text, body)``; ``(None, content)`` when absent."""
    if not content.startswith("---"):
        return None, content
    parts = content.split("---", 2)
    if len(parts) < _FRONTMATTER_PARTS:
        return None, content
    return parts[1], parts[2].lstrip("\n")


def _parse_frontmatter_keys(frontmatter: str) -> dict[str, str]:
    """Parse simple ``key: value`` frontmatter lines into an ordered dict."""
    keys: dict[str, str] = {}
    for line in frontmatter.splitlines():
        match = _FM_KEY_RE.match(line)
        if match:
            keys[match.group(1).lower()] = match.group(2).strip()
    return keys


def yaml_str(text: str) -> str:
    r"""``text`` as a YAML double-quoted scalar, on one line.

    Interpolating into ``"..."`` by hand broke every description holding a
    Windows path: YAML reads ``B:\Summer`` as the escape ``\S``, the whole
    frontmatter fails to parse and the skill silently never loads. JSON string
    escaping is valid YAML, so ``json.dumps`` round-trips any text.
    """
    return json.dumps(" ".join(text.split()), ensure_ascii=False)


def _scalar(raw: str) -> str:
    """Decode a raw frontmatter value; a broken quoted one is taken literally."""
    raw = raw.strip()
    if raw[:1] in ('"', "'"):
        try:
            import yaml

            value = yaml.safe_load(raw)
            if isinstance(value, str):
                return value
        except Exception:  # noqa: BLE001, S110 — the old writer meant "B:\Summer" literally
            pass
        return raw.strip('"').strip("'")
    return raw


def normalize_skill_frontmatter(content: str, name: str, description: str = "") -> str:
    """Ensure SKILL.md frontmatter carries name/description/version/tags.

    Tolerant: never raises, only fills missing keys. An existing body and any
    extra frontmatter keys the author wrote are preserved; if there is no
    frontmatter at all, one is synthesized and *content* becomes the body.
    Idempotent — running it twice yields the same output.

    Args:
        content: Raw SKILL.md content (with or without frontmatter), or a bare body.
        name: Skill name to use when frontmatter omits one.
        description: Fallback description (trigger) when frontmatter omits one.

    Returns:
        Normalized SKILL.md content.
    """
    frontmatter, body = _split_frontmatter(content)
    existing = _parse_frontmatter_keys(frontmatter) if frontmatter else {}

    name_v = existing.get("name") or name
    desc_v = (
        _scalar(existing.get("description", ""))
        or description
        or f"Reusable workflow: {name_v}"
    )
    version_v = existing.get("version") or DEFAULT_VERSION
    tags_v = existing.get("tags") or "[]"

    lines = [
        "---",
        f"name: {name_v}",
        f"description: {yaml_str(desc_v)}",
        f"version: {version_v}",
        f"tags: {tags_v}",
    ]
    # Preserve any extra author-written keys (order-stable).
    lines.extend(
        f"{key}: {value}" for key, value in existing.items() if key not in _REQUIRED_KEYS
    )
    lines.append("---")
    front = "\n".join(lines)

    body_text = (body if frontmatter else content).strip()
    return f"{front}\n\n{body_text}\n"


def validate_skill_structure(content: str) -> list[str]:
    """Return a list of structural issues (advisory; empty ⇒ schema-complete).

    Checks frontmatter has name + description and that the body mentions each
    canonical section heading. Used by tests and the debate critic — never fatal.
    """
    issues: list[str] = []
    frontmatter, body = _split_frontmatter(content)
    if frontmatter is None:
        issues.append("missing YAML frontmatter")
        keys: dict[str, str] = {}
        body = content
    else:
        keys = _parse_frontmatter_keys(frontmatter)

    issues.extend(
        f"frontmatter missing '{required}'"
        for required in ("name", "description")
        if not keys.get(required)
    )

    lowered = (body or "").lower()
    issues.extend(
        f"missing section: {section}"
        for section in CANONICAL_SECTIONS
        if section.lower() not in lowered
    )
    return issues


def render_schema_block() -> str:
    """Return the canonical-structure guidance text (for prompts / the critic)."""
    sections = "\n".join(f"   - ## {s}" for s in CANONICAL_SECTIONS)
    return (
        "A SKILL.md has two parts:\n\n"
        "1. YAML frontmatter (lean):\n"
        "   ```\n"
        "   ---\n"
        "   name: <kebab-case-name>\n"
        "   description: <verb> <object>. Use when: <signal 1>, <signal 2>.\n"
        f"   version: {DEFAULT_VERSION}\n"
        "   tags: [tag-one, tag-two]\n"
        "   ---\n"
        "   ```\n"
        "2. Markdown body with these sections (imperative, concrete, no prose/theory):\n"
        f"{sections}\n"
    )


__all__ = [
    "CANONICAL_SECTIONS",
    "DEFAULT_VERSION",
    "normalize_skill_frontmatter",
    "yaml_str",
    "render_schema_block",
    "validate_skill_structure",
]
