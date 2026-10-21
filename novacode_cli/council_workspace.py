"""Session-only Council workspace presets and public advisor metadata."""

from dataclasses import replace

from novacode_cli.council_settings import personality_templates

PRESETS = {
    "code": ("Code Audit", ("architect", "security", "tester", "performance", "product")),
    "arch": ("Architecture", ("architect", "security", "performance", "pragmatist", "minimalist")),
    "creative": ("Creative Debate", ("innovator", "pragmatist", "skeptic", "product", "ux")),
}


def apply_web_preset(settings, key: str):
    """Change only this web round's perspectives; preserve models and saved settings."""
    if not key or key == "saved":
        return settings
    if key not in PRESETS:
        raise ValueError("Unknown Council preset")
    templates = {p.id: p for p in personality_templates()}
    perspectives = iter(PRESETS[key][1])
    members = []
    for member in settings.members:
        if member.id == "ux":
            members.append(member)
            continue
        template = templates[next(perspectives)]
        members.append(replace(member, name=template.name, system=template.system))
    return replace(settings, members=tuple(members))


def panel_descriptor(settings) -> dict:
    """Describe actual configured models, without credentials or invented telemetry."""

    def describe(member, *, chairman=False):
        sentences = member.system.split(". ")
        role = (
            sentences[1]
            if sentences[0].lower().startswith("you are") and len(sentences) > 1
            else sentences[0]
        )
        return {
            "id": "chairman" if chairman else member.id,
            "name": member.name,
            "avatar": member.avatar,
            "color": member.color,
            "role": "Consensus engine" if chairman else role[:160],
            "model": f"{member.provider}:{member.model}"
            if member.provider
            else "Session model (inherit)",
        }

    return {
        "agents": [describe(m) for m in settings.members if m.id != "ux"],
        "chairman": describe(settings.judge, chairman=True),
    }
