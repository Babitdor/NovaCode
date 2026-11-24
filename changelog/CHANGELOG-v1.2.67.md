# NovaCode 1.2.67

## Fixed: TUI image preview for virtual `.shots` paths

- Clicking an image card the agent read (e.g. `read_file(/.shots/theme-light.png)`)
  opened the preview at the raw virtual path and failed with
  `Could not load image: [Errno 2] No such file or directory`.
- The TUI preview now resolves agent-side paths against the workspace root via
  the same `resolve_workspace_image` the Telegram and Discord bridges use, so
  `/.shots/x.png` maps to `<workspace>/.shots/x.png`. Missing files, paths
  outside the workspace, and URLs still fall back to a clear load error.
- The TUI preview test previously accepted the error text as "content"; it now
  asserts the image actually loads through the workspace mapping.
