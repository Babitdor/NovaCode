"""Protocol selection for the documented Zen and Go gateway endpoints.

Sources: https://opencode.ai/docs/zen/#endpoints and /docs/go/#endpoints.
Google native and System One decision models require separate integrations and
are excluded from the conversational model picker.
"""

from novacode_cli._version import __version__


def headers(session_id: str) -> dict[str, str]:
    return {"User-Agent": f"NovaCode/{__version__}", "x-opencode-session": session_id}


def protocol(provider: str, model: str) -> str:
    name = model.lower()
    if name.startswith(("jev-", "gemini-")):
        return "unsupported"
    if name.startswith(("gpt-", "grok-", "muse-spark-")):
        return "responses"
    if name.startswith(("claude-", "qwen")):
        return "messages"
    if provider == "opencode" and name.startswith("minimax-"):
        return "messages"
    return "chat"
