"""Shared, bounded frame-rate choices for live terminal animations."""

DEFAULT_ANIMATION_FPS = 30
ANIMATION_FPS_CHOICES = (15, 30, 60)


def animation_fps(value: object) -> int:
    """Use the default for unsupported or malformed saved preferences."""
    return value if type(value) is int and value in ANIMATION_FPS_CHOICES else DEFAULT_ANIMATION_FPS
