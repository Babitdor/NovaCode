"""Git safety utilities for command validation and approval.

This module provides safety checks for git operations, including:
- Command prefix detection for allowlisting
- Command injection detection
- Dangerous command blocking
"""

import re
from typing import Literal

# Dangerous git commands that should require explicit approval
DANGEROUS_GIT_COMMANDS = {
    "push --force": "Force push can overwrite remote history",
    "push -f": "Force push can overwrite remote history",
    "reset --hard": "Hard reset discards all uncommitted changes",
    "reset --hard HEAD~": "Resets to previous commit, discarding changes",
    "clean -fd": "Removes untracked files and directories",
    "clean -fdx": "Removes all untracked files including ignored",
    "checkout -- .": "Discards all working directory changes",
    "stash clear": "Removes all stashed changes permanently",
    "branch -D": "Force deletes a branch",
    "tag -d": "Deletes a tag",
    "remote remove": "Removes a remote",
    "rebase -i": "Interactive rebase (requires user input)",
    "add -i": "Interactive add (requires user input)",
}

# Commands that should never be allowed
BLOCKED_COMMANDS = {
    "git config": "Never update git config from agent",
    "git commit --amend": "Never amend commits (unless explicitly requested)",
    "--no-verify": "Never skip hooks",
    "--no-gpg-sign": "Never skip GPG signing",
}

#: `git config` flags that change the file. Any of them makes the subcommand a
#: write, whatever else is on the line.
_GIT_CONFIG_WRITE_FLAGS = frozenset(
    {
        "--add",
        "--replace-all",
        "--unset",
        "--unset-all",
        "--remove-section",
        "--rename-section",
        "--edit",
        "-e",
    }
)
#: Flags that consume the next token, so that token is not a config value.
_GIT_CONFIG_FLAGS_WITH_ARGUMENT = frozenset(
    {
        "--file",
        "-f",
        "--blob",
        "--type",
        "-t",
        "--default",
        "--get",
        "--get-all",
        "--get-regexp",
        "--get-urlmatch",
        "--comment",
    }
)


def _git_config_writes(words: list[str]) -> bool:
    """Whether these ``git config`` arguments would change configuration."""
    if any(word in _GIT_CONFIG_WRITE_FLAGS for word in words):
        return True
    values: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in _GIT_CONFIG_FLAGS_WITH_ARGUMENT
            continue
        values.append(word)
    # One positional is the key being read (``git config user.name``); a second
    # is the value being written (``git config user.name bob``).
    return len(values) > 1


def git_config_writes(command: str) -> bool:
    """Whether any ``git config`` in the command would change configuration.

    Reading is how an agent learns the identity it is about to commit with, so
    only writes are blocked. ``git config`` arrives through the same policy gate
    as every other command, and blocking the read cost a retry round-trip while
    protecting nothing.
    """
    for segment in re.split(r"[;&|\n]+", command):
        words = segment.split()
        while words and re.match(r"^\w+=", words[0]):  # leading FOO=1 assignments
            words = words[1:]
        for index, word in enumerate(words[:-1]):
            if (
                word == "git"
                and words[index + 1] == "config"
                and _git_config_writes(words[index + 2 :])
            ):
                return True
    return False


def blocked_command_reason(command: str) -> str | None:
    """Why a command is blocked, or None.

    One source of truth for the block list: ``git config`` is decided by its
    form (a read is not a change), every other entry by substring.
    """
    command_lower = command.lower()
    for blocked, reason in BLOCKED_COMMANDS.items():
        if blocked == "git config":
            if git_config_writes(command):
                return f"Blocked: {reason}"
            continue
        if blocked.lower() in command_lower:
            return f"Blocked: {reason}"
    return None


# Command injection patterns to detect
COMMAND_INJECTION_PATTERNS = [
    r'\$\(',           # $(...)
    r'`',              # backticks
    r'\n\s*\w',        # newline followed by command
    r'\|\s*curl',      # pipe to curl
    r'\|\s*wget',      # pipe to wget
    r'\|\s*nc',        # pipe to netcat
    r'\|\s*bash',      # pipe to bash
    r'\|\s*sh',        # pipe to sh
    r';\s*\w',         # semicolon followed by command
    r'&&\s*\w',        # && followed by command
    r'\|\|\s*\w',      # || followed by command
]


def detect_command_injection(command: str) -> bool:
    """Check if a command contains injection patterns.
    
    Args:
        command: The git command to check
        
    Returns:
        True if command injection is detected, False otherwise
    """
    for pattern in COMMAND_INJECTION_PATTERNS:
        if re.search(pattern, command):
            return True
    return False


