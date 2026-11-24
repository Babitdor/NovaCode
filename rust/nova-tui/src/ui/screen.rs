//! One renderer for every modal the server can ask for.
//!
//! @@novacode_cli/ui/port.ScreenSpec@@ defines a closed vocabulary of screens -
//! list, table, detail, form, markdown, progress, confirm - so a command that
//! needs a modal picks a kind instead of shipping a widget. This module is the
//! whole client side of that contract: @@draw@@ paints whatever kind arrived and
//! @@handle_key@@ turns keys into the JSON answer that kind documents.
//!
//! Answer shapes (also in @@rust/PROTOCOL.md@@):
//!
//! * list / table: @@{"id": "<row id>"}@@
//! * detail: @@{"action": "<action id>"}@@ (or @@null@@ when dismissed)
//! * form: @@{"<field key>": "<value>", ...}@@
//! * confirm: @@true@@ or @@false@@
//! * markdown / progress: @@null@@

use crossterm::event::{KeyCode, KeyEvent};
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;
use serde_json::Value;

use crate::state::{AppState, Effect, ScreenKind, ScreenView};
use crate::theme::Theme;
use crate::ui::modal::centered;

/// Rows the popup keeps visible at once. The popup is 70% of the terminal, so
/// this is deliberately conservative: a form that fits one screen never
/// scrolls, and a longer one can always reach its last field.
const SCREEN_WINDOW: usize = 12;

