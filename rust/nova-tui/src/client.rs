//! Socket client: one writer thread, one reader thread, no async runtime.
//!
//! The server is line-delimited JSON over TCP (see `rust/PROTOCOL.md`). A reader
//! thread turns lines into [`ClientEvent`]s on a channel, so the UI loop can
//! wait on `recv_timeout` for either a key press or a frame without blocking on
//! either. Writes go through their own thread so a slow socket can never stall a
//! key press.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpStream;
use std::sync::mpsc::{channel, Receiver, Sender};
use std::thread;
use std::time::Duration;

use crate::frames::{self, Inbound};

const HELLO_TIMEOUT: Duration = Duration::from_secs(10);
/// The Python server may initialize the session after authentication.
const SESSION_READY_TIMEOUT: Duration = Duration::from_secs(30);

/// Something that happened on the socket.
#[derive(Debug, Clone)]
pub enum ClientEvent {
    /// One raw line, ready for [`frames::decode`].
    Line(String),
    /// The connection ended, with the reason to show the user.
    Closed(String),
}

/// A connected, authenticated client.
pub struct Client {
    outbox: Sender<String>,
    events: Receiver<ClientEvent>,
    /// The `ready` frame that ended the handshake.
    pub ready: Inbound,
}

impl Client {
    /// Connect, perform the handshake and return a live client.
    ///
    /// # Errors
    /// Returns the underlying IO error when the socket cannot be opened, and an
    /// `InvalidData` error when the handshake is refused or malformed.
    pub fn connect(address: &str, token: &str) -> std::io::Result<Self> {
        let stream = TcpStream::connect(address)?;
        let write_half = stream.try_clone()?;
        let (outbox, outbox_rx) = channel::<String>();
        let (event_tx, events) = channel::<ClientEvent>();

        thread::spawn(move || {
            let mut writer = write_half;
            for line in outbox_rx {
                if writer.write_all(line.as_bytes()).is_err() || writer.flush().is_err() {
                    return;
                }
            }
        });

        let reader_events = event_tx.clone();
        thread::spawn(move || {
            let reader = BufReader::new(stream);
            for line in reader.lines() {
                match line {
                    Ok(line) => {
                        if reader_events.send(ClientEvent::Line(line)).is_err() {
                            return;
                        }
                    }
                    Err(error) => {
                        let _ = reader_events.send(ClientEvent::Closed(error.to_string()));
                        return;
                    }
                }
            }
            let _ = reader_events.send(ClientEvent::Closed("server closed the connection".into()));
        });

        let mut client = Self {
            outbox,
            events,
            ready: Inbound::Unknown {
                kind: "pending".into(),
            },
        };
        client.handshake(token)?;
        Ok(client)
    }

    /// Send one already-serialized frame.
    ///
    /// # Errors
    /// Returns an IO error when the writer thread is gone (connection closed).
    pub fn send(&self, line: String) -> std::io::Result<()> {
        self.outbox
            .send(line)
            .map_err(|_| std::io::Error::new(std::io::ErrorKind::BrokenPipe, "connection closed"))
    }

    /// Block for the next socket event, up to *timeout*.
    pub fn recv_timeout(&self, timeout: Duration) -> Option<ClientEvent> {
        self.events.recv_timeout(timeout).ok()
    }

    /// Apply a client action (`capture`/`quit`) that the server asked for.
    pub fn request_shutdown(&self) {
        let _ = self.send(frames::shutdown());
    }

    fn handshake(&mut self, token: &str) -> std::io::Result<()> {
        self.send(frames::hello())?;
        let hello = self.expect_frame(HELLO_TIMEOUT)?;
        match hello {
            Inbound::Hello { protocol } if protocol != frames::PROTOCOL_VERSION => {
                // Forward compatibility: warn on stderr later, but never abort
                // on a newer server — both sides skip unknown frames.
            }
            Inbound::Hello { .. } => {}
            other => {
                return Err(invalid(format!("expected hello, got {other:?}")));
            }
        }
        self.send(frames::auth(token))?;
        let ready = self.expect_frame(SESSION_READY_TIMEOUT)?;
        match ready {
            Inbound::Error { message, .. } => Err(invalid(message)),
            Inbound::Ready { .. } => {
                self.ready = ready;
                Ok(())
            }
            other => Err(invalid(format!("expected ready, got {other:?}"))),
        }
    }

    fn expect_frame(&self, timeout: Duration) -> std::io::Result<Inbound> {
        let deadline = std::time::Instant::now() + timeout;
        loop {
            let remaining = deadline.saturating_duration_since(std::time::Instant::now());
            if remaining.is_zero() {
                return Err(invalid("handshake timed out".into()));
            }
            match self.events.recv_timeout(remaining) {
                Ok(ClientEvent::Line(line)) => {
                    if let Some(frame) = frames::decode(&line) {
                        return Ok(frame);
                    }
                    // Noise between frames is skipped, exactly like the server.
                }
                Ok(ClientEvent::Closed(reason)) => return Err(invalid(reason)),
                Err(_) => return Err(invalid("handshake timed out".into())),
            }
        }
    }
}

fn invalid(message: String) -> std::io::Error {
    std::io::Error::new(std::io::ErrorKind::InvalidData, message)
}
