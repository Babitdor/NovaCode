//! `nova-tui` — the ratatui client for the Nova TUI server.
//!
//! Argv is parsed by hand: the client needs five flags and no subcommands, so a
//! full argument-parsing dependency would be the largest thing in the binary.
//!
//! The client owns the terminal, never the session: it decodes frames into
//! [`AppState`], renders, and sends intents back. Everything about the agent
//! lives on the other side of the socket (`rust/PROTOCOL.md`).

use std::io::Write;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use serde_json::json;

use nova_tui::client::{Client, ClientEvent};
use nova_tui::frames::{self, Inbound};
use nova_tui::server::Supervised;
use nova_tui::state::{apply, AppState, Effect, Modal};
use nova_tui::theme::Theme;
use nova_tui::ui;

/// How long to wait for the server's first frame when the client started it.
const CONNECT_DEADLINE: Duration = Duration::from_secs(60);

/// How long the event loop waits for a key before drawing again.
const TICK: Duration = Duration::from_millis(50);

const USAGE: &str = "\
nova-tui — ratatui client for the Nova TUI server

USAGE:
    nova-tui --port <PORT> --token <TOKEN> [--host <HOST>] [--server-cmd <CMD>]

OPTIONS:
    --host <HOST>        Server interface (default 127.0.0.1)
    --port <PORT>        Server port (required)
    --token <TOKEN>      Handshake token (required)
    --server-cmd <CMD>   Start this command first and connect to it
    --probe <COMMAND>    Internal: run non-interactively for the cross-language
                         test - print frames, answer screens, then exit
    --probe-grace-ms <N> How long the probe keeps listening after its last frame
    --help               Show this help
    --version            Show the version
";

/// Parsed command line.
struct Args {
    host: String,
    port: u16,
    token: String,
    server_cmd: Option<String>,
    /// Run non-interactively: send this slash command (empty for none), print
    /// every frame received as JSON, answer any screen with its documented
    /// default, and exit. Used by the cross-language test, which has no TTY.
    probe: Option<String>,
    /// How long the probe keeps listening after its last expected frame, so a
    /// screen the server asks for afterwards is still answered.
    probe_grace: Duration,
}

fn parse_args(mut argv: Vec<String>) -> Result<Args, String> {
    let mut host = "127.0.0.1".to_string();
    let mut port: Option<u16> = None;
    let mut token: Option<String> = None;
    let mut server_cmd: Option<String> = None;
    let mut probe: Option<String> = None;
    let mut probe_grace = Duration::from_millis(500);
    while let Some(flag) = argv.first().cloned() {
        argv.remove(0);
        let mut value = |name: &str| -> Result<String, String> {
            if argv.is_empty() {
                return Err(format!("{name} needs a value"));
            }
            Ok(argv.remove(0))
        };
        match flag.as_str() {
            "--host" => host = value("--host")?,
            "--port" => {
                let raw = value("--port")?;
                port = Some(raw.parse().map_err(|_| format!("invalid port: {raw}"))?);
            }
            "--token" => token = Some(value("--token")?),
            "--server-cmd" => server_cmd = Some(value("--server-cmd")?),
            "--probe" => probe = Some(value("--probe")?),
            "--probe-grace-ms" => {
                let raw = value("--probe-grace-ms")?;
                let millis: u64 = raw.parse().map_err(|_| format!("invalid grace: {raw}"))?;
                probe_grace = Duration::from_millis(millis);
            }
            other => return Err(format!("unknown argument: {other}")),
        }
    }
    Ok(Args {
        host,
        port: port.ok_or("--port is required")?,
        token: token.ok_or("--token is required")?,
        server_cmd,
        probe,
        probe_grace,
    })
}

