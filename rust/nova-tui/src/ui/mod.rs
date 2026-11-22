//! Drawing: layout, transcript, composer, status bar and modals.
//!
//! `draw` is the whole rendering entry point; the replay test calls it against
//! `TestBackend` so a layout regression is a diff, not a bug report.

pub mod composer;
pub mod council;
pub mod modal;
pub mod palette;
pub mod screen;
pub mod statusbar;
pub mod tabs;
pub mod transcript;

use ratatui::layout::{Constraint, Layout};
use ratatui::Frame;

use crate::state::AppState;
use crate::theme::Theme;

/// Draw the whole client into *frame*.
pub fn draw(frame: &mut Frame, state: &AppState, theme: &Theme) {
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
    if tab_row == 1 {
        tabs::draw(frame, chunks[1], state, theme);
    }
    composer::draw(frame, chunks[2], state, theme);
    statusbar::draw(frame, chunks[3], state, theme);
    // Overlays stack in priority order: an interrupt owns the screen, then a
    // server-requested screen, then the palette.
    if state.modal.is_some() {
        modal::draw(frame, area, state, theme);
    } else if state.screen.is_some() {
        screen::draw(frame, area, state, theme);
    } else {
        palette::draw(frame, area, state, theme);
    }
}
