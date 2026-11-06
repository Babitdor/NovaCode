"""Canonical event-loop entry and explicit suspended-run continuation."""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from novacode_cli import ui_events as ev
from novacode_cli.prime_factory import DEFAULT_OUTPUT_LIMIT, ROLES
from novacode_cli.prime_factory.controller import FactoryController, storage_path
from novacode_cli.prime_factory.runner import SpecialistRunner
from novacode_cli.prime_factory.store import FactoryError, FactoryStore
from novacode_cli.prime_factory.workflow import FactoryWorkflow
from novacode_cli.prime_memory import session_directory


def is_factory_request(text: str, session: Any) -> bool:  # noqa: ANN401 - Session proxies.
    """Only explicit development goals or selected continuations start factory work."""
    if getattr(session, "prime_enabled", False) is not True:
        return False
    normalized = text.strip().lower()
    if re.match(
        r"^(?:(?:can you|could you|please)\s+)?"
        r"(?:update me\b|test results\b|what\b|why\b|how\b|explain\b|"
        r"describe\b|show\b|summarize\b|status\b)",
        normalized,
    ):
        return False
    if getattr(session, "prime_factory_resume", None):
        return True
    if normalized.startswith("continue") and getattr(session, "prime_pending", None):
        return True
    return bool(
        re.match(
            r"^(?:(?:please|can you|could you|i want (?:you )?to)\s+)?"
            r"(?:fix|build|implement|add|create|replace|refactor|remove|update|upgrade|develop|"
            r"resolve|repair|complete|test|optimize|migrate|finish)\b",
            normalized,
        )
    )


def model_routes(profile: Any, session: Any, agent: Any) -> dict[str, Any]:  # noqa: ANN401 - Framework routes.
    """Inherit the actual bound session model while keeping configured roles exact."""
    inherited = getattr(agent, "factory_model_route", None) or profile.get_model_config() or {}
    override = getattr(session, "cli_model_override", None)
    if isinstance(override, str) and ":" in override:
        provider, model = override.split(":", 1)
        inherited = {"provider": provider, "model": model}
    routes = {role: profile.get_factory_model(role) or inherited for role in ROLES}
    session_provider = getattr(session, "session_model_provider", None)
    if any(not route.get("provider") or not route.get("model") for route in routes.values()):
        message = "Factory requires explicit provider/model routes or a serializable session model"
        raise FactoryError(message)
    if (
        session_provider
        and session_provider != inherited.get("provider")
        and any(not profile.get_factory_model(role) for role in ROLES)
    ):
        message = "Session model route is unavailable; configure factory role models"
        raise FactoryError(message)
    return routes


async def prepare_controller(
    text: str,
    session: Any,  # noqa: ANN401 - Session proxies.
    agent: Any,  # noqa: ANN401 - Compiled graph implementations.
    store: FactoryStore,
    root: Path,
    *,
    requested_thread: str | None = None,
) -> FactoryController:
    """Load a durable continuation or persist a new explicitly requested goal."""
    requested_thread = session.thread_id if requested_thread is None else requested_thread
    if session.thread_id != requested_thread:
        message = "Conversation changed during factory startup; no work was dispatched"
        raise FactoryError(message)
    identity = getattr(session, "prime_factory_resume", None)
    if identity:
        record = await asyncio.to_thread(store.load, identity)
        saved_root = await asyncio.to_thread(Path(record["root"]).resolve)
        if saved_root != root:
            message = "Resume from the factory's original repository workspace"
            raise FactoryError(message)
        record.setdefault("origin_thread", record["thread"])
        record["thread"] = requested_thread
        controller = FactoryController(store, record, session)
        await controller.resume()
        controller.ensure_active()
        session.prime_factory_resume = None
        if record["stage"] not in {"requirements", "research", "architecture", "plan_approval"}:
            record["stage"] = record["phase"] = "plan_approval"
        controller.save()
        session.prime_factory = controller
        return controller
    existing = getattr(session, "prime_report", None)
    if (
        isinstance(existing, dict)
        and existing.get("factory")
        and existing.get("status")
        not in {
            "published",
            "cancelled",
        }
    ):
        message = f"Resume or cancel existing factory run {existing['id']} first"
        raise FactoryError(message)
    from novacode_cli.config.nova_config import NovaConfig

    profile = await asyncio.to_thread(NovaConfig)
    routes = await asyncio.to_thread(model_routes, profile, session, agent)
    output_limit = profile.get("max_tokens") or DEFAULT_OUTPUT_LIMIT
    if type(output_limit) is not int or output_limit <= 0:
        message = "Factory model output limit must be a positive integer"
        raise FactoryError(message)
    pending = getattr(session, "prime_pending", None)
    goal = pending.get("goal", text) if isinstance(pending, dict) else text
    controller = await asyncio.to_thread(
        FactoryController.create,
        root,
        goal,
        requested_thread,
        routes,
        session=session,
        store=store,
    )
    container = profile.get("team_container")
    controller.record["output_token_limit"] = output_limit
    controller.save()
    if container is not None:
        if not isinstance(container, dict):
            message = "Configured container profile is invalid"
            raise FactoryError(message)
        controller.record["isolation"] = {
            "workers": "container",
            "integration_checks": "worktree",
            "container_profile": {
                key: container.get(key)
                for key in (
                    "image",
                    "network",
                    "user",
                    "memory",
                    "cpus",
                    "readonly",
                )
            },
        }
        controller.save()
    controller.ensure_active()
    session.prime_pending = None
    session.prime_factory = controller
    return controller


