# Nova's own computer

Nova can operate a separate local Linux VM and show its desktop in a private
browser window. The `computer` extra uses the MIT-licensed Cua SDK 0.9.0 with
QEMU explicitly selected. It never selects a container, a cloud provider, or
the host desktop as a fallback. There are no hosting fees or account requirements.
Nova defaults Cua usage telemetry off for its process unless you explicitly
set `CUA_TELEMETRY` in the environment.

## Install

For a global installation:

```sh
uv tool install --upgrade "novacode-cli[computer] @ git+https://github.com/Babitdor/NovaCode.git"
```

Include your other desired extras in the same command; omitted extras are
removed when reinstalling. From a checkout:

```sh
uv sync --locked --extra computer --group dev
```

The optional SDK and bundled Cua CLI download about 106 MiB on Windows x64.
The Linux desktop image is an additional download on the first VM start and
is cached by Cua. Allow several GiB of free disk space and enough free memory
for a 4 GiB guest plus Nova and your host applications.

Install QEMU (`qemu-system-x86_64` and `qemu-img` on x64 hosts) and make the
binaries available on PATH. On Windows:

```powershell
winget install --id SoftwareFreedomConservancy.QEMU --exact
```

Nova also checks the standard `%PROGRAMFILES%\qemu` directory when the required
binaries are missing from its inherited PATH. Custom installations still need
their directory on PATH. This fallback changes only Nova's process environment.

Restart the terminal after installation and add QEMU's installation directory
to PATH if needed. Existing Nova and terminal processes retain their old PATH,
even after updating the saved Windows environment. To refresh the current
PowerShell before launching Nova:

```powershell
$env:Path = "C:\Program Files\qemu;$env:Path"
nova
```

Enable hardware virtualization in your firmware and the
Windows Hypervisor Platform feature for WHPX acceleration. Changing that
Windows feature may require administrator rights and a reboot. Linux normally
uses KVM; macOS normally uses HVF. Emulation without acceleration can be slow.

Inspect prerequisites without starting or installing anything:

```sh
uvx --from cua==0.9.0 cua runtime --json doctor
```

Check the report's `qemu` backend. Docker/container and Lume readiness are not
required for Nova's QEMU computer. On this development machine the SDK imports
successfully and QEMU 11.1.0 reports ready after refreshing PATH; a real guest
boot has not been verified here.

## Use

| Command | Result |
| --- | --- |
| `/computer start` | Create a Linux desktop for the current session and open its preview |
| `/computer status` or `/computer` | Show readiness and whether input is paused |
| `/computer preview` | Open or reopen the current session's preview |
| `/computer pause` | Block subsequent agent input |
| `/computer resume` | Permit input again; Nova must obtain a new screenshot |
| `/computer stop` | Delete the guest and its temporary files |

Then ask, for example: “Use your computer to open the browser and research
Python's latest release.” Select a model with vision support for reliable
visual interaction. A text-only model can use Nova's separately configured
`VISION` tool to inspect desktop images, with additional model calls.

The root agent receives `computer_status`, `computer_screenshot`, and
`computer_action`. Actions support click, double click, right click, pointer
movement, drag, text, key chords, and scrolling. Each action uses an observation
ID and returns the next screenshot. After a failed or uncertain input, Nova
must observe again before retrying. Screenshots and actions use the direct
multimodal tool path; Code Mode can query status. Input follows Nova's existing
approval policy and is blocked during plan mode. Auto-approve keeps its existing
meaning; it never overrides the user's pause control.

VMs belong to individual Nova sessions and survive graph/model rebuilds within
that process. Delegated agents and restricted Cowork agents do not inherit the
root's desktop tools. Starting a VM does not move ordinary shell or file tools
into it; those retain the session's existing backend.
VM creation is available in local Nova sessions; a remote sandbox session does
not gain access to the host's local desktop runtime.

## Preview and boundaries

The preview binds to `127.0.0.1` on an available port. Each session has a random
access token. Requests require that token; browser origins and host headers are
checked. The page contains no third-party scripts or fonts and is never
published through Nova's artifact sharing. Treat the preview URL as private.
It shows screenshots at up to one frame per second while the tab is visible;
hidden tabs stop requesting images. It is a viewing surface with pause, resume,
and stop controls, rather than a remote keyboard or mouse client.

Pause blocks new input; input already delivered to the guest can finish. Stop
deletes the guest, including its files. Nova also attempts deletion on normal
exit. If deletion fails, the runtime handle is retained so `/computer stop` or
shutdown can retry. A forcibly killed Nova process cannot guarantee cleanup;
inspect local Cua sandboxes before starting more guests.
Nova names its guests with a `nova-` prefix so they can be identified in the
local Cua sandbox list after an interrupted process.

No project directory, host clipboard, browser profile, model API key, or host
credentials are mounted or copied into the guest. File transfer and credential
sharing are not part of this initial feature. The guest has ordinary outbound
VM networking, which can reach Internet and local network services. This is
not a network air gap. Desktop and webpage content remain untrusted data;
Nova must obtain approval for external commitments under its normal policy.

## Verification

Offline tests cover session separation, explicit startup, QEMU-only creation,
multimodal tool results, stale observations, coordinate scaling, input bounds,
pause/resume, startup cancellation, cleanup retries, approval/plan-mode guards,
the TUI command lifecycle, and preview authentication/origin checks. Tests use
a fake guest and do not download or boot an operating system. The installed
Cua 0.9.0 constructor and screenshot option APIs were also inspected directly.

Upstream references:

- [Cua Python SDK](https://github.com/trycua/cua/tree/main/libs/cua/python)
- [Local runtime implementation](https://github.com/trycua/cua/tree/main/libs/cua/crates/cua-vmm)
- [SDK architecture](https://cua.ai/docs/cua-sdk/concepts/architecture)
