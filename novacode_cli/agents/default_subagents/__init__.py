# Default subagents for NOVA CLI

from .async_subagents import retrieve_async_subagents
from .subagents import retrieve_core_subagents
from .prompt import (
    FINANCIAL_ANALYST,
    LITERATURE_REVIEWER,
    MARKET_ANALYST,
    TECHNICAL_RESEARCHER,
    WEB_RESEARCHER,
)

__all__ = [
    # Async subagents
    "retrieve_async_subagents",
    # Sync subagents
    "retrieve_core_subagents",
    # Subagent prompts
    "FINANCIAL_ANALYST",
    "LITERATURE_REVIEWER",
    "MARKET_ANALYST",
    "TECHNICAL_RESEARCHER",
    "WEB_RESEARCHER",
]
