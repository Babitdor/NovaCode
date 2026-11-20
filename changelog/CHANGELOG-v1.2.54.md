# NovaCode 1.2.54

## Code Mode

- Edits made inside a snippet now reach the transcript. A nested write has no
  message of its own, so the eval call is the only card the transcript has; the
  file-operation tracker records the nested write against that call, and its diff
  is emitted before the eval result. Code Mode edits now show a diff, participate
  in undo, and count for verification — all three read the same records.
- A snippet that fails, times out or is cancelled abandons its pending record
  instead of reading the file back: the write may or may not have landed, and a
  diff built from that would describe a guess as a change.
- The recovery hint and the Code Mode prompt now say what the runtime actually
  wants: a snippet is a script, leave the value in its final expression instead
  of writing `return`, and a background job reports back on its own rather than
  being polled with `Start-Sleep` once per round trip.

## Policy and approvals

- `git config` is judged by its form instead of by substring. Reading is how an
  agent learns the identity it is about to commit with, so reads are allowed;
  `--add`, `--unset`, `--replace-all`, `--edit` and a second positional argument
  are still blocked. A command with several segments is split first, so a read in
  front of a write cannot hide it.
- Commands whose effect has already left the machine — a pushed tag, a published
  package — now always require a fresh decision. A remembered "always allow" for
  `npm run build` was never an answer for `npm publish`, and a published version
  number can never be reused.
- A policy denial names the tool and its reason in the approval prompt, instead of
  a bare "denied by policy".

## Shell

- Captured output is stripped of terminal colour and escape sequences. Tools that
  believe they are talking to a console (PowerShell’s `Select-String` highlights
  the match inside the line it prints) were sending `ESC[7m` through to the model.
  Stripping happens where the log is joined, so a sequence split across two reads
  is removed whole.
- A command whose only effect is passing time (`Start-Sleep`, `sleep`) gets one
  advisory per session in the model’s copy of the output, never in the job result.

## Updates

- A `uv tool` installation created from a Git requirement can update again. uv
  resolves a Git requirement to the commit it already has, so `uv tool upgrade`
  reported "Nothing to upgrade" forever while `nova update` then claimed Nova was
  up to date. The recorded requirement is now reinstalled (`uv tool install
  --force --reinstall`); registry installs keep using `uv tool upgrade`.
- On POSIX, installers take a real lock on the installation, and the lock is
  re-entrant inside a process so the TUI can still update while its own launch
  holds the shared runtime lock. Two updates can no longer write one prefix at
  the same time.
- Every install step has a 15-minute deadline: a stalled `uv` or `git` is killed
  and the failure names the limit. Ctrl+C stops the child instead of waiting for
  it to finish.
- A source checkout updates with untracked files present (they cannot block a
  fast-forward). A checkout with commits that are not on its upstream says to
  rebase or merge them, before anything is fetched.
- A handoff that times out now prints why and how to resume, including the session
  id, instead of dropping to a bare prompt.
- Whether an update changed anything is read from the installed version and
  revision, not from uv’s wording.
- The Windows native-file pre-check walks the prefix with build output pruned
  instead of globbing every path.
- CI now exercises the updater against real installations: `nova update --check`
  from a registry install, the refusal a locally built wheel must produce, and the
  Git requirement install at the version the checkout declares.

## TUI

- Tool rows show their own duration and the turn’s elapsed time: `Took 120ms ·
  Elapsed 1.3s`. Durations render in milliseconds below a second, seconds with one
  decimal above, and minutes beyond that; the elapsed value updates while a call
  runs and replayed history carries no timing.

## Install or upgrade

Close NovaCode before upgrading from your shell.

```text
uv tool upgrade novacode-cli
```

For a fresh installation, run `uv tool install --python 3.11 novacode-cli`.
Include any optional extras you use.

```text
uv tool install --python 3.11 "novacode-cli[browser,voice]"
```
