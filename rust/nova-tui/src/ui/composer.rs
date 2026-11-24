//! The multiline prompt and Textual's composer controls.

use ratatui::layout::Rect;
use ratatui::style::{Color, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, BorderType, Borders, Paragraph, Wrap};
use ratatui::Frame;

use crate::state::{AppState, Effect};
use crate::theme::Theme;

const CONTROLS: [(&str, &str, u16); 7] = [
    ("agent", "Agent", 16),
    ("prime", "Prime", 8),
    ("passing", "Passing", 11),
    ("plan", "Plan", 8),
    ("lite", "Lite", 8),
    ("code", "Code Mode", 13),
    ("oracle", "Oracle", 10),
];

fn control_rows(state: &AppState, width: u16) -> u16 {
    if state.status.composer.is_none() {
        return 0;
    }
    match width {
        82.. => 1,
        50..=81 => 2,
        _ => 3,
    }
}

/// Height of the prompt and the rows of controls at this window width.
pub fn height(state: &AppState, width: u16) -> u16 {
    let rows = wrapped_rows(&prompt_lines(state, &Theme::default()), width);
    rows.clamp(1, 8) as u16 + 2 + control_rows(state, width)
}

fn wrapped_rows(lines: &[Line<'_>], width: u16) -> usize {
    let width = usize::from(width.saturating_sub(2)).max(1);
    lines
        .iter()
        .map(|line| line.width().div_ceil(width).max(1))
        .sum()
}

fn controls(area: Rect, rows: u16) -> impl Iterator<Item = (Rect, &'static str, &'static str)> {
    let mut x = area.x + 1;
    let start = x;
    let top = area.bottom().saturating_sub(rows);
    CONTROLS
        .into_iter()
        .enumerate()
        .filter_map(move |(index, (id, label, wide))| {
            if rows == 0 {
                return None;
            }
            let (y, width) = if rows == 1 {
                (top, wide)
            } else {
                let columns = if rows == 2 { 4 } else { 3 };
                let slot = index as u16 + u16::from(rows == 3 && index >= 5);
                let col = slot % columns;
                let space = area.width.saturating_sub(columns + 1);
                let left = space * col / columns;
                let right = space * (col + 1) / columns;
                x = start + left + col;
                (top + slot / columns, right - left)
            };
            let rect =
                Rect::new(x, y, width.min(area.right().saturating_sub(x)), 1).intersection(area);
            x = x.saturating_add(width + 1);
            Some((rect, id, label))
        })
}

/// Resolve a mouse position to the same intent the keyboard sends.
pub fn control_at(state: &AppState, area: Rect, x: u16, y: u16) -> Option<&'static str> {
    controls(area, control_rows(state, area.width).min(area.height))
        .find_map(|(rect, id, _)| (x >= rect.x && x < rect.right() && y == rect.y).then_some(id))
}

/// Route a composer intent through the shared command server.
pub fn activate(state: &mut AppState, control: &str) -> Effect {
    state.composer_control = None;
    Effect::Send(crate::frames::command(
        &state.next_request_id(),
        "ui-composer",
        Some(control),
    ))
}

/// Move focus through the prompt and its controls.
pub fn move_focus(state: &mut AppState, reverse: bool) {
    let current = state.composer_control.map_or(0, |index| index + 1);
    let next = (current + if reverse { CONTROLS.len() } else { 1 }) % (CONTROLS.len() + 1);
    state.composer_control = next.checked_sub(1);
}

/// Return the control selected with the keyboard.
pub fn focused(state: &AppState) -> Option<&'static str> {
    state
        .composer_control
        .and_then(|index| CONTROLS.get(index).map(|item| item.0))
}

/// Draw the composer into *area*.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    frame.render_widget(Block::default().style(theme.body().bg(theme.surface)), area);
    let rows = control_rows(state, area.width).min(area.height);
    let prompt = Rect::new(area.x, area.y, area.width, area.height.saturating_sub(rows));
    let mode = state.status.composer.as_ref();
    let border = if mode.is_some_and(|mode| mode.plan) {
        theme.warning()
    } else if state.turn_running || mode.is_some_and(|mode| mode.prime) {
        theme.accent_border()
    } else {
        theme.border()
    };
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(border);
    let lines = prompt_lines(state, theme);
    let line_count = wrapped_rows(&lines, prompt.width);
    let paragraph = Paragraph::new(lines).wrap(Wrap { trim: false });
    let scroll = line_count.saturating_sub(prompt.height.saturating_sub(2) as usize);
    frame.render_widget(
        paragraph
            .scroll((scroll.min(u16::MAX as usize) as u16, 0))
            .block(block),
        prompt,
    );
    let Some(mode) = mode else { return };
    for (rect, id, label) in controls(area, rows) {
        let active = match id {
            "prime" => mode.prime,
            "passing" => mode.passing,
            "plan" => mode.plan,
            "lite" => mode.lite == "caveman" || mode.lite == "ponytail",
            "code" => mode.code,
            "oracle" => mode.oracle,
            _ => false,
        };
        let (label, style): (String, Style) = if id == "agent" {
            (
                format!("{} ▾", mode.agent),
                theme.speaker(mode.color.as_ref()),
            )
        } else if active {
            let style = if id == "lite" && mode.lite == "caveman" {
                theme.warning()
            } else if id == "lite" && mode.lite == "ponytail" {
                theme.success().add_modifier(ratatui::style::Modifier::BOLD)
            } else {
                theme.heading()
            };
            (label.to_string(), style)
        } else {
            (label.to_string(), theme.muted())
        };
        let highlighted = focused(state) == Some(id) || state.hovered_control == Some(id);
        let tint = if active || id == "agent" || highlighted {
            style.fg.unwrap_or(theme.accent)
        } else {
            theme.text
        };
        let strength = if highlighted {
            32
        } else if active || id == "agent" {
            18
        } else {
            8
        };
        frame.render_widget(
            Paragraph::new(label)
                .style(style.bg(blend(theme.surface, tint, strength)))
                .centered(),
            rect,
        );
    }
}

fn blend(background: Color, tint: Color, weight: u16) -> Color {
    match (background, tint) {
        (Color::Rgb(r, g, b), Color::Rgb(tr, tg, tb)) => {
            let channel = |base, tone| {
                ((u16::from(base) * (100 - weight) + u16::from(tone) * weight) / 100) as u8
            };
            Color::Rgb(channel(r, tr), channel(g, tg), channel(b, tb))
        }
        _ => background,
    }
}

fn prompt_lines(state: &AppState, theme: &Theme) -> Vec<Line<'static>> {
    if state.composer.is_empty() {
        return vec![Line::from(vec![
            Span::styled(" ❯ ", theme.accent_style()),
            Span::styled("Ask Nova anything…", theme.muted()),
        ])];
    }
    let mut lines: Vec<_> = state
        .composer
        .split('\n')
        .enumerate()
        .map(|(i, line)| {
            Line::from(vec![
                Span::styled(if i == 0 { " ❯ " } else { "   " }, theme.accent_style()),
                Span::styled(line.to_owned(), theme.body()),
            ])
        })
        .collect();
    if let Some(last) = lines.last_mut() {
        last.spans.push(Span::styled("▏", theme.accent_style()));
    }
    lines
}
