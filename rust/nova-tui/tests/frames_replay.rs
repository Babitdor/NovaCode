//! Replay the recorded session and compare rendered frames.
//!
//! The corpus is `tests/fixtures/core_chat.jsonl`, produced by
//! `scripts/record_tui_session.py` from the production server path. The test
//! replays it through the same reducer the binary uses, renders at fixed
//! checkpoints with `TestBackend(100, 35)`, and compares against
//! `tests/fixtures/core_chat.frames.txt`.
//!
//! Regenerate after an intentional layout change:
//!
//! ```text
//! BLESS=1 cargo test -p nova-tui --test frames_replay
//! ```
//!
//! The targeted assertions below exist so a blessed snapshot cannot hide a
//! semantic regression: they check the facts the Textual suite checks
//! (`tests/test_tui_replay_layouts.py`) — message order and text, the tool
//! output's final line, a modal that occupies the screen, the composer value.

use std::path::PathBuf;

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use nova_tui::frames::{self, Inbound};
use nova_tui::state::{apply, AppState, BlockKind, Effect, Modal, ScreenKind};
use nova_tui::theme::Theme;
use nova_tui::ui;
use ratatui::backend::TestBackend;
use ratatui::Terminal;

const WIDTH: u16 = 100;
const HEIGHT: u16 = 35;

fn fixture() -> Vec<Inbound> {
    include_str!("fixtures/core_chat.jsonl")
        .lines()
        .filter_map(frames::decode)
        .collect()
}

fn render(state: &AppState) -> String {
    let theme = Theme::default();
    let backend = TestBackend::new(WIDTH, HEIGHT);
    let mut terminal = Terminal::new(backend).expect("terminal");
    terminal
        .draw(|frame| ui::draw(frame, state, &theme))
        .expect("draw");
    let buffer = terminal.backend().buffer().clone();
    let mut out = String::new();
    for y in 0..HEIGHT {
        for x in 0..WIDTH {
            out.push_str(buffer[(x, y)].symbol());
        }
        out.push('\n');
    }
    out
}

/// The replay report: one rendered frame per checkpoint, labelled.
fn report() -> String {
    let mut state = AppState::new();
    let mut out = String::new();
    let mut saw_assistant = false;
    let mut saw_tool = false;
    for frame in fixture() {
        let effects = apply(&mut state, &frame);
        assert!(
            !effects.contains(&Effect::Quit),
            "the recording must not quit the client"
        );
        match &frame {
            Inbound::Event(nova_tui::frames::UiEvent::AssistantMessage { .. })
                if !saw_assistant =>
            {
                saw_assistant = true;
                out.push_str("### assistant\n");
                out.push_str(&render(&state));
            }
            Inbound::Event(nova_tui::frames::UiEvent::ToolResult { .. }) if !saw_tool => {
                saw_tool = true;
                out.push_str("### tool\n");
                out.push_str(&render(&state));
            }
            Inbound::Interrupt { .. } => {
                out.push_str("### modal\n");
                out.push_str(&render(&state));
            }
            _ => {}
        }
    }
    out.push_str("### final\n");
    out.push_str(&render(&state));
    out
}

fn snapshot_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/core_chat.frames.txt")
}

#[test]
fn replay_matches_the_committed_frames() {
    let report = report();
    if std::env::var("BLESS").is_ok() {
        std::fs::write(snapshot_path(), &report).expect("write snapshot");
        return;
    }
    let committed = std::fs::read_to_string(snapshot_path()).expect("committed frames");
    assert_eq!(
        report, committed,
        "rendered frames differ from tests/fixtures/core_chat.frames.txt — \
         re-run with BLESS=1 if the change is intentional"
    );
}

