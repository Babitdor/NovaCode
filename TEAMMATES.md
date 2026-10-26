# Persistent local teammates

Implementation contract approved by “Implement the proposed plan.”

Profile-owned teammates run in one detached local worker, independent of TUI
lifetime. Each agent's only operational tools are its own Linux desktop, vision,
and private notes. Each agent input requires an owner decision. Existing temporary
`/computer` sessions retain their current stop-and-delete behavior.

Acceptance scenarios:

- Closing/reopening the TUI reconnects to the same worker and durable task history.
- Stop and automatic idle suspension retain VM disk, files, and browser profile.
- Worker restart recovers paused; unfinished/uncertain actions never replay automatically.
- Pending decisions survive client disconnect; changed observations/control invalidate them.
- Manual takeover gates agent input and serializes pointer/keyboard operations.
- Host files, shell, credentials, Code Mode, global tool discovery, and delegation are denied.
- Deletion requires confirmation and verified ownership; failed deletion retains recovery data.
- Concurrent clients cannot start competing workers or run two tasks on the same agent.
- Agent tabs, settings, history, approvals, private notes, and computers remain independent.

Failure model: lost worker ownership, reused PIDs, duplicate requests, a crash
between input and recording its result, a stale approval, forged/foreign VM
identity, hostile webpage instructions, failed suspension, and interrupted
deletion. Cover these through focused lifecycle, RPC, authorization, and recovery
tests plus a real QEMU persistence probe. Use existing dependencies only.

Defaults: local QEMU, 2 CPUs/4 GiB RAM per active guest, one task at a time per agent, explicit
Resume after restart, automatic suspension on task termination, no OS-startup
registration. Instagram monitoring and cloud computers are outside this version.

## Use

Open `/teammates` in a local Nova TUI. Use **+ New teammate** to create agents;
each gets a named tab containing its own Work, History, and Settings views.
Switching agent tabs preserves unsent tasks, settings drafts, and history selections.
Create a name, edit its role, and assign a task. Agents can work concurrently;
Pause, Resume, Stop, Delete, and Computer affect the selected agent only.
Model and vision selections are copied from Nova when creating the
teammate; credentials continue to come from Nova's credential system. A capable
main model receives screenshots directly; otherwise the configured vision model
describes them. A failed vision request prevents desktop input.

In Settings, choose a provider and enter its model ID, then Save changes.
Pause active work before saving. The optional vision selector supplies a separate
vision-capable model for text-only assistants. These choices belong to the teammate;
they preserve its computer, notes, conversation, and task history and do not change
Nova's main model. Configure provider credentials through `/auth`. Saving settings
does not test credentials; the next task uses the new selection.

Each desktop input appears as an exact proposal in the panel. Open Preview to
compare the live desktop with the immutable observation attached to the pending
proposal, then Approve once or Deny. Global auto-approve does not bypass this.
Closing the panel or TUI leaves the detached worker and pending proposal alive.

After opening `/teammates`, task updates also appear in the main conversation as
collapsible entries while the TUI is connected, even after closing the panel.
Each entry shows the named teammate, task state, Markdown result, and recorded
usage. Entries update in place and keep your expanded/collapsed preference.
These owner-visible entries are not added to the main agent's model context.
The teammate's History tab retains its own task selector, Markdown results,
per-task usage breakdown, and totals across its task history. Input/output tokens,
cache usage, model calls, and estimated cost include vision and resumed attempts;
missing token counts and unknown prices are explicitly marked.

Preview's Take Control pauses the task and waits for issued input. Click the
image, use its private text field, or send a key chord such as `ctrl,l`.
Manually typed text is not added to input records or conversation messages.
Return Control leaves the task paused; Resume in the panel starts with a fresh
observation. Closing the browser does not return control automatically.

Pause retains unfinished work. Stop cancels unfinished assignments and suspends
the guest, preserving files and browser profile. Delete requires entering the
teammate's exact name and removes its verified guest, private checkpoints, notes,
and saved observations. Cleanup failures retain records for inspection/retry.

`nova teammates status` lists all agents in a running worker without starting one.
`nova teammates stop` gracefully stops all agents and the worker. Reopening the panel launches
it again if necessary; recovered assignments remain paused until Resume.
No OS startup registration is installed.

When upgrading from a running single-agent worker, finish current work, run
`nova teammates stop` once, and reopen `/teammates` to load the multi-agent worker.
Existing computers and history stay in place. Stop cancels unfinished assignments.

Profile state lives under `~/.nova/teammates/`: the worker fence and loopback
credentials stay at the profile root. New agents retain their own coordination DB,
private conversation checkpoints, VM manifest, and approval images under
`agents/<agent-id>/`. An existing single teammate is adopted in place: its files,
computer identity, conversation, and history are not moved or recreated.
Project/tab/conversation resets do not change teammate identity. One worker
fences profile writes; each agent's inbox runs its assignments sequentially.
All agents recover paused after worker restart. Existing team payload, turn,
result, task, and authorization limits apply; the standard graph recursion limit
bounds each invocation. History reaching those limits requires deleting and
creating a new teammate. There are no new dependencies.

## Verification

Offline regression coverage: `tests/test_teammate_registry.py`, `tests/test_teammates_ui.py`,
`tests/test_teammate_updates.py`, `tests/test_teammates.py`, and
`tests/test_teammate_computer.py`, alongside existing computer, team lifecycle,
preview security, deepagents compatibility, and Windows input tests.

Opt-in real-VM proof (creates its own 2-CPU/4-GiB guest; may download Linux):

```powershell
python -m scripts.verify_teammate_vm prepare
python -m scripts.verify_teammate_vm verify
```

The commands must run in separate processes. They write a guest file and
Chromium local-storage sentinel into a dedicated guest browser profile, suspend,
resume, and verify both. The second command reattaches by the saved identity.
After rebooting the host, run `verify` again to check actual host-reboot recovery.
Never delete/recreate the guest to make a failed persistence check pass.
Only after completing the checks, explicitly remove this proof guest with:

```powershell
python -m scripts.verify_teammate_vm cleanup
```

Local proof on 2026-10-26 passed on Windows with Cua 0.9.0 and QEMU
11.1.0 (`v11.1.0-12130-ge470268ff4`), using WHPX. File and Chromium local storage
survived repeated suspend/resume and a fresh Python process. Cua 0.9.0 refuses
`sandboxes.connect` for a suspended guest: Nova verifies its listed identity,
uses the supported `spaces.start` power operation, then reconnects. Recovery
suspends an already-running retained guest without booting a suspended one.
Actual host reboot and Linux-host real-VM behavior remain unverified.
