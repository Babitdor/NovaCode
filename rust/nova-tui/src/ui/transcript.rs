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
use crate::state::{AppState, BlockKind};
use crate::theme::Theme;
use crate::ui::{diff, tool_card};

/// Build every transcript line for the replay and semantic checks.
pub fn lines(state: &AppState, theme: &Theme) -> Vec<Line<'static>> {
    let mut lines = (0..state.entries.len())
        .flat_map(|index| entry_lines(state, theme, index).0)
        .collect::<Vec<_>>();
    lines.extend(tail_lines(state, theme));
    lines
}

fn entry_lines(
    state: &AppState,
    theme: &Theme,
    index: usize,
) -> (Vec<Line<'static>>, Option<usize>) {
    let entry = &state.entries[index];
    let previous_is_tool = index > 0 && matches!(state.entries[index - 1].kind, BlockKind::Tool(_));
    let is_tool = matches!(entry.kind, BlockKind::Tool(_));
    let mut out = Vec::new();
    let mut header = None;
    if previous_is_tool && !is_tool {
        out.push(Line::default());
    }
    match &entry.kind {
        BlockKind::User => {
            for (index, line) in entry.text.split('\n').enumerate() {
                out.push(
                    Line::from(vec![
                        Span::styled(if index == 0 { " ❯ " } else { "   " }, theme.user()),
                        Span::styled(line.to_owned(), theme.body()),
                    ])
                    .style(theme.body().bg(theme.surface)),
                );
            }
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
        BlockKind::Tool(card) => {
            if !previous_is_tool {
                out.push(Line::from(Span::styled("Tools", theme.heading())));
            }
            header = Some(out.len());
            tool_card::lines(
                card,
                theme,
                state.expand_all,
                state.turn_running,
                state.tick,
                &mut out,
            );
        }
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
        BlockKind::FileOp(change) => diff::file(change, state.expand_all, theme, &mut out),
    }
    (out, header)
}

fn tail_lines(state: &AppState, theme: &Theme) -> Vec<Line<'static>> {
    let mut out = crate::ui::council::lines(state, theme);
    if !state.streaming.is_empty() {
        if state
            .entries
            .last()
            .is_some_and(|entry| matches!(entry.kind, BlockKind::Tool(_)))
        {
            out.push(Line::default());
        }
        let composer = state.status.composer.as_ref();
        let name = composer.map_or("Nova", |mode| mode.agent.as_str());
        out.push(Line::from(Span::styled(
            name.to_owned(),
            theme.speaker(composer.and_then(|mode| mode.color.as_ref())),
        )));
        out.extend(markdown::render(&state.streaming, theme));
    }
    if out.is_empty() && state.entries.is_empty() {
        out.push(Line::from(Span::styled(
            "Nova is ready. Type a prompt, or /help.",
            theme.muted(),
        )));
    }
    out
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
            "diff" => out.extend(diff::lines(&block.text, true, theme)),
            "markdown" => out.extend(markdown::render(&block.text, theme)),
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

fn screen_rows(lines: &[Line<'static>], width: u16) -> usize {
    lines
        .iter()
        .map(|line| {
            if line.width() > usize::from(width) {
                Paragraph::new(line.clone())
                    .wrap(Wrap { trim: false })
                    .line_count(width)
            } else {
                1
            }
        })
        .sum()
}

/// Draw visible transcript blocks without reparsing older Markdown at idle.
pub fn draw(frame: &mut Frame, area: Rect, state: &mut AppState, theme: &Theme) {
    state.transcript_tools.clear();
    if area.is_empty() {
        return;
    }
    let tail = tail_lines(state, theme);
    let visible_tail = usize::from(area.height) + usize::from(state.scroll);
    let mut rows = screen_rows(&tail, area.width);
    let mut blocks = Vec::new();
    for index in (0..state.entries.len()).rev() {
        if rows >= visible_tail {
            break;
        }
        let (entry, tool) = entry_lines(state, theme, index);
        let height = screen_rows(&entry, area.width);
        rows += height;
        let tool = tool.map(|header| screen_rows(&entry[..header], area.width));
        blocks.push((index, entry, tool, height));
    }

    let offset = rows.saturating_sub(visible_tail);
    let mut position = 0;
    let mut transcript = Vec::new();
    for (index, entry, tool, height) in blocks.into_iter().rev() {
        if let Some(header) = tool {
            if let Some(row) = (position + header).checked_sub(offset) {
                if row < usize::from(area.height) {
                    state.transcript_tools.push((area.y + row as u16, index));
                }
            }
        }
        position += height;
        transcript.extend(entry);
    }
    transcript.extend(tail);
    let paragraph = Paragraph::new(transcript)
        .wrap(Wrap { trim: false })
        .scroll((offset.min(u16::MAX as usize) as u16, 0));
    frame.render_widget(paragraph, area);
}

pub fn toggle_tool_at(state: &mut AppState, row: u16) {
    if let Some((_, index)) = state.transcript_tools.iter().find(|(y, _)| *y == row) {
        if let Some(entry) = state.entries.get_mut(*index) {
            if let BlockKind::Tool(card) = &mut entry.kind {
                card.expanded = Some(!card.expanded.unwrap_or(state.expand_all));
            }
        }
    }
}

pub fn toggle_all_tools(state: &mut AppState) {
    state.expand_all = !state.expand_all;
    for entry in &mut state.entries {
        if let BlockKind::Tool(card) = &mut entry.kind {
            card.expanded = None;
        }
    }
}