fn main() -> ExitCode {
    let argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.iter().any(|arg| arg == "--help" || arg == "-h") {
        let _ = std::io::stdout().write_all(USAGE.as_bytes());
        return ExitCode::SUCCESS;
    }
    if argv.iter().any(|arg| arg == "--version") {
        let version = format!("nova-tui {}\n", env!("CARGO_PKG_VERSION"));
        let _ = std::io::stdout().write_all(version.as_bytes());
        return ExitCode::SUCCESS;
    }
    let args = match parse_args(argv) {
        Ok(args) => args,
        Err(message) => {
            let _ = writeln!(std::io::stderr(), "nova-tui: {message}\n\n{USAGE}");
            return ExitCode::from(2);
        }
    };

    // Owned before connecting: dropping it kills the server on every exit path.
    let _supervised = match &args.server_cmd {
        Some(command) => match Supervised::spawn(command) {
            Ok(child) => Some(child),
            Err(error) => {
                let _ = writeln!(std::io::stderr(), "nova-tui: cannot start server: {error}");
                return ExitCode::FAILURE;
            }
        },
        None => None,
    };

    let address = format!("{}:{}", args.host, args.port);
    let client = match connect_with_retry(&address, &args.token, args.server_cmd.is_some()) {
        Ok(client) => client,
        Err(error) => {
            let _ = writeln!(std::io::stderr(), "nova-tui: cannot connect: {error}");
            return ExitCode::FAILURE;
        }
    };

    if let Some(command) = args.probe.as_deref() {
        return probe(&client, command, args.probe_grace);
    }

    // The palette is driven by the server's catalogue, so ask for it as soon as
    // the handshake is done: it is protocol state, not UI state, and a build
    // that cannot open a terminal still exercises the request.
    if let Err(error) = client.send(frames::catalogue()) {
        let _ = writeln!(
            std::io::stderr(),
            "nova-tui: catalogue request failed: {error}"
        );
        return ExitCode::FAILURE;
    }

    let mut state = AppState::new();
    // The same reducer the loop uses: the handshake must not grow a second path.
    if matches!(client.ready, Inbound::Ready { .. }) {
        let _ = apply(&mut state, &client.ready);
    }

    let terminal = ratatui::try_init();
    let mut terminal = match terminal {
        Ok(terminal) => terminal,
        Err(error) => {
            let _ = writeln!(
                std::io::stderr(),
                "nova-tui: cannot start the terminal: {error}"
            );
            return ExitCode::FAILURE;
        }
    };
    let outcome = run(&mut terminal, &client, &mut state);
    ratatui::restore();
    match outcome {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            let _ = writeln!(std::io::stderr(), "nova-tui: {error}");
            ExitCode::FAILURE
        }
    }
}

/// Run the client non-interactively: ask for the catalogue, optionally run one
/// slash command, print every frame as JSON, answer any screen, then exit.
///
/// This is the seam the cross-language test uses: a test runner has no TTY, so
/// the interactive loop cannot run, but the same decoders, the same encoders and
/// the same `screen` answer shapes are exercised against the real server.
///
/// Returns:
///     `ExitCode::SUCCESS` when the expected frames arrived, `FAILURE` otherwise.
fn probe(client: &Client, command: &str, grace: Duration) -> ExitCode {
    if let Err(error) = client.send(frames::catalogue()) {
        let _ = writeln!(
            std::io::stderr(),
            "nova-tui: catalogue request failed: {error}"
        );
        return ExitCode::FAILURE;
    }
    let mut wanted: Option<String> = None;
    if !command.is_empty() {
        let trimmed = command.trim_start_matches('/');
        let mut parts = trimmed.splitn(2, ' ');
        let name = parts.next().unwrap_or_default();
        let args = parts.next();
        let id = "probe-1".to_string();
        if let Err(error) = client.send(frames::command(&id, name, args)) {
            let _ = writeln!(std::io::stderr(), "nova-tui: command failed: {error}");
            return ExitCode::FAILURE;
        }
        wanted = Some(id);
    }

    let mut catalogue = false;
    let mut answered = wanted.is_none();
    let mut screens = 0_u32;
    let deadline = Instant::now() + Duration::from_secs(60);
    while Instant::now() < deadline {
        let Some(event) = client.recv_timeout(grace) else {
            // Idle for the grace period: whatever was expected has arrived.
            break;
        };
        let ClientEvent::Line(line) = event else {
            break;
        };
        let mut stdout = std::io::stdout();
        let _ = writeln!(stdout, "{}", line.trim());
        let _ = stdout.flush();
        match frames::decode(&line) {
            Some(Inbound::Catalogue { .. }) => catalogue = true,
            Some(Inbound::CommandResult { id, .. }) => {
                if wanted.as_deref() == Some(id.as_str()) {
                    answered = true;
                }
            }
            Some(Inbound::Screen {
                id, kind, payload, ..
            }) => {
                screens += 1;
                let view = nova_tui::state::ScreenView::decode(
                    &id,
                    nova_tui::state::ScreenKind::parse(&kind)
                        .unwrap_or(nova_tui::state::ScreenKind::Markdown),
                    "",
                    &payload,
                );
                if let Err(error) = client.send(frames::screen_result(&id, view.cancel())) {
                    let _ = writeln!(std::io::stderr(), "nova-tui: answer failed: {error}");
                    return ExitCode::FAILURE;
                }
            }
            _ => {}
        }
        if catalogue && answered {
            // Keep listening briefly in case the server asks for a screen after
            // the turn, then stop: the probe is not a UI.
            continue;
        }
    }
    if catalogue && answered {
        let _ = writeln!(std::io::stderr(), "probe: {screens} screen(s) answered");
        return ExitCode::SUCCESS;
    }
    let _ = writeln!(
        std::io::stderr(),
        "probe: missing frames (catalogue={catalogue}, command={answered})"
    );
    ExitCode::FAILURE
}

