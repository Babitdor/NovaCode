"""Skills middleware: a small listing, per-turn suggestions, and a search tool.

deepagents' :class:`SkillsMiddleware` puts every skill's name, description and
path in the system prompt on every call: ~200k chars for Nova's ~630 skills.
This subclass keeps the progressive-disclosure contract but tiers it, the way
Claude Code (1% of the window), Codex (2%) and Anthropic's Tool Search Tool do:

1. **Listing** — only the most-used skills, within a char budget. Frozen for
   the life of the middleware so the cached prompt prefix never changes.
2. **Suggestions** — each new user message is matched against the whole
   library (:mod:`novacode_cli.skills.retrieval`) and the few that clear a
   relevance bar are added as an ``Internal context`` message. Automatic,
   because agents are poor at noticing when they need a skill
   (arXiv 2604.24594); in the messages, not the system prompt, so the cache
   holds.
3. **``skills_search``** — for needs that surface mid-task.

It also re-lists when the watched skill directories change, so a skill created
mid-session (``skill_manage`` / Hermes review) is usable at once (see
``specs/2026-06-22-mid-session-skill-refresh-design.md``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, NotRequired

from deepagents.middleware.skills import SkillsMiddleware, SkillsState
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from novacode_cli.skills.retrieval import first_sentence, get_index, prewarm
from novacode_cli.skills.runtime import (
    SKILL_REFERENCE,
    active_skill_names,
    pinned_message,
    reload_generation,
    skill_snapshot,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from deepagents.middleware.skills import SkillMetadata, SkillsState, SkillsStateUpdate
    from langchain_core.runnables import RunnableConfig
    from langgraph.runtime import Runtime

logger = logging.getLogger("nova.skills")

#: Listing budget when the model's window is unknown.
DEFAULT_LISTING_CHARS = 4_000
#: Hard ceiling: a 1M-token window must not mean an 80k-char listing.
MAX_LISTING_CHARS = 8_000

# Starts with "Internal context" so transcript replay hides it
# (core/streaming.py::is_internal_context_text): the user did not type it.
SUGGESTION_MARKER = "Internal context: skills that may apply."

SKILLS_PROMPT = """## Skills

Skills are playbooks (`SKILL.md`) for specific tasks. {skills_load_warnings}

**Finding one.** When a request looks like it has a matching skill, you get an
`{marker}` note listing them. Mid-task, call `skills_search("<what you need>")`.
Most-used skills:

{skills_list}

