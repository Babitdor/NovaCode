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

## Linking attribution to a GitHub account

The default `.invalid` email cannot be linked to a GitHub profile. A logo in a PR
description also does not create contribution credit. To associate new Nova
co-author trailers with a real account, configure its existing GitHub identity:

```json
{
  "attribution": {
    "commits": true,
    "pullRequests": true,
    "coAuthor": {
      "name": "NovaCode",
      "email": "YOUR_ID+YOUR_NOVA_ACCOUNT@users.noreply.github.com"
    }
  }
}
```

Replace the example with the **actual commit email shown in that account's GitHub
email settings**. Do not copy the placeholder, use an unrelated person's email,
or assume that changing a display name creates an account. The `name` defaults
to NovaCode when omitted. Malformed identity fields fall back to the existing
unlinked identity, preventing multiline trailer injection.

GitHub requires an email associated with an account for co-author contribution
credit. Its Contributors graph also depends on eligible commits on the default
branch and its display/ranking rules. GitHub's profile avatar controls the linked
identity's image; put the Nova logo on that account to use it as the avatar.
This setting does not create accounts, upload profile images, link emails or
modify GitHub's contributor statistics directly.

Changing configuration affects future Nova-generated attribution. It does not
add trailers to previous commits or rewrite published history. The middleware
runs inside Nova: commits made directly by another Git client or a separate
coding assistant do not pass through it. A valid Git trailer test establishes
syntax and preservation, not live GitHub identity association.

Sources: [GitHub co-author requirements](https://docs.github.com/en/pull-requests/how-tos/commit-changes/creating-a-commit-with-multiple-authors),
[Contributors graph requirements](https://docs.github.com/en/repositories/viewing-activity-and-data-for-your-repository/viewing-a-projects-contributors).

## Pull request footer

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
