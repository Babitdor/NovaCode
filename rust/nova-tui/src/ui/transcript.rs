//! The transcript: prose, reasoning, tool cards, command output and notices.
//!
//! A collapsed tool card still shows the **last** line of its output. That is a
//! deliberate choice: the useful part of a tool result is almost always its
//! conclusion (a test summary, a count, an error), and the parity harness asserts
//! exactly that fact — ``final line`` is visible without expanding anything.

use ratatui::layout::Rect;
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Wrap};
use ratatui::Frame;

use crate::frames::Block;
use crate::markdown;
use crate::state::{AppState, BlockKind, ToolCard};
use crate::theme::Theme;

/// How many lines of a tool's output a collapsed card shows before the tail.
const COLLAPSED_HEAD_LINES: usize = 2;

/// Build every transcript line for *state*.
pub fn lines(state: &AppState, theme: &Theme) -> Vec<Line<'static>> {
    let mut out: Vec<Line<'static>> = Vec::new();
    for entry in &state.entries {
        match &entry.kind {
            BlockKind::User => {
                out.push(Line::from(Span::styled(
                    format!("❯ {}", entry.text),
                    theme.user(),
                )));
                out.push(Line::from(""));
            }
            BlockKind::Assistant => {
                out.push(Line::from(Span::styled(
                    entry.agent.clone(),
                    theme.speaker(entry.agent_color.as_ref()),
                )));
                out.extend(markdown::render(&entry.text, theme));
                out.push(Line::from(""));
            }
            BlockKind::Reasoning => {
                for line in entry.text.lines() {
                    out.push(Line::from(Span::styled(line.to_string(), theme.muted())));
                }
            }
            BlockKind::Tool(card) => tool_lines(card, theme, state.expand_all, &mut out),
            BlockKind::Command {
                ok,
                message,
                blocks,
            } => {
                command_lines(*ok, message, blocks, theme, &mut out);
            }
            BlockKind::Notice { level, text } => {
                let style = if level == "error" {
                    theme.error()
                } else if level == "warning" {
                    theme.warning()
                } else {
                    theme.muted()
                };
                out.push(Line::from(Span::styled(format!("· {text}"), style)));
            }
            BlockKind::Context { icon } => {
                out.push(Line::from(Span::styled(
                    format!("{icon} {}", entry.text),
                    theme.muted(),
                )));
            }
            BlockKind::Error => {
                out.push(Line::from(Span::styled(
                    format!("✗ {}", entry.text),
                    theme.error(),
                )));
            }
            BlockKind::FileOp {
                operation, path, ..
            } => {
                out.push(Line::from(Span::styled(
                    format!("◆ {operation} {path}"),
                    theme.muted(),
                )));
            }
        }
    }
    out.extend(crate::ui::council::lines(state, theme));
    if !state.streaming.is_empty() {
        let composer = state.status.composer.as_ref();
        let name = composer.map_or("Nova", |mode| mode.agent.as_str());
        out.push(Line::from(Span::styled(
            name.to_owned(),
            theme.speaker(composer.and_then(|mode| mode.color.as_ref())),
        )));
        out.extend(markdown::render(&state.streaming, theme));
    }
    if let Some(activity) = &state.status.activity {
        out.push(Line::from(Span::styled(
            format!("… {activity}"),
            theme.muted(),
        )));
    }
    if out.is_empty() {
        out.push(Line::from(Span::styled(
            "Nova is ready. Type a prompt, or /help.",
            theme.muted(),
        )));
    }
    out
}

fn tool_lines(card: &ToolCard, theme: &Theme, expand_all: bool, out: &mut Vec<Line<'static>>) {
    let status_style = if card.is_error {
        theme.error()
    } else if card.finished {
        theme.success()
    } else {
        theme.muted()
    };
    let icon = if card.icon.is_empty() {
        "⚙"
    } else {
        &card.icon
    };
    let label = if card.display.is_empty() {
        card.name.clone()
    } else {
        card.display.clone()
    };
    let marker = if card.finished { "✓" } else { "…" };
    out.push(Line::from(vec![
        Span::styled(format!("{marker} {icon} "), status_style),
        Span::styled(label, theme.body()),
    ]));
    if !card.preview.is_empty() {
        out.push(Line::from(Span::styled(
            format!("   {}", card.preview),
            theme.muted(),
        )));
    }
    let full_lines: Vec<&str> = card
        .full_output
        .lines()
        .filter(|line| !line.trim().is_empty())
        .collect();
    let expanded = card.expanded || expand_all;
    if expanded {
        for line in &full_lines {
            out.push(Line::from(Span::styled(format!("   {line}"), theme.code())));
        }
    } else if full_lines.len() > COLLAPSED_HEAD_LINES {
        for line in full_lines.iter().take(COLLAPSED_HEAD_LINES) {
            out.push(Line::from(Span::styled(format!("   {line}"), theme.code())));
        }
        out.push(Line::from(Span::styled("   …", theme.muted())));
        if let Some(last) = full_lines.last() {
            out.push(Line::from(Span::styled(format!("   {last}"), theme.code())));
        }
        out.push(Line::from(Span::styled(
            "   [ctrl+o to expand]",
            theme.muted(),
        )));
    } else {
        for line in &full_lines {
            out.push(Line::from(Span::styled(format!("   {line}"), theme.code())));
        }
    }
}

fn command_lines(
    ok: bool,
    message: &str,
    blocks: &[Block],
    theme: &Theme,
    out: &mut Vec<Line<'static>>,
) {
    let style = if ok { theme.body() } else { theme.error() };
    if !message.is_empty() {
        out.push(Line::from(Span::styled(format!("▸ {message}"), style)));
    }
    for block in blocks {
        match block.kind.as_str() {
            "code" => {
                for line in block.text.lines() {
                    out.push(Line::from(Span::styled(line.to_string(), theme.code())));
                }
            }
            "heading" => out.push(Line::from(Span::styled(
                block.text.clone(),
                theme.heading(),
            ))),
            _ => {
                for line in block.text.lines() {
                    out.push(Line::from(Span::styled(line.to_string(), style)));
                }
            }
        }
    }
}

/// The window of *lines* currently visible.
///
/// `scroll` counts lines back from the end, so `0` follows the tail — the
/// behaviour a chat needs, and the reason the last line of a tool result is
/// visible without scrolling. Wrapping means this is an approximation of the
/// rendered height; it is exact when the terminal is wider than the content.
pub fn window(lines: Vec<Line<'static>>, height: usize, scroll: usize) -> Vec<Line<'static>> {
    if height == 0 || lines.len() <= height {
        return lines;
    }
    let max_scroll = lines.len() - height;
    let end = lines.len() - scroll.min(max_scroll);
    let start = end - height;
    lines[start..end].to_vec()
}

/// Draw the transcript into *area*, following the tail unless scrolled back.
pub fn draw(frame: &mut Frame, area: Rect, state: &AppState, theme: &Theme) {
    let window = window(
        lines(state, theme),
        area.height as usize,
        state.scroll as usize,
    );
    let paragraph = Paragraph::new(window).wrap(Wrap { trim: false });
    frame.render_widget(paragraph, area);
}
