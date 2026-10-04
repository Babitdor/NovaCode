# Default subagents for NOVA CLI

from .async_subagents import (
    CODE_REVIEW_AGENT_DESCRIPTION,
    DEPENDENCY_AUDIT_AGENT_DESCRIPTION,
    DOCUMENTATION_UPDATE_AGENT_DESCRIPTION,
    REFACTORING_AGENT_DESCRIPTION,
    TEST_GENERATION_AGENT_DESCRIPTION,
    build_code_review_agent,
    build_dependency_audit_agent,
    build_documentation_update_agent,
    build_refactoring_agent,
    build_test_generation_agent,
    retrieve_async_subagents,
)
from .subagents import retrieve_core_subagents
from .prompt import (
    BROWSER_AUTOMATION_AGENT,
    BUG_FIX_AGENT,
    CODE_EXPLORER,
    FINANCIAL_ANALYST,
    LITERATURE_REVIEWER,
    MARKET_ANALYST,
    REFACTORING_SPECIALIST_AGENT,
    SECURITY_AUDITOR_AGENT,
    TECHNICAL_RESEARCHER,
    TESTING_AGENT,
    WEB_RESEARCHER,
)

__all__ = [
    # Async subagents
    "CODE_REVIEW_AGENT_DESCRIPTION",
    "DEPENDENCY_AUDIT_AGENT_DESCRIPTION",
    "DOCUMENTATION_UPDATE_AGENT_DESCRIPTION",
    "REFACTORING_AGENT_DESCRIPTION",
    "TEST_GENERATION_AGENT_DESCRIPTION",
    "build_code_review_agent",
    "build_dependency_audit_agent",
    "build_documentation_update_agent",
    "build_refactoring_agent",
    "build_test_generation_agent",
    "retrieve_async_subagents",
    # Sync subagents
    "retrieve_core_subagents",
    # Subagent prompts
    "BROWSER_AUTOMATION_AGENT",
    "BUG_FIX_AGENT",
    "CODE_EXPLORER",
    "FINANCIAL_ANALYST",
    "LITERATURE_REVIEWER",
    "MARKET_ANALYST",
    "REFACTORING_SPECIALIST_AGENT",
    "SECURITY_AUDITOR_AGENT",
    "TECHNICAL_RESEARCHER",
    "TESTING_AGENT",
    "WEB_RESEARCHER",
]
