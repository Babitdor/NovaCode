"""Shell module: pattern constants and compiled regex patterns.

This module contains all pattern lists and compiled regex patterns used by
the shell middleware for prompt detection, server readiness, dangerous commands,
auto-answering, and platform detection.
"""

from __future__ import annotations

import re

# Patterns that indicate a line is an interactive prompt requiring user input
PROMPT_PATTERNS = [
    r"\(y/n\)",  # Yes/No prompts
    r"\(yes/no\)",  # Full yes/no
    r"\[y/n\]",  # Bracketed yes/no
    r"\[yes/no\]",  # Bracketed full yes/no
    r"proceed\?",  # "Ok to proceed?"
    r"continue\?",  # "Do you want to continue?"
    r"overwrite\?",  # "Overwrite existing file?"
    r"ok to proceed",  # npm's "Ok to proceed? (y)"
    r"would you like to",  # "Would you like to use..."
    r"do you want to",  # "Do you want to..."
    r"enter .*:",  # "Enter your name:"
    r"password:",  # Password prompts
    r"username:",  # Username prompts
    r"select.*:",  # "Select a framework:"
    r"choose.*:",  # "Choose an option:"
    r"pick.*:",  # "Pick a template:"
]

# Patterns that indicate a long-running server has successfully started
# When any of these patterns are found, we consider the command "successful"
# and can return control to the agent (leaving the process running in background)
SERVER_READY_PATTERNS = [
    # Generic server patterns
    r"listening on",
    r"listening at",
    r"server running",
    r"server started",
    r"server is running",
    r"ready on",
    r"ready in",
    r"started server",
    r"started at",
    r"started on",
    # Next.js / React patterns
    r"local:\s*http",
    r"➜\s*local:",
    r"ready -",
    r"▲ next",
    # Vite patterns
    r"vite.*ready",
    r"dev server running",
    # Python patterns
    r"running on http",
    r"uvicorn running",
    r"starting.*server",
    r"serving at",
    r"serving on",
    r"serving http",
    # Flask patterns
    r"running on all addresses",
    r"debugger is active",
    # Django patterns
    r"starting development server",
    r"quit the server",
    # Node patterns
    r"app listening",
    r"express.*listening",
    # Generic port listening
    r"port \d+",
    r":\d{4,5}/?$",  # URLs ending with port
]

# Commands that are known to be long-running dev servers
LONG_RUNNING_COMMANDS = [
    "npm run dev",
    "npm start",
    "npm run start",
    "yarn dev",
    "yarn start",
    "pnpm dev",
    "pnpm start",
    "next dev",
    "next start",
    "vite",
    "vite dev",
    "vite preview",
    "nuxt dev",
    "flask run",
    "uvicorn",
    "gunicorn",
    "python -m http.server",
    "python3 -m http.server",
    "python -m uvicorn",
    "django runserver",
    "manage.py runserver",
    "cargo run",
    "go run",
    "nodemon",
    "ts-node-dev",
    "tsx watch",
    "docker compose up",
    "docker-compose up",
]

# Commands that are known to be interactive and require user input.
# These will automatically use interactive mode to handle prompts.
INTERACTIVE_COMMANDS = [
    # Project scaffolding tools
    "create-next-app",
    "create-react-app",
    "create-vite",
    "npm init",
    "yarn init",
    "pnpm init",
    "npx create-next-app",
    "npx create-react-app",
    "npx create-vite",
    "npm create vite",
    "yarn create vite",
    "pnpm create vite",
    # Framework CLIs
    "ng new",  # Angular CLI
    "vue create",  # Vue CLI
    "nuxt init",  # Nuxt
    "remix create",  # Remix
    "astro create",  # Astro
    "svelte-create",  # Svelte
    # Package managers with prompts
    "npm install -g",  # May prompt for permissions
    "yarn global add",
    # Git commands that can be interactive
    "git rebase -i",
    "git add -p",  # Interactive staging
    "git stash -p",  # Interactive stash
    # Other interactive tools
    "django-admin startproject",
    "rails new",
    "cargo new",
    "go mod init",
    # Configuration tools
    "tsconfig.json",  # TypeScript init
    "eslint --init",
    "prettier --init",
    "husky install",
]

