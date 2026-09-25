"""Every SessionState field used by Nova's own code must be declared —
visible on a fresh instance without falling through to the _dynamic dict.

If a declaration is removed from SessionState while a module still uses the
field, this test fails (the name would silently land in _dynamic again).
"""

from __future__ import annotations

from novacode_cli.states.Session import SessionState

# Field -> the Nova module(s) that use it. Plain instance attributes declared
# in SessionState.__init__.
CONCRETE_FIELDS = [
    "thread_id",
    "session_id",
    "is_continued",
    "todos",
    "steering_instructions",
    "headless",  # main.py, headless/runner.py
    "headless_prompt",
    "headless_output_format",
    "headless_max_turns",
    "headless_deny_tools",
    "headless_out_fd",
    "headless_exit_code",
    "workspace_root",  # commands/log_commands.py, tui/app.py
    "verify_enabled",  # ui/execution.py
    "active_goal",  # commands/side_commands.py, core/agent_loop.py
    "active_rubric",
    "browser_use_tasks",  # commands/browser_use_handler.py
    "create_server",  # commands/create_handler.py, tui/app.py
    "_cron_scheduler",  # commands/cron_handler.py, main.py
    "_webhook_server",  # commands/webhook_handler.py
    "_ralph_stop_requested",  # commands/ralph_handler.py, tui/app.py
    "_ralph_checkpoint_requested",
    "_background_threads",  # commands/ralph_handler.py
    "_voice_pipeline",  # main.py, tui/app.py
    "_remote_tool_notify",  # remote/processor.py, ui/execution.py
    "_remote_todo_notify",
    "_remote_processor_task",  # main.py
    "_pending_approvals",
    "_wiki",
]

# Fields served by @property descriptors delegating to a state slice.
PROPERTY_FIELDS = [
    "auto_approve", "no_splash", "plan_mode_enabled", "verbose",
    "exit_hint_until", "exit_hint_handle",
    "token_tracker", "plan_agent", "plan_backend", "plan_content",
    "approved_plan_content",
    "_agent", "_backend", "_checkpointer", "_store", "_tools",
    "_assistant_id", "_model", "_sandbox_type",
    "_remote_message_queue", "_remote_message_lock", "_remote_bridge_manager",
    "_pre_remote_auto_approve", "_image_tracker", "_seen_message_ids",
    "_composite_backend", "_console",
    "background_ralph_tasks", "trello_server",
    "notifications",
    "wiki_root", "wiki_enabled", "wiki_page_count", "wiki_last_ingest",
]


def test_all_internal_fields_declared_without_dynamic():
    ss = SessionState()

    # Nothing internal may leak into the dynamic-fallback dict at construction.
    assert ss._dynamic == {}

    for name in CONCRETE_FIELDS:
        assert name in vars(ss), f"{name} not a concrete attr on SessionState()"

    for name in PROPERTY_FIELDS:
        assert isinstance(
            getattr(type(ss), name, None), property
        ), f"{name} is not a @property on SessionState"
        getattr(ss, name)  # readable on a fresh instance

    # Writes to declared fields must stay out of _dynamic.
    ss.active_goal = "goal"
    ss.wiki_page_count = 3
    assert ss._dynamic == {}
    assert ss.active_goal == "goal"
    assert ss.wiki_page_count == 3

    # The plugin escape hatch still works.
    ss.some_plugin_field = 42
    assert ss.some_plugin_field == 42
    assert ss._dynamic == {"some_plugin_field": 42}
