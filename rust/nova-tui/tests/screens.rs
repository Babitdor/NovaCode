//! The command catalogue, the screen round trip and the @@/@@ palette.
//!
//! These are reducer-level tests: frames in, state and effects out. The point is
//! that every new envelope in @@rust/PROTOCOL.md@@ is decoded, every screen kind
//! answers in the documented shape, and an unknown kind fails closed instead of
//! leaving the server waiting for its @@SCREEN_TIMEOUT@@.

use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::json;

use nova_tui::frames::{self, Inbound};
use nova_tui::state::{apply, AppState, Effect, ScreenKind};
use nova_tui::ui;
use nova_tui::ui::{palette, screen};

/// A key press with no modifiers.
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

/// Render the whole client into a buffer and return the visible text.
fn screen_text(state: &mut AppState) -> String {
    use ratatui::backend::TestBackend;
    use ratatui::Terminal as Ratatui;

    let backend = TestBackend::new(120, 40);
    let mut terminal = Ratatui::new(backend).expect("terminal");
    let theme = nova_tui::theme::Theme::default();
    terminal
        .draw(|frame| nova_tui::ui::draw(frame, state, &theme))
        .expect("draw");
    let buffer = terminal.backend().buffer();
    let mut text = String::new();
    for row in 0..buffer.area.height {
        for col in 0..buffer.area.width {
            text.push_str(buffer[(col, row)].symbol());
        }
        text.push('\n');
    }
    text
}

/// Apply every line of *lines*, for terser tests.
fn applied(lines: &str) -> AppState {
    let mut state = AppState::new();
    for line in lines.lines() {
        if let Some(frame) = frames::decode(line) {
            apply(&mut state, &frame);
        }
    }
    state
}

const CATALOGUE_LINE: &str = concat!(
    r#"{"t":"catalogue","commands":["#,
    r#"{"name":"help","help":"show this help","aliases":["?"],"wants_text":false,"#,
    r#""route":"passthrough","served":true},"#,
    r#"{"name":"theme","help":"switch style","aliases":["themes"],"wants_text":false,"#,
    r#""route":"screen","served":false},"#,
    r#"{"name":"model","help":"configure models","aliases":["models"],"wants_text":true,"#,
    r#""route":"passthrough","served":true}]}"#,
);

#[test]
fn a_catalogue_frame_reaches_the_client() {
    let state = applied(CATALOGUE_LINE);
    let names: Vec<&str> = state.commands.iter().map(|c| c.name.as_str()).collect();
    assert_eq!(names, ["help", "theme", "model"]);
    assert!(state.commands[0].aliases.contains(&"?".to_string()));
    assert!(!state.commands[1].served, "an unserved row must say so");
    assert!(state.commands[2].wants_text);
}

#[test]
fn the_catalogue_request_is_a_frame_the_server_knows() {
    let decoded = frames::decode(frames::catalogue().trim()).expect("decode");
    assert_eq!(decoded, Inbound::Catalogue { commands: vec![] });
}

#[test]
fn the_palette_filters_and_completes() {
    let mut state = applied(CATALOGUE_LINE);
    state.composer.clear();
    state.palette.open = true;
    state.palette.query = "mod".to_string();
    let commands = state.commands.clone();
    state.palette.refilter(&commands);
    assert_eq!(state.palette.matches.len(), 1);
    assert_eq!(
        state.commands[state.palette.current().unwrap()].name,
        "model"
    );

    // Tab completes instead of running: this command wants arguments.
    palette::handle_key(&mut state, &key(KeyCode::Tab));
    assert_eq!(state.composer, "/model ");
    assert!(!state.palette.open);
}

