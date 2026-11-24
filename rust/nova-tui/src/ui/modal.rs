//! Modals: the three human-in-the-loop interrupts, plus the key hints that make
//! them answerable without documentation.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, Paragraph, Wrap};
use ratatui::Frame;
use serde_json::json;

use crate::frames;
use crate::state::{AppState, Effect, Modal};
use crate::theme::Theme;

/// A rectangle *percent_x* by *percent_y* of *area*, centred.
pub fn centered(area: Rect, percent_x: u16, percent_y: u16) -> Rect {
    let width = area.width.saturating_mul(percent_x) / 100;
    let height = area.height.saturating_mul(percent_y) / 100;
    Rect {
        x: area.x + (area.width.saturating_sub(width)) / 2,
        y: area.y + (area.height.saturating_sub(height)) / 2,
        width: width.max(20),
        height: height.max(7),
    }
}

/// Apply one decision or edit to the active interrupt.
pub fn handle_key(state: &mut AppState, key: &KeyEvent) -> Option<Effect> {
    let (id, result) = match state.modal.as_mut()? {
        Modal::Question {
            id,
            options,
            selected,
            input,
            ..
        } => match key.code {
            KeyCode::Up => {
                *selected = selected.saturating_sub(1);
                return None;
            }
            KeyCode::Down => {
                *selected = (*selected + 1).min(options.len());
                return None;
            }
            KeyCode::Char(ch)
                if !key
                    .modifiers
                    .intersects(KeyModifiers::ALT | KeyModifiers::CONTROL) =>
            {
                *selected = options.len();
                input.push(ch);
                return None;
            }
            KeyCode::Backspace if *selected == options.len() => {
                input.pop();
                return None;
            }
            KeyCode::Enter => {
                let answer = options.get(*selected).map_or(input.trim(), String::as_str);
                if answer.is_empty() {
                    return None;
                }
                (id.clone(), json!({"answer": answer}))
            }
            KeyCode::Esc => (id.clone(), json!({})),
            _ => return None,
        },
        Modal::Approval {
            id,
            kind,
            detail,
            scroll,
            decisions,
            ..
        } => {
            if matches!(key.code, KeyCode::Up | KeyCode::PageUp) {
                *scroll = scroll.saturating_sub(if key.code == KeyCode::PageUp { 8 } else { 1 });
                return None;
            }
            if matches!(key.code, KeyCode::Down | KeyCode::PageDown) {
                *scroll = (*scroll + if key.code == KeyCode::PageDown { 8 } else { 1 })
                    .min(detail.len().saturating_sub(1));
                return None;
            }
            let approve = matches!(key.code, KeyCode::Char('y' | 'Y'));
            let reject = matches!(key.code, KeyCode::Char('n' | 'N') | KeyCode::Esc);
            let result = match kind.as_str() {
                "oracle" => json!({"action": match key.code {
                    KeyCode::Char('y' | 'Y') => "accept",
                    KeyCode::Char('n' | 'N') | KeyCode::Esc => "pause",
                    KeyCode::Char('f' | 'F') => "fix",
                    KeyCode::Char('e' | 'E') => "extend",
                    _ => return None,
                }}),
                "plan" if approve || reject => json!({
                    "response": {"approved": approve, "action": if approve {"approve"} else {"reject"}, "feedback": ""},
                    "state_update": if approve { json!({"plan_mode_enabled": false}) } else { json!({}) },
                }),
                "tool" if approve || reject => {
                    if key.code == KeyCode::Esc {
                        decisions.resize(detail.len(), "reject");
                    } else if decisions.len() < detail.len() {
                        decisions.push(if approve { "approve" } else { "reject" });
                    }
                    if decisions.len() < detail.len() {
                        *scroll = 0;
                        return None;
                    }
                    json!({
                        "decisions": decisions.iter().map(|decision| json!({"type": decision})).collect::<Vec<_>>(),
                        "any_rejected": decisions.contains(&"reject") || decisions.is_empty(),
                    })
                }
                _ if key.code == KeyCode::Esc => json!({}),
                _ => return None,
            };
            (id.clone(), result)
        }
    };
    state.modal = None;
    Some(Effect::Send(frames::interrupt_reply(&id, result)))
}

/// Draw the active modal, if any, over the whole screen area.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    let Some(modal) = &state.modal else {
        return;
    };
    let popup = centered(area, 76, 60);
    frame.render_widget(Clear, popup);
    frame
        .buffer_mut()
        .set_style(popup, theme.body().bg(theme.surface));
    let mut lines: Vec<Line<'static>> = Vec::new();
    match modal {
        Modal::Approval {
            kind,
            headline,
            detail,
            scroll,
            decisions,
            ..
        } => {
            lines.push(Line::from(Span::styled(headline.clone(), theme.heading())));
            lines.push(Line::from(""));
            let page = popup.height.saturating_sub(6).max(1) as usize;
            let details = if kind == "tool" {
                detail
                    .get(decisions.len())
                    .map(std::slice::from_ref)
                    .unwrap_or(&[])
            } else {
                detail.as_slice()
            };
            for line in details
                .iter()
                .flat_map(|line| line.lines())
                .skip(*scroll)
                .take(page)
            {
                lines.push(Line::from(Span::styled(line.to_owned(), theme.body())));
            }
            lines.push(Line::from(""));
            let hints = if kind == "oracle" {
                "y accept · f fix · e extend · n/Esc pause"
            } else if kind == "plan" {
                "y approve · n reject · ↑↓ scroll · Esc reject"
            } else {
                "y approve · n reject · Esc rejects remaining"
            };
            lines.push(Line::from(Span::styled(hints, theme.muted())));
        }
        Modal::Question {
            question,
            context,
            options,
            selected,
            input,
            ..
        } => {
            lines.push(Line::from(Span::styled(question.clone(), theme.heading())));
            if !context.is_empty() {
                lines.push(Line::from(Span::styled(context.clone(), theme.muted())));
            }
            let page = popup.height.saturating_sub(7).max(1) as usize;
            let from = selected.saturating_sub(page.saturating_sub(1));
            let other = format!("Other · {input}");
            for (index, option) in options
                .iter()
                .chain(std::iter::once(&other))
                .enumerate()
                .skip(from)
                .take(page)
            {
                let marker = if index == *selected { "▸ " } else { "  " };
                lines.push(Line::from(Span::styled(
                    format!("{marker}{option}"),
                    if index == *selected {
                        theme.heading()
                    } else {
                        theme.body()
                    },
                )));
            }
            lines.push(Line::from(Span::styled(
                "↑↓ choose · type for Other · Enter send · Esc dismiss",
                theme.muted(),
            )));
        }
    }
    let block = Block::default()
        .borders(Borders::ALL)
        .border_style(theme.accent_border())
        .title(Span::styled(" approval ", theme.muted()));
    frame.render_widget(
        Paragraph::new(lines)
            .block(block)
            .wrap(Wrap { trim: false }),
        popup,
    );
}