/// Handle one key while a screen is open.
///
/// Returns:
///     @@Some(Effect::Answer)@@ when the modal produced its answer, else @@None@@.
pub fn handle_key(state: &mut AppState, key: &KeyEvent) -> Option<Effect> {
    let (screen_id, value) = {
        let view = state.screen.as_mut()?;
        match key.code {
            KeyCode::Up if matches!(view.kind, ScreenKind::List | ScreenKind::Table) => {
                view.selected = view.selected.saturating_sub(1);
                keep_visible(view);
                return None;
            }
            KeyCode::Down if matches!(view.kind, ScreenKind::List | ScreenKind::Table) => {
                let last = view.rows.len().saturating_sub(1);
                view.selected = (view.selected + 1).min(last);
                keep_visible(view);
                return None;
            }
            KeyCode::Up | KeyCode::Down if view.kind == ScreenKind::Form => {
                let last = view.fields.len().saturating_sub(1);
                view.focused = if matches!(key.code, KeyCode::Up) {
                    view.focused.saturating_sub(1)
                } else {
                    (view.focused + 1).min(last)
                };
                keep_visible(view);
                return None;
            }
            KeyCode::Up | KeyCode::Down if view.kind == ScreenKind::Split => {
                let last = view.actions.len().saturating_sub(1);
                view.selected = if matches!(key.code, KeyCode::Up) {
                    view.selected.saturating_sub(1)
                } else {
                    (view.selected + 1).min(last)
                };
                return None;
            }
            KeyCode::Up | KeyCode::Down
                if matches!(view.kind, ScreenKind::Detail | ScreenKind::Markdown) =>
            {
                let step: isize = if matches!(key.code, KeyCode::Down) {
                    1
                } else {
                    -1
                };
                view.scroll = (view.scroll as isize + step).max(0) as u16;
                return None;
            }
            KeyCode::PageUp | KeyCode::PageDown => {
                let step = SCREEN_WINDOW as isize;
                let delta: isize = if matches!(key.code, KeyCode::PageDown) {
                    step
                } else {
                    -step
                };
                match view.kind {
                    ScreenKind::List | ScreenKind::Table => {
                        let last = view.rows.len().saturating_sub(1) as isize;
                        view.selected = (view.selected as isize + delta).clamp(0, last) as usize;
                        keep_visible(view);
                    }
                    ScreenKind::Form => {
                        let last = view.fields.len().saturating_sub(1) as isize;
                        view.focused = (view.focused as isize + delta).clamp(0, last) as usize;
                        keep_visible(view);
                    }
                    ScreenKind::Detail | ScreenKind::Markdown => {
                        view.scroll = (view.scroll as isize + delta).max(0) as u16;
                    }
                    ScreenKind::Progress | ScreenKind::Confirm => {}
                    ScreenKind::Split => {}
                }
                return None;
            }
            KeyCode::Home if matches!(view.kind, ScreenKind::List | ScreenKind::Table) => {
                view.selected = 0;
                keep_visible(view);
                return None;
            }
            KeyCode::End if matches!(view.kind, ScreenKind::List | ScreenKind::Table) => {
                view.selected = view.rows.len().saturating_sub(1);
                keep_visible(view);
                return None;
            }
            KeyCode::Home if view.kind == ScreenKind::Form => {
                view.focused = 0;
                keep_visible(view);
                return None;
            }
            KeyCode::End if view.kind == ScreenKind::Form => {
                view.focused = view.fields.len().saturating_sub(1);
                keep_visible(view);
                return None;
            }
            KeyCode::Left if matches!(view.kind, ScreenKind::Detail) => {
                view.selected = view.selected.saturating_sub(1);
                return None;
            }
            KeyCode::Right if matches!(view.kind, ScreenKind::Detail) => {
                let last = view.actions.len().saturating_sub(1);
                view.selected = (view.selected + 1).min(last);
                return None;
            }
            KeyCode::Left | KeyCode::Right
                if view.kind == ScreenKind::Form && focused_choice(view) =>
            {
                let step: isize = if matches!(key.code, KeyCode::Right) {
                    1
                } else {
                    -1
                };
                if let Some(field) = view.fields.get_mut(view.focused) {
                    let count = field.options.len() as isize;
                    let current = field
                        .options
                        .iter()
                        .position(|option| option == &field.value)
                        .map_or(0, |index| index as isize);
                    let next = (current + step).rem_euclid(count);
                    field.value = field.options[next as usize].clone();
                }
                return None;
            }
            KeyCode::Char(' ') if view.kind == ScreenKind::Form && focused_toggle(view) => {
                if let Some(field) = view.fields.get_mut(view.focused) {
                    field.value = if field.value == "off" { "on" } else { "off" }.to_string();
                }
                return None;
            }
            KeyCode::Char(ch) if view.kind == ScreenKind::Form => {
                if let Some(field) = view.fields.get_mut(view.focused) {
                    if field.toggle {
                        // Typing must never corrupt a toggle: only Space flips it.
                        return None;
                    }
                    field.value.push(ch);
                }
                return None;
            }
            KeyCode::Backspace if view.kind == ScreenKind::Form => {
                if let Some(field) = view.fields.get_mut(view.focused) {
                    field.value.pop();
                }
                return None;
            }
            KeyCode::Enter | KeyCode::Tab if view.kind == ScreenKind::Form => {
                // Enter walks the fields, then submits; Tab only walks.
                let next = view.focused + 1;
                if next < view.fields.len() {
                    view.focused = next;
                    keep_visible(view);
                    return None;
                }
                (view.id.clone(), Some(view.accept()))
            }
            KeyCode::Char('n') | KeyCode::Char('N') if view.kind == ScreenKind::Confirm => {
                (view.id.clone(), Some(false.into()))
            }
            KeyCode::Char('y') | KeyCode::Char('Y') if view.kind == ScreenKind::Confirm => {
                (view.id.clone(), Some(true.into()))
            }
            KeyCode::Enter => (view.id.clone(), Some(view.accept())),
            KeyCode::Esc => (view.id.clone(), Some(view.cancel())),
            _ => return None,
        }
    };
    let value = value?;
    state.screen = None;
    // A screen attached to a command result has no id: it is informational, so
    // closing it answers nothing.
    if screen_id.is_empty() {
        return None;
    }
    Some(Effect::Answer { screen_id, value })
}

