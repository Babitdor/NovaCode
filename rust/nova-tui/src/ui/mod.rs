//! Drawing: layout, transcript, composer, status bar and modals.
//!
//! `draw` is the whole rendering entry point; the replay test calls it against
//! `TestBackend` so a layout regression is a diff, not a bug report.

pub mod avatar;
pub mod brand_art;
pub mod composer;
pub mod council;
pub mod home;
pub mod matrix;
pub mod modal;
pub mod palette;
pub mod screen;
pub mod statusbar;
pub mod tabs;
pub mod terminal;
pub mod transcript;

use ratatui::layout::{Constraint, Layout};
use ratatui::Frame;

use crate::state::AppState;
use crate::theme::Theme;

/// Whether the empty-conversation home banner belongs on screen.
fn home_visible(state: &AppState) -> bool {
    state.entries.is_empty()
        && state.streaming.is_empty()
        && !state.turn_running
        && state.modal.is_none()
        && state.screen.is_none()
        && state
            .home
            .as_ref()
            .is_some_and(crate::ui::home::Home::shows_banner)
}

/// Draw the whole client into *frame*.
pub fn draw(frame: &mut Frame, state: &mut AppState, theme: &Theme) {
    let area = frame.area();
    // The tab bar costs a row only while it exists: one session is a label, not
    // a choice, and a permanently empty row would be a small lie about the UI.
    let tab_row = u16::from(tabs::visible(state));
    let chunks = Layout::vertical([
        Constraint::Min(1),
        Constraint::Length(tab_row),
        Constraint::Length(3),
        Constraint::Length(1),
    ])
    .split(area);

    transcript::draw(frame, chunks[0], state, theme);
    // The home banner owns an empty conversation, the way the terminal UI's home
    // screen does: art and rain while there is nothing to read.
    if home_visible(state) {
        if let Some(banner) = state.home.as_mut() {
            banner.draw(frame, chunks[0], theme);
        }
    }
    if tab_row == 1 {
        tabs::draw(frame, chunks[1], state, theme);
    }
    composer::draw(frame, chunks[2], state, theme);
    statusbar::draw(frame, chunks[3], state, theme);
    // A running shell command owns the conversation area: its output is what the
    // user asked for, and the transcript is still there when it closes.
    if let Some(terminal) = state.terminal.as_ref() {
        terminal::draw(frame, chunks[0], terminal, theme);
    }
    // Overlays stack in priority order: an interrupt owns the screen, then a
    // server-requested screen, then the palette.
    if state.modal.is_some() {
        modal::draw(frame, area, state, theme);
    } else if state.screen.is_some() {
        screen::draw(frame, area, state, theme);
    } else {
        palette::draw(frame, area, state, theme);
    }
    // `/nova` floats over the conversation above the composer. The server keeps
    // it off the keyboard modals, and the client keeps it off its own overlays
    // so a dialog never fights a sprite for the same cells.
    if let Some(sprite) = state.avatar.as_ref() {
        if state.modal.is_none() && state.screen.is_none() && !state.palette.open {
            let talking = !state.streaming.is_empty();
            let busy = state.turn_running || talking;
            avatar::draw(frame, chunks[0], sprite, busy, talking, busy && !talking);
        }
    }
}
