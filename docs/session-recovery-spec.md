# Crash recovery acceptance criteria

This autonomous implementation uses only existing dependencies. Spec approval was
not obtained separately; the user authorized implementation of crash-safe sessions.

1. A saved session has one atomic snapshot containing its metadata, messages,
   todos and optional state. A failed replacement leaves the previous snapshot readable.
2. A crash while updating compatibility files cannot mix old and new conversation
   halves on resume. Existing sessions without a snapshot remain readable.
3. Prompt submission saves the incoming prompt before starting the model. During a
   turn Nova saves graph checkpoints every five seconds and after completion,
   interruption or error. Recovery never executes a saved tool call itself.
4. Autosave failures are logged and do not terminate a turn. Empty crash state cannot
   overwrite a previously saved conversation. Writes are serialized; /clear retains
   its cleared marker and a save for an old thread cannot overwrite a new session.
5. Sessions remain accessible with `nova --continue` and the existing session picker.

Failure model: interrupted writes (replacement and compatibility-file tests), abrupt
process exit (subprocess recovery test), stale or concurrent writes (serialized-save
tests), invalid legacy files (fallback tests), unavailable graph state (failsafe tests).
Hard termination can lose graph progress since the last completed save; uncommitted
streaming tokens are not graph messages. No filesystem can guarantee durability when
the disk itself fails.
