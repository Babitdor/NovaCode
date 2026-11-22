//! Ratatui client for the Nova TUI server.
//!
//! Split so the interesting parts are testable without a terminal:
//!
//! * [`frames`] — decoding/encoding the wire protocol (`rust/PROTOCOL.md`);
//! * [`state`] — the pure reducer from frames to transcript state;
//! * [`client`] — a two-thread socket client (no async runtime);
//! * [`server`] — supervises a Python server the client was told to start;
//! * [`ui`], [`markdown`], [`theme`] — drawing.

pub mod client;
pub mod frames;
pub mod markdown;
pub mod server;
pub mod state;
pub mod theme;
pub mod ui;
