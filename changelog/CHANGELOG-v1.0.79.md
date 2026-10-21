# NovaCode v1.0.79

## Fixes

- Stop the background dream from dying on `'charmap' codec can't decode byte 0x9d`. The dream agent served its memory files with upstream deepagents' filesystem backend, whose ripgrep path decoded search output with the platform locale codec (cp1252 on Windows); a single non-ASCII character in a matched line raised `UnicodeDecodeError` and stopped the dream. The dream memory route now uses NovaCode's own backend, and both its ripgrep path and its Python fallback decode search output as UTF-8 with a replacement fallback instead of depending on the active code page.
- Make a LangSmith configuration entered at runtime actually take effect. LangSmith caches the environment and the client it built from it, so setting `LANGSMITH_*` after startup left the tracer detached. `/trace enable` and `/trace disable` now clear that cached state as well.

## Features

- `/trace` now takes the LangSmith environment from the user and activates tracing with it. A form asks for `LANGSMITH_TRACING`, `LANGSMITH_ENDPOINT`, `LANGSMITH_API_KEY` and `LANGSMITH_PROJECT` (pre-filled with the values already in use), and the result is applied and switched on without restarting Nova. The same four questions are asked on the command line outside the TUI, where an empty answer keeps the value that is already set.
- Keep the rest of the tracing command working next to the new setup flow: `/trace status`, `/trace enable [KEY] [--project NAME]`, `/trace disable`, `/trace projects` and `/trace traces [--limit N]`.
- Add regression coverage for the encoding fix (UTF-8 files through both the ripgrep path and the Python fallback, a cp1252 file in the fallback, and a grep over the dream memory files) and for the tracing setup (the four environment variables, the cache reset that makes a runtime change visible, the TUI form and the command-line prompts).
