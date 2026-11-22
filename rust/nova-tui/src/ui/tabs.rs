//! The session tab bar: one chip per session the server holds.
//!
//! It only appears when there is more than one session, because a single tab
//! is a label rather than a choice. The active chip is the connection's own
//! session — the server keeps that honest by echoing `active` on `sessions`
//! and after `attach`.

use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::Paragraph;
use ratatui::Frame;

use crate::state::{AppState, Effect};
use crate::theme::Theme;

/// How many tabs to draw before the rest are summarised as `+n`.
const MAX_TABS: usize = 8;

/// Whether the tab bar has anything to say.
///
/// Args:
///     state: Client state.
pub fn visible(state: &AppState) -> bool {
    state.sessions.len() > 1
}

/// Draw the tab bar into *area*.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    frame.render_widget(
        Paragraph::new(line(state, theme, area.width as usize)),
        area,
    );
}

/// Build the tab line, truncated to *width*.
pub fn line(state: &AppState, theme: &Theme, width: usize) -> Line<'static> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    let active = state.active_session.as_deref();
    for (index, session) in state.sessions.iter().take(MAX_TABS).enumerate() {
        let label = tab_label(index, session, active, theme);
        spans.push(label);
    }
    if state.sessions.len() > MAX_TABS {
        spans.push(Span::styled(
            format!("  +{}", state.sessions.len() - MAX_TABS),
            theme.muted(),
        ));
    }
    Line::from(fit(spans, width))
}

/// One chip: `alt+<n>` prefix, the name, and a running marker.
fn tab_label(
    index: usize,
    session: &crate::frames::SessionInfo,
    active: Option<&str>,
    theme: &Theme,
) -> Span<'static> {
    let current = active == Some(session.id.as_str());
    let name = if session.name.is_empty() {
        short_id(&session.id)
    } else {
        session.name.clone()
    };
    let busy = if session.status == "running" {
        "•"
    } else {
        ""
    };
    let text = format!(" {}{} {busy}", index + 1, name);
    if current {
        Span::styled(text, theme.accent_border())
    } else {
        Span::styled(text, theme.muted())
    }
}

/// The effect for an `Alt+<digit>` press: attach to that tab position.
///
/// Args:
///     state: Client state, for the session list the number indexes into.
///     digit: The pressed digit, `'1'` onwards.
///
/// Returns:
///     A send effect for an existing tab, or `None` when there is no such tab
///     (a key that does nothing beats a guess at what the user meant).
pub fn attach_for(state: &AppState, digit: char) -> Option<Effect> {
    let index = digit.to_digit(10)? as usize;
    let session = state.sessions.get(index.checked_sub(1)?)?;
    Some(Effect::Send(crate::frames::attach(&session.id)))
}

/// The first eight characters of an id, for a session that was never named.
fn short_id(id: &str) -> String {
    id.chars().take(8).collect()
}

/// Keep the spans that fit in *width*, so the bar never wraps.
fn fit(spans: Vec<Span<'static>>, width: usize) -> Vec<Span<'static>> {
    let mut used = 0usize;
    let mut kept = Vec::new();
    for span in spans {
        let size = span.content.chars().count();
        if used + size > width {
            break;
        }
        used += size;
        kept.push(span);
    }
    kept
}
