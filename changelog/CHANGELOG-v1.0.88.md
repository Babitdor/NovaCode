# NovaCode v1.0.88

## Features

- Add the optional Rootapp remote companion, with saved configuration, sender
  authorization, editable responses and session routing through Root's local DevHost.
- Give Discord sessions their own threads or forum posts, restore thread routing
  on reconnect and resume, and archive threads when their sessions close.

## Fixes

- Reject Windows drive, UNC, device, alternate-stream and traversal escapes in
  virtual filesystem paths. Resolve memory and mounted paths through their actual
  backend and enforce canonical path denials and teammate memory-write boundaries.
- Correct saved path approval validation, root grants and Windows case handling;
  persist grants with restrictive atomic file permissions.
- Refuse daemon stop operations when process identity cannot be verified, tighten
  PID reuse checks, treat exited Linux zombie processes as stopped, and use the
  Windows system taskkill executable for cleanup.
- Isolate vision configuration tests and block accidental writes to live user settings.
- Update vulnerable Python dependencies and the Root companion's locked dependencies.
  One OpenTelemetry advisory remains constrained by the optional agent server's
  upstream dependency requirements.

## Verification

- Validate security, remote routing, portable paths, images and daemon operations
  with focused regression tests, including real daemon process-tree checks on
  Windows and Ubuntu.
- Include the new security and daemon regressions in Windows and Linux CI.