/// Scroll just enough that the selection or focused field is on screen.
fn keep_visible(view: &mut ScreenView) {
    let count = if view.kind == ScreenKind::Form {
        view.fields.len()
    } else {
        view.rows.len()
    };
    let current = if view.kind == ScreenKind::Form {
        view.focused
    } else {
        view.selected
    };
    // A table spends one row on its heading.
    let header = usize::from(view.kind == ScreenKind::Table);
    let row = current + header;
    let top = view.scroll as usize;
    if row < top {
        view.scroll = row as u16;
    } else if row >= top + SCREEN_WINDOW {
        view.scroll = row.saturating_sub(SCREEN_WINDOW - 1) as u16;
    }
    let max = (count + header).saturating_sub(SCREEN_WINDOW);
    view.scroll = view.scroll.min(max as u16);
}

/// Draw the open screen, if any, over the whole client area.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    let Some(view) = state.screen.as_ref() else {
        return;
    };
    let popup = centered(area, 84, 70);
    frame.render_widget(Clear, popup);
    frame
        .buffer_mut()
        .set_style(popup, theme.body().bg(theme.surface));
    if view.kind == ScreenKind::Split {
        draw_split(frame, popup, view, theme);
        return;
    }
    let mut lines: Vec<Line<'static>> = Vec::new();
    match view.kind {
        ScreenKind::List => {
            for (index, row) in view.rows.iter().enumerate() {
                lines.push(row_line(
                    index == view.selected,
                    row.label(),
                    &row.detail,
                    theme,
                ));
            }
        }
        ScreenKind::Table => {
            lines.push(Line::from(Span::styled(
                view.columns.join("  "),
                theme.heading(),
            )));
            for (index, row) in view.rows.iter().enumerate() {
                lines.push(row_line(
                    index == view.selected,
                    &row.cells.join("  "),
                    "",
                    theme,
                ));
            }
        }
        ScreenKind::Detail | ScreenKind::Markdown => {
            for line in view.body.lines() {
                lines.push(Line::from(Span::styled(line.to_string(), theme.body())));
            }
            if !view.actions.is_empty() {
                lines.push(Line::from(""));
                for (index, action) in view.actions.iter().enumerate() {
                    lines.push(row_line(index == view.selected, action.label(), "", theme));
                }
            }
        }
        ScreenKind::Form => {
            for (index, field) in view.fields.iter().enumerate() {
                let marker = if index == view.focused { "▏" } else { " " };
                // A toggle draws as a checkbox: the value is a state, not text.
                let body = if field.toggle {
                    let box_mark = if field.value == "off" { "[ ]" } else { "[x]" };
                    format!("{box_mark} {}", field.label)
                } else if field.options.is_empty() {
                    let value = if field.secret {
                        "•".repeat(field.value.chars().count())
                    } else {
                        field.value.clone()
                    };
                    format!("{}: {value}", field.label)
                } else {
                    // A choice shows that it is a choice: the arrows are the hint.
                    format!("{}: ◂ {} ▸", field.label, field.value)
                };
                lines.push(Line::from(Span::styled(
                    format!("{body}{marker}"),
                    if index == view.focused {
                        theme.heading()
                    } else {
                        theme.body()
                    },
                )));
            }
        }
        ScreenKind::Progress => {
            let total = view.total.max(1);
            let filled = (view.done.min(view.total) * 24 / total) as usize;
            lines.push(Line::from(Span::styled(view.body.clone(), theme.body())));
            lines.push(Line::from(Span::styled(
                format!(
                    "{}{}  {}/{}",
                    "█".repeat(filled),
                    "·".repeat(24 - filled),
                    view.done,
                    view.total
                ),
                theme.heading(),
            )));
        }
        ScreenKind::Confirm => {
            lines.push(Line::from(Span::styled(view.body.clone(), theme.body())));
        }
        ScreenKind::Split => {}
    }
    lines.push(Line::from(""));
    lines.push(Line::from(Span::styled(hints(view), theme.muted())));

    let title = if view.title.is_empty() {
        view.kind.label().to_string()
    } else {
        view.title.clone()
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(theme.accent_border())
        .title(Span::styled(format!(" {title} "), theme.muted()));
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false })
            .scroll((view.scroll, 0)),
        popup,
    );
}