#[test]
fn replay_preserves_message_order_and_text() {
    let mut state = AppState::new();
    for frame in fixture() {
        apply(&mut state, &frame);
    }
    let prose = state.prose();
    assert!(
        prose.iter().any(|text| text.starts_with("# Plan")),
        "the assistant markdown must be committed as one message: {prose:?}"
    );
    assert!(
        prose
            .iter()
            .any(|text| text.contains("All green: 28 passed")),
        "the closing message must arrive after the approval: {prose:?}"
    );
    let plan_index = prose.iter().position(|text| text.starts_with("# Plan"));
    let closing_index = prose.iter().position(|text| text.contains("All green"));
    assert!(plan_index < closing_index, "messages must stay in order");
}

#[test]
fn tool_card_keeps_the_final_line_of_its_output() {
    let mut state = AppState::new();
    for frame in fixture() {
        apply(&mut state, &frame);
    }
    let card = state
        .entries
        .iter()
        .find_map(|entry| match &entry.kind {
            BlockKind::Tool(card) => Some(card.clone()),
            _ => None,
        })
        .expect("a tool card");
    assert!(card.finished, "the tool result must close its card");
    assert!(!card.is_error);
    assert_eq!(card.display, "shell: pytest -q");
    assert!(card.full_output.contains("final line: 28 passed"));
    assert!(
        render_at_tool_result().contains("final line: 28 passed"),
        "the final line of a tool result must be visible while collapsed"
    );
}

/// The frame drawn immediately after the tool result, where its card is on screen.
fn render_at_tool_result() -> String {
    let mut state = AppState::new();
    for frame in fixture() {
        apply(&mut state, &frame);
        if matches!(
            frame,
            Inbound::Event(nova_tui::frames::UiEvent::ToolResult { .. })
        ) {
            return render(&state);
        }
    }
    panic!("the fixture must contain a tool result");
}

#[test]
fn interrupt_opens_a_modal_that_occupies_the_screen() {
    let mut state = AppState::new();
    let mut modal_frame = String::new();
    for frame in fixture() {
        apply(&mut state, &frame);
        if matches!(frame, Inbound::Interrupt { .. }) {
            modal_frame = render(&state);
        }
    }
    match state.modal {
        Some(Modal::Approval { .. }) => {}
        None => panic!("the interrupt must stay open until it is answered"),
    }
    assert!(modal_frame.contains("Approve tool call?"));
    assert!(
        modal_frame.contains("y approve"),
        "the modal must say how to answer"
    );
    assert!(modal_frame.contains("pytest -q in the project root"));
}

#[test]
fn status_bar_and_composer_render_live_values() {
    let mut state = AppState::new();
    for frame in fixture() {
        apply(&mut state, &frame);
    }
    assert_eq!(state.status.model_label(), "recording-model");
    assert_eq!(state.status.cwd_label(), "nova");
    assert!(
        !state.turn_running,
        "turn_done must clear the working state"
    );

    state.composer = "/status".to_string();
    let frame = render(&state);
    assert!(
        frame.contains("/status"),
        "the composer value must be drawn"
    );
    assert!(
        frame.contains("recording-model"),
        "the status bar must name the model"
    );
    assert!(
        frame.contains("todos 1/2"),
        "the todo count must reach the status bar"
    );
}

#[test]
fn command_output_is_shown_as_a_result_entry() {
    let mut state = AppState::new();
    for frame in fixture() {
        apply(&mut state, &frame);
    }
    let command = state
        .entries
        .iter()
        .find_map(|entry| match &entry.kind {
            BlockKind::Command {
                ok,
                message,
                blocks,
            } => Some((*ok, message.clone(), blocks.clone())),
            _ => None,
        })
        .expect("the recorded /status result");
    assert!(command.0);
    assert_eq!(command.1, "Status");
    assert!(command.2[0].text.contains("Session   recorded-session"));
}

