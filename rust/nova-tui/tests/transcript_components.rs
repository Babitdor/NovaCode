//! Render the updated transcript through the same entry point as the terminal.

use nova_tui::frames;
use nova_tui::markdown;
use nova_tui::state::{apply, AppState, BlockKind};
use nova_tui::theme::Theme;
use nova_tui::ui;
use ratatui::backend::TestBackend;
use ratatui::style::Modifier;
use ratatui::Terminal;

fn paint(state: &mut AppState, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| ui::draw(frame, state, &Theme::default()))
        .unwrap();
    let buffer = terminal.backend().buffer();
    let mut text = String::new();
    for y in 0..height {
        for x in 0..width {
            text.push_str(buffer[(x, y)].symbol());
        }
        text.push('\n');
    }
    text
}

#[test]
fn markdown_keeps_nested_emphasis_tables_and_literal_code() {
    let source = "**first _nested_ bold**\n\n- [x] Done\n\n| Name | Status |\n| --- | ---: |\n| Tests | 21 |\n\n> Evidence\n\n[Guide](https://example.com/guide)\n\n~~~rust\nlet x = 1;\n~~~";
    let lines = markdown::render(source, &Theme::default());
    let spans: Vec<_> = lines.iter().flat_map(|line| &line.spans).collect();
    let nested = spans.iter().find(|span| span.content == "nested").unwrap();
    assert!(nested
        .style
        .add_modifier
        .contains(Modifier::BOLD | Modifier::ITALIC));
    let bold = spans.iter().find(|span| span.content == " bold").unwrap();
    assert_eq!(bold.style.add_modifier, Modifier::BOLD);
    let output: Vec<String> = lines
        .iter()
        .map(|line| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect()
        })
        .collect();
    assert!(output.iter().any(|line| line.contains("☑ Done")));
    assert!(output.iter().any(|line| line.contains("Tests │     21")));
    assert!(output.iter().any(|line| line.contains("│ Evidence")));
    assert!(output
        .iter()
        .any(|line| line.contains("Guide (https://example.com/guide)")));
    assert!(output.iter().any(|line| line.contains("1 │ let x = 1;")));
}

#[test]
fn a_nested_file_edit_renders_under_its_tool_with_clickable_full_diff() {
    let mut state = AppState::new();
    for wire in [
        r#"{"t":"ev","c":"ToolCall","d":{"name":"eval","call_id":"parent","display_str":"Edit project configuration"}}"#,
        r#"{"t":"ev","c":"ToolCall","d":{"name":"shell","call_id":"other","display_str":"Run tests"}}"#,
        r#"{"t":"ev","c":"ToolResult","d":{"call_id":"unmatched","is_error":true,"preview":"Command failed"}}"#,
        r#"{"t":"ev","c":"FileOp","d":{"parent_call_id":"parent","record":{"tool_name":"edit_file","display_path":"src/main.rs","metrics":{"lines_added":1,"lines_removed":1},"diff":"--- a/src/main.rs\n+++ b/src/main.rs\n@@ -12,2 +20,2 @@\n context\n-old code\n+new code"}}}"#,
    ] {
        apply(&mut state, &frames::decode(wire).unwrap());
    }
    let compact = paint(&mut state, 94, 28);
    assert!(compact.contains("src/main.rs  +1 −1"));
    assert!(compact.contains("13      - old code"));
    assert!(compact.contains("21 + new code"));
    assert!(!compact.contains("@@ -12,2"));
    let next_call = compact.find("Run tests").unwrap();
    assert!(compact.find("src/main.rs").unwrap() < next_call);
    let BlockKind::Tool(other) = &state.entries[1].kind else {
        panic!("the parallel tool remains a tool card")
    };
    assert!(
        !other.finished,
        "a result with a different ID cannot close it"
    );
    let (header, _) = state
        .transcript_tools
        .iter()
        .find(|(_, index)| *index == 0)
        .copied()
        .unwrap();
    ui::transcript::toggle_tool_at(&mut state, header);
    let expanded = paint(&mut state, 94, 28);
    assert!(expanded.contains("@@ -12,2 +20,2 @@"));
    assert!(expanded.contains("12   20   context"));
}

#[test]
fn a_narrow_transcript_follows_the_end_of_wrapped_text() {
    let mut state = AppState::new();
    state.push(
        BlockKind::Assistant,
        format!("FIRST_MARKER {} LAST_MARKER", "long paragraph ".repeat(100)),
        "Nova",
    );
    let followed = paint(&mut state, 32, 14);
    assert!(followed.contains("LAST_MARKER"));
    assert!(!followed.contains("FIRST_MARKER"));
    state.scroll = u16::MAX;
    assert!(paint(&mut state, 32, 14).contains("FIRST_MARKER"));
}

#[test]
fn approval_controls_stay_visible_while_long_details_scroll() {
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};

    let mut state = AppState::new();
    let arguments = format!("{} APPROVAL_END", "long argument ".repeat(120));
    let wire = serde_json::json!({
        "t": "interrupt",
        "id": "approval",
        "kind": "tool",
        "payload": {"action_requests": [{"name": "shell", "args": {"command": arguments}}]},
    });
    apply(&mut state, &frames::decode(&wire.to_string()).unwrap());
    let before = paint(&mut state, 86, 24);
    assert!(before.contains("y approve · n reject"));
    assert!(!before.contains("APPROVAL_END"));
    for _ in 0..12 {
        ui::modal::handle_key(
            &mut state,
            &KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE),
        );
    }
    let after = paint(&mut state, 86, 24);
    assert!(after.contains("APPROVAL_END"));
    assert!(after.contains("y approve · n reject"));
}

#[test]
fn pending_tools_animate_and_completed_tools_stay_still() {
    let mut state = AppState::new();
    let call = r#"{"t":"ev","c":"ToolCall","d":{"name":"shell","call_id":"one"}}"#;
    apply(&mut state, &frames::decode(call).unwrap());
    let first = paint(&mut state, 80, 18);
    state.tick = 2;
    let next = paint(&mut state, 80, 18);
    assert!(first.contains("⠋ shell") && next.contains("⠙ shell"));
    let finish = r#"{"t":"ev","c":"ToolResult","d":{"call_id":"one","preview":"done"}}"#;
    apply(&mut state, &frames::decode(finish).unwrap());
    assert!(paint(&mut state, 80, 18).contains("✓ shell"));
}
