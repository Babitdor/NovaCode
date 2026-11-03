"""Single-use remote approvals bound to the requesting platform account."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from novacode_cli.remote.bridge import RemoteMessage

logger = logging.getLogger(__name__)
APPROVAL_TTL = 300.0
_SEND_TIMEOUT = 15.0
_MAX_DETAILS = 100_000
_PART_SIZE = 1_400  # Leave room for metadata and the nonce within Discord's 2,000 characters.
_MAX_LABEL = 256
_COMMAND_PARTS = 2
_COMMAND = re.compile(r"^/?(approve|reject)(?:@[\w]+)?(?:\s|$)", re.IGNORECASE)
Owner = tuple[str, str, str | None, str]


def _owner(msg: RemoteMessage) -> Owner | None:
    def identifier(value: object) -> str | None:
        return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) else None

    platform = identifier(getattr(getattr(msg, "platform", None), "value", None))
    if platform is None:
        platform = identifier(getattr(msg, "platform", None))
    chat = identifier(getattr(msg, "chat_id", None))
    sender = identifier(getattr(msg, "sender_id", None))
    thread = getattr(msg, "thread_id", None)
    topic = identifier(thread)
    if (
        not platform
        or platform not in {"telegram", "discord"}
        or not chat
        or not sender
        or not sender.strip()
        or (thread is not None and not topic)
    ):
        return None
    return platform, chat, topic, sender


def _reject(kind: str) -> dict[str, Any]:
    # Match default_interrupt_response without importing the agent/UI stack.
    if kind == "tool":
        return {"decisions": [], "any_rejected": True}
    if kind == "plan":
        return {
            "response": {"approved": False, "action": "reject", "feedback": ""},
            "state_update": {},
        }
    return {}


def _details(kind: str, payload: object) -> tuple[str, int] | None:
    if not isinstance(payload, dict) or kind not in {"tool", "plan"}:
        return None
    content: dict[str, Any]
    if kind == "tool":
        actions = payload.get("action_requests")
        if not isinstance(actions, list) or not actions:
            return None
        shown = []
        for action in actions:
            if (
                not isinstance(action, dict)
                or not isinstance(action.get("name"), str)
                or not action["name"].strip()
                or not isinstance(action.get("args"), dict)
            ):
                return None
            shown.append({"name": action["name"], "args": action["args"]})
        content, count = {"actions": shown}, len(shown)
    else:
        plan = payload.get("plan")
        if not isinstance(plan, str) or not plan.strip():
            return None
        content, count = {"plan": plan}, 1
    try:
        # JSON preserves controls and Unicode visibly. Escaping backticks keeps
        # untrusted arguments and plans inside every platform's code fences.
        text = json.dumps(content, ensure_ascii=True, allow_nan=False, indent=2).replace(
            "`", r"\u0060"
        )
        valid = json.loads(text) == content and len(text) <= _MAX_DETAILS
    except (TypeError, ValueError, RecursionError):
        return None
    return (text, count) if valid else None


@dataclass
class _Pending:
    owner: Owner
    expires: float
    future: asyncio.Future[bool]
    label: str
    approval_check: Callable[[str], bool] | None = None
    respondent: str | None = None
    cancelled: bool = False

    def authorized(self, sender: str | None) -> bool:
        """Recheck the bridge's current policy for a real platform account."""
        if sender is None or sender != self.owner[-1]:
            return False
        try:
            return self.approval_check(sender) is True if self.approval_check is not None else True
        except Exception:  # noqa: BLE001 - a broken policy must reject.
            logger.debug("Remote approval authorization failed", exc_info=True)
            return False


