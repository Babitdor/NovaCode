# NovaCode 1.1.6

## Terminal interface

- Replace generic UI emojis with compact text symbols that accept theme colors,
  including tool activity, voice, memory, remote status, and dialogs. Preserve
  success, warning, and error distinctions without requiring extra fonts.
- Keep emojis in conversation content, filenames, tool arguments, and remote
  reactions unchanged.
- Remove the composer placeholder and focus highlights for a cleaner input area.

## Remote sessions

- Request approval through Telegram and Discord for tasks started in remote chat,
  instead of automatically approving them. Keep terminal task approvals local.
- Reconnect previously enabled remote bridges when resuming a saved session.

## Performance and caching

- Reduce background monitoring stalls and improve waiter cancellation and cleanup.
- Share the embedding model across local retrieval features and bound local caches.
- Improve request cache diagnostics and preserve reusable prompt prefixes during
  tool discovery and compaction, with portable behavior for unsupported routes.
- Record live network load validation results separately from offline checks;
  cache savings remain dependent on the provider, route, and workload.
