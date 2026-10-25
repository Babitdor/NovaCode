# NovaCode 1.0.99

## Local Linux computer preview

- Add an optional `computer` extra using Cua 0.9.0 and an explicitly selected
  local Linux QEMU VM with 2 CPUs and 4 GiB RAM. Check QEMU before guest download.
- Add `/computer start|preview|status|pause|resume|stop` with nonblocking startup
  and a private token-protected loopback browser preview.
- Give the local root agent screenshot and bounded desktop-input tools with
  observation IDs, normal approvals, plan-mode restrictions and user pause.
  Delegated agents do not inherit desktop tools; no host files or credentials
  are mounted by this feature.
- Keep screenshots and actions on the direct multimodal tool path. Attempt guest
  deletion on shutdown and retain failed-cleanup handles for retry.
- Document prerequisites, temporary guest files, ordinary guest networking and
  refreshing an existing Windows terminal's PATH after installing QEMU.

## Documentation and interface catalog

- Shorten the README to setup essentials and product capabilities; preserve
  detailed installation, voice and workflow instructions in SETUP.md.
- Refresh FEATURES.md with landing-page copy, native Code Mode, Whistle,
  lightweight speech options, download progress and explicitly labeled VM preview.
- Include the previously added eight interface styles, character art and animated
  homes in the v1.0.99 feature reference.

Validation: offline computer lifecycle, desktop tools, preview authentication,
TUI controls and agent/policy compatibility checks. QEMU 11.1.0 prerequisite
inspection succeeds on Windows after refreshing PATH. A real guest boot has not
been verified; this release does not claim measured VM or audio performance.
The Cua SDK is optional; base installations do not gain a hypervisor dependency.