fn connect_with_retry(address: &str, token: &str, started_here: bool) -> std::io::Result<Client> {
    let deadline = Instant::now() + CONNECT_DEADLINE;
    loop {
        match Client::connect(address, token) {
            Ok(client) => return Ok(client),
            Err(error) => {
                if !started_here || Instant::now() >= deadline {
                    return Err(error);
                }
                // A server we started takes seconds to import Python; retry.
                std::thread::sleep(Duration::from_millis(250));
            }
        }
    }
}

fn run(
    terminal: &mut ratatui::DefaultTerminal,
    client: &Client,
    state: &mut AppState,
) -> std::io::Result<()> {
    let mut theme = Theme::default();
    let base = Theme::default();
    loop {
        state.tick = state.tick.wrapping_add(1);
        // The sprite animates on the client's own clock, so it keeps moving
        // without a server frame per step.
        if let Some(banner) = state.home.as_mut() {
            banner.advance(TICK.as_secs_f64());
        }
        if let Some(sprite) = state.avatar.as_mut() {
            let busy = state.turn_running || !state.streaming.is_empty();
            sprite.advance(TICK.as_secs_f64(), busy);
        }
        terminal.draw(|frame| ui::draw(frame, state, &theme))?;

        if event::poll(TICK)? {
            if let Event::Key(key) = event::read()? {
                if key.kind == KeyEventKind::Press {
                    for effect in handle_key(state, &key) {
                        perform(client, effect)?;
                    }
                }
            }
        }

        while let Some(event) = client.recv_timeout(Duration::ZERO) {
            match event {
                ClientEvent::Line(line) => {
                    if let Some(frame) = frames::decode(&line) {
                        for effect in apply(state, &frame) {
                            perform(client, effect)?;
                        }
                        if let Some(palette) = state.pending_theme.take() {
                            // A @@/theme@@ choice repaints from the next frame; the
                            // transcript itself is untouched.
                            theme = Theme::from_palette(&palette, base);
                            state.status.theme = palette.name;
                        }
                    }
                }
                ClientEvent::Closed(reason) => {
                    state.status.connected = false;
                    state.entries.push(nova_tui::state::Entry {
                        kind: nova_tui::state::BlockKind::Notice {
                            level: "error".to_string(),
                            text: format!("disconnected: {reason}"),
                        },
                        text: reason,
                        agent: "Nova".to_string(),
                    });
                    terminal.draw(|frame| ui::draw(frame, state, &theme))?;
                    return Ok(());
                }
            }
        }

        if state.quit {
            return Ok(());
        }
    }
}

fn perform(client: &Client, effect: Effect) -> std::io::Result<()> {
    match effect {
        Effect::Send(line) => client.send(line),
        Effect::Copy { text, all } => {
            // OSC 52 sets the clipboard through the terminal itself, so no
            // platform crate is needed; a long transcript still fits one escape.
            let what = if all { "conversation" } else { "last response" };
            use crossterm::clipboard::CopyToClipboard;
            let _ = crossterm::execute!(
                std::io::stdout(),
                CopyToClipboard::to_clipboard_from(text.as_bytes())
            );
            let _ = std::io::Write::write_all(
                &mut std::io::stderr(),
                format!(
                    "copied {what} ({} chars)
",
                    text.chars().count()
                )
                .as_bytes(),
            );
            Ok(())
        }
        Effect::Answer { screen_id, value } => {
            if screen_id.is_empty() {
                // Informational screen: nothing to answer, so nothing is sent.
                return Ok(());
            }
            client.send(frames::screen_result(&screen_id, value))
        }
        Effect::Quit | Effect::ClearTranscript => Ok(()),
    }
}

