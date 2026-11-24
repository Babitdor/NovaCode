# NovaCode 1.2.63

## Agent image reads become inspectable

When the agent calls `read_file` on an image (deepagents already hands it back
as base64 content blocks with the source path stamped into the tool message),
that read now surfaces everywhere Nova shows activity:

- **TUI**: a dim `Image read: <path> — click to inspect` card mounts under the
  tool card. Clicking it (or focusing and pressing Enter) opens an image
  preview modal: native terminal graphics (Sixel/TGP via `textual-image`)
  when the terminal supports them, PIL half-block pixel rendering otherwise,
  with `Esc` to close.
- **Telegram**: the bridge sends the image as a native photo at the moment of
  the read. The path resolves through the same workspace-root mapping as the
  final-response previews — virtual paths (`/.shots/x.png`), relative paths,
  and in-workspace absolute paths work; URLs and anything outside the
  workspace are refused.
- **Discord**: the bridge gains image sending entirely (`discord.File`), wired
  for both inbound-message sessions and pre-provisioned session targets, with
  the same workspace-root resolution and best-effort error handling.
- The headless/remote event loop (`execute_task`) forwards image reads through
  a `read_image_fn` hook on the remote message; bridges without one (none
  today) simply no-op.

Data plumbing: `FileOperationRecord.read_image` carries the read's image path
(from the tool message's `read_file_path`/`read_file_media_type`), so no new
event type or agent-loop change was needed.
