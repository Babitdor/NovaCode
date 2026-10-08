# NovaCode 1.0.17 — Specialist skills and tool resolvers

- Give foreground and async specialists selective skill libraries with their own activation state. General-purpose agents keep the full enabled library. Discover selected directories before parsing instructions to reduce specialist startup work.
- Configure custom agents with `skill_names: [skill-name, another-skill]`, `skill_names: []` for no skills, or `skill_names: "*"` for the full library.
- Resolve space-separated `metadata.include_tools` names to private tools, runtime aliases, or integration groups. Support async resolvers and recheck activation and run context before private tool execution. Compaction removes access when activation messages disappear.
- Map MCP server names and unambiguous original tool names to the existing inventory without reconnecting servers. Tools registered independently remain searchable; their implementation takes precedence over a private tool with the same name.
- Keep Nova's verified Deep Agents dependency range. Nova implements this behavior in its own middleware, including on the locked 0.7.10 release; it does not depend on the newer upstream skill-tools API.
