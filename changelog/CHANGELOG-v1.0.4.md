# Changelog - v1.0.4

## Security Improvements

- **More conservative decision approvals**: Approval decisions now include the active user request, use a stricter risk rubric and confidence threshold, and deterministically reject high-impact deletion, overwrite, Git-history, database, and system commands. Unclear or low-confidence answers still fail closed.