def extract_command_prefix(command: str) -> str | Literal["none"] | Literal["command_injection_detected"]:
    """Extract the command prefix for allowlisting.
    
    This function determines the command prefix for safety checks.
    The prefix must be a string prefix of the full command.
    
    Args:
        command: The git command to analyze
        
    Returns:
        - The command prefix (e.g., "git status", "git commit")
        - "none" if no prefix applies
        - "command_injection_detected" if malicious patterns found
        
    Examples:
        >>> extract_command_prefix("git status")
        'git status'
        >>> extract_command_prefix("git commit -m 'message'")
        'git commit'
        >>> extract_command_prefix("git status`ls`")
        'command_injection_detected'
    """
    # First check for command injection
    if detect_command_injection(command):
        return "command_injection_detected"
    
    # Strip leading whitespace
    command = command.strip()
    
    # Parse the command parts
    parts = command.split()
    
    if not parts:
        return "none"
    
    # Handle environment variables before git
    # e.g., "FOO=bar git status" -> env vars stripped
    env_vars = []
    i = 0
    while i < len(parts) and "=" in parts[i]:
        env_vars.append(parts[i])
        i += 1
    
    if env_vars:
        # Skip env vars and get the actual git command
        remaining = parts[i:]
        if not remaining:
            return "none"
        if remaining[0] != "git":
            return "none"
        if len(remaining) < 2:
            return "git"
        
        # Get git subcommand
        subcommand = remaining[1]
        
        # Return prefix without env vars
        if subcommand in ["push", "pull", "fetch", "commit", "add", "reset",
                          "checkout", "branch", "tag", "remote", "stash",
                          "log", "diff", "show", "status", "blame"]:
            return f"git {subcommand}"
        else:
            return f"git {subcommand}"
    
    # No env vars, just git command
    if parts[0] != "git":
        return "none"
    
    if len(parts) < 2:
        return "git"
    
    subcommand = parts[1]
    
    # Special handling for common git commands
    if subcommand in ["push", "pull", "fetch"]:
        return f"git {subcommand}"
    elif subcommand in ["commit", "add", "reset", "checkout", "branch", "tag", "remote", "stash"]:
        return f"git {subcommand}"
    elif subcommand in ["log", "diff", "show", "status", "blame"]:
        return f"git {subcommand}"
    else:
        return f"git {subcommand}"


def is_dangerous_command(command: str) -> tuple[bool, str]:
    """Check if a git command is dangerous.
    
    Args:
        command: The git command to check
        
    Returns:
        Tuple of (is_dangerous, reason)
    """
    command_lower = command.lower()

    # Check for blocked commands
    blocked = blocked_command_reason(command)
    if blocked is not None:
        return True, blocked

    # Check for dangerous commands
    for dangerous, reason in DANGEROUS_GIT_COMMANDS.items():
        if dangerous.lower() in command_lower:
            return True, reason

    return False, ""


def is_safe_git_command(command: str) -> tuple[bool, str]:
    """Validate if a git command is safe to execute.
    
    Args:
        command: The git command to validate
        
    Returns:
        Tuple of (is_safe, message)
    """
    # Check for command injection
    prefix = extract_command_prefix(command)
    if prefix == "command_injection_detected":
        return False, "Command injection detected - potential security risk"
    
    # Check for dangerous commands
    is_dangerous, reason = is_dangerous_command(command)
    if is_dangerous:
        return False, f"Dangerous command: {reason}"
    
    return True, "Command is safe to execute"


def get_command_description(command: str) -> str:
    """Generate a clear, concise description of what a git command does.
    
    Args:
        command: The git command to describe
        
    Returns:
        A human-readable description of the command
    """
    command = command.strip()
    
    # Parse the command
    parts = command.split()
    if not parts or parts[0] != "git":
        return f"Execute: {command}"
    
    if len(parts) < 2:
        return "Run git"
    
    subcommand = parts[1]
    
    # Common git command descriptions
    descriptions = {
        "status": "Show working tree status",
        "log": "Show commit history",
        "diff": "Show changes between commits",
        "show": "Show various types of objects",
        "blame": "Show line-by-line attribution",
        "add": "Stage changes for commit",
        "commit": "Create a new commit",
        "reset": "Reset current HEAD to specified state",
        "checkout": "Switch branches or restore files",
        "branch": "List, create, or delete branches",
        "merge": "Join two or more development histories",
        "rebase": "Reapply commits on top of another base tip",
        "pull": "Fetch from and integrate with another repository",
        "push": "Update remote refs along with associated objects",
        "fetch": "Download objects and refs from another repository",
        "clone": "Clone a repository into a new directory",
        "init": "Create an empty Git repository",
        "stash": "Stash the changes in a dirty working directory",
        "tag": "Create, list, or delete tags",
        "remote": "Manage set of tracked repositories",
        "config": "Get and set repository or global options",
        "clean": "Remove untracked files from working tree",
        "mv": "Move or rename a file, directory, or symlink",
        "rm": "Remove files from the working tree and from the index",
        "restore": "Restore working tree files",
        "switch": "Switch branches",
        "archive": "Create an archive of files from a named tree",
        "bisect": "Find by binary search the change that introduced a bug",
        "grep": "Print lines matching a pattern",
        "ls-files": "Show information about files in the index",
        "ls-tree": "List the contents of a tree object",
        "rev-parse": "Pick out and massage parameters",
        "shortlog": "Summarize git log output",
        "describe": "Give an object a human readable name",
        "reflog": "Manage reflog information",
        "cherry-pick": "Apply changes introduced by some existing commits",
        "revert": "Revert some existing commits",
    }
    
    if subcommand in descriptions:
        desc = descriptions[subcommand]
        
        # Add context for common flags
        if "-m" in parts:
            desc += f" with message"
        if "--hard" in parts:
            desc += " (hard reset - discards all changes)"
        if "--soft" in parts:
            desc += " (soft reset - keeps changes staged)"
        if "--amend" in parts:
            desc += " (amend previous commit)"
        if "-f" in parts or "--force" in parts:
            desc += " (force)"
        if "-d" in parts or "--delete" in parts:
            desc += " (delete)"
        if "-a" in parts or "--all" in parts:
            desc += " (all)"
        
        return desc
    
    return f"Execute git {subcommand}"