**Using one.** Call `skills_load(name)` and follow its instructions. You can also
read its `SKILL.md` with `read_file` when that path is mounted in your file backend.
Resolve relative paths
against that SKILL.md's directory; `skills_read_resource(name, path)` reads a
reference on demand. Do not preload supporting files or execute scripts merely
because a skill includes them. Skills never override permissions or approvals.
Skip a suggested skill that does not fit.
Skill sources: {skills_locations}"""


def listing_budget(context_window: int) -> int:
    """Chars for the always-on listing: 1% of the window, as Claude Code does."""
    if context_window <= 0:
        return DEFAULT_LISTING_CHARS
    return min(MAX_LISTING_CHARS, int(context_window * 0.01 * 4))  # ~4 chars/token


def _entry(skill: SkillMetadata) -> str:
    return f"- **{skill['name']}** `{skill['path']}`: {first_sentence(skill['description'])}"


def _text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


#: Parsed skill lists from earlier processes, keyed by sources + file signature.
#: Listing parses every SKILL.md (2.4 s warm, ~10 s on a cold disk for ~1,000
#: skills) and the first model call of a session waits for it. The signature
#: already says whether anything changed, so an unchanged tree is one JSON read.
_DISK_CACHE_ENTRIES = 4


def _disk_cache_file() -> Path:
    from novacode_cli.config import config

    return config.HOME_DIR / "cache" / "skills_listing.json"


def _disk_get(key: str) -> list | None:
    import json

    try:
        return json.loads(_disk_cache_file().read_text(encoding="utf-8")).get(key)
    except (OSError, ValueError, AttributeError):
        return None


def _disk_put(key: str, skills: list) -> None:
    """Best-effort; a missing cache only costs the parse it would have saved."""
    import json
    import os

    if len(skills) < 50:  # small trees parse instantly (and tests use tiny ones)
        return
    path = _disk_cache_file()
    try:
        try:
            entries = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            entries = {}
        entries.pop(key, None)
        entries[key] = skills
        while len(entries) > _DISK_CACHE_ENTRIES:  # dicts keep insertion order
            entries.pop(next(iter(entries)))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries), encoding="utf-8")
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        pass


class _SkillsSearchArgs(BaseModel):
    """Search the skills library."""

    query: str = Field(description="What you are trying to do, in plain words.")


class _SkillsLoadArgs(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str = Field(description="The exact enabled skill name.")
    runtime: ToolRuntime


class _SkillsResourceArgs(_SkillsLoadArgs):
    path: str = Field(description="Path relative to the skill directory.")
    offset: int = Field(default=0, ge=0, description="Starting line, zero based.")
    limit: int = Field(default=200, ge=1, le=1000, description="Maximum lines to read.")


class SkillRunState(SkillsState):
    pinned_skills: NotRequired[list[str]]


class RefreshingSkillsMiddleware(SkillsMiddleware):
    """Tiered skills: budgeted listing + per-turn suggestions + ``skills_search``."""

    state_schema = SkillRunState

    def __init__(
        self,
        *,
        backend: object,
        sources: Sequence[object],
        watch_dirs: Sequence[Path] = (),
        listing_chars: int = DEFAULT_LISTING_CHARS,
        tools: Sequence[Any] | Callable[..., Any] = (),
        library_names: Sequence[str] | None = None,
    ) -> None:
        """``listing_chars=0`` lists nothing (subagents: suggestions + search only)."""
        super().__init__(
            backend=backend,  # type: ignore[arg-type]
            sources=sources,  # type: ignore[arg-type]
            system_prompt=SKILLS_PROMPT.replace("{marker}", SUGGESTION_MARKER),
        )
        self._watch_dirs = [Path(d) for d in watch_dirs]
        self._library_names = tuple(library_names) if library_names is not None else None
        self._last_signature: frozenset[tuple[str, float]] | None = None
        self._listing_chars = listing_chars
        self._usage: dict[str, int] | None = None  # read once, then frozen
        self._skills: list[SkillMetadata] = []
        # Every skill, as loaded from disk. Held HERE, not in graph state: state
        # is serialized into every checkpoint, and 1,089 skills are ~386 KB —
        # written on every agent step and re-read at the start of every turn.
        # That one key was most of a 12.7 GB checkpoint database.
        self._all_skills: list[SkillMetadata] | None = None
        self._load_errors: list[str] = []
        self._load_lock = threading.Lock()
        self._reload_generation = reload_generation()
        self._tool_resolver = tools if callable(tools) else None
        self._private_tools = {} if self._tool_resolver else {tool.name: tool for tool in tools}
        prewarm()  # load the embedder while the user types, not on turn one
        self.tools = [
            StructuredTool.from_function(
                self._skill_load,
                name="skills_load",
                response_format="content_and_artifact",
                args_schema=_SkillsLoadArgs,
                description="Load an enabled skill's instructions and activate its optional tools.",
            ),
            StructuredTool.from_function(
                self._read_resource,
                name="skills_read_resource",
                args_schema=_SkillsResourceArgs,
                description="Read a skill reference on demand. Path is relative to its SKILL.md directory.",
            ),
            StructuredTool.from_function(
                self._skill_search,
                name="skills_search",
                description=(
                    "Search the skill library (playbooks for specific tasks) by what you "
                    "need to do. Returns the best matches with the path of each SKILL.md."
                ),
                # Given, not inferred: inference builds three pydantic models per
                # call (~6.6 ms), and deepagents creates one of these middlewares
                # for each of ~90 subagents at agent-build time.
                args_schema=_SkillsSearchArgs,
            ),
        ]
        # Keep search first for integrations that use the original single-tool API.
        self.tools.sort(key=lambda tool: tool.name != "skills_search")

    def _skill_backend(self, state: dict, runtime: Any, config: dict) -> Any:
        """Resolve older factory backends and the current instance-only API."""
        getter = getattr(super(), "_get_backend", None)
        if getter is not None:
            return getter(state, runtime, config)
        return self._backend

    def _download_skill(
        self, skill: dict, runtime: Any, state: dict | None = None, config: dict | None = None
    ) -> str:
        backend = self._skill_backend(
            state if state is not None else getattr(runtime, "state", {}),
            runtime,
            config if config is not None else getattr(runtime, "config", {}),
        )
        result = backend.download_files([skill["path"]])[0]
        if result.error or result.content is None:
            raise OSError(f"Could not read skill: {result.error}")
        if len(result.content) > 10 * 1024 * 1024:
            raise ValueError("SKILL.md exceeds 10 MB")
        return result.content.decode("utf-8")

    def _skill_load(self, name: str, runtime: ToolRuntime) -> tuple[str, dict]:
        """Load one enabled skill without reading any supporting files."""
        skill = next((s for s in self._skills if s["name"] == name), None)
        if skill is None:
            return f"Error: skill {name!r} is unknown or disabled.", {}
        try:
            content = self._download_skill(skill, runtime)
        except (OSError, ValueError, UnicodeError) as exc:
            return f"Error: {exc}", {}
        body, snapshot = skill_snapshot(skill, content)
        return f"Skill: {name}\nPath: {skill['path']}\n\n{body}", {"skill": snapshot}

    def _read_resource(
        self, name: str, path: str, runtime: ToolRuntime, offset: int = 0, limit: int = 200
    ) -> str:
        """Read a bounded page from a resource within an enabled skill."""
        import posixpath
        from pathlib import PurePosixPath

        skill = next((s for s in self._skills if s["name"] == name), None)
        relative = PurePosixPath(path.replace("\\", "/"))
        if skill is None:
            return "Error: unknown or disabled skill."
        if relative.is_absolute() or ".." in relative.parts or ":" in path or not path:
            return "Error: resource path must stay inside the skill directory."
        resource = posixpath.join(
            posixpath.dirname(skill["path"].replace("\\", "/")), str(relative)
        )
        backend = self._skill_backend(runtime.state, runtime, runtime.config)
        result = backend.read(resource, offset=max(0, offset), limit=max(1, min(limit, 1000)))
        if isinstance(result, str):
            return result[:64_000]
        if result.error or result.file_data is None:
            return f"Error: {result.error or 'resource unavailable'}"
        if result.file_data.get("encoding") == "base64":
            return "Error: binary resources must be opened with the file/media tools."
        return str(result.file_data["content"])[:64_000]

    def _active_tools(self, messages: list[Any]) -> set[str]:
        active = active_skill_names(messages, self._skills)
        names: set[str] = set()
        for skill in self._skills:
            if skill["name"] in active:
                metadata = skill.get("metadata") or {}
                include = metadata.get("include_tools", "") if isinstance(metadata, dict) else ""
                if isinstance(include, str):
                    names.update(include.split())
        return names

    def _resolve_tools(self, messages: list[Any], runtime: Any) -> dict[str, Any]:
        """Resolve only active aliases; never cache tools across run contexts."""
        import inspect

        resolved = {}
        for name in sorted(self._active_tools(messages)):
            if self._tool_resolver is None:
                found = [self._private_tools[name]] if name in self._private_tools else []
            else:
                found = self._tool_resolver(name, runtime)
                if inspect.isawaitable(found):
                    if inspect.iscoroutine(found):
                        found.close()
                    message = "Async skill tool resolvers require ainvoke or astream."
                    raise RuntimeError(message)
            resolved.update((tool.name, tool) for tool in found or [])
        return resolved

    async def _aresolve_tools(self, messages: list[Any], runtime: Any) -> dict[str, Any]:
        import inspect

        if self._tool_resolver is None:
            return self._resolve_tools(messages, runtime)
        resolved = {}
        for name in sorted(self._active_tools(messages)):
            found = self._tool_resolver(name, runtime)
            if inspect.isawaitable(found):
                found = await found
            resolved.update((tool.name, tool) for tool in found or [])
        return resolved

    def _bind_skill_tools(self, request: Any, resolved: dict) -> Any:
        # An ordinary graph tool wins a name collision and remains callable
        # independently. Skill-owned tools are bound and dispatched dynamically.
        known = {getattr(tool, "name", None) for tool in request.tools}
        tools = [
            tool.model_copy(
                update={"metadata": {**(tool.metadata or {}), "_nova_skill_active": True}}
            )
            if getattr(tool, "name", None) in resolved and hasattr(tool, "model_copy")
            else tool
            for tool in request.tools
        ]
        tools.extend(
            tool.model_copy(
                update={"metadata": {**(tool.metadata or {}), "_nova_skill_active": True}}
            )
            for name, tool in resolved.items()
            if name not in known
        )
        return super().modify_request(
            request.override(
                tools=tools, state={**request.state, "skills_load_errors": self._load_errors}
            )
        )

    def modify_request(self, request: Any) -> Any:
        """Expose active skill tools in a synchronous model request."""
        return self._bind_skill_tools(
            request, self._resolve_tools(request.messages, request.runtime)
        )

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        """Resolve async tool aliases before binding the next model request."""
        resolved = await self._aresolve_tools(request.messages, request.runtime)
        return await handler(self._bind_skill_tools(request, resolved))

    def wrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Recheck activation and runtime context before dispatching private tools."""
        name = request.tool_call["name"]
        if request.tool is not None:
            return handler(request)
        resolved = self._resolve_tools(request.state.get("messages", []), request.runtime)
        if name in resolved:
            return handler(request.override(tool=resolved[name]))
        if name in self._private_tools:
            return ToolMessage(
                content="Error: load the enabled skill before using this tool.",
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        return handler(request)

    async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
        """Async activation/context check, with no shared resolved-tool cache."""
        name = request.tool_call["name"]
        if request.tool is not None:
            return await handler(request)
        resolved = await self._aresolve_tools(request.state.get("messages", []), request.runtime)
        if name in resolved:
            return await handler(request.override(tool=resolved[name]))
        if name in self._private_tools:
            return ToolMessage(
                content="Error: load the enabled skill before using this tool.",
                tool_call_id=request.tool_call["id"],
                status="error",
            )
        return await handler(request)

    # ── skills_search ──────────────────────────────────────────────────────

    def _skill_search(self, query: str) -> str:
        """Search the skills library.

        Args:
            query: What you are trying to do, in plain words.
        """
        hits = get_index(self._skills).search(query, k=5) if self._skills else []
        if not hits:
            return f"No skill matches {query!r}. Proceed without one."
        return "\n".join(
            f"- **{s['name']}** `{s['path']}`: {first_sentence(s['description'], 300)}"
            for s, _, _ in hits
        )

    # ── tier 1: the listing ────────────────────────────────────────────────

    def _format_skills_list(self, skills: list[SkillMetadata]) -> str:
        """The most-used skills that fit the budget; the rest via search."""
        # deepagents passes state["skills_metadata"], which is deliberately empty
        # now (see _all_skills) — the list lives on the instance.
        skills = skills or self._skills
        usage = self._usage or {}
        ranked = sorted(skills, key=lambda s: -usage.get(s["name"], 0))  # stable: source order
        lines: list[str] = []
        used = 0
        for skill in ranked:
            line = _entry(skill)
            if used + len(line) > self._listing_chars:
                break
            lines.append(line)
            used += len(line) + 1
        rest = len(skills) - len(lines)
        if rest:
            lines.append(f"- ({rest} more skills: `skills_search` finds them)")
        return "\n".join(lines)

    # ── tier 2: suggestions ────────────────────────────────────────────────

    def _suggestions(self, state: SkillsState, skills: list[SkillMetadata]) -> HumanMessage | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, HumanMessage):
            return None
        query = _text(last)
        if not query.strip() or query.lstrip().startswith("Internal context"):
            return None
        already = {
            name
            for m in messages
            if isinstance(m, HumanMessage) and _text(m).startswith(SUGGESTION_MARKER)
            for name in re.findall(r"^- \*\*([^*]+)\*\*", _text(m), re.M)
        }
        picks = get_index(skills).suggest(query[:2000], exclude=already)
        if not picks:
            return None
        body = "\n".join(_entry(s) for s in picks)
        return HumanMessage(
            content=(
                f"{SUGGESTION_MARKER} If one fits this request, read its SKILL.md "
                f"before starting; otherwise ignore this.\n{body}"
            )
        )

    async def _read_usage(self, runtime: Runtime) -> None:
        """Skill invocation counts from Hermes' tracker (best effort, once)."""
        if self._usage is not None:
            return
        self._usage = {}
        store = getattr(runtime, "store", None)
        if store is None:
            return
        try:
            for item in await store.asearch(("nova", "skill_usage"), limit=1000):
                self._usage[item.key] = int((item.value or {}).get("invocations", 0))
        except Exception:  # noqa: BLE001 — ranking is a nicety, never break a turn
            logger.debug("skill usage unavailable", exc_info=True)

    def _load_skills(
        self, runtime: Runtime, config: RunnableConfig, *, force: bool = False
    ) -> None:
        """(Re)load the skill list into the instance when files changed.

        Blocking (directory walk + a parse per SKILL.md, ~2 s for 1,000 skills),
        so the async path runs it in a worker thread.
        """
        from novacode_cli.skills.skills_prefs import effective_disabled

        with self._load_lock:
            changed = (
                self._skills_changed() or self._reload_generation != reload_generation() or force
            )
            self._reload_generation = reload_generation()
            if self._all_skills is None or changed:
                key = self._disk_key()
                cached = _disk_get(key) if key and not changed else None
                if cached is not None:
                    self._all_skills = cached
                else:
                    # An empty state makes the base class list unconditionally.
                    if self._library_names is None:
                        update = SkillsMiddleware.before_agent(self, {}, runtime, config)  # type: ignore[arg-type]
                    else:
                        from deepagents.middleware.skills import _list_skills_with_errors

                        from novacode_cli.skills.libraries import LibraryDiscoveryBackend

                        backend = LibraryDiscoveryBackend(
                            self._skill_backend({}, runtime, config), self._library_names
                        )
                        selected = {}
                        errors = []
                        for source in self.sources if self._library_names else []:
                            entries, error = _list_skills_with_errors(backend, source)
                            selected.update(
                                (skill["name"], skill)
                                for skill in entries
                                if skill["name"] in self._library_names
                            )
                            if error:
                                errors.append(error)
                        update = {
                            "skills_metadata": list(selected.values()),
                            "skills_load_errors": errors,
                        }
                    self._all_skills = list((update or {}).get("skills_metadata") or [])
                    self._load_errors = list((update or {}).get("skills_load_errors") or [])
                    if key:
                        _disk_put(key, self._all_skills)
            disabled = effective_disabled()
            self._skills = [s for s in self._all_skills if s["name"] not in disabled]
            # Build (or fetch) the search index HERE, on the worker thread. Left
            # to _suggestions it was built on the UI's loop on first use and
            # after every skill change: ~440 ms of BM25 + embeddings for 1,000
            # skills, the largest single stall of a session's first turn.
            try:
                if self._skills:
                    get_index(self._skills)
            except Exception:  # noqa: BLE001 — suggestions are optional
                logger.debug("skill index warm-up failed", exc_info=True)

    def _disk_key(self) -> str | None:
        """Identifies "these sources, with these exact files" for the disk cache."""
        if self._last_signature is None:
            return None
        import hashlib

        blob = repr(
            (list(map(str, self.sources)), self._library_names, sorted(self._last_signature))
        )
        return hashlib.sha1(blob.encode("utf-8"), usedforsecurity=False).hexdigest()

    def _finish(self, state: SkillsState) -> SkillsStateUpdate | None:
        update: dict[str, Any] = {}
        # A thread saved before this change still carries the full list; blank
        # it so its later checkpoints stop paying for it too.
        if "skills_metadata" in state and state.get("skills_metadata") != []:
            update["skills_metadata"] = []
        try:
            note = self._suggestions(state, self._skills) if self._skills else None
        except Exception:  # noqa: BLE001 — retrieval must never break a turn
            logger.debug("skill suggestion failed", exc_info=True)
            note = None
        if note is not None:
            update["messages"] = [note]
        return update or None  # type: ignore[return-value]

    def _pin_named_skills(
        self, state: SkillsState, runtime: Any, config: dict, update: dict | None
    ) -> dict | None:
        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        explicit = state.get("pinned_skills") or []
        if not explicit and (
            not isinstance(last, HumanMessage)
            or _text(last).startswith("Internal context")
            or last.additional_kwargs.get("lc_source") == "pinned_skill"
        ):
            return update
        names = list(
            dict.fromkeys(
                [
                    *explicit,
                    *(
                        SKILL_REFERENCE.findall(_text(last))
                        if isinstance(last, HumanMessage)
                        else []
                    ),
                ]
            )
        )
        if explicit:
            update = dict(update or {})
            update["pinned_skills"] = []
        if not names:
            return update
        by_name = {s["name"]: s for s in self._skills}
        pins = []
        for name in names:
            skill = by_name.get(name)
            if skill is None:
                logger.debug("Skipping unknown or disabled pinned skill %s", name)
                continue
            try:
                pins.append(
                    pinned_message(skill, self._download_skill(skill, runtime, state, config))
                )
            except (OSError, ValueError, UnicodeError):
                logger.debug("Could not pin skill %s", name, exc_info=True)
        if pins:
            update = dict(update or {})
            update["messages"] = [*(update.get("messages") or []), *pins]
        return update

    # ── refresh when skill files change ────────────────────────────────────

    def _compute_signature(self) -> frozenset[tuple[str, float]]:
        """A (path, mtime) frozenset over watched ``*/SKILL.md`` files + prefs.

        The skill-curation preference files are folded in so toggling a skill
        on/off (which only rewrites ``skills_prefs.json``) forces a re-list on
        the next turn — the curated set updates without a restart.
        """
        import os

        from novacode_cli.skills.skills_prefs import prefs_signature

        # os.scandir + os.stat, not Path.glob + Path.stat: this runs before
        # every turn over ~1,000 skills, and pathlib's per-entry object churn
        # made it the slowest part of the check (160 ms vs ~40 ms).
        sig: set[tuple[str, float]] = set()
        for directory in self._watch_dirs:
            if self._library_names is not None:
                for name in self._library_names:
                    skill_md = str(directory / name / "SKILL.md")
                    try:
                        stat = os.stat(skill_md)
                        sig.add((skill_md, (stat.st_mtime_ns, stat.st_size)))
                    except OSError:
                        continue
                continue
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        skill_md = os.path.join(entry.path, "SKILL.md")
                        try:
                            stat = os.stat(skill_md)
                            sig.add((skill_md, (stat.st_mtime_ns, stat.st_size)))
                        except OSError:
                            continue
            except OSError:
                continue
        return frozenset(sig | prefs_signature())

    def _skills_changed(self) -> bool:
        """True if the skill files changed since the last call (best-effort)."""
        if not self._watch_dirs:
            return False
        try:
            current = self._compute_signature()
        except Exception:  # noqa: BLE001 - best-effort; never break a turn
            return False
        changed = current != self._last_signature
        self._last_signature = current
        return changed

    def before_agent(
        self, state: SkillsState, runtime: Runtime, config: RunnableConfig
    ) -> SkillsStateUpdate | None:
        """Re-list if skills changed, load, then suggest for the new message."""
        self._load_skills(
            runtime, config, force="skills_metadata" in state and state["skills_metadata"] is None
        )
        return self._pin_named_skills(state, runtime, config, self._finish(state))

    async def abefore_agent(
        self, state: SkillsState, runtime: Runtime, config: RunnableConfig
    ) -> SkillsStateUpdate | None:
        """Async twin of :meth:`before_agent` (the path the agent actually runs)."""
        await self._read_usage(runtime)
        # Off the event loop: the change check stats every SKILL.md and a reload
        # parses them all, and this loop is the one the UI paints on.
        await asyncio.to_thread(
            self._load_skills,
            runtime,
            config,
            force="skills_metadata" in state and state["skills_metadata"] is None,
        )
        return await asyncio.to_thread(
            self._pin_named_skills, state, runtime, config, self._finish(state)
        )


class SubagentSkillsMiddleware(RefreshingSkillsMiddleware):
    """What deepagents builds for subagents: no listing, suggestions + search.

    A subagent's first message is its task description, which is exactly the
    query the suggestions need.
    """

    def __init__(self, *, backend: object, sources: Sequence[object], **options: Any) -> None:
        """Signature-compatible with deepagents' ``SkillsMiddleware(backend=, sources=)``."""
        super().__init__(
            backend=backend,
            sources=sources,
            listing_chars=0,
            watch_dirs=options.get("watch_dirs", ()),
            tools=options.get("tools", ()),
            library_names=options.get("library_names"),
        )
        if not self._watch_dirs and not callable(backend):
            for source in self.sources:
                route = getattr(backend, "_get_backend_and_key", None)
                target, key = route(source) if route else (backend, source)
                root = getattr(target, "cwd", None)
                if root is not None:
                    self._watch_dirs.append(Path(root) / key.lstrip("/"))
