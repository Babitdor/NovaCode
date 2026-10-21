"""Shared Council configuration exposed to the local web editor."""

from dataclasses import asdict

from novacode_cli.council_settings import default_settings, load_settings, personality_templates


def settings_catalog() -> dict:
    """Return editable settings and public provider metadata, without credentials."""
    from novacode_cli.config.model_manager import MODEL_PRESETS

    warning = ""
    try:
        settings = load_settings()
    except ValueError as exc:
        settings = default_settings()
        warning = f"Saved settings are invalid: {exc}. Review defaults before saving."
    return {
        "settings": settings.to_dict(),
        "warning": warning,
        "defaults": default_settings().to_dict(),
        "templates": [asdict(template) for template in personality_templates()],
        "providers": [
            {"id": key, "name": preset["name"], "models": preset.get("models", [])}
            for key, preset in MODEL_PRESETS.items()
        ],
    }


def save_web_settings(raw: object) -> dict:
    """Use exactly the inline editor's validation and persistent configuration key."""
    from novacode_cli.config.nova_config import NovaConfig
    from novacode_cli.council_settings import parse_settings

    if raw is None:
        raise ValueError("Settings must be an object")
    settings = parse_settings(raw)
    saved = settings.to_dict()
    config = NovaConfig()
    config.set("council", saved)
    return saved
