"""Rootapp-specific remote setup and startup, sharing Nova's bridge manager."""

from __future__ import annotations

import os
import re
import shlex


def valid_root_id(value: str) -> bool:
    """Root GUID strings (compact or UUID form); never command fragments."""
    return isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value))


async def handle_rootapp_remote(args: str, state, manager, console) -> bool:
    from novacode_cli.remote.config import async_load_remote_config, async_save_remote_config

    if args.lower() == "setup rootapp":
        from novacode_cli.remote.rootapp_setup import setup_rootapp

        console.print("Installing Root's optional Node SDK…")
        try:
            result = await setup_rootapp()
        except Exception as exc:
            console.print(f"Rootapp setup error: {exc}", markup=False)
        else:
            console.print(result, markup=False)
        return True
    if not re.match(r"start\s+rootapp(?:\s|$)", args, re.I):
        return False
    try:
        tokens = shlex.split(args)[2:]
    except ValueError:
        console.print("Invalid Rootapp options; check quoted values.")
        return True
    options = {}
    while tokens:
        key = tokens.pop(0)
        if key not in {"--token", "--bot-id", "--channel", "--user-id"} or not tokens:
            console.print(
                "Usage: /remote start rootapp --token DEV_TOKEN --bot-id BOT_ID --channel CHANNEL_ID [--user-id USER_ID]"
            )
            return True
        options[key] = tokens.pop(0)
    saved = (await async_load_remote_config()).get("rootapp", {})
    if not isinstance(saved, dict):
        saved = {}
    token = options.get("--token") or saved.get("token") or os.environ.get("ROOTAPP_DEV_TOKEN", "")
    bot_id = options.get("--bot-id") or saved.get("bot_id", "")
    channel = options.get("--channel") or saved.get("channel_id", "")
    if (
        not isinstance(token, str)
        or not token
        or not valid_root_id(bot_id)
        or not valid_root_id(channel)
    ):
        console.print(
            "Rootapp requires a Dev Token, Bot ID and channel ID from the Root Developer Portal/client."
        )
        console.print("First install its SDK: /remote setup rootapp")
        return True
    values: dict = {"token": token, "bot_id": bot_id, "channel_id": channel}
    user = options.get("--user-id")
    if user:
        if not valid_root_id(user):
            console.print("Enter a valid Root user ID.")
            return True
        stored_users = saved.get("allowed_user_ids", [])
        users = {str(value) for value in stored_users} if isinstance(stored_users, list) else set()
        values["allowed_user_ids"] = sorted(users | {user})
    await async_save_remote_config({"rootapp": values})
    console.print("Starting Rootapp DevHost…")
    success, error = await manager.start_rootapp(token, channel, bot_id)
    if not success:
        console.print("Rootapp error: " + error, markup=False)
        return True
    if not state.auto_approve:
        state._pre_remote_auto_approve = False
        state.auto_approve = True
    console.print("Rootapp connected. Send /nova <prompt> in the configured channel.")
    if not values.get("allowed_user_ids") and not saved.get("allowed_user_ids"):
        console.print("Authorize your account locally: /remote allow-user rootapp USER_ID")
    return True
