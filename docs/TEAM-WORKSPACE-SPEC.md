# Team file-write approval boundary

Spec approval: not obtained (autonomous run).

The reported session selected Research as an output destination. Preserve the
ability to approve external output; do not infer permission from a worker's
task or generated instructions.

Acceptance criteria:

- In-project file writes retain the existing approval policy and auto-approve.
- Writes resolving outside the worker's actual cwd require explicit parent
  approval even with auto-approve, unless a configured path allow rule grants
  that resolved destination. Explicit deny rules always win.
- Resolve mounted paths, traversal, and existing symlinks before the decision.
- An approved call is bound to its original arguments and cannot be replayed.
- Worktree workers use their own cwd, not the lead's original checkout.
- Resolution failure never executes a write. Read-only tools remain unchanged.
- A teammate's system instructions identify its actual project directory and
  distinguish task instructions from permission to write elsewhere.

Scope: built-in path-bearing file editing tools. This is not an OS sandbox for
shell, Python, plugins, or arbitrary external MCP tools. Filesystem races between
resolution and execution remain possible. Existing external output selections
in prose are not converted into broad directory grants; the parent approval UI
or explicitly configured path rules authorizes external file calls.

Setup: standard library and existing test/lint/type/coverage dependencies only.
Add focused unit/integration tests, this spec, and an evidence report. Bump the
release metadata and changelog, push main, and check CI under the user's standing
request. Preserve unrelated user files and avoid global cwd changes.

User-requested extension: keep the main-agent base templates in the package,
ignoring global prompt-history and activated-artifact replacements. Continue to
inject global agent.md as memory, without treating it as an agent definition.
Improve System_Prompt_Nova.jinja using the user's Codex.md as inspiration, with
Nova tool names, scoped planning, verification, progress and output guidance.