#[test]
fn scrolling_offsets_a_taller_transcript() {
    let mut state = AppState::new();
    for frame in fixture() {
        apply(&mut state, &frame);
    }
    let height = ui::transcript::lines(&state, &Theme::default()).len();
    assert!(height > 0);
    // A transcript taller than the viewport must actually move when scrolled.
    for index in 0..80 {
        state.push(BlockKind::Assistant, format!("line {index}"), "Nova");
    }
    let tail = render(&state);
    assert!(
        tail.contains("line 79"),
        "scroll 0 must follow the tail, so the newest line is visible"
    );
    state.scroll = 10;
    let scrolled = render(&state);
    assert_ne!(
        tail, scrolled,
        "the scroll offset must change the transcript view"
    );
    assert!(
        !scrolled.contains("line 79"),
        "scrolling back must hide the newest line"
    );
    assert_eq!(
        ui::transcript::window(vec![ratatui::text::Line::from("a")], 35, 0).len(),
        1,
        "a short transcript is never truncated"
    );
}

#[test]
fn unknown_frames_and_events_are_skipped() {
    let unknown_frame = frames::decode(r#"{"t":"from-the-future","x":1}"#);
    assert_eq!(
        unknown_frame,
        Some(Inbound::Unknown {
            kind: "from-the-future".into()
        })
    );
    let unknown_event = frames::decode(r#"{"t":"ev","c":"BrandNewEvent","d":{"x":1}}"#);
    match unknown_event {
        Some(Inbound::Event(nova_tui::frames::UiEvent::Other(name))) => {
            assert_eq!(name, "BrandNewEvent");
        }
        other => panic!("expected an Other event, got {other:?}"),
    }
    // Applying them must not disturb the transcript's meaning.
    let mut state = AppState::new();
    apply(&mut state, &Inbound::Unknown { kind: "x".into() });
    apply(
        &mut state,
        &Inbound::Event(nova_tui::frames::UiEvent::Other("y".into())),
    );
    assert!(state.prose().is_empty());
}

/// The recorded command corpus, produced by
/// `uv run python scripts/record_tui_session.py --scenario commands`.
///
/// It holds the read-only commands the server answers today, so the command
/// rendering (heading / text / code blocks) is replayed against real output.
const COMMAND_FIXTURE: &str = include_str!("fixtures/commands.jsonl");

#[test]
fn the_command_corpus_replays_as_successful_results() {
    let mut state = AppState::new();
    let mut results = 0;
    for line in COMMAND_FIXTURE.lines() {
        let Some(frame) = frames::decode(line) else {
            continue;
        };
        apply(&mut state, &frame);
        if let Inbound::CommandResult { ok, blocks, .. } = &frame {
            assert!(*ok, "the corpus only records commands the server answered");
            assert!(
                !blocks.is_empty(),
                "a served command must return its output as blocks"
            );
            results += 1;
        }
    }
    assert!(results >= 13, "the corpus lost commands: {results}");
    assert!(
        state
            .entries
            .iter()
            .any(|entry| matches!(entry.kind, BlockKind::Command { .. })),
        "command results must be their own transcript entries"
    );
}

/// The recorded `/theme nova-signal` session: palette frame, then the result.
fn theme_fixture() -> Vec<Inbound> {
    include_str!("fixtures/theme.jsonl")
        .lines()
        .filter_map(frames::decode)
        .collect()
}

#[test]
fn the_recorded_theme_repaints_the_client() {
    let mut state = AppState::new();
    let mut palette = None;
    let mut announced = None;
    for frame in theme_fixture() {
        apply(&mut state, &frame);
        if let Some(waiting) = state.pending_theme.take() {
            palette = Some(waiting);
        }
        if let Inbound::CommandResult {
            message,
            action,
            ok,
            ..
        } = &frame
        {
            announced = Some((message.clone(), action.clone(), *ok));
        }
    }

    let palette = palette.expect("the corpus carries a ui_theme frame");
    assert_eq!(palette.name, "nova-signal");
    assert!(palette.dark);
    assert_eq!(palette.primary.as_deref(), Some("#f4723f"));
    assert_eq!(palette.muted.as_deref(), Some("#afa6a0"));

    let theme = Theme::from_palette(&palette, Theme::default());
    assert_eq!(
        theme.accent,
        ratatui::style::Color::Rgb(0xf4, 0x72, 0x3f),
        "the accent comes from the frame, not the baked default"
    );
    assert_eq!(theme.dim, ratatui::style::Color::Rgb(0xaf, 0xa6, 0xa0));

    let (message, action, ok) = announced.expect("the corpus carries the result");
    assert!(ok);
    assert_eq!(action.as_deref(), Some("theme"));
    assert_eq!(message, "Theme: nova-signal");
    // The transcript keeps the human line; the frame only changed the colours.
    let text = render(&state);
    assert!(text.contains("Theme set to nova-signal"), "got {text:.0}");
}

#[test]
fn a_recorded_command_can_open_a_screen() {
    // `/context` answers with blocks *and* a table spec: reading a report needs
    // no reply, so the client opens it as an informational modal.
    let mut state = AppState::new();
    let mut opened = false;
    for line in COMMAND_FIXTURE.lines() {
        let Some(frame) = frames::decode(line) else {
            continue;
        };
        apply(&mut state, &frame);
        if let Inbound::CommandResult {
            screen: Some(_), ..
        } = &frame
        {
            let view = state.screen.as_ref().expect("the screen opens");
            assert_eq!(view.kind, ScreenKind::Table);
            assert_eq!(view.title, "Context window");
            assert!(view.rows.len() >= 5, "one row per context category");
            assert_eq!(view.rows[0].cells[0], "System + memory");
            assert!(
                view.id.is_empty(),
                "an informational screen answers nothing"
            );
            opened = true;
        }
    }
    assert!(
        opened,
        "the corpus must record a command that opens a screen"
    );
    // Closing it must not send an answer: nothing is waiting on one.
    assert!(nova_tui::ui::screen::handle_key(
        &mut state,
        &KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE)
    )
    .is_none());
    assert!(state.screen.is_none());
}

#[test]
fn command_output_becomes_transcript_lines() {
    // The transcript window follows the tail, so a long help listing shows its
    // end, not its start; assert on the rendered lines rather than the window.
    let mut state = AppState::new();
    for line in COMMAND_FIXTURE.lines() {
        let Some(frame) = frames::decode(line) else {
            continue;
        };
        apply(&mut state, &frame);
        if matches!(frame, Inbound::CommandResult { .. }) {
            let theme = Theme::default();
            let painted: Vec<String> = nova_tui::ui::transcript::lines(&state, &theme)
                .iter()
                .map(|line| {
                    line.spans
                        .iter()
                        .map(|span| span.content.to_string())
                        .collect::<String>()
                })
                .collect();
            assert!(
                painted
                    .iter()
                    .any(|line| line.starts_with("Interactive Commands")),
                "the help output must become transcript lines: {painted:?}"
            );
            // ...and it must still reach a real buffer.
            let rendered = render(&state);
            assert!(!rendered.trim().is_empty(), "the frame must draw");
            return;
        }
    }
    panic!("the corpus records at least one command result");
}
#[test]
fn clear_and_quit_actions_are_explicit_effects() {
    let mut state = AppState::new();
    state.push(BlockKind::Assistant, "hello", "Nova");
    let clear = Inbound::CommandResult {
        id: "c1".into(),
        ok: true,
        message: "Transcript cleared.".into(),
        blocks: vec![],
        action: Some("clear".into()),
        prompt: None,
        screen: None,
        attach: None,
        create: None,
        resume: None,
        close: None,
    };
    let effects = apply(&mut state, &clear);
    assert!(effects.contains(&Effect::ClearTranscript));
    assert!(
        state.entries.is_empty(),
        "a clear must empty the transcript"
    );

    let quit = Inbound::CommandResult {
        id: "c2".into(),
        ok: true,
        message: "Closing session.".into(),
        blocks: vec![],
        action: Some("quit".into()),
        prompt: None,
        screen: None,
        attach: None,
        create: None,
        resume: None,
        close: None,
    };
    let effects = apply(&mut state, &quit);
    assert!(effects.contains(&Effect::Quit));
    assert!(state.quit);
}

#[test]
fn a_clear_that_carries_blocks_rewrites_the_transcript() {
    // `/rewind` empties the transcript and replays the kept messages in one
    // result, so those blocks must land in the transcript the clear just emptied.
    let mut state = AppState::new();
    state.push(BlockKind::Assistant, "a turn that will be undone", "Nova");
    let rewind = Inbound::CommandResult {
        id: "c1".into(),
        ok: true,
        message: "⟲ Rewound to before “add a parser”.".into(),
        blocks: vec![
            nova_tui::frames::Block {
                kind: "heading".into(),
                text: "you".into(),
            },
            nova_tui::frames::Block {
                kind: "text".into(),
                text: "add a parser".into(),
            },
        ],
        action: Some("clear".into()),
        prompt: None,
        screen: None,
        attach: None,
        create: None,
        resume: None,
        close: None,
    };
    let effects = apply(&mut state, &rewind);
    assert!(effects.contains(&Effect::ClearTranscript));
    let painted = render(&state);
    assert!(
        painted.contains("add a parser"),
        "the replayed conversation must survive the clear: {painted:?}"
    );
    assert!(
        !painted.contains("a turn that will be undone"),
        "the rewound turn must be gone: {painted:?}"
    );
}

/// The recorded tab switch, produced by
/// `uv run python scripts/record_tui_session.py --scenario tabs`.
fn tabs_fixture() -> Vec<Inbound> {
    include_str!("fixtures/tabs.jsonl")
        .lines()
        .filter_map(frames::decode)
        .collect()
}

#[test]
fn the_recorded_tab_switch_keeps_each_transcript() {
    let mut state = AppState::new();
    let mut beta = None;
    for frame in tabs_fixture() {
        apply(&mut state, &frame);
        if state.active_session.as_deref() == Some("session-beta") {
            beta = Some(state.prose());
        }
    }
    assert_eq!(
        beta.as_deref(),
        Some(&["Beta one".to_string()][..]),
        "the beta tab paints only its own turn"
    );
    assert_eq!(state.active_session.as_deref(), Some("session-alpha"));
    assert_eq!(
        state.prose(),
        vec!["Alpha one".to_string(), "Alpha two".to_string()],
        "returning to alpha restores its transcript and appends the new turn"
    );
    assert_eq!(state.sessions.len(), 2);
    assert_eq!(state.status.cwd.as_deref(), Some("C:/work/alpha"));
}

/// The recorded council scenario, produced by
/// `uv run python scripts/record_tui_session.py --scenario council`.
fn council_fixture() -> Vec<Inbound> {
    include_str!("fixtures/council.jsonl")
        .lines()
        .filter_map(frames::decode)
        .collect()
}

#[test]
fn the_recorded_council_folds_into_a_card_and_actions() {
    let mut state = AppState::new();
    for frame in council_fixture() {
        apply(&mut state, &frame);
    }
    let council = state.council.as_ref().expect("the card exists");
    assert_eq!(council.outcome, "done");
    assert_eq!(council.proposals, 2);
    assert_eq!(council.ballots, 2);
    assert_eq!(council.agents.len(), 2);
    assert!(council
        .done
        .contains(&("architect".to_string(), "brainstorming".to_string())));

    let screen = state.screen.as_ref().expect("plans open as a screen");
    assert_eq!(screen.kind, ScreenKind::Split);
    let ids: Vec<&str> = screen
        .actions
        .iter()
        .map(|action| action.id.as_str())
        .collect();
    assert_eq!(ids, ["approve:1", "approve:2", "reject", "close"]);
    assert!(screen.body_right.contains("Exponential backoff"));
    assert!(screen.body_left.contains("Exponential backoff"));
}