/// Draw a side-by-side screen: two markdown panes and selectable actions.
fn draw_split(frame: &mut Frame, area: Rect, view: &ScreenView, theme: &Theme) {
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(theme.accent_border())
        .title(Span::styled(
            format!(
                " {} ",
                if view.title.is_empty() {
                    "split"
                } else {
                    &view.title
                }
            ),
            theme.muted(),
        ));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let rows = ratatui::layout::Layout::vertical([
        ratatui::layout::Constraint::Min(1),
        ratatui::layout::Constraint::Length((view.actions.len() + 1) as u16),
    ])
    .split(inner);
    let columns = ratatui::layout::Layout::horizontal([
        ratatui::layout::Constraint::Percentage(50),
        ratatui::layout::Constraint::Percentage(50),
    ])
    .split(rows[0]);
    for (chunk, body) in [
        (columns[0], &view.body_left),
        (columns[1], &view.body_right),
    ] {
        let lines: Vec<Line<'static>> = body
            .lines()
            .map(|line| Line::from(Span::styled(line.to_string(), theme.body())))
            .collect();
        frame.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), chunk);
    }
    let mut footer: Vec<Line<'static>> = Vec::new();
    for (index, action) in view.actions.iter().enumerate() {
        footer.push(row_line(index == view.selected, action.label(), "", theme));
    }
    footer.push(Line::from(Span::styled(hints(view), theme.muted())));
    frame.render_widget(Paragraph::new(footer).wrap(Wrap { trim: false }), rows[1]);
}

/// Whether the focused field is a fixed choice, so Left/Right cycles it.
fn focused_choice(view: &ScreenView) -> bool {
    view.fields
        .get(view.focused)
        .is_some_and(|field| !field.options.is_empty())
}

/// Whether any field is a fixed choice, so the hints can mention the arrows.
fn hint_choice(view: &ScreenView) -> bool {
    view.fields.iter().any(|field| !field.options.is_empty())
}

/// Whether any field is a toggle, so the hints can mention Space.
fn hint_toggle(view: &ScreenView) -> bool {
    view.fields.iter().any(|field| field.toggle)
}

/// Whether the focused field is a toggle, so Space flips instead of typing.
fn focused_toggle(view: &ScreenView) -> bool {
    view.fields
        .get(view.focused)
        .is_some_and(|field| field.toggle)
}

/// One selectable row with a marker, its label and optional detail.
fn row_line(selected: bool, label: &str, detail: &str, theme: &Theme) -> Line<'static> {
    let marker = if selected { "▸ " } else { "  " };
    let text = if detail.is_empty() {
        format!("{marker}{label}")
    } else {
        format!("{marker}{label}  {detail}")
    };
    Line::from(Span::styled(
        text,
        if selected {
            theme.heading()
        } else {
            theme.body()
        },
    ))
}

/// The key hints for a screen kind.
fn hints(view: &ScreenView) -> String {
    match view.kind {
        ScreenKind::List | ScreenKind::Table => "↑↓ move · Enter select · Esc close".to_string(),
        ScreenKind::Detail => "←→ action · Enter run · ↑↓ scroll · Esc close".to_string(),
        ScreenKind::Form if hint_toggle(view) && hint_choice(view) => {
            "␣ toggle · ←→ change · Tab next · Enter save · Esc cancel".to_string()
        }
        ScreenKind::Form if hint_toggle(view) => {
            "␣ toggle · Tab next field · Enter next/submit · Esc cancel".to_string()
        }
        ScreenKind::Form if hint_choice(view) => {
            "←→ change · Tab next field · Enter next/submit · Esc cancel".to_string()
        }
        ScreenKind::Form => {
            "type to edit · Tab next field · Enter next/submit · Esc cancel".to_string()
        }
        ScreenKind::Confirm => "y/Enter confirm · n/Esc cancel".to_string(),
        ScreenKind::Markdown => "↑↓/PgUp/PgDn scroll · Esc close".to_string(),
        ScreenKind::Split => "↑↓ action · Enter run · Esc close".to_string(),
        ScreenKind::Progress => "Esc close".to_string(),
    }
}

/// Whether *value* answers the open screen (used by the reducer's tests).
pub fn answers_to(value: &Value) -> bool {
    !value.is_null()
}
