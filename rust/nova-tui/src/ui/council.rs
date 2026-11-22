//! The council card: one in-place summary of a planning run.
//!
//! The Textual panel animates a phase stepper and every seat. Ratatui draws the
//! same facts as stable lines inside the transcript, so scrolling back still
//! shows the run instead of a transient widget. Motion is deliberately omitted:
//! a terminal that repaints a card mid-scroll is worse than one that does not.

use ratatui::text::{Line, Span};

use crate::state::{AppState, CouncilState};
use crate::theme::Theme;

/// Phase keys and labels, in planner order.
const PHASES: [(&str, &str); 4] = [
    ("brainstorming", "Plan"),
    ("critiquing", "Critique"),
    ("voting", "Vote"),
    ("judging", "Judge"),
];

/// Every line the council card contributes to the transcript.
pub fn lines(state: &AppState, theme: &Theme) -> Vec<Line<'static>> {
    let Some(council) = state.council.as_ref() else {
        return Vec::new();
    };
    let mut out = Vec::new();
    out.push(Line::from(Span::styled(
        format!("╭─ Council · {}", headline(council)),
        if council.outcome == "failed" {
            theme.error()
        } else if council.outcome == "done" {
            theme.success()
        } else {
            theme.accent_border()
        },
    )));
    let prompt = council.prompt.lines().next().unwrap_or("").trim();
    if !prompt.is_empty() {
        out.push(Line::from(Span::styled(
            format!("│ {prompt}"),
            theme.body(),
        )));
    }
    out.push(Line::from(Span::styled(
        format!("│ {}", stepper(council, state.tick)),
        theme.heading(),
    )));
    for agent in &council.agents {
        out.push(agent_line(council, agent, theme, state.tick));
    }
    out.push(Line::from(Span::styled(
        format!(
            "│ {} plan(s) · {} critique(s) · {}/{} vote(s){}",
            council.proposals,
            council.critiques,
            council.ballots,
            council.agents.len(),
            council
                .entropy
                .map(|value| format!(" · disagreement {value:.2}"))
                .unwrap_or_default()
        ),
        theme.muted(),
    )));
    if !council.message.is_empty() {
        out.push(Line::from(Span::styled(
            format!("│ {}", council.message),
            theme.error(),
        )));
    }
    out.push(Line::from(Span::styled("╰─", theme.muted())));
    out
}

/// One frame of the active-phase spinner.
fn spinner(tick: u64) -> &'static str {
    const FRAMES: [&str; 4] = ["◐", "◓", "◑", "◒"];
    FRAMES[(tick / 2) as usize % FRAMES.len()]
}

/// The header's status word.
fn headline(council: &CouncilState) -> String {
    match council.outcome.as_str() {
        "done" => "plans ready".to_string(),
        "failed" => "failed".to_string(),
        _ if council.phase.is_empty() => "starting".to_string(),
        _ => council.phase.replace('_', " "),
    }
}

/// One phase chip per planner phase.
fn stepper(council: &CouncilState, tick: u64) -> String {
    let position = PHASES
        .iter()
        .position(|(key, _)| *key == council.phase)
        .map(|index| index as isize);
    let mut parts = Vec::new();
    for (index, (_, label)) in PHASES.iter().enumerate() {
        let index = index as isize;
        let marker = if council.outcome == "done" {
            "✓"
        } else if council.outcome == "failed" && index == position.unwrap_or(-1) {
            "✖"
        } else if position == Some(index) {
            spinner(tick)
        } else if position.is_some_and(|current| index < current) {
            "✓"
        } else {
            "·"
        };
        parts.push(format!("{marker} {label}"));
    }
    parts.join("  ")
}

/// One seat's status in the current phase.
fn agent_line(
    council: &CouncilState,
    agent: &crate::frames::CouncilAgent,
    theme: &Theme,
    tick: u64,
) -> Line<'static> {
    let failed = council.failed.get(&agent.id);
    let done = council
        .done
        .contains(&(agent.id.clone(), council.phase.clone()));
    let (marker, style) = if failed.is_some() {
        ("✖", theme.error())
    } else if done {
        ("✓", theme.success())
    } else if council.outcome.is_empty() {
        (spinner(tick), theme.muted())
    } else {
        ("·", theme.muted())
    };
    let avatar = if agent.avatar.is_empty() {
        "•".to_string()
    } else {
        agent.avatar.clone()
    };
    let name = if agent.name.is_empty() {
        agent.id.clone()
    } else {
        agent.name.clone()
    };
    Line::from(vec![
        Span::styled(format!("│ {marker} {avatar} "), style),
        Span::styled(name, theme.body()),
    ])
}
