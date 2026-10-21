# NovaCode v1.0.59

## Added

- Temporary public sharing for Artifacts, Council, Cowork, and Create through
  `/share`, with commands to list and stop active shares.
- Responsive mobile layouts for the shared web interfaces.
- Artifact restoration when resuming a saved session. Switching sessions now
  loads that session's artifacts without retaining artifacts from the previous
  session.

## Fixed

- `/share` without arguments once again uploads a session gist without raising
  `UnboundLocalError`.