#[test]
fn a_palette_row_without_arguments_runs_immediately() {
    let mut state = applied(CATALOGUE_LINE);
    state.palette.open = true;
    state.palette.query = "hel".to_string();
    let commands = state.commands.clone();
    state.palette.refilter(&commands);
    let effect = palette::handle_key(&mut state, &key(KeyCode::Enter)).expect("effect");
    match effect {
        Effect::Send(line) => assert!(line.contains(r#""name":"help""#), "{line}"),
        other => panic!("expected a command frame, got {other:?}"),
    }
    assert!(!state.palette.open);
    assert!(state.turn_running);
}

/// A `cancel` action is how `/stop` reaches the host: the server cannot cancel.
#[test]
fn a_cancel_action_sends_the_cancel_frame() {
    let mut state = AppState::new();
    let line = r##"{"t":"command_result","id":"c1","ok":true,"message":"Stopping…","action":"cancel","blocks":[]}"##;
    let frame = frames::decode(line).expect("decodes");
    let effects = apply(&mut state, &frame);
    assert_eq!(effects.len(), 1);
    match &effects[0] {
        Effect::Send(payload) => {
            let sent: serde_json::Value = serde_json::from_str(payload).expect("json");
            assert_eq!(sent["t"], "cancel");
        }
        other => panic!("expected a send, got {other:?}"),
    }
    assert!(!state.quit, "stopping a turn is not quitting the client");
}

/// Two sessions: the tab bar draws both, marks the active one, and Alt+2 sends
/// an `attach` for the second.
#[test]
fn the_tab_bar_lists_sessions_and_alt_digits_attach() {
    let state = applied(
        r##"{"t":"sessions","sessions":[{"id":"alpha","name":"alpha","cwd":"C:/a","status":"idle","model":"m1"},{"id":"beta","name":"beta","cwd":"C:/b","status":"running","model":"m2"}],"active":"alpha"}"##,
    );
    assert!(nova_tui::ui::tabs::visible(&state));
    let theme = nova_tui::theme::Theme::default();
    let text: String = nova_tui::ui::tabs::line(&state, &theme, 80)
        .spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect();
    assert!(text.contains("alpha"), "got {text:?}");
    assert!(text.contains("beta"), "got {text:?}");
    assert!(text.contains("•"), "a running session is marked: {text:?}");

    // Alt+2 attaches to the second tab; Alt+9 is out of range and does nothing.
    let attach = nova_tui::ui::tabs::attach_for(&state, '2').expect("second tab");
    match &attach {
        Effect::Send(line) => {
            let frame: serde_json::Value = serde_json::from_str(line).expect("json");
            assert_eq!(frame["t"], "attach");
            assert_eq!(frame["session_id"], "beta");
        }
        other => panic!("expected a send, got {other:?}"),
    }
    assert!(
        nova_tui::ui::tabs::attach_for(&state, '9').is_none(),
        "no ninth session, no frame"
    );
}

/// The tab bar costs a row only while there is more than one session.
#[test]
fn a_single_session_has_no_tab_bar() {
    let state = applied(
        r##"{"t":"sessions","sessions":[{"id":"alpha","name":"alpha","cwd":"C:/a","status":"idle","model":"m1"}],"active":"alpha"}"##,
    );
    assert!(!nova_tui::ui::tabs::visible(&state));
}

/// Drawn output: the tab bar occupies row 5 (after the 5-row transcript) with
/// two sessions, and the transcript grows back to row 6's space with one.
#[test]
fn the_tab_bar_takes_a_row_only_when_it_exists() {
    use ratatui::backend::TestBackend;
    use ratatui::Terminal;

    fn render(state: &mut AppState) -> String {
        let backend = TestBackend::new(40, 10);
        let mut terminal = Terminal::new(backend).expect("terminal");
        let theme = nova_tui::theme::Theme::default();
        terminal
            .draw(|frame| nova_tui::ui::draw(frame, state, &theme))
            .expect("draw");
        let buffer = terminal.backend().buffer();
        let mut text = String::new();
        for y in 0..10 {
            for x in 0..40 {
                text.push_str(buffer[(x, y)].symbol());
            }
            text.push('\n');
        }
        text
    }

    let mut two = applied(
        r##"{"t":"sessions","sessions":[{"id":"alpha","name":"alpha","cwd":"C:/a","status":"idle","model":"m1"},{"id":"beta","name":"beta","cwd":"C:/b","status":"idle","model":"m2"}],"active":"alpha"}"##,
    );
    let rendered = render(&mut two);
    let row5: String = rendered.lines().nth(5).unwrap_or_default().to_string();
    assert!(row5.contains("alpha"), "tab row: {row5:?}");
    assert!(row5.contains("beta"), "tab row: {row5:?}");

    let mut one = applied(
        r##"{"t":"sessions","sessions":[{"id":"alpha","name":"alpha","cwd":"C:/a","status":"idle","model":"m1"}],"active":"alpha"}"##,
    );
    let rendered_one = render(&mut one);
    let row5_one: String = rendered_one.lines().nth(5).unwrap_or_default().to_string();
    assert!(!row5_one.contains("alpha"), "no tab bar: {row5_one:?}");
}

/// A choice field cycles with the arrows and answers its value as a string.
#[test]
fn a_choice_field_cycles_and_answers_its_value() {
    let mut state = applied(
        r##"{"t":"screen","id":"s1","kind":"form","title":"Settings","payload":{"fields":[{"key":"reasoning_effort","label":"Reasoning effort","value":"off","options":["off","low","medium","high"]},{"key":"context_budget_tokens","label":"Context budget","value":"64K","options":["64K","128K"]}]}}"##,
    );
    let view = state.screen.as_ref().expect("the form opens");
    assert_eq!(view.fields[0].options.len(), 4);
    assert_eq!(view.fields[0].value, "off");

    // Right cycles forward, Left back, wrapping at both ends.
    screen::handle_key(&mut state, &key(KeyCode::Right));
    assert_eq!(state.screen.as_ref().unwrap().fields[0].value, "low");
    screen::handle_key(&mut state, &key(KeyCode::Left));
    assert_eq!(state.screen.as_ref().unwrap().fields[0].value, "off");
    screen::handle_key(&mut state, &key(KeyCode::Left));
    assert_eq!(
        state.screen.as_ref().unwrap().fields[0].value,
        "high",
        "wraps to the last option"
    );

    // Moving on sends both values as strings, not booleans.
    screen::handle_key(&mut state, &key(KeyCode::Tab));
    match screen::handle_key(&mut state, &key(KeyCode::Enter)).expect("answers") {
        Effect::Answer { value, .. } => {
            assert_eq!(value["reasoning_effort"], "high");
            assert_eq!(value["context_budget_tokens"], "64K");
        }
        other => panic!("expected an answer, got {other:?}"),
    }
}

/// A form whose fields are toggles answers booleans, and Space flips them.
#[test]
fn a_toggle_form_answers_booleans() {
    let mut state = applied(
        r##"{"t":"screen","id":"s1","kind":"form","title":"Status line","payload":{"fields":[{"key":"activity","label":"Activity","value":"on","toggle":true},{"key":"model","label":"Model","value":"off","toggle":true}]}}"##,
    );
    let view = state.screen.as_ref().expect("the form opens");
    assert!(view.fields.iter().all(|field| field.toggle));
    assert_eq!(view.fields[0].value, "on");

    // Space flips the focused toggle; typing must not corrupt its state.
    screen::handle_key(&mut state, &key(KeyCode::Char(' ')));
    assert_eq!(state.screen.as_ref().unwrap().fields[0].value, "off");
    screen::handle_key(&mut state, &key(KeyCode::Char('x')));
    assert_eq!(state.screen.as_ref().unwrap().fields[0].value, "off");

    // Tab walks to the second field, Enter there submits both decisions.
    screen::handle_key(&mut state, &key(KeyCode::Tab));
    assert_eq!(state.screen.as_ref().unwrap().focused, 1);
    screen::handle_key(&mut state, &key(KeyCode::Char(' ')));
    match screen::handle_key(&mut state, &key(KeyCode::Enter)).expect("answers") {
        Effect::Answer { screen_id, value } => {
            assert_eq!(screen_id, "s1");
            assert_eq!(value, json!({"activity": false, "model": true}));
        }
        other => panic!("expected an answer, got {other:?}"),
    }
    assert!(state.screen.is_none(), "the form closes on submit");
}

/// A `ui_theme` frame: decoded, parked, then turned into a live palette.
#[test]
fn a_theme_frame_repaints_the_client() {
    let state = applied(
        r##"{"t":"ui_theme","name":"nova-signal","dark":true,"primary":"#f4723f","foreground":"#f2eeeb","panel":"#222222"}"##,
    );
    let palette = state.pending_theme.clone().expect("a palette is waiting");
    assert_eq!(palette.name, "nova-signal");
    assert_eq!(palette.primary.as_deref(), Some("#f4723f"));
    assert_eq!(palette.border, None, "an absent colour stays absent");

    let base = nova_tui::theme::Theme::default();
    let theme = nova_tui::theme::Theme::from_palette(&palette, base);
    assert_eq!(theme.accent, ratatui::style::Color::Rgb(0xf4, 0x72, 0x3f));
    assert_eq!(theme.text, ratatui::style::Color::Rgb(0xf2, 0xee, 0xeb));
    assert_eq!(theme.dim, base.dim, "no muted colour keeps the default");
}

/// The status bar names the theme in force, once the server has sent one.
#[test]
fn the_status_bar_can_name_the_theme() {
    let mut state = AppState::new();
    state.status.theme = "nova-classic".to_string();
    let theme = nova_tui::theme::Theme::default();
    let line = nova_tui::ui::statusbar::line(&state, &theme, 200);
    let text: String = line
        .spans
        .iter()
        .map(|span| span.content.as_ref())
        .collect();
    assert!(text.contains("nova-classic"), "got {text:?}");
}

#[test]
fn escape_closes_the_palette() {
    let mut state = applied(CATALOGUE_LINE);
    state.palette.open = true;
    state.palette.query = "he".to_string();
    palette::handle_key(&mut state, &key(KeyCode::Esc));
    assert!(!state.palette.open);
    assert!(state.palette.query.is_empty());
}

/// One @@screen@@ frame, as the server sends it.
fn screen_line(kind: &str, payload: serde_json::Value) -> String {
    json!({"t": "screen", "id": "scr-1", "kind": kind, "title": "Picker", "payload": payload})
        .to_string()
}

#[test]
fn a_list_screen_answers_with_the_selected_id() {
    let mut state = applied(&screen_line(
        "list",
        json!({"items": [{"id": "a", "label": "first"}, {"id": "b", "label": "second"}]}),
    ));
    let view = state.screen.as_ref().expect("screen");
    assert_eq!(view.kind, ScreenKind::List);
    assert_eq!(view.rows.len(), 2);
    screen::handle_key(&mut state, &key(KeyCode::Down));
    let effect = screen::handle_key(&mut state, &key(KeyCode::Enter)).expect("answer");
    assert_eq!(
        effect,
        Effect::Answer {
            screen_id: "scr-1".to_string(),
            value: json!({"id": "b"}),
        }
    );
    assert!(state.screen.is_none(), "the modal closes on its answer");
}

#[test]
fn a_table_screen_shows_columns_and_answers_a_row() {
    let mut state = applied(&screen_line(
        "table",
        json!({
            "columns": [{"key": "name", "title": "Session"}, {"key": "status", "title": "Status"}],
            "rows": [{"id": "s1", "name": "nova", "status": "idle"}],
        }),
    ));
    let view = state.screen.as_ref().expect("screen");
    assert_eq!(view.columns, ["Session", "Status"]);
    assert_eq!(view.rows[0].cells, ["nova", "idle"]);
    let effect = screen::handle_key(&mut state, &key(KeyCode::Enter)).expect("answer");
    assert_eq!(
        effect,
        Effect::Answer {
            screen_id: "scr-1".to_string(),
            value: json!({"id": "s1"}),
        }
    );
}

#[test]
fn a_confirm_screen_answers_a_bool() {
    let mut state = applied(&screen_line("confirm", json!({"message": "Delete it?"})));
    let effect = screen::handle_key(&mut state, &key(KeyCode::Char('y'))).expect("answer");
    assert_eq!(
        effect,
        Effect::Answer {
            screen_id: "scr-1".to_string(),
            value: json!(true),
        }
    );

    let mut state = applied(&screen_line("confirm", json!({"message": "go?"})));
    let effect = screen::handle_key(&mut state, &key(KeyCode::Esc)).expect("answer");
    assert_eq!(
        effect,
        Effect::Answer {
            screen_id: "scr-1".to_string(),
            value: json!(false),
        }
    );
}

#[test]
fn a_form_screen_edits_fields_then_submits_them() {
    let mut state = applied(&screen_line(
        "form",
        json!({
            "fields": [
                {"key": "model", "label": "Model", "value": "gpt"},
                {"key": "effort", "label": "Effort", "value": "low"},
            ],
            "submit": "Save",
        }),
    ));
    screen::handle_key(&mut state, &key(KeyCode::Char('5')));
    screen::handle_key(&mut state, &key(KeyCode::Enter));
    assert_eq!(state.screen.as_ref().unwrap().focused, 1);
    let effect = screen::handle_key(&mut state, &key(KeyCode::Enter)).expect("answer");
    assert_eq!(
        effect,
        Effect::Answer {
            screen_id: "scr-1".to_string(),
            value: json!({"model": "gpt5", "effort": "low"}),
        }
    );
}

#[test]
fn an_unknown_screen_kind_fails_closed() {
    let mut state = AppState::new();
    let effects = apply(
        &mut state,
        &frames::decode(&screen_line("hologram", json!({}))).expect("decode"),
    );
    assert!(effects.contains(&Effect::Answer {
        screen_id: "scr-1".to_string(),
        value: serde_json::Value::Null,
    }));
    assert!(state.screen.is_none());
}

#[test]
fn a_progress_screen_reports_counts_and_closes_on_escape() {
    let mut state = applied(&screen_line(
        "progress",
        json!({"label": "reindexing", "done": 3, "total": 10}),
    ));
    let view = state.screen.as_ref().expect("screen");
    assert_eq!(view.done, 3);
    assert_eq!(view.total, 10);
    let effect = screen::handle_key(&mut state, &key(KeyCode::Esc)).expect("answer");
    assert_eq!(
        effect,
        Effect::Answer {
            screen_id: "scr-1".to_string(),
            value: serde_json::Value::Null,
        }
    );
}

#[test]
fn a_command_result_can_carry_a_screen_without_an_id() {
    let mut state = AppState::new();
    let line = json!({
        "t": "command_result",
        "id": "c1",
        "ok": true,
        "message": "",
        "blocks": [],
        "screen": {"kind": "detail", "title": "Report", "payload": {"markdown": "# hi"}},
    })
    .to_string();
    apply(&mut state, &frames::decode(&line).expect("decode"));
    let view = state.screen.as_ref().expect("screen");
    assert!(view.id.is_empty());
    assert_eq!(view.body, "# hi");
    // Informational screens answer nothing, so closing one is silent.
    assert!(screen::handle_key(&mut state, &key(KeyCode::Esc)).is_none());
}

/// The recorded screen corpus, produced by
/// `uv run python scripts/record_tui_session.py --scenario screens`.
const SCREEN_FIXTURE: &str = include_str!("fixtures/screens.jsonl");

/// Render the whole client at a fixed size, for layout assertions.
fn render(state: &mut AppState) -> String {
    let theme = nova_tui::theme::Theme::default();
    let backend = ratatui::backend::TestBackend::new(100, 35);
    let mut terminal = ratatui::Terminal::new(backend).expect("terminal");
    terminal
        .draw(|frame| nova_tui::ui::draw(frame, state, &theme))
        .expect("draw");
    let buffer = terminal.backend().buffer().clone();
    let mut out = String::new();
    for y in 0..35 {
        for x in 0..100 {
            out.push_str(buffer[(x, y)].symbol());
        }
        out.push('\n');
    }
    out
}

#[test]
fn the_recorded_catalogue_keeps_every_command() {
    let frames: Vec<Inbound> = SCREEN_FIXTURE.lines().filter_map(frames::decode).collect();
    let catalogue = frames
        .iter()
        .find_map(|frame| match frame {
            Inbound::Catalogue { commands } => Some(commands),
            _ => None,
        })
        .expect("the scenario records a catalogue");
    assert_eq!(catalogue.len(), 105);
    assert!(catalogue.iter().any(|command| command.name == "help"));
    assert!(catalogue.iter().any(|command| !command.served));
}

#[test]
fn every_recorded_screen_renders_and_answers_from_the_fixture() {
    let mut state = AppState::new();
    let mut seen: Vec<ScreenKind> = Vec::new();
    let mut titles: Vec<String> = Vec::new();
    for line in SCREEN_FIXTURE.lines() {
        let Some(frame) = frames::decode(line) else {
            continue;
        };
        apply(&mut state, &frame);
        if !matches!(frame, Inbound::Screen { .. }) {
            continue;
        }
        let view = state.screen.clone().expect("the frame opens a modal");
        seen.push(view.kind);
        titles.push(view.title.clone());
        let painted = render(&mut state);
        assert!(
            painted.contains(&view.title) || painted.contains(view.kind.label()),
            "the {} screen must paint: {painted}",
            view.kind.label()
        );
        // Answering closes the modal; the server owns the effect beyond that.
        let _ = screen::handle_key(&mut state, &key(KeyCode::Enter));
    }
    for kind in [
        ScreenKind::List,
        ScreenKind::Table,
        ScreenKind::Confirm,
        ScreenKind::Form,
        ScreenKind::Progress,
        ScreenKind::Detail,
    ] {
        assert!(
            seen.contains(&kind),
            "the corpus must cover {kind:?}: {seen:?}"
        );
    }
    assert_eq!(titles.len(), 6);
}
#[test]
fn a_screen_result_frame_round_trips() {
    let line = frames::screen_result("scr-9", json!({"id": "row"}));
    let decoded = frames::decode(line.trim()).expect("decode");
    // The client only sends this frame; decoding it as unknown is correct, and
    // what matters here is the wire shape the server parses.
    assert_eq!(
        decoded,
        Inbound::Unknown {
            kind: "screen_result".to_string()
        }
    );
    assert!(line.contains(r#""id":"scr-9""#));
    assert!(line.contains(r#""result":{"id":"row"}"#));
}

/// A settings form taller than the popup scrolls to keep the focused field in
/// view, and Home returns to the top. Without this a long form would hide its
/// last fields with no way to reach them.
#[test]
fn a_long_form_scrolls_to_keep_the_focused_field_visible() {
    let fields: Vec<serde_json::Value> = (0..20)
        .map(|index| {
            json!({
                "key": format!("field{index}"),
                "label": format!("Field {index}"),
                "value": "value",
            })
        })
        .collect();
    let mut state = applied(&screen_line("form", json!({ "fields": fields })));
    for _ in 0..14 {
        screen::handle_key(&mut state, &key(KeyCode::Down));
    }
    let view = state.screen.as_ref().expect("screen");
    assert_eq!(view.focused, 14);
    assert!(view.scroll > 0, "scroll must follow the focused field");
    assert!(view.scroll as usize + 12 > view.focused);

    screen::handle_key(&mut state, &key(KeyCode::Home));
    let view = state.screen.as_ref().expect("screen");
    assert_eq!(view.focused, 0);
    assert_eq!(view.scroll, 0);

    screen::handle_key(&mut state, &key(KeyCode::PageDown));
    let view = state.screen.as_ref().expect("screen");
    assert_eq!(view.focused, 12);
    assert!(view.scroll > 0);
}

/// `/tabs` answers with an `attach` field; the client sends the existing
/// `attach` frame instead of inventing a new one.
#[test]
fn a_tabs_choice_attaches_the_connection() {
    let mut state = AppState::new();
    let line = r##"{"t":"command_result","id":"c1","ok":true,"message":"Attached.","blocks":[],"attach":"beta"}"##;
    let effects = apply(&mut state, &frames::decode(line).expect("decodes"));
    assert_eq!(effects.len(), 1);
    match &effects[0] {
        Effect::Send(payload) => {
            let sent: serde_json::Value = serde_json::from_str(payload).expect("json");
            assert_eq!(sent["t"], "attach");
            assert_eq!(sent["session_id"], "beta");
        }
        other => panic!("expected an attach frame, got {other:?}"),
    }
}

/// Each tab keeps its own transcript: switching away stashes this session,
/// switching back restores exactly what it had. Model/cwd come from the row.
#[test]
fn switching_sessions_keeps_each_transcript_separate() {
    let mut state = applied(concat!(
        r##"{"t":"ready","protocol":1,"session_id":"alpha","cwd":"C:/a","model":"m-a","assistant_id":"nova"}"##,
        "\n",
        r##"{"t":"sessions","sessions":[{"id":"alpha","name":"main","cwd":"C:/a","status":"idle","model":"m-a"},{"id":"beta","name":"docs","cwd":"C:/b","status":"idle","model":"m-b"}],"active":"alpha"}"##,
        "\n",
        r##"{"t":"ev","c":"AssistantMessage","d":{"text":"alpha says hi","agent_name":"Nova","agent_color":"cyan","is_subagent":false}}"##,
    ));
    assert_eq!(state.prose(), vec!["alpha says hi".to_string()]);
    assert_eq!(state.status.model.as_deref(), Some("m-a"));
    state.composer = "alpha draft".to_string();

    let switched = r##"{"t":"sessions","sessions":[{"id":"alpha","name":"main","cwd":"C:/a","status":"idle","model":"m-a"},{"id":"beta","name":"docs","cwd":"C:/b","status":"idle","model":"m-b"}],"active":"beta"}"##;
    apply(&mut state, &frames::decode(switched).expect("decodes"));
    assert!(
        state.entries.is_empty(),
        "beta starts with its own transcript"
    );
    assert_eq!(state.status.model.as_deref(), Some("m-b"));
    assert_eq!(state.status.cwd.as_deref(), Some("C:/b"));

    apply(
        &mut state,
        &frames::decode(
            r##"{"t":"ev","c":"AssistantMessage","d":{"text":"beta says hi","agent_name":"Nova","agent_color":"cyan","is_subagent":false}}"##,
        )
        .expect("decodes"),
    );
    assert_eq!(state.prose(), vec!["beta says hi".to_string()]);

    let back = r##"{"t":"sessions","sessions":[{"id":"alpha","name":"main","cwd":"C:/a","status":"idle","model":"m-a"},{"id":"beta","name":"docs","cwd":"C:/b","status":"idle","model":"m-b"}],"active":"alpha"}"##;
    apply(&mut state, &frames::decode(back).expect("decodes"));
    assert_eq!(state.prose(), vec!["alpha says hi".to_string()]);
    assert_eq!(state.status.model.as_deref(), Some("m-a"));
    assert_eq!(state.composer, "alpha draft");
}

/// `/tabs new` and `/tabs close` name a connection action: the client asks the
/// server to spawn or stop a child session instead of the command doing it.
#[test]
fn tab_create_and_close_actions_send_session_frames() {
    let mut state = AppState::new();
    state.active_session = Some("alpha".to_string());
    let create = r##"{"t":"command_result","id":"c1","ok":true,"message":"Starting docs.","blocks":[],"create":"docs"}"##;
    let effects = apply(&mut state, &frames::decode(create).expect("decodes"));
    match &effects[0] {
        Effect::Send(payload) => {
            let sent: serde_json::Value = serde_json::from_str(payload).expect("json");
            assert_eq!(sent["t"], "create_session");
            assert_eq!(sent["name"], "docs");
        }
        other => panic!("expected a create frame, got {other:?}"),
    }

    // An empty id means "the active tab": the client fills it in.
    let close = r##"{"t":"command_result","id":"c2","ok":true,"message":"Closing.","blocks":[],"close":""}"##;
    let effects = apply(&mut state, &frames::decode(close).expect("decodes"));
    match &effects[0] {
        Effect::Send(payload) => {
            let sent: serde_json::Value = serde_json::from_str(payload).expect("json");
            assert_eq!(sent["t"], "close_session");
            assert_eq!(sent["session_id"], "alpha");
        }
        other => panic!("expected a close frame, got {other:?}"),
    }
}

/// `/resume` asks for a tab that continues a saved session, so the create frame
/// carries the id the child must pass to `--continue`.
#[test]
fn a_resume_result_creates_a_tab_that_continues_the_saved_session() {
    let mut state = AppState::new();
    let resume = r##"{"t":"command_result","id":"c1","ok":true,"message":"⟲ Resuming 4f8a11c2 in a new tab (NovaCode), with its full history.","blocks":[],"create":"NovaCode · 4f8a11c2","resume":"4f8a11c2-9f00-4d1e-8a2b-77c1d2e3f401"}"##;
    let effects = apply(&mut state, &frames::decode(resume).expect("decodes"));
    match &effects[0] {
        Effect::Send(payload) => {
            let sent: serde_json::Value = serde_json::from_str(payload).expect("json");
            assert_eq!(sent["t"], "create_session");
            assert_eq!(sent["name"], "NovaCode · 4f8a11c2");
            assert_eq!(sent["resume"], "4f8a11c2-9f00-4d1e-8a2b-77c1d2e3f401");
        }
        other => panic!("expected a create frame, got {other:?}"),
    }
}

/// Council progress folds into one card and paints phases, agents and counters.
#[test]
fn council_updates_paint_a_single_card() {
    let mut state = AppState::new();
    apply(
        &mut state,
        &frames::decode(
            r##"{"t":"ev","c":"CouncilUpdate","d":{"prompt":"Add retry","event":"council.started","agents":[{"id":"architect","name":"Architect","avatar":"🏛","color":"cyan"},{"id":"critic","name":"Critic","avatar":"🔍","color":"red"}]}}"##,
        )
        .expect("decodes"),
    );
    apply(
        &mut state,
        &frames::decode(
            r##"{"t":"ev","c":"CouncilUpdate","d":{"event":"council.phase.started","phase":"brainstorming"}}"##,
        )
        .expect("decodes"),
    );
    apply(
        &mut state,
        &frames::decode(
            r##"{"t":"ev","c":"CouncilUpdate","d":{"event":"agent.completed","phase":"brainstorming","agent_id":"architect","proposals":1}}"##,
        )
        .expect("decodes"),
    );
    let council = state.council.as_ref().expect("a council card");
    assert_eq!(council.phase, "brainstorming");
    assert_eq!(council.proposals, 1);
    assert!(council
        .done
        .contains(&("architect".to_string(), "brainstorming".to_string())));
    let theme = nova_tui::theme::Theme::default();
    let text = nova_tui::ui::council::lines(&state, &theme)
        .iter()
        .map(|line| {
            line.spans
                .iter()
                .map(|span| span.content.as_ref())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n");
    assert!(text.contains("Council"));
    assert!(text.contains("Plan"));
    assert!(text.contains("Architect"));
    assert!(text.contains("1 plan(s)"));
}

/// A failed council records the reason and stops presenting as "working".
#[test]
fn a_failed_council_keeps_its_reason() {
    let mut state = AppState::new();
    apply(
        &mut state,
        &frames::decode(
            r##"{"t":"ev","c":"CouncilUpdate","d":{"event":"council.failed","outcome":"failed","reason":"only one plan"}}"##,
        )
        .expect("decodes"),
    );
    let council = state.council.as_ref().expect("a council card");
    assert_eq!(council.outcome, "failed");
    assert_eq!(council.message, "only one plan");
}

/// A split screen carries two markdown panes and a selectable action list.
#[test]
fn a_split_screen_answers_with_the_selected_action() {
    let mut state = applied(&screen_line(
        "split",
        json!({
            "left": {"title": "Plans", "markdown": "#1 Plan A"},
            "right": {"title": "Detail", "markdown": "Full plan text"},
            "actions": [
                {"id": "approve:1", "label": "Approve #1"},
                {"id": "close", "label": "Close"},
            ],
        }),
    ));
    let view = state.screen.as_ref().expect("screen");
    assert_eq!(view.body_left, "#1 Plan A");
    assert_eq!(view.body_right, "Full plan text");
    assert_eq!(view.actions.len(), 2);

    screen::handle_key(&mut state, &key(KeyCode::Down));
    let effect = screen::handle_key(&mut state, &key(KeyCode::Enter)).expect("answer");
    match effect {
        Effect::Answer { screen_id, value } => {
            assert_eq!(screen_id, "scr-1");
            assert_eq!(value, json!({"action": "close"}));
        }
        other => panic!("expected an answer, got {other:?}"),
    }
}

/// `/copy` gathers the last Nova reply; `/copy all` gathers the transcript.
#[test]
fn copy_actions_gather_transcript_text() {
    let mut state = applied(concat!(
        r##"{"t":"ev","c":"AssistantMessage","d":{"text":"first reply","agent_name":"Nova","agent_color":"cyan","is_subagent":false}}"##,
        "\n",
        r##"{"t":"ev","c":"AssistantMessage","d":{"text":"second reply","agent_name":"Nova","agent_color":"cyan","is_subagent":false}}"##,
    ));
    state.entries.push(nova_tui::state::Entry {
        kind: nova_tui::state::BlockKind::User,
        text: "a question".to_string(),
        agent: "you".to_string(),
        agent_color: None,
    });

    let last = r##"{"t":"command_result","id":"c1","ok":true,"message":"copying","blocks":[],"action":"copy"}"##;
    let effects = apply(&mut state, &frames::decode(last).expect("decodes"));
    match &effects[0] {
        Effect::Copy { text, all } => {
            assert!(!all);
            assert_eq!(text, "second reply");
        }
        other => panic!("expected a copy, got {other:?}"),
    }

    let all = r##"{"t":"command_result","id":"c2","ok":true,"message":"copying","blocks":[],"action":"copy-all"}"##;
    let effects = apply(&mut state, &frames::decode(all).expect("decodes"));
    match &effects[0] {
        Effect::Copy { text, all: true } => {
            assert!(text.contains("## Nova\nsecond reply"));
            assert!(text.contains("## You\na question"));
        }
        other => panic!("expected a copy-all, got {other:?}"),
    }
}

/// A `ready` frame that names the saved home layout.
fn ready_with_home(style: &str, animation: bool) -> String {
    json!({
        "t": "ready",
        "protocol": frames::PROTOCOL_VERSION,
        "session_id": "s1",
        "model": "test-model",
        "ui": "ratatui",
        "home": {"style": style, "animation": animation, "version": "9.9.9"},
    })
    .to_string()
}

#[test]
fn the_banner_draws_on_an_empty_conversation_for_the_banner_layouts() {
    let mut state = AppState::new();
    apply(
        &mut state,
        &frames::decode(&ready_with_home("signal", false)).expect("decode"),
    );
    let banner = state.home.as_ref().expect("home");
    assert_eq!(banner.style, "signal");
    assert!(banner.shows_banner());
    // At 100 columns the lockup tiers in: the portrait's top row, plus the
    // version caption under the wordmark.
    let rendered = screen_text(&mut state);
    assert!(
        rendered.contains(nova_tui::ui::brand_art::PORTRAIT[0]),
        "{rendered}"
    );
    assert!(rendered.contains("v9.9.9"), "{rendered}");
}

#[test]
fn a_plain_layout_gets_no_banner_and_an_empty_conversation_stays_empty() {
    let mut state = AppState::new();
    apply(
        &mut state,
        &frames::decode(&ready_with_home("studio", true)).expect("decode"),
    );
    let rendered = screen_text(&mut state);
    assert!(!rendered.contains("v9.9.9"), "{rendered}");
    assert!(
        !rendered.contains(nova_tui::ui::brand_art::PORTRAIT[0]),
        "{rendered}"
    );
}

#[test]
fn a_conversation_hides_the_banner() {
    let mut state = AppState::new();
    for line in [
        ready_with_home("dashboard", false),
        r#"{"t":"ev","c":"TextDelta","d":{"text":"hello"},"prompt_id":"p1"}"#.to_string(),
    ] {
        if let Some(frame) = frames::decode(&line) {
            for _ in apply(&mut state, &frame) {}
        }
    }
    assert!(!state.streaming.is_empty(), "the conversation has text");
    let rendered = screen_text(&mut state);
    assert!(!rendered.contains("v9.9.9"), "{rendered}");
}

/// A two-cell sprite: enough to prove the payload path and the overlay rule.
fn sprite_sheet() -> serde_json::Value {
    json!({
        "width": 2,
        "height": 1,
        "fps": {"idle": 1.0},
        "foreground": ["#00ff00", "#00ff00"],
        "background": ["", ""],
        "frames": [{
            "state": "idle",
            "rows": ["##", "  "],
            "cells": [[1, 0]],
        }],
    })
}

#[test]
fn the_nova_command_puts_the_sprite_on_screen_only_when_there_is_room() {
    let mut state = AppState::new();
    for line in [
        ready_with_home("studio", false),
        json!({
            "t": "command_result",
            "id": "c1",
            "ok": true,
            "message": "Avatar shown at 2x1.",
            "blocks": [{"kind": "text", "text": "Avatar shown at 2x1."}],
            "action": "avatar",
            "avatar": sprite_sheet(),
        })
        .to_string(),
    ] {
        apply(&mut state, &frames::decode(&line).expect("decode"));
    }
    assert_eq!(state.avatar.as_ref().map(|sprite| sprite.width), Some(2));
    let large = render(&mut state);
    assert!(large.contains("##"), "{large}");
    assert!(
        large.contains("Avatar shown at 2x1."),
        "the transcript keeps its text"
    );

    // Below the terminal UI's minimum the sprite is absent, not squeezed.
    let theme = nova_tui::theme::Theme::default();
    let backend = ratatui::backend::TestBackend::new(80, 24);
    let mut terminal = ratatui::Terminal::new(backend).expect("terminal");
    terminal
        .draw(|frame| nova_tui::ui::draw(frame, &mut state, &theme))
        .expect("draw");
    let buffer = terminal.backend().buffer().clone();
    let mut small = String::new();
    for y in 0..24 {
        for x in 0..80 {
            small.push_str(buffer[(x, y)].symbol());
        }
        small.push('\n');
    }
    assert!(!small.contains("##"), "{small}");
}

#[test]
fn a_shell_command_fills_the_terminal_pane_and_keeps_its_exit() {
    let mut state = applied(concat!(
        r#"{"t":"pty","id":"p1","command":"pwd","text":"C:/work"}"#,
        "\n",
        r#"{"t":"pty","id":"p1","command":"pwd","text":"\r\ndone\r\n"}"#,
        "\n",
        r#"{"t":"pty_exit","id":"p1","code":0}"#,
    ));

    let lines = state.terminal.as_ref().expect("a pane").view();
    assert_eq!(lines, vec!["C:/work".to_string(), "done".to_string()]);
    let rendered = screen_text(&mut state);
    assert!(rendered.contains("!!pwd"), "{rendered}");
    assert!(rendered.contains("done"), "{rendered}");
    assert!(rendered.contains("exit 0"), "{rendered}");
}

#[test]
fn a_running_terminal_takes_the_keystrokes() {
    let mut state = applied(r#"{"t":"pty","id":"p1","command":"python","text":"$ "}"#);

    let enter = ui::terminal::handle_key(&mut state, &key(KeyCode::Enter));
    let Some(Effect::Send(frame)) = enter else {
        panic!("Enter should reach the child");
    };
    assert!(frame.contains("pty_input"), "{frame}");
    assert!(frame.contains(r#""text":"\r""#), "{frame}");

    let escape = ui::terminal::handle_key(&mut state, &key(KeyCode::Esc));
    let Some(Effect::Send(frame)) = escape else {
        panic!("Escape should close the pane");
    };
    assert!(frame.contains("pty_close"), "{frame}");
    assert!(state.terminal.is_none());
}

#[test]
fn a_finished_terminal_ignores_the_keyboard() {
    let mut state = applied(concat!(
        r#"{"t":"pty","id":"p1","command":"true","text":""}"#,
        "\n",
        r#"{"t":"pty_exit","id":"p1","code":0}"#,
    ));

    assert!(ui::terminal::handle_key(&mut state, &key(KeyCode::Char('a'))).is_none());
}

#[test]
fn a_collapsed_tool_card_hints_at_ctrl_o_and_expand_all_shows_everything() {
    use nova_tui::state::{BlockKind, Entry, ToolCard};

    let mut state = AppState::new();
    state.entries.push(Entry {
        kind: BlockKind::Tool(ToolCard {
            name: "shell".into(),
            display: "shell: pytest -q".into(),
            full_output: "one\ntwo\nthree\nfour".into(),
            finished: true,
            ..ToolCard::default()
        }),
        text: String::new(),
        agent: String::new(),
        agent_color: None,
    });

    // Collapsed: head lines and the final line stay visible, the middle does
    // not, and the card advertises the chord.
    let text = screen_text(&mut state);
    assert!(text.contains("1 hidden line · Ctrl+O"));
    assert!(text.contains("four"));
    assert!(!text.contains("three"));

    // Expansion is global: every line shows and the hint disappears.
    nova_tui::ui::transcript::toggle_all_tools(&mut state);
    let text = screen_text(&mut state);
    assert!(text.contains("three"));
    assert!(!text.contains("hidden line · Ctrl+O"));

    // Pressing the chord again restores the collapsed rendering.
    nova_tui::ui::transcript::toggle_all_tools(&mut state);
    let text = screen_text(&mut state);
    assert!(text.contains("1 hidden line · Ctrl+O"));
    assert!(!text.contains("three"));
}