async def iterate_factory_events(  # noqa: PLR0912, PLR0915 - Event stream and cancellation cleanup.
    text: str,
    session: Any,  # noqa: ANN401 - Session proxies.
    *,
    agent: Any = None,  # noqa: ANN401 - Compiled graph implementations.
) -> AsyncIterator[Any]:
    """Drive host transitions while keeping approval routing in the originating consumer."""
    controller = None
    driver = None
    store = None
    originating_thread = session.thread_id
    try:
        root = await asyncio.to_thread(lambda: Path(session_directory(session)).resolve())
        store = await asyncio.to_thread(FactoryStore, storage_path(root))
        controller = await prepare_controller(
            text,
            session,
            agent,
            store,
            root,
            requested_thread=originating_thread,
        )
        if agent is not None:
            from langchain_core.messages import HumanMessage

            await agent.aupdate_state(
                {"configurable": {"thread_id": controller.record["thread"]}},
                {"messages": [HumanMessage(text, id=uuid.uuid4().hex)]},
            )
        runner = SpecialistRunner(controller)
        workflow = await asyncio.to_thread(FactoryWorkflow, controller, runner)
        driver = asyncio.create_task(
            workflow.run(), name=f"prime-factory-{controller.record['id']}"
        )
        while not driver.done() or not controller.events.empty():
            try:
                event = await asyncio.wait_for(controller.events.get(), timeout=0.25)
            except TimeoutError:
                continue
            if session.thread_id != controller.record["thread"]:
                controller.request_pause()
                if isinstance(event, ev.InterruptRequest) and not event.future.done():
                    event.future.set_result({"response": {"approved": False}})
                continue
            yield event
        await driver
        if session.thread_id != controller.record["thread"]:
            yield ev.Done(had_response=False)
            return
        report = controller.record
        message = f"Prime factory {report['id']} · {report['status']} · {report['stage']}"
        if report["blockers"]:
            message += "\n" + "\n".join(report["blockers"])
        if report["status"] not in {"published", "cancelled"}:
            message += f"\nContinue with /prime resume {report['id']}."
        if agent is not None:
            from langchain_core.messages import AIMessage

            await agent.aupdate_state(
                {"configurable": {"thread_id": controller.record["thread"]}},
                {"messages": [AIMessage(message, id=uuid.uuid4().hex)]},
            )
        if session.thread_id != controller.record["thread"]:
            yield ev.Done(had_response=False)
            return
        yield ev.ContextMessage(
            message,
            event_type="nova_prime_receipt",
            record_id=report["id"],
            data=controller.progress(),
        )
        # Headless consumers need a useful continuation in their final result too.
        yield ev.AssistantMessage(text=message, agent_name="Prime", agent_color="cyan")
        yield ev.Done(had_response=True)
    except asyncio.CancelledError:
        if controller is not None:
            controller.request_pause()
        raise
    except Exception as exc:  # noqa: BLE001 - Frontends receive durable failure state.
        if controller is not None and not controller._storage_failed:
            controller.record["blockers"] = [str(exc)]
            controller.request_pause()
        if session.thread_id != originating_thread:
            yield ev.Done(had_response=False)
            return
        yield ev.Error(f"Prime factory suspended: {exc}")
        yield ev.Done(had_response=False)
    finally:
        if driver is not None and not driver.done():
            driver.cancel()
            await asyncio.gather(driver, return_exceptions=True)
        if controller is not None:
            try:
                await controller.stop_workers()
            finally:
                if getattr(session, "prime_factory", None) is controller:
                    session.prime_factory = None
        if store is not None:
            await asyncio.to_thread(store.close)