# Commands that are destructive, irreversible, or enable remote code execution.
# These are hard-blocked before any subprocess is spawned.
DANGEROUS_PATTERNS = [
    # Recursive/forced deletion of root, home, or all files in cwd
    r"rm\s+(-\w*r\w*f|-\w*f\w*r)\s+[/~]",
    r"rm\s+(-\w*r\w*f|-\w*f\w*r)\s+\*",
    # Raw disk writes (data destruction)
    r"dd\s+.*of\s*=\s*/dev/",
    r">\s*/dev/(sd|hd|nvme|vd)",
    r"mkfs\.",
    r"fdisk\s+/dev/",
    # Fork bomb
    r":\(\)\s*\{.*\|",
    # System control on host machine
    r"\b(shutdown|reboot|halt|poweroff)\b",
    # Piped remote-code execution
    r"(curl|wget)\s+.*\|\s*(bash|sh|python|python3|node|ruby|perl)",
    # Recursive world-writable permission on /
    r"chmod\s+(-\w*R|-R\w*)\s+[0-7]*7+\s+/",
    # Privilege escalation
    r"\bsudo\b",
    # Environment dump (leaks secrets): a bare `env`/`printenv` as its own command
    # — at the start or after a chain operator, optionally piped/redirected. Does
    # NOT match legitimate uses like `conda env list`, `python -m venv`, `uv venv`,
    # or `env FOO=bar cmd` (setting a var, not dumping).
    r"(?:^|[;&|])\s*printenv\b",
    r"(?:^|[;&|])\s*env\s*(?:[|>]|$)",
    # SSH key theft
    r"\bcat\s+.*\.ssh[/\\]",
    # File ownership change (often used in container escape)
    r"\bchown\b",
]

_COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in PROMPT_PATTERNS]
_COMPILED_SERVER_READY = [re.compile(p, re.IGNORECASE) for p in SERVER_READY_PATTERNS]
_COMPILED_DANGEROUS = [re.compile(p, re.IGNORECASE) for p in DANGEROUS_PATTERNS]

# Prompts that are safe to answer automatically without user interaction.
# Maps regex pattern -> auto-response string.
AUTO_ANSWER_PATTERNS: dict[str, str] = {
    r"ok to proceed\??": "y",  # npm/npx install confirmation
    r"need to install the following": "y",  # npm "Need to install packages:"
    r"do you want to install": "y",  # pnpm dlx and similar
}
_COMPILED_AUTO_ANSWER: dict[re.Pattern[str], str] = {
    re.compile(p, re.IGNORECASE): resp for p, resp in AUTO_ANSWER_PATTERNS.items()
}

# Regex to detect `npx` commands that don't already have --yes/-y so we can inject it.
_NPX_YES_RE = re.compile(r"^(npx)\s+(?!--yes\b)(?!-y\b)", re.IGNORECASE)

# Known API key / token environment variable suffixes.
_API_KEY_SUFFIXES = frozenset({"_API_KEY", "_API_TOKEN", "_TOKEN", "_SECRET"})

# Environment variables that can redirect interpreter loading or import resolution.
# Stripped from the subprocess environment before execution.
_DANGEROUS_ENV_VARS: frozenset[str] = frozenset(
    {
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONEXECUTABLE",
        "PYTHONHOME",
        "COMSPEC",  # Windows command interpreter
        "LD_PRELOAD",  # Linux dynamic linker injection
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",  # macOS equivalent of LD_PRELOAD
        "DYLD_LIBRARY_PATH",
        # API keys — never leak to subprocesses
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENCODE_API_KEY",
        "OPENCODE_ZEN_API_KEY",
        "TAVILY_API_KEY",
        "REPLICATE_API_KEY",
        "REPLICATE_API_TOKEN",
        "E2B_API_KEY",
        "GROQ_API_KEY",
        "NVIDIA_API_KEY",
        "LANGSMITH_API_KEY",
    }
)


__all__ = [
    "PROMPT_PATTERNS",
    "SERVER_READY_PATTERNS",
    "LONG_RUNNING_COMMANDS",
    "INTERACTIVE_COMMANDS",
    "DANGEROUS_PATTERNS",
    "AUTO_ANSWER_PATTERNS",
    "_COMPILED_PATTERNS",
    "_COMPILED_SERVER_READY",
    "_COMPILED_DANGEROUS",
    "_COMPILED_AUTO_ANSWER",
    "_NPX_YES_RE",
    "_API_KEY_SUFFIXES",
    "_DANGEROUS_ENV_VARS",
]