/// Turn one key press into effects (frames to send, state changes).
fn handle_key(state: &mut AppState, key: &KeyEvent) -> Vec<Effect> {
    let mut effects = Vec::new();

    if state.modal.is_some() {
        match key.code {
            KeyCode::Char('y') | KeyCode::Char('Y') => {
                effects.push(Effect::Send(answer(state, "approve")));
            }
            KeyCode::Char('n') | KeyCode::Char('N') => {
                effects.push(Effect::Send(answer(state, "reject")));
            }
            KeyCode::Char('a') | KeyCode::Char('A') => {
                effects.push(Effect::Send(answer(state, "always")));
            }
            KeyCode::Esc => {
                state.modal = None;
                effects.push(Effect::Send(frames::cancel()));
            }
            _ => {}
        }
        return effects;
    }

    // A live terminal takes the keyboard: every printable key is the child's,
    // and escape closes the pane instead of clearing the composer.
    if state.terminal.is_some() {
        if let Some(effect) = ui::terminal::handle_key(state, key) {
            effects.push(effect);
        }
        return effects;
    }

    if state.screen.is_some() {
        if let Some(effect) = ui::screen::handle_key(state, key) {
            effects.push(effect);
        }
        return effects;
    }

    if state.palette.open {
        if let Some(effect) = ui::palette::handle_key(state, key) {
            effects.push(effect);
        }
        return effects;
    }

    match key.code {
        KeyCode::Char('/') if state.composer.is_empty() => {
            // An empty composer turns '/' into the command palette, so a
            // command is discoverable instead of memorised.
            state.palette.open = true;
            state.palette.query.clear();
            state.palette.selected = 0;
            let commands = state.commands.clone();
            state.palette.refilter(&commands);
        }
        KeyCode::Char('n') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            // A new child session: the server owns spawning, the client names it.
            let name = format!("session {}", state.sessions.len() + 1);
            effects.push(Effect::Send(frames::create_session(&name, None)));
        }
        KeyCode::Char('w') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            if let Some(id) = state.active_session.clone() {
                effects.push(Effect::Send(frames::close_session(&id)));
            }
        }
        KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            if state.turn_running {
                effects.push(Effect::Send(frames::cancel()));
            } else {
                state.quit = true;
                effects.push(Effect::Quit);
            }
        }
        KeyCode::Esc => {
            if state.turn_running {
                effects.push(Effect::Send(frames::cancel()));
            }
        }
        KeyCode::Char('o') if key.modifiers.contains(KeyModifiers::CONTROL) => {
            // ctrl+o: expand/collapse every tool card, like the other coding CLIs.
            state.expand_all = !state.expand_all;
        }
        KeyCode::Char(digit @ '1'..='9') if key.modifiers.contains(KeyModifiers::ALT) => {
            // Alt+<n> picks a tab by position, matching the number the tab bar
            // prints; the decision lives in `tabs` so it is testable there.
            if let Some(effect) = ui::tabs::attach_for(state, digit) {
                effects.push(effect);
            }
        }
        KeyCode::Tab => effects.push(Effect::Send(frames::sessions())),
        KeyCode::Up => state.history_previous(),
        KeyCode::Down => state.history_next(),
        KeyCode::Backspace => {
            state.composer.pop();
        }
        KeyCode::Enter => {
            if key.modifiers.contains(KeyModifiers::ALT) {
                state.composer.push('\n');
            } else {
                effects.extend(submit(state));
            }
        }
        KeyCode::Char(ch) => state.composer.push(ch),
        _ => {}
    }
    effects
}

fn submit(state: &mut AppState) -> Vec<Effect> {
    let text = state.composer.trim().to_string();
    if text.is_empty() {
        return Vec::new();
    }
    state.composer.clear();
    state.remember(&text);
    state.scroll = 0;
    state.turn_running = true;
    let id = state.next_request_id();
    if let Some(rest) = text.strip_prefix('/') {
        let mut parts = rest.splitn(2, ' ');
        let name = parts.next().unwrap_or_default().to_string();
        let args = parts.next().map(str::to_string);
        if name == "quit" || name == "exit" {
            state.quit = true;
            return vec![Effect::Quit];
        }
        return vec![Effect::Send(frames::command(&id, &name, args.as_deref()))];
    }
    state.entries.push(nova_tui::state::Entry {
        kind: nova_tui::state::BlockKind::User,
        text: text.clone(),
        agent: "you".to_string(),
    });
    vec![Effect::Send(frames::prompt(&id, &text))]
}

/// Build the interrupt reply for a decision.
fn answer(state: &mut AppState, decision: &str) -> String {
    let Some(Modal::Approval { id, kind, .. }) = state.modal.take() else {
        return frames::cancel();
    };
    let result = match kind.as_str() {
        "plan" => json!({
            "response": {"type": if decision == "reject" { "reject" } else { "accept" }},
            "state_update": {},
        }),
        "question" => json!({"answer": if decision == "reject" { "" } else { "continue" }}),
        _ => json!({
            "decisions": [decision],
            "any_rejected": decision == "reject",
        }),
    };
    frames::interrupt_reply(&id, result)
}