class RemoteApprovalBroker:
    """Route explicit approval replies without changing any permission settings."""

    def __init__(self) -> None:
        """Keep only unresolved, short-lived approval requests in memory."""
        self._pending: dict[str, _Pending] = {}

    def _nonce(self) -> str:
        nonce = secrets.token_hex(16)
        while nonce in self._pending:
            nonce = secrets.token_hex(16)
        return nonce

    def cancel(self, label: str) -> None:
        """Reject unresolved approvals for one stopped session, never its peers."""
        for pending in self._pending.values():
            if pending.label == label:
                pending.cancelled = True
                if not pending.future.done():
                    pending.future.set_result(False)

    @staticmethod
    async def _reply(msg: RemoteMessage, text: str) -> None:
        try:
            async with asyncio.timeout(_SEND_TIMEOUT):
                await msg.reply_fn(text)
        except Exception:  # noqa: BLE001 - reporting must never grant approval.
            logger.debug("Remote approval notification failed", exc_info=True)

    @staticmethod
    async def _retract(retracts: list[Callable[[], Awaitable[None]]]) -> None:
        if retracts:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(_SEND_TIMEOUT):
                    await asyncio.gather(
                        *(retract() for retract in retracts), return_exceptions=True
                    )

    async def request(
        self,
        msg: RemoteMessage,
        kind: str,
        payload: object,
        *,
        label: str = "",
    ) -> dict[str, Any]:
        """Display every action, then await one scoped nonce response or reject."""
        details = _details(kind, payload)
        approval_check = getattr(msg, "approval_check", None)
        owner = _owner(msg)
        label_json = (
            json.dumps(label, ensure_ascii=True).replace("`", r"\u0060")
            if isinstance(label, str)
            else ""
        )
        if (
            details is None
            or owner is None
            or not isinstance(label, str)
            or len(label_json) > _MAX_LABEL
        ):
            await self._reply(msg, "Approval unavailable; request rejected.")
            return _reject(kind)
        text, count = details
        nonce = self._nonce()
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        pending = _Pending(owner, time.monotonic() + APPROVAL_TTL, future, label, approval_check)
        if not pending.authorized(owner[-1]):
            return _reject(kind)
        self._pending[nonce] = pending
        retracts: list[Callable[[], Awaitable[None]]] = []
        send = getattr(msg, "ask_fn", None) or msg.reply_fn
        native_approval = getattr(msg, "approval_fn", None)
        approved = False
        try:
            try:
                async with asyncio.timeout(APPROVAL_TTL):
                    pages = [text[i : i + _PART_SIZE] for i in range(0, len(text), _PART_SIZE)]
                    heading = f"Review {kind} approval"
                    if label:
                        heading += f"\nLabel (JSON):\n```\n{label_json}\n```"
                    for index, page in enumerate(pages, 1):
                        prompt = (
                            f"{heading}\nDetails {index}/{len(pages)} (JSON):\n```\n{page}\n```"
                        )
                        final_page = index == len(pages)
                        if final_page:
                            prompt += (
                                "\nChoose Approve or Reject below.\nExpires in 5 minutes."
                                if native_approval is not None
                                else f"\nApprove {nonce}\nor Reject {nonce}\nExpires in 5 minutes."
                            )
                        async with asyncio.timeout(_SEND_TIMEOUT):
                            retract = (
                                await native_approval(prompt, nonce)
                                if final_page and native_approval is not None
                                else await send(prompt)
                            )
                        if callable(retract):
                            retracts.append(retract)
                    approved = await future
            except Exception:  # noqa: BLE001 - delivery failures reject.
                logger.debug("Remote approval request failed or expired", exc_info=True)
            finally:
                if not future.done():
                    future.cancel()
                await self._retract(retracts)
            # Retractions can yield to revocation, session cancellation, or a
            # changed request. No approval decision may precede those awaits.
            if (
                approved
                and pending.authorized(pending.respondent)
                and _details(kind, payload) == details
                and time.monotonic() < pending.expires
                and not pending.cancelled
            ):
                if kind == "tool":
                    return {
                        "decisions": [{"type": "approve"} for _ in range(count)],
                        "any_rejected": False,
                    }
                return {
                    "response": {"approved": True, "mode": "manual"},
                    "state_update": {"plan_mode_enabled": False},
                }
            return _reject(kind)
        finally:
            self._pending.pop(nonce, None)

    async def handle(self, msg: RemoteMessage) -> bool:
        """Consume approval commands; ordinary conversation keeps its normal route."""
        platform = getattr(msg, "platform", None)
        if getattr(platform, "value", platform) not in ("telegram", "discord"):
            return False
        text = getattr(msg, "text", "") or ""
        match = _COMMAND.match(text.strip()) if isinstance(text, str) else None
        if match is None:
            return False
        parts = text.strip().split()
        nonce = parts[1].lower() if len(parts) == _COMMAND_PARTS else ""
        pending = self._pending.get(nonce)
        if pending is not None and time.monotonic() >= pending.expires:
            self._pending.pop(nonce, None)
            if not pending.future.done():
                pending.future.set_result(False)
            pending = None
        owner = _owner(msg)
        if (
            pending is None
            or owner is None
            or owner[:3] != pending.owner[:3]
            or not pending.authorized(owner[-1])
            or pending.future.done()
        ):
            await self._reply(msg, "No matching approval is available for this conversation.")
            return True
        pending.respondent = owner[-1]
        pending.future.set_result(match.group(1).lower() == "approve")
        await self._reply(msg, "Approval response recorded.")
        return True
