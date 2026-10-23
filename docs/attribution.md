# Git and pull request attribution

Nova enables commit and pull request attribution by default. It reads these
independent preferences from `~/.nova/Nova.config.json` on each model or supported
creation-tool call. To disable both, merge this block into that file:

```json
{
  "attribution": {
    "commits": false,
    "pullRequests": false
  }
}
```

Omitted switches default to `true`. Either switch can be disabled independently.

Commits created by Nova receive this plain-text Git trailer:

```text
Co-Authored-By: NovaCode <noreply@novacode.invalid>
```

This is an unlinked product identity, not a verified GitHub bot account. GitHub
controls account avatars, so a Git trailer cannot display a custom image. It
credits the whole commit and does not track which lines were AI-written. Nova
preserves the human author and existing co-author and sign-off trailers.

Pull requests receive this footer, using the supplied NovaCode logo:

---

<img src="https://raw.githubusercontent.com/Babitdor/NovaCode/main/assets/nova-attribution.png" alt="NovaCode logo" width="24" height="24" /> Generated with [NovaCode](https://github.com/Babitdor/NovaCode)

---

The preferences are included in main-agent and Nova-managed subagent instructions,
including background agents. Shell and `gh` commands follow these instructions;
Nova does not parse or rewrite arbitrary shell commands. Supported structured
creation tools additionally receive attribution through middleware: names equal
to, or ending with an underscore followed by, `create_pull_request` with a string
`body`, and `git_commit`, `create_or_update_file`, or `push_files` with a string
`message`. An omitted PR body is filled only when its tool schema supports it.
Unknown tools and malformed arguments retain normal execution and validation.
Duplicate Nova trailers and identical footers are avoided.

Disabling attribution leaves tool payloads unchanged and tells the agent to omit
automatic Nova attribution. Existing user-supplied text is retained. Settings do
not modify historical commits or PRs, install Git hooks, change Git identity, or
authorize commits, pushes, or PR creation. Independently hosted third-party agents
must implement their own attribution behavior.
