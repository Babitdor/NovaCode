//! Optionally supervise a Python server the client was told to start.
//!
//! The launcher path (`nova --ui ratatui`) never uses this: Python is already
//! running and spawns the client. `--server-cmd` exists for the case where the
//! client is started on its own (a dev loop, an integrator wiring the pieces),
//! and it is the only case where the client owns a child process.
//!
//! The child is killed on drop, so a panic or an early return cannot leave a
//! server holding the agent's files and the port.

use std::io;
use std::process::{Child, Command};

/// A server process owned by this client.
#[derive(Debug)]
pub struct Supervised {
    child: Child,
}

impl Supervised {
    /// Start `command` through the platform shell.
    ///
    /// # Errors
    /// Returns the spawn error when the shell or the command cannot start.
    pub fn spawn(command: &str) -> io::Result<Self> {
        let mut command_line = if cfg!(windows) {
            let mut cmd = Command::new("cmd");
            cmd.args(["/C", command]);
            cmd
        } else {
            let mut cmd = Command::new("sh");
            cmd.args(["-c", command]);
            cmd
        };
        let child = command_line.spawn()?;
        Ok(Self { child })
    }

    /// The child's process id, for diagnostics.
    pub fn id(&self) -> u32 {
        self.child.id()
    }
}

impl Drop for Supervised {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
