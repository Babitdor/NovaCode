# NovaCode 1.2.39

## A more responsive terminal

- QuickJS starts and restores its state off the terminal event loop. This keeps
  the composer responsive while Code Mode starts, including during Playwright runs.
- Tool cards load their exact source and full output when expanded. Code Mode
  also avoids duplicate tool discovery work and reuses its saved preference
  until the profile changes.
- Oracle refreshes displayed scores when relevant workspace files change.
  Approval still requires a full workspace fingerprint check.

## Ralph, Proactive agents, and Telegram

- `/ralph` shows progress, elapsed time, Pause, and Stop in the transcript.
  Runs have separate checklists and logs, use the session’s approval settings,
  and can resume in their original workspace with `/ralph --resume`. Pass
  `--verify` to run the command in `.nova/ralph/verify.txt`.
- `/proactive` separates configuration from run activity. Both panes appear on
  wide terminals, with tabs on compact layouts.
- Plan approval in Telegram renders headings, lists, and code. Approve and
  Reject appear after the full plan has been delivered.

## Install or upgrade

```text
uv tool install --python 3.11 novacode-cli==1.2.39
uv tool upgrade novacode-cli
```

Restart NovaCode after upgrading. Include any optional extras you use.

```text
uv tool install --python 3.11 --force "novacode-cli[browser,voice]==1.2.39"
```
