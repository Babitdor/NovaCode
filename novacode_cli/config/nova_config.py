"""Configuration management for Nova CLI.

Manages persistent settings stored in ~/.nova/Nova.config.json
"""

import copy
import json
import os
from typing import Any

from novacode_cli.config.config import Settings, console


#: The agent roles a model can be chosen for. ``main`` is the agent the user talks
#: to; ``subagent`` the in-process roster it delegates to; ``async`` the remote
#: graphs on the LangGraph server; ``dynamic`` the agents discovered in the agent
#: directories (which is also what an ``/eval`` fan-out dispatches).
ROLE_NAMES: tuple[str, ...] = ("main", "subagent", "async", "dynamic")

#: Strings a hand-edited boolean key may hold. Python treats every non-empty
#: string as true, so a bare ``bool(...)`` reads ``"false"`` as "on" -- switching
#: *on* a feature somebody had just tried to switch off. A value in neither set
#: (an unrecognised string, a list, ``None``) falls back to the safe default.
_TRUTHY_STRINGS: frozenset[str] = frozenset({"true", "1", "yes", "on"})
_FALSY_STRINGS: frozenset[str] = frozenset({"false", "0", "no", "off", ""})


def _config_bool(value: Any, *, default: bool = False) -> bool:
    """Read a boolean config value, failing *closed* on anything unrecognised.

    The setters always write a real JSON boolean, so a string or a list only
    reaches here from a hand-edited config file -- which is exactly where the
    ``bool("false") is True`` trap lives.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _FALSY_STRINGS:
            return False
        return text in _TRUTHY_STRINGS
    return default


def _role_key(role: str) -> str:
    """Config key holding one role's model.

    One top-level key per role, so ``_save`` merges them independently. ``main``
    deliberately reuses the existing ``model`` key rather than duplicating it.
    """
    return "model" if role == "main" else f"model_role_{role}"


def _require_role(role: str) -> None:
    if role not in ROLE_NAMES:
        raise ValueError(f"Unknown model role {role!r}. Valid roles: {', '.join(ROLE_NAMES)}")


class NovaConfig:
    """Manages persistent configuration for Nova CLI."""

    def __init__(self) -> None:
        """Initialize configuration manager."""
        settings = Settings.from_environment()
        self.config_dir = settings.user_deepagents_dir
        self.config_path = self.config_dir / "Nova.config.json"
        self._config: dict[str, Any] = {}
        self._loaded: dict[str, Any] = {}  # what _config held when last synced with disk
        self._load()

    def _read_disk(self) -> dict[str, Any]:
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # If config is corrupted, start fresh
            console.print(f"[yellow]Warning: Could not load config: {e}[/yellow]")
            return {}

    def _load(self) -> None:
        """Load configuration from disk."""
        self._config = self._read_disk()
        self._loaded = copy.deepcopy(self._config)

    def _save(self) -> None:
        """Save configuration to disk atomically (temp file + rename)."""
        # Ensure config directory exists
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Write to temp file first, then atomically replace.
        # Use Path.replace (os.replace), not rename: on Windows rename raises
        # FileExistsError when the target exists, whereas replace overwrites
        # atomically on both Windows and POSIX.
        # Write back only the keys THIS instance changed, on top of what is on
        # disk now. Writing the whole in-memory snapshot let any instance loaded
        # earlier (a long-lived ModelManager, another Nova process) put back a
        # model the user had since switched away from, the moment it saved an
        # unrelated setting such as the theme: the "always nemotron" bug.
        merged = self._read_disk()
        for key in set(self._config) | set(self._loaded):
            if key not in self._config:
                if key in self._loaded:
                    merged.pop(key, None)
            elif self._config[key] != self._loaded.get(key):
                merged[key] = copy.deepcopy(self._config[key])
        self._config = merged
        self._loaded = copy.deepcopy(merged)

        tmp_path = self.config_path.with_suffix(".tmp." + str(os.getpid()))
        try:
            tmp_path.write_text(json.dumps(self._config, indent=2), encoding="utf-8")
            tmp_path.replace(self.config_path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def get_model_config(self) -> dict[str, str] | None:
        """Get saved model provider configuration.

        Returns:
            Dict with 'provider' and 'model' keys, or None if not configured
        """
        self._load()  # fresh: another instance or process may have switched it
        return self._config.get("model")

    def set_model_config(self, provider: str, model: str, base_url: str | None = None) -> None:
        """Save model provider configuration.

        Args:
            provider: Provider ID (openai, anthropic, ollama, google)
            model: Model name
            base_url: Optional OpenAI-compatible endpoint override (Azure,
                LM Studio, vLLM, a LiteLLM proxy…). Omitted from the saved
                config when blank, so the default endpoint is used.
        """
        entry: dict[str, str] = {"provider": provider, "model": model}
        if base_url:
            entry["base_url"] = base_url
        self._config["model"] = entry
        self._save()

    def get_model_base_url(self) -> str | None:
        """The saved endpoint override for the current provider, if any."""
        model_cfg = self._config.get("model")
        if isinstance(model_cfg, dict):
            url = model_cfg.get("base_url")
            if isinstance(url, str) and url.strip():
                return url.strip()
        return None

    def clear_model_config(self) -> None:
        """Clear saved model configuration."""
        if "model" in self._config:
            del self._config["model"]
            self._save()

    # ── Per-role models ─────────────────────────────────────────────────────
    #
    # A role with no saved entry inherits the main agent's model, which is what
    # every role did before this existed, so an untouched config behaves exactly
    # as it did. Each role lives under its own top-level key because `_save`
    # merges per top-level key: one nested `roles` map would let two Nova
    # processes clobber each other's roles, the same way the `credentials`
    # setter re-reads the file to avoid.

    def get_role_model(self, role: str) -> dict[str, str] | None:
        """Saved model for *role*, or None when the role inherits the main one."""
        _require_role(role)
        self._load()  # fresh: another instance or process may have set it
        entry = self._config.get(_role_key(role))
        return dict(entry) if isinstance(entry, dict) else None

    def set_role_model(
        self, role: str, provider: str, model: str, base_url: str | None = None
    ) -> None:
        """Save a model for one role.

        ``provider`` + ``model`` are stored separately because that is how the
        rest of Nova resolves them; deepagents is handed the joined
        ``provider:model`` spec, which is the form it documents.
        """
        _require_role(role)
        self._load()
        entry: dict[str, str] = {"provider": provider, "model": model}
        if base_url:
            entry["base_url"] = base_url
        self._config[_role_key(role)] = entry
        self._save()

    def clear_role_model(self, role: str) -> None:
        """Drop a role's saved model so it inherits the main agent's again."""
        _require_role(role)
        self._load()
        if _role_key(role) in self._config:
            del self._config[_role_key(role)]
            self._save()

    def all_role_models(self) -> dict[str, dict[str, str]]:
        """Every role that has an explicit model, keyed by role name."""
        self._load()
        found: dict[str, dict[str, str]] = {}
        for role in ROLE_NAMES:
            entry = self._config.get(_role_key(role))
            if isinstance(entry, dict):
                found[role] = dict(entry)
        return found

    # ── Credential metadata ─────────────────────────────────────────────────
    #
    # The secret itself lives in the OS keychain (see
    # `novacode_cli.config.credentials`); only the non-secret facts that have to
    # travel with it — the endpoint it belongs to, a project name, when it was
    # last written — are kept here. Keyed by the credential's environment
    # variable name, which is the stable identifier the keychain entry uses too.

    def get_credential_meta(self) -> dict[str, dict[str, str]]:
        """Get recorded metadata for stored credentials.

        Returns:
            Mapping of environment variable name to its recorded fields
            (``base_url``, ``project``, ``added_at``). Empty when nothing was
            recorded, which is the normal case for a plain key.
        """
        creds = self._config.get("credentials")
        if not isinstance(creds, dict):
            return {}
        return {
            str(env_var): dict(fields)
            for env_var, fields in creds.items()
            if isinstance(fields, dict)
        }

    def set_credential_meta(
        self,
        env_var: str,
        *,
        base_url: str | None = None,
        project: str | None = None,
        added_at: str | None = None,
    ) -> None:
        """Record metadata for a stored credential.

        Re-reads the config from disk first so a concurrent Nova that saved a
        credential for a different provider is not clobbered: `_save` merges per
        top-level key, so without the refresh this would write back the whole
        `credentials` map from a stale snapshot.

        Args:
            env_var: The credential's environment variable name.
            base_url: Endpoint paired with the key, or None.
            project: Project/workspace name paired with the key, or None.
            added_at: ISO-8601 timestamp of the write, or None.
        """
        self._load()
        creds = self._config.get("credentials")
        if not isinstance(creds, dict):
            creds = {}
            self._config["credentials"] = creds
        entry: dict[str, str] = {}
        if base_url:
            entry["base_url"] = base_url
        if project:
            entry["project"] = project
        if added_at:
            entry["added_at"] = added_at
        creds[env_var] = entry
        self._save()

    def delete_credential_meta(self, env_var: str) -> None:
        """Drop recorded metadata for a credential.

        Args:
            env_var: The credential's environment variable name.
        """
        self._load()
        creds = self._config.get("credentials")
        if not isinstance(creds, dict) or env_var not in creds:
            return
        del creds[env_var]
        if not creds:
            # Keep the file clean: an empty map and no map mean the same thing,
            # and a leftover `"credentials": {}` reads like configuration the
            # user set.
            del self._config["credentials"]
        self._save()

    # ── Recent model picks ──────────────────────────────────────────────────

    #: How many recent picks are remembered. Enough to cover the handful of
    #: models someone rotates between; short enough to stay a convenience
    #: rather than a second copy of the model list.
    RECENT_MODELS_LIMIT = 8

    def get_recent_models(self) -> list[str]:
        """Get recently selected ``provider:model`` specs, most recent first.

        Returns:
            The recorded specs, newest first. Empty before any switch.
        """
        recent = self._config.get("model_recent")
        if not isinstance(recent, list):
            return []
        return [spec for spec in recent if isinstance(spec, str) and spec]

    def push_recent_model(self, spec: str) -> list[str]:
        """Record a model pick as the most recent one.

        Args:
            spec: The ``provider:model`` spec that was selected.

        Returns:
            The updated recent list, newest first.
        """
        spec = spec.strip()
        if not spec:
            return self.get_recent_models()
        self._load()
        recent = [entry for entry in self.get_recent_models() if entry != spec]
        recent.insert(0, spec)
        self._config["model_recent"] = recent[: self.RECENT_MODELS_LIMIT]
        self._save()
        return list(self._config["model_recent"])

    # ── Vision model config (for image routing) ─────────────────────────────

    VISION_MODEL_DEFAULT = "gemma4:31b-cloud"
    VISION_PROVIDER_DEFAULT = "ollama"

    def get_vision_model_config(self) -> dict[str, str]:
        """Get saved vision model provider configuration.

        Returns:
            Dict with 'provider' and 'model' keys. Defaults to ollama/gemma4:31b-cloud
            when not configured.
        """
        cfg = self._config.get("vision_model")
        if cfg and isinstance(cfg, dict) and "provider" in cfg and "model" in cfg:
            return {"provider": cfg["provider"], "model": cfg["model"]}
        return {"provider": self.VISION_PROVIDER_DEFAULT, "model": self.VISION_MODEL_DEFAULT}

    def set_vision_model_config(self, provider: str, model: str) -> None:
        """Save vision model provider configuration.

        Args:
            provider: Provider ID (openai, anthropic, ollama, google)
            model: Model name
        """
        self._config["vision_model"] = {
            "provider": provider,
            "model": model,
        }
        self._save()

    def clear_vision_model_config(self) -> None:
        """Clear saved vision model configuration (reverts to default)."""
        if "vision_model" in self._config:
            del self._config["vision_model"]
            self._save()

    # ── Main-model multimodal override ──────────────────────────────────────

    def get_main_model_multimodal(self) -> bool | None:
        """Explicit override for whether the MAIN model accepts images.

        Returns ``None`` (auto-detect via the pattern registry), ``True``
        (force multimodal — images go straight to the main model), or ``False``
        (force text-only — images are captioned by the auxiliary vision model).
        """
        value = self._config.get("main_model_multimodal")
        if isinstance(value, bool):
            return value
        return None

    def set_main_model_multimodal(self, value: bool | None) -> None:
        """Persist the main-model multimodal override (``None`` clears it)."""
        if value is None:
            self._config.pop("main_model_multimodal", None)
        else:
            self._config["main_model_multimodal"] = bool(value)
        self._save()

    # ── Learning / self-improvement loop (Hermes) ───────────────────────────

    #: The periodic review + skill-creation loop fires out-of-band LLM calls
    #: (every N tool calls). It is OFF by default to keep per-turn LLM cost and
    #: latency minimal; users who want self-improvement opt in explicitly.
    LEARNING_ENABLED_DEFAULT: bool = False

    def get_learning_enabled(self) -> bool:
        """Whether the Hermes learning/review loop is enabled (default: off)."""
        return bool(self._config.get("learning_enabled", self.LEARNING_ENABLED_DEFAULT))

    def set_learning_enabled(self, enabled: bool) -> None:
        """Persist the learning-loop toggle."""
        self._config["learning_enabled"] = bool(enabled)
        self._save()

    # ── Memory injection budget ─────────────────────────────────────────────

    def get_memory_block_chars(self) -> int:
        """Per-block char budget for injected memory files.

        Applies to each of ``agent.md``, the topic index, ``HABITS.md`` and
        project memory. Defaults to
        :data:`~novacode_cli.memory.limits.DEFAULT_MEMORY_BLOCK_CHARS`.
        """
        from novacode_cli.memory.limits import DEFAULT_MEMORY_BLOCK_CHARS

        value = self._config.get("memory_block_chars")
        try:
            n = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return DEFAULT_MEMORY_BLOCK_CHARS
        return n if n > 0 else DEFAULT_MEMORY_BLOCK_CHARS

    def set_memory_block_chars(self, chars: int) -> None:
        """Persist the per-block memory injection budget."""
        self._config["memory_block_chars"] = int(chars)
        self._save()

    def get_memory_index_chars(self) -> int:
        """Chars of the topic-memory INDEX injected into the system prompt.

        The index is a pointer list; the rest is reachable via ``memory_search``.
        Defaults to :data:`~novacode_cli.memory.limits.DEFAULT_MEMORY_INDEX_CHARS`.
        """
        from novacode_cli.memory.limits import DEFAULT_MEMORY_INDEX_CHARS

        value = self._config.get("memory_index_chars")
        try:
            n = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return DEFAULT_MEMORY_INDEX_CHARS
        return n if n > 0 else DEFAULT_MEMORY_INDEX_CHARS

    def set_memory_index_chars(self, chars: int) -> None:
        """Persist the topic-index injection budget."""
        self._config["memory_index_chars"] = int(chars)
        self._save()

    # ── Decision-model tool-result pruning (OFF by default) ─────────────────
    #
    # When enabled, the tool-result reducer clears only the results a local
    # decision model scored stale, instead of everything older than the trigger
    # except the newest few. It is OFF because it was measured and did not win:
    # on six real sessions, capped to the same window the model can be asked
    # about, the existing rule cleared 70 results / 72,682 chars at 0.19
    # regretted per 1k, against 49 / 18,364 at 0.44 for the verdicts. The model
    # itself is good (tev1:4b passes a 12-question positive control 12/12 with a
    # 0.866 margin); the limiting factor is that its ~2,000-token context holds
    # only the newest ~12 results of a session that holds 65-308.
    #
    # Kept behind a flag so it can be tried in a live session, and so the
    # decision stays re-measurable rather than becoming a claim in a comment.

    #: Model name at the endpoint. `tev1:4b` is the separated one; the 0.8B puts
    #: plainly-false statements at 0.476-0.524, straddling any 0.5 threshold.
    TOOL_VERDICT_DEFAULT_MODEL = "tev1:4b"

    #: Where the decisions are asked of. Ollama serves the System One shape.
    TOOL_VERDICT_DEFAULT_ENDPOINT = "http://localhost:11434/v1/systemone"

    TOOL_VERDICT_JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
    TOOL_VERDICT_JEV_MODEL = "jev-latest"
    TOOL_VERDICT_OPENAI_ENDPOINT = "https://api.openai.com/v1/decisions"
    TOOL_VERDICT_OPENAI_MODEL = "gpt-6-luna"

    def set_tool_verdict_settings(self, *, enabled: bool, endpoint: str, model: str) -> None:
        """Persist a complete decision configuration in one write."""
        from urllib.parse import urlsplit

        endpoint, model = endpoint.strip(), model.strip()
        try:
            parts = urlsplit(endpoint)
            valid = (
                parts.scheme in {"http", "https"}
                and bool(parts.hostname)
                and parts.username is None
                and parts.password is None
            )
            _ = parts.port
        except ValueError:
            valid = False
        if not valid:
            message = "Endpoint must be an http(s) URL without embedded credentials."
            raise ValueError(message)
        if not model:
            message = "Decision model cannot be empty."
            raise ValueError(message)
        self._config.update(
            tool_verdicts_enabled=bool(enabled),
            tool_verdict_endpoint=endpoint,
            tool_verdict_model=model,
        )
        self._save()

    def get_tool_verdicts_enabled(self) -> bool:
        """Whether the tool-result reducer consults a decision model.

        Parsed strictly rather than with ``bool()``: ``bool("false")`` is ``True``,
        and this is the master switch for a feature that is off by default, so a
        hand-edited string must never be able to turn it on by accident. See
        :func:`_config_bool`.
        """
        return _config_bool(self._config.get("tool_verdicts_enabled", False))

    def set_tool_verdicts_enabled(self, enabled: bool) -> None:
        """Persist the decision-model pruning flag."""
        self._config["tool_verdicts_enabled"] = bool(enabled)
        self._save()

    def get_auto_approve_decisions_enabled(self) -> bool:
        """Whether auto-approve asks the configured decision model for approval."""
        return _config_bool(self._config.get("auto_approve_decisions_enabled", False))

    def set_auto_approve_decisions_enabled(self, enabled: bool) -> None:
        """Persist the opt-in decision-model approval setting."""
        self._config["auto_approve_decisions_enabled"] = bool(enabled)
        self._save()

    def get_tool_verdict_endpoint(self) -> str:
        """System One endpoint the verdicts are asked of."""
        value = self._config.get("tool_verdict_endpoint")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return self.TOOL_VERDICT_DEFAULT_ENDPOINT

    def set_tool_verdict_endpoint(self, endpoint: str) -> None:
        """Persist the System One endpoint."""
        self._config["tool_verdict_endpoint"] = str(endpoint).strip()
        self._save()

    def get_tool_verdict_model(self) -> str:
        """Model name at the endpoint."""
        value = self._config.get("tool_verdict_model")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return self.TOOL_VERDICT_DEFAULT_MODEL

    def set_tool_verdict_model(self, model: str) -> None:
        """Persist the decision-model name."""
        self._config["tool_verdict_model"] = str(model).strip()
        self._save()

    def get_tool_verdict_keep_threshold(self) -> float:
        """Minimum probability that a result is still needed for it to survive.

        Thresholds do not transfer between models, or between the two Tev1
        sizes, so this is separately settable rather than shared with the
        heuristic's ``keep`` count.
        """
        from novacode_cli.agents.tool_verdicts import DEFAULT_KEEP_THRESHOLD

        raw = self._config.get("tool_verdict_keep_threshold")
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return DEFAULT_KEEP_THRESHOLD
        return value if 0.0 < value < 1.0 else DEFAULT_KEEP_THRESHOLD

    def set_tool_verdict_keep_threshold(self, threshold: float) -> None:
        """Persist the keep threshold."""
        self._config["tool_verdict_keep_threshold"] = float(threshold)
        self._save()

    # ── Model routing (/router, OFF by default) ─────────────────────────────
    #
    # When enabled, each turn is classified by a System One decision model
    # (Jev in the cloud, or a local model such as tev1) and routed to one of
    # the configured routes. A route is a ``provider:model`` pair plus the
    # criteria that describe when it should be chosen, so the router is
    # provider- and model-agnostic: any provider ``build_chat_model`` knows
    # about can be a target.
    #
    # The whole block lives under one top-level ``router`` key so ``_save``'s
    # per-key merge keeps it atomic -- a half-written route list is not a state
    # the reader has to defend against.

    #: Decision-model defaults. Deliberately the same endpoint/model as the
    #: tool-verdict feature, so a user who already configured Jev or tev1 gets
    #: routing working with no extra setup. Stored separately so the two
    #: features can diverge without one silently changing the other.
    ROUTER_DEFAULT_ENDPOINT = TOOL_VERDICT_DEFAULT_ENDPOINT
    ROUTER_DEFAULT_MODEL = TOOL_VERDICT_DEFAULT_MODEL

    #: Minimum classifier confidence for a route to be taken. Below this the
    #: default route wins, because a coin-flip between two models is worse than
    #: a predictable one.
    ROUTER_DEFAULT_MIN_CONFIDENCE = 0.5

    def _router_block(self) -> dict[str, Any]:
        """The active profile settings, with support for pre-profile configs."""
        block = self._config.get("router")
        if not isinstance(block, dict):
            return {}
        profiles = block.get("profiles")
        if isinstance(profiles, dict):
            active = str(block.get("active_profile", "default"))
            profile = profiles.get(active)
            return profile if isinstance(profile, dict) else {}
        return block

    def _migrate_router_profiles(self) -> dict[str, Any]:
        """Convert the legacy single-router block into the Default profile."""
        root = self._config.get("router")
        if not isinstance(root, dict):
            root = {}
        if not isinstance(root.get("profiles"), dict):
            legacy = {
                key: copy.deepcopy(value)
                for key, value in root.items()
                if key not in {"profiles", "active_profile"}
            }
            root = {
                "active_profile": "default",
                "profiles": {"default": {"name": "Default", **legacy}},
            }
            self._config["router"] = root
        elif not root["profiles"]:
            root["profiles"]["default"] = {"name": "Default"}
            root["active_profile"] = "default"
        if root.get("active_profile") not in root["profiles"]:
            root["active_profile"] = next(iter(root["profiles"]))
        return root

    def get_router_profiles(self) -> list[dict[str, str]]:
        """List named router profiles for the /router profile switcher."""
        root = self._migrate_router_profiles()
        return [
            {"id": str(profile_id), "name": str(profile.get("name", profile_id))}
            for profile_id, profile in root["profiles"].items()
            if isinstance(profile, dict)
        ]

    def get_active_router_profile(self) -> str:
        """Return the id of the currently selected router profile."""
        return str(self._migrate_router_profiles()["active_profile"])

    def get_router_profile_settings(self, profile_id: str) -> dict[str, Any]:
        """Return a detached settings copy for one profile."""
        root = self._migrate_router_profiles()
        profile = root["profiles"].get(profile_id)
        if not isinstance(profile, dict):
            raise ValueError(f"Unknown router profile {profile_id!r}.")
        return copy.deepcopy(profile)

    def _store_active_router_block(self, block: dict[str, Any]) -> None:
        """Store settings without losing the profile's display name."""
        root = self._migrate_router_profiles()
        profile_id = str(root["active_profile"])
        existing = root["profiles"].get(profile_id, {})
        profile = copy.deepcopy(block)
        profile.setdefault("name", existing.get("name", profile_id))
        root["profiles"][profile_id] = profile

    def set_active_router_profile(self, profile_id: str) -> None:
        """Select an existing router profile."""
        root = self._migrate_router_profiles()
        if profile_id not in root["profiles"]:
            raise ValueError(f"Unknown router profile {profile_id!r}.")
        root["active_profile"] = profile_id
        self._save()

    def create_router_profile(self, name: str) -> str:
        """Create and activate a blank named routing profile; return its id."""
        import re

        name = name.strip()
        profile_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not profile_id:
            raise ValueError("A router profile needs a name.")
        root = self._migrate_router_profiles()
        if profile_id in root["profiles"]:
            raise ValueError(f"Router profile {name!r} already exists.")
        root["profiles"][profile_id] = {"name": name, "enabled": False, "routes": []}
        root["active_profile"] = profile_id
        self._save()
        return profile_id

    def delete_router_profile(self, profile_id: str) -> None:
        """Delete a profile, retaining at least one selectable profile."""
        root = self._migrate_router_profiles()
        profiles = root["profiles"]
        if profile_id not in profiles:
            raise ValueError(f"Unknown router profile {profile_id!r}.")
        if len(profiles) == 1:
            raise ValueError("At least one router profile must remain.")
        del profiles[profile_id]
        if root["active_profile"] == profile_id:
            root["active_profile"] = next(iter(profiles))
        self._save()

    def get_router_enabled(self) -> bool:
        """Whether turns are routed by a decision model.

        Parsed strictly rather than with ``bool()`` for the same reason as
        :meth:`get_tool_verdicts_enabled`: ``bool("false")`` is ``True``, and
        this is the master switch for a feature that is off by default.
        """
        return _config_bool(self._router_block().get("enabled", False))

    def set_router_enabled(self, enabled: bool) -> None:
        """Persist the routing flag."""
        block = self._router_block()
        block["enabled"] = bool(enabled)
        self._store_active_router_block(block)
        self._save()

    def get_router_routes(self) -> list[dict[str, Any]]:
        """The configured routes, in the order they were added.

        Returns a deep copy: callers mutate what they get back (the TUI screen
        edits a working list), and a shared reference would let an abandoned
        edit reach the live config.
        """
        routes = self._router_block().get("routes")
        if not isinstance(routes, list):
            return []
        return [copy.deepcopy(r) for r in routes if isinstance(r, dict)]

    def set_router_routes(self, routes: list[dict[str, Any]]) -> None:
        """Persist the route list, validating it first.

        Validation is here rather than in the UI because this is the last point
        every writer passes through, and a route with an unknown provider would
        otherwise fail at model-construction time -- mid-turn, in the agent,
        where the error is far from its cause.

        Raises:
            ValueError: A route is malformed, ids collide, or a provider is
                unknown.
        """
        from novacode_cli.config.model_manager import MODEL_PRESETS

        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, route in enumerate(routes):
            if not isinstance(route, dict):
                message = f"Route {index} is not an object."
                raise ValueError(message)
            route_id = str(route.get("id", "")).strip()
            provider = str(route.get("provider", "")).strip()
            model = str(route.get("model", "")).strip()
            criteria = str(route.get("criteria", "")).strip()
            if not route_id:
                message = f"Route {index} has no id."
                raise ValueError(message)
            if route_id in seen:
                message = f"Duplicate route id {route_id!r}."
                raise ValueError(message)
            if provider not in MODEL_PRESETS:
                message = f"Route {route_id!r} has unknown provider {provider!r}."
                raise ValueError(message)
            if not model:
                message = f"Route {route_id!r} has no model."
                raise ValueError(message)
            if not criteria:
                message = f"Route {route_id!r} has no criteria."
                raise ValueError(message)
            seen.add(route_id)
            entry: dict[str, Any] = {
                "id": route_id,
                "provider": provider,
                "model": model,
                "criteria": criteria,
            }
            base_url = route.get("base_url")
            if isinstance(base_url, str) and base_url.strip():
                entry["base_url"] = base_url.strip()
            cleaned.append(entry)

        block = self._router_block()
        block["routes"] = cleaned
        self._store_active_router_block(block)
        self._save()

    def get_router_default_route(self) -> str | None:
        """Route id used when the classifier fails or is unsure."""
        value = self._router_block().get("default_route")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def set_router_default_route(self, route_id: str | None) -> None:
        """Persist the fallback route id (``None`` clears it)."""
        block = self._router_block()
        if route_id is None or not str(route_id).strip():
            block.pop("default_route", None)
        else:
            block["default_route"] = str(route_id).strip()
        self._store_active_router_block(block)
        self._save()

    def get_router_decision_endpoint(self) -> str:
        """System One endpoint the routing decisions are asked of."""
        value = self._router_block().get("decision_endpoint")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return self.ROUTER_DEFAULT_ENDPOINT

    def set_router_decision_endpoint(self, endpoint: str) -> None:
        """Persist the routing decision endpoint."""
        block = self._router_block()
        block["decision_endpoint"] = str(endpoint).strip()
        self._store_active_router_block(block)
        self._save()

    def get_router_decision_model(self) -> str:
        """Model name at the routing decision endpoint."""
        value = self._router_block().get("decision_model")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return self.ROUTER_DEFAULT_MODEL

    def set_router_decision_model(self, model: str) -> None:
        """Persist the routing decision model name."""
        block = self._router_block()
        block["decision_model"] = str(model).strip()
        self._store_active_router_block(block)
        self._save()

    def get_router_min_confidence(self) -> float:
        """Minimum classifier confidence for a route to be taken."""
        raw = self._router_block().get("min_confidence")
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return self.ROUTER_DEFAULT_MIN_CONFIDENCE
        return value if 0.0 < value <= 1.0 else self.ROUTER_DEFAULT_MIN_CONFIDENCE

    def set_router_min_confidence(self, threshold: float) -> None:
        """Persist the minimum routing confidence."""
        block = self._router_block()
        block["min_confidence"] = float(threshold)
        self._store_active_router_block(block)
        self._save()
    # ── Voice config (local STT / VAD / TTS) ────────────────────────────────

    VOICE_DEFAULTS: dict[str, Any] = {  # noqa: RUF012
        "enabled": False,
        "mode": "push_to_talk",  # "push_to_talk" | "listen"
        "speak_responses": True,
        "stt_provider": "faster-whisper",
        "tts_provider": "piper",
        # Legacy flat keys (deprecated — kept for backward compat; merged into providers dict on save).
        "stt_model": "base",
        "stt_device": "auto",
        "tts_voice": "en_US-lessac-medium",
        # Per-provider configuration (keys match provider ids in audio/providers.py).
        "providers": {
            "faster-whisper": {"model": "distil-large-v3", "device": "auto"},
            "deepgram": {"api_key": "", "model": "nova-2"},
            "piper": {"voice": "en_US-lessac-medium"},
            "elevenlabs": {"api_key": "", "voice_id": "21m00Tcm4TlvDq8ikWAM"},
            "orpheus": {"voice": "tara", "lang": "en"},
        },
    }

    def get_voice_config(self) -> dict[str, Any]:
        """Return the saved voice settings merged over the defaults."""
        merged = dict(self.VOICE_DEFAULTS)
        cfg = self._config.get("voice")
        if isinstance(cfg, dict):
            merged.update({k: cfg[k] for k in cfg if k in self.VOICE_DEFAULTS})
        return merged

    def set_voice_config(self, **updates: Any) -> dict[str, Any]:
        """Merge ``updates`` (known keys only) into the voice config and persist."""
        cfg = self.get_voice_config()
        cfg.update({k: v for k, v in updates.items() if k in self.VOICE_DEFAULTS})
        self._config["voice"] = cfg
        self._save()
        return cfg

    def get_voice_provider_config(self, provider: str) -> dict[str, Any]:
        """Return saved config for a specific voice provider (e.g. deepgram, elevenlabs)."""
        cfg = self.get_voice_config()
        providers = cfg.get("providers", {})
        return dict(providers.get(provider, {}))

    def set_voice_provider_config(self, provider: str, **updates: Any) -> dict[str, Any]:
        """Merge ``updates`` into a provider's config and persist."""
        cfg = self.get_voice_config()
        providers = dict(cfg.get("providers", {}))
        pcfg = dict(providers.get(provider, {}))
        pcfg.update(updates)
        providers[provider] = pcfg
        cfg["providers"] = providers
        self._config["voice"] = cfg
        self._save()
        return pcfg

    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value.

        Args:
            key: Configuration key
            default: Default value if key doesn't exist

        Returns:
            Configuration value or default
        """
        return self._config.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a configuration value.

        Args:
            key: Configuration key
            value: Value to set
        """
        self._config[key] = value
        self._save()

    def delete(self, key: str) -> None:
        """Delete a configuration value.

        Args:
            key: Configuration key to delete
        """
        if key in self._config:
            del self._config[key]
            self._save()

    def get_all(self) -> dict[str, Any]:
        """Get all configuration values.

        Returns:
            Copy of all configuration
        """
        return self._config.copy()
