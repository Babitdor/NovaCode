//! Shared turn activity for the transcript, footer, and pending tools.

use crate::state::AppState;

pub fn spinner(tick: u64) -> &'static str {
    const FRAMES: [&str; 8] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧"];
    FRAMES[(tick / 2 % FRAMES.len() as u64) as usize]
}

pub fn label(state: &AppState) -> Option<&str> {
    if let Some(activity) = state.status.activity.as_deref() {
        return Some(activity.lines().next().unwrap_or("Working"));
    }
    state.turn_running.then_some("Working")
}
