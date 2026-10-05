"""MCP server presets and templates.

Provides pre-configured templates for popular MCP servers that can be easily
installed and configured through the /mcp command.
"""

from typing import Any

from novacode_cli.mcp.config import MCPServerConfig

# Pre-defined MCP server presets
MCP_PRESETS: dict[str, dict[str, Any]] = {
    "playwright": {
        "name": "Playwright MCP",
        "description": "Browser automation using Playwright for web scraping and testing",
        "package": "@playwright/mcp",
        "config": {
            "transport": "stdio",
            "command": "npx",
            "args": [
                "-y",
                "@playwright/mcp@latest",
                "--browser=chrome",
                "--viewport-size=1280x720",
                "--headless",
                "--timeout-action=30000",
                "--timeout-navigation=30000",
            ],  #  Headless by default; remove for headed
            "env": {},
        },
    },
    "serena": {
        "name": "Serena MCP",
        "description": "Semantic code editing and analysis with LSP integration",
        "package": "serena",
        "config": {
            "transport": "stdio",
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/oraios/serena",
                "serena",
                "start-mcp-server",
                "--project-from-cwd",
                "--context",
                "agent",
            ],
            "env": {},
        },
    },
    "context7": {
        "name": "Context7 MCP",
        "description": "Up-to-date library documentation and code examples from Context7",
        "package": "@upstash/context7-mcp",
        "config": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@upstash/context7-mcp", "--api-key", "{context7_api_key}"],
            "env": {},
        },
        "setup_prompt": "Enter your Upstash Context7 API key:",
        "setup_key": "context7_api_key",
    },
    "apify": {
        "name": "Apify MCP",
        "description": "Apify Actors, documentation, and Instagram scraping (OAuth via mcp-remote)",
        "package": "mcp-remote",
        "config": {
            "transport": "stdio",
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote@latest",
                "https://mcp.apify.com/?tools=actors%2Cdocs%2Capify%2Finstagram-scraper",
            ],
            "env": {},
        },
    },
    "cua-driver": {
        "name": "CUA Driver MCP",
        "description": "Desktop computer automation using an installed CUA Driver",
        "package": "cua-driver (install separately)",
        "config": {
            "transport": "stdio",
            "command": "{cua_driver_path}",
            "args": ["mcp"],
            "env": {},
        },
        "setup_prompt": "Enter the full path to your installed cua-driver executable:",
        "setup_key": "cua_driver_path",
    },
    "sequential-thinking": {
        "name": "Sequential Thinking MCP",
        "description": "Structured, iterative reasoning for complex problem solving",
        "package": "@modelcontextprotocol/server-sequential-thinking",
        "config": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
            "env": {},
        },
    },
    "chrome-devtools": {
        "name": "Chrome DevTools MCP",
        "description": "Official Chrome debugging, network inspection, and performance tools",
        "package": "chrome-devtools-mcp",
        "config": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@latest"],
            "env": {},
        },
    },
    "firecrawl": {
        "name": "Firecrawl MCP",
        "description": "Web scraping, crawling, search, and structured data extraction",
        "package": "firecrawl-mcp",
        "config": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "firecrawl-mcp"],
            "env": {"FIRECRAWL_API_KEY": "{firecrawl_api_key}"},
        },
        "setup_prompt": "Enter your Firecrawl API key:",
        "setup_key": "firecrawl_api_key",
        "env_mapping": {"firecrawl_api_key": "FIRECRAWL_API_KEY"},
    },
    "tavily": {
        "name": "Tavily MCP",
        "description": "Real-time web search, extraction, site mapping, and crawling",
        "package": "tavily-mcp",
        "config": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "tavily-mcp@latest"],
            "env": {"TAVILY_API_KEY": "{tavily_api_key}"},
        },
        "setup_prompt": "Enter your Tavily API key:",
        "setup_key": "tavily_api_key",
        "env_mapping": {"tavily_api_key": "TAVILY_API_KEY"},
    },
    "exa": {
        "name": "Exa MCP",
        "description": "Hosted web search and content fetching (anonymous access with rate limits)",
        "package": "Hosted Exa MCP (no local package required)",
        "config": {
            "transport": "http",
            "url": "https://mcp.exa.ai/mcp",
        },
    },
    "perplexity": {
        "name": "Perplexity MCP",
        "description": "Official Perplexity web search, reasoning, and research tools",
        "package": "@perplexity-ai/mcp-server",
        "config": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@perplexity-ai/mcp-server"],
            "env": {"PERPLEXITY_API_KEY": "{perplexity_api_key}"},
        },
        "setup_prompt": "Enter your Perplexity API key:",
        "setup_key": "perplexity_api_key",
        "env_mapping": {"perplexity_api_key": "PERPLEXITY_API_KEY"},
    },
}


def get_preset(name: str) -> dict[str, Any] | None:
    """Get MCP preset by name.

    Args:
        name: Preset identifier (e.g., 'filesystem', 'github')

    Returns:
        Preset configuration dict or None if not found
    """
    return MCP_PRESETS.get(name)


def list_presets() -> dict[str, dict[str, Any]]:
    """List all available MCP presets.

    Returns:
        Dictionary of all presets
    """
    return MCP_PRESETS.copy()


def create_config_from_preset(
    preset_name: str, user_inputs: dict[str, str] | None = None
) -> MCPServerConfig | None:
    """Create an MCPServerConfig from a preset with user inputs.

    Args:
        preset_name: Name of the preset to use
        user_inputs: Dictionary of user-provided values for placeholders

    Returns:
        Configured MCPServerConfig or None if preset not found
    """
    preset = get_preset(preset_name)
    if not preset:
        return None

    config = preset["config"].copy()
    user_inputs = user_inputs or {}

    # Local executable presets must remain portable across installations.
    if config.get("command") and "{" in config["command"]:
        config["command"] = config["command"].format(**user_inputs)

    # Replace placeholders in args
    if config.get("args"):
        config["args"] = [
            arg.format(**user_inputs) if "{" in arg else arg for arg in config["args"]
        ]

    # Replace placeholders in env
    if config.get("env"):
        env_mapping = preset.get("env_mapping", {})
        new_env = {}
        for env_key, env_value in config["env"].items():
            if "{" in env_value:
                # Find the corresponding user input
                for input_key, mapped_env_key in env_mapping.items():
                    if mapped_env_key == env_key and input_key in user_inputs:
                        new_env[env_key] = user_inputs[input_key]
                        break
            else:
                new_env[env_key] = env_value
        config["env"] = new_env

    # Add description from preset
    config["description"] = preset["description"]

    return MCPServerConfig(**config)
