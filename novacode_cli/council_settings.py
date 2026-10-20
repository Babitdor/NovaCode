"""Persistent council identities, personality templates and per-member models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from novacode_cli.council import Persona

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass(frozen=True)
class CouncilMember:
    """A stable seat with editable identity and optional provider/model override."""

    id: str
    name: str
    system: str
    avatar: str
    color: str
    provider: str = ""
    model: str = ""

    def persona(self) -> Persona:
        """Return the persona used by council runners."""
        return Persona(self.id, self.name, self.avatar, self.color, self.system)


@dataclass(frozen=True)
class CouncilSettings:
    """One immutable configuration snapshot for a future council run."""

    members: tuple[CouncilMember, ...]
    judge: CouncilMember

    def to_dict(self) -> dict:
        """Serialize only configuration, never model objects or credentials."""
        return {
            "version": 1,
            "members": [asdict(m) for m in self.members],
            "judge": asdict(self.judge),
        }

    def personas(self, *, debate: bool = False) -> list[Persona]:
        """Keep the web debate's five seats and the planner's six seats."""
        return [m.persona() for m in self.members if not debate or m.id != "ux"]


def default_settings() -> CouncilSettings:
    """Preserve the existing council when no settings have been saved."""
    from novacode_cli.council_planner import PLANNING_PERSONAS

    return CouncilSettings(
        tuple(CouncilMember(p.id, p.name, p.system, p.avatar, p.color) for p in PLANNING_PERSONAS),
        CouncilMember(
            "judge",
            "The Judge",
            "Weigh correctness, feasibility and evidence. "
            "Explain tradeoffs fairly and flag uncertainty.",
            "⚖️",
            "#c3a777",
        ),
    )


def personality_templates() -> tuple[Persona, ...]:
    """Reusable perspectives; applying one never changes the member's model."""
    defaults = default_settings()
    return (
        *(m.persona() for m in defaults.members),
        Persona(
            "security",
            "The Security Reviewer",
            "🔐",
            "#d46a6a",
            "You are a security reviewer. Examine trust boundaries, authorization, secret handling "
            "and abuse cases. Favor concrete threat models and proportionate mitigations.",
        ),
        Persona(
            "tester",
            "The Test Engineer",
            "🧪",
            "#4cae8a",
            "You are a test engineer. Turn assumptions into falsifiable checks. Look for failure "
            "paths, reproducibility and evidence that the proposed change solves the real problem.",
        ),
        Persona(
            "performance",
            "The Performance Analyst",
            "⏱️",
            "#d4a13b",
            "You are a performance analyst. Examine latency, memory, throughput and cost. Ask for "
            "measurements, identify likely bottlenecks and avoid speculative optimization.",
        ),
        Persona(
            "product",
            "The Product Advocate",
            "🧭",
            "#b06ae8",
            "You are a product advocate. Connect the proposal to the user's goal. Examine scope, "
            "discoverability, accessibility, adoption and whether the benefit "
            "justifies the effort.",
        ),
        defaults.judge.persona(),
    )


def parse_settings(raw: object) -> CouncilSettings:
    """Validate a draft before saving or using it; IDs and seat count stay stable."""
    from novacode_cli.config.model_manager import MODEL_PRESETS

    defaults = default_settings()
    if raw is None:
        return defaults
    if not isinstance(raw, dict) or raw.get("version", 1) != 1:
        message = "Unsupported council settings format"
        raise ValueError(message)
    entries = raw.get("members")
    if not isinstance(entries, list) or len(entries) != len(defaults.members):
        message = "Council settings must contain all six member seats"
        raise ValueError(message)
    indexed = {
        entry["id"]: entry
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    if set(indexed) != {m.id for m in defaults.members}:
        message = "Council member IDs must match the existing seats"
        raise ValueError(message)

    def member(entry: object, base: CouncilMember) -> CouncilMember:
        if not isinstance(entry, dict) or entry.get("id", base.id) != base.id:
            message = "Invalid council member"
            raise ValueError(message)
        values = {}
        for field, limit in (("name", 64), ("system", 12000), ("provider", 64), ("model", 240)):
            value = entry.get(field, getattr(base, field))
            if not isinstance(value, str) or len(value) > limit:
                message = f"Council {field} must be text of at most {limit} characters"
                raise ValueError(message)
            values[field] = value.strip()
        if not values["name"] or not values["system"]:
            message = "Every council member needs a name and system prompt"
            raise ValueError(message)
        if not values["name"].isprintable():
            message = "Council names must be a single line without control characters"
            raise ValueError(message)
        if bool(values["provider"]) != bool(values["model"]):
            message = "Select both a provider and model ID, or inherit the session model"
            raise ValueError(message)
        if values["provider"] and values["provider"] not in MODEL_PRESETS:
            message = f"Unknown council provider: {values['provider']}"
            raise ValueError(message)
        # Visual identity is fixed to the seat; templates change the perspective,
        # while saved arbitrary CSS/color strings never enter the renderers.
        return replace(base, **values)

    members = tuple(member(indexed[m.id], m) for m in defaults.members)
    judge = member(raw.get("judge", asdict(defaults.judge)), defaults.judge)
    names = [m.name.casefold() for m in (*members, judge)]
    if len(set(names)) != len(names):
        message = "Council members and judge need distinct names"
        raise ValueError(message)
    return CouncilSettings(members, judge)


def load_settings() -> CouncilSettings:
    """Read fresh user configuration without changing the active main model."""
    from novacode_cli.config.nova_config import NovaConfig

    return parse_settings(NovaConfig().get("council"))


def resolve_models(
    settings: CouncilSettings,
    default_model: Any,  # noqa: ANN401 - Provider model protocol.
    *,
    build: Callable[[str, str], Any] | None = None,
    debate: bool = False,
) -> dict[str, Any]:
    """Construct overrides once per run, failing explicitly instead of substituting models."""
    from novacode_cli.config.model_create import build_chat_model

    builder = build or build_chat_model
    cache: dict[tuple[str, str], Any] = {}
    resolved = {}
    seats = settings.members if debate else (*settings.members, settings.judge)
    for seat in seats:
        if debate and seat.id == "ux":
            continue
        if not seat.provider:
            resolved[seat.id] = default_model
            continue
        key = (seat.provider, seat.model)
        if key not in cache:
            try:
                cache[key] = builder(*key)
            except Exception as exc:
                message = (
                    f"Could not initialize {seat.name}'s council model "
                    f"({seat.provider}:{seat.model}; {type(exc).__name__}). "
                    "Check /auth and the model ID."
                )
                raise ValueError(message) from exc
        resolved[seat.id] = cache[key]
    return resolved
