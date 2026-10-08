# NovaCode 1.0.15 — Update completion screen

- After a successful `nova update`, clear interactive progress output and show a centered NOVA logo, the newly installed version, “Update successful.” and “Restart Nova”.
- Show the same completion screen from the Windows update helper after the launcher exits and installation finishes. Read the version from the updated environment instead of a previously imported version.
- Keep failed-update diagnostics visible, preserve the up-to-date message for no-op updates, and leave redirected output free of terminal control codes.
