"""Task discipline: todo recitation, the done gate, the protocol, empty results."""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from novacode_cli.agents import task_discipline as td
from novacode_cli.agents.task_discipline import DONE_GATE_MARKER, TaskDisciplineMiddleware

TODOS = [
    {"content": "Reproduce the bug", "status": "completed"},
    {"content": "Fix parse_date", "status": "in_progress"},
    {"content": "test_dates passes", "status": "pending"},
]


def _write_todos_call() -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": "w", "name": "write_todos", "args": {}}])


class _Request(SimpleNamespace):
    def override(self, **kw):  # noqa: ANN003, ANN201
        return _Request(**{**self.__dict__, **kw})


def _recited(messages: list, todos: list | None = TODOS) -> str:
    req = _Request(
        state={"todos": todos}, messages=messages, system_message=SystemMessage(content="BASE")
    )
    out = TaskDisciplineMiddleware().wrap_model_call(req, lambda r: r)
    return "".join(b.get("text", "") for b in out.system_message.content_blocks)


# ── recitation ─────────────────────────────────────────────────────────────


def test_plan_is_recited_once_it_scrolls_out_of_view() -> None:
    old = [_write_todos_call()] + [
        HumanMessage(content=str(i)) for i in range(td.RECITE_AFTER_MESSAGES)
    ]
    text = _recited(old)
    assert text.startswith("BASE"), "the recitation must come AFTER the cached prompt"
    assert "[>] Fix parse_date" in text and "[ ] test_dates passes" in text


def test_plan_in_recent_view_is_not_repeated() -> None:
    """Re-showing it every call would re-bill the cached conversation."""
    assert _recited([HumanMessage(content="go"), _write_todos_call()]) == "BASE"


def test_nothing_to_recite_without_open_items() -> None:
    done = [dict(t, status="completed") for t in TODOS]
    assert _recited([HumanMessage(content="go")], todos=done) == "BASE"
    assert _recited([HumanMessage(content="go")], todos=None) == "BASE"


# ── done gate ──────────────────────────────────────────────────────────────


def _gate(messages: list, todos: list | None = TODOS):  # noqa: ANN202
    return TaskDisciplineMiddleware(verify_after_edit=True).after_model(
        {"messages": messages, "todos": todos}, None
    )


def test_stopping_with_open_todos_sends_the_agent_back() -> None:
    out = _gate([HumanMessage(content="fix it"), AIMessage(content="Done!")])
    assert out["jump_to"] == "model"
    nudge = out["messages"][0].content
    assert nudge.startswith(DONE_GATE_MARKER) and "2 todo(s)" in nudge


def test_the_gate_fires_once_per_turn() -> None:
    history = [
        HumanMessage(content="fix it"),
        AIMessage(content="Done!"),
        HumanMessage(content=f"{DONE_GATE_MARKER} ..."),
        AIMessage(content="Blocked on credentials."),
    ]
    assert _gate(history) is None, "a real blocker must still be able to end the turn"
    # A new user turn re-arms it.
    assert _gate([*history, HumanMessage(content="next"), AIMessage(content="ok")]) is not None


def test_the_gate_ignores_work_in_progress_and_finished_lists() -> None:
    assert _gate([HumanMessage(content="x"), _write_todos_call()]) is None
    done = [dict(t, status="completed") for t in TODOS]
    assert _gate([HumanMessage(content="x"), AIMessage(content="Done")], todos=done) is None


def test_the_nudge_is_hidden_from_the_transcript() -> None:
    from novacode_cli.core.streaming import is_internal_context_text

    assert is_internal_context_text(DONE_GATE_MARKER + " You ended your turn")


def _tool_step(
    name: str, call_id: str, args: dict, result: str = "ok", *, status: str = "success"
) -> list[AIMessage | ToolMessage]:
    return [
        AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": args}]),
        ToolMessage(content=result, tool_call_id=call_id, status=status),
    ]


def test_edit_without_later_check_gets_one_verification_reminder() -> None:
    history = [HumanMessage(content="fix it"), *_tool_step("edit_file", "e", {"file_path": "x.py"})]
    out = _gate([*history, AIMessage(content="Fixed")], todos=None)
    assert out["jump_to"] == "model"
    assert out["messages"][0].content.startswith(td.VERIFY_GATE_MARKER)
    assert "passed" not in out["messages"][0].content
    assert "already ran" in out["messages"][0].content
    assert "cite the command and its result" in out["messages"][0].content

    history += [AIMessage(content="Fixed"), *out["messages"]]
    assert _gate([*history, AIMessage(content="Cannot run tests here")], todos=None) is None
    assert (
        _gate(
            [
                HumanMessage(content="fix another"),
                *_tool_step("write_file", "e2", {}),
                AIMessage(content="Done"),
            ],
            todos=None,
        )
        is not None
    )


def test_edit_reminder_is_off_by_default() -> None:
    history = [HumanMessage(content="fix"), *_tool_step("write_file", "e", {})]
    assert (
        TaskDisciplineMiddleware().after_model(
            {"messages": [*history, AIMessage(content="Done")], "todos": None}, None
        )
        is None
    )


def test_check_must_follow_last_successful_edit() -> None:
    check = _tool_step("bash", "t", {"command": "python -m pytest tests/test_x.py"})
    edit = _tool_step("write_file", "e", {"file_path": "x.py"})
    start = [HumanMessage(content="fix it")]
    assert _gate([*start, *check, *edit, AIMessage(content="Done")], todos=None)
    assert _gate([*start, *edit, *check, AIMessage(content="Done")], todos=None) is None


def test_failed_check_after_edit_does_not_satisfy_verification() -> None:
    history = [
        HumanMessage(content="fix it"),
        *_tool_step("edit_file", "e", {"file_path": "x.py"}),
        AIMessage(
            content="",
            tool_calls=[{"id": "t", "name": "bash", "args": {"command": "pytest test_x.py"}}],
        ),
        ToolMessage(
            content="1 failed", tool_call_id="t", artifact={"exit_code": 1}, status="success"
        ),
        AIMessage(content="Done"),
    ]
    out = _gate(history, todos=None)
    assert out is not None
    assert out["messages"][0].content.startswith(td.VERIFY_GATE_MARKER)
    assert "failed" in out["messages"][0].content
    assert _gate([*history, *out["messages"], AIMessage(content="Blocked")], todos=None) is None


def test_unpaired_check_does_not_satisfy_verification() -> None:
    history = [
        HumanMessage(content="fix it"),
        *_tool_step("edit_file", "e", {"file_path": "x.py"}),
        AIMessage(
            content="",
            tool_calls=[{"id": "t", "name": "bash", "args": {"command": "pytest test_x.py"}}],
        ),
        AIMessage(content="Done"),
    ]
    assert _gate(history, todos=None) is not None


def test_successful_check_after_failed_check_clears_reminder() -> None:
    history = [
        HumanMessage(content="fix it"),
        *_tool_step("edit_file", "e", {"file_path": "x.py"}),
        AIMessage(
            content="",
            tool_calls=[{"id": "bad", "name": "bash", "args": {"command": "pytest test_x.py"}}],
        ),
        ToolMessage(content="1 failed", tool_call_id="bad", artifact={"exit_code": 1}),
        *_tool_step("bash", "good", {"command": "pytest test_x.py"}, "1 passed"),
        AIMessage(content="Done"),
    ]
    assert _gate(history, todos=None) is None


def test_shell_failure_wrapper_is_treated_as_failed_check() -> None:
    history = [
        HumanMessage(content="fix it"),
        *_tool_step("edit_file", "e", {"file_path": "x.py"}),
        *_tool_step(
            "execute",
            "t",
            {"command": "pytest test_x.py"},
            "FAILED test_x.py\n[Command failed with exit code 1]",
        ),
        AIMessage(content="Done"),
    ]
    out = _gate(history, todos=None)
    assert out is not None
    assert "failed" in out["messages"][0].content


def test_mentioning_a_test_tool_does_not_count_as_running_it() -> None:
    history = [
        HumanMessage(content="fix"),
        *_tool_step("edit_file", "e", {"file_path": "x.py"}),
        *_tool_step("bash", "install", {"command": "pip install pytest"}),
        AIMessage(content="Done"),
    ]
    assert _gate(history, todos=None)["messages"][0].content.startswith(td.VERIFY_GATE_MARKER)


def test_failed_edit_and_non_file_work_do_not_trigger_verification() -> None:
    start = [HumanMessage(content="help")]
    failed = _tool_step("edit_file", "e", {}, "path not found", status="error")
    assert _gate([*start, *failed, AIMessage(content="Blocked")], todos=None) is None
    read = _tool_step("read_file", "r", {"file_path": "x.py"})
    assert _gate([*start, *read, AIMessage(content="Explained")], todos=None) is None


def test_verification_reminder_is_hidden_from_transcript() -> None:
    from novacode_cli.core.streaming import is_internal_context_text

    assert is_internal_context_text(td.VERIFY_GATE_MARKER + " Run a check")


def test_verification_reminder_runs_in_an_agent_graph() -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool

    class _Model(GenericFakeChatModel):
        def bind_tools(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
            return self

    @tool
    def edit_file(file_path: str) -> str:
        """Edit one file in the scripted replay."""
        return f"updated {file_path}"

    @tool
    def bash(command: str) -> str:
        """Run a check in the scripted replay."""
        return f"{command}: 1 passed"

    model = _Model(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "e", "name": "edit_file", "args": {"file_path": "x.py"}}],
                ),
                AIMessage(content="Done"),
                AIMessage(
                    content="",
                    tool_calls=[
                        {"id": "t", "name": "bash", "args": {"command": "pytest tests/test_x.py"}}
                    ],
                ),
                AIMessage(content="Checked and done"),
            ]
        )
    )
    result = create_agent(
        model,
        tools=[edit_file, bash],
        middleware=[TaskDisciplineMiddleware(verify_after_edit=True)],
    ).invoke({"messages": [HumanMessage(content="Fix x.py")]})
    contents = [str(message.content) for message in result["messages"]]
    assert any(content.startswith(td.VERIFY_GATE_MARKER) for content in contents)
    assert contents[-1] == "Checked and done"


def test_failed_check_recovery_runs_in_an_agent_graph() -> None:
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.tools import tool

    class _Model(GenericFakeChatModel):
        def bind_tools(self, *_a, **_k) -> _Model:  # noqa: ANN002, ANN003
            return self

    @tool
    def edit_file(file_path: str) -> str:
        """Edit one file in the scripted replay."""
        return f"updated {file_path}"

    @tool
    def bash(command: str) -> str:
        """Run a check in the scripted replay."""
        return "[Command failed with exit code 1]" if "first" in command else "1 passed"

    model = _Model(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[{"id": "e", "name": "edit_file", "args": {"file_path": "x.py"}}],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"id": "t1", "name": "bash", "args": {"command": "pytest first"}}],
                ),
                AIMessage(content="Done"),
                AIMessage(
                    content="",
                    tool_calls=[{"id": "t2", "name": "bash", "args": {"command": "pytest second"}}],
                ),
                AIMessage(content="Checked and done"),
            ]
        )
    )
    result = create_agent(
        model,
        tools=[edit_file, bash],
        middleware=[TaskDisciplineMiddleware(verify_after_edit=True)],
    ).invoke({"messages": [HumanMessage(content="Fix x.py")]})
    contents = [str(message.content) for message in result["messages"]]
    assert sum(content.startswith(td.VERIFY_GATE_MARKER) for content in contents) == 1
    assert contents[-1] == "Checked and done"


def test_the_gate_really_jumps_back_in_an_agent_graph() -> None:
    """hook_config(can_jump_to) wiring, end to end through langchain's graph."""
    from langchain.agents import create_agent
    from langchain.agents.middleware import TodoListMiddleware
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    class _Model(GenericFakeChatModel):
        def bind_tools(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
            return self

    plan = AIMessage(
        content="",
        tool_calls=[{"id": "w1", "name": "write_todos", "args": {"todos": TODOS}}],
    )
    model = _Model(
        messages=iter([plan, AIMessage(content="Done!"), AIMessage(content="Finished all.")])
    )
    # As in Nova's stack, `todos` enters state only through the write_todos tool.
    agent = create_agent(model, middleware=[TodoListMiddleware(), TaskDisciplineMiddleware()])
    result = agent.invoke({"messages": [HumanMessage(content="fix it")]})
    contents = [m.content for m in result["messages"]]
    assert any(c.startswith(DONE_GATE_MARKER) for c in contents), contents
    assert contents[-1] == "Finished all."


# ── truncation gate ────────────────────────────────────────────────────────
# A turn with no tool call that is empty, or stopped at the output limit, did not
# finish. Ending there drops the task silently (Terminal-Bench: a capped
# reasoning block returned nothing and the trial scored 0 with work half done).


def _cut_off(reason: str = "length", content: str = "Let me reason about this") -> AIMessage:
    return AIMessage(content=content, response_metadata={"done_reason": reason})


def test_an_empty_turn_sends_the_agent_back() -> None:
    out = _gate([HumanMessage(content="go"), AIMessage(content="")], todos=None)
    assert out["jump_to"] == "model"
    assert out["messages"][0].content.startswith(td.CUT_OFF_MARKER)


def test_a_turn_stopped_at_the_output_limit_sends_the_agent_back() -> None:
    out = _gate([HumanMessage(content="go"), _cut_off()], todos=None)
    assert out["jump_to"] == "model"
    # finish_reason is the OpenAI-style spelling of the same thing.
    other = AIMessage(content="cut", response_metadata={"finish_reason": "length"})
    assert _gate([HumanMessage(content="go"), other], todos=None) is not None


def test_a_turn_that_ended_naturally_is_left_alone() -> None:
    done = AIMessage(content="All set.", response_metadata={"done_reason": "stop"})
    assert _gate([HumanMessage(content="go"), done], todos=None) is None
    # No metadata at all (many providers) must not look like truncation.
    assert _gate([HumanMessage(content="go"), AIMessage(content="All set.")], todos=None) is None


def test_a_capped_turn_that_still_called_a_tool_is_left_alone() -> None:
    """Hitting the cap mid-tool-call is fine: the tool result continues the turn."""
    call = AIMessage(
        content="",
        tool_calls=[{"id": "c", "name": "ls", "args": {}}],
        response_metadata={"done_reason": "length"},
    )
    assert _gate([HumanMessage(content="go"), call], todos=None) is None


def test_the_truncation_gate_has_a_per_turn_budget() -> None:
    history: list = [HumanMessage(content="go")]
    for _ in range(td.MAX_CUT_OFF_NUDGES):
        history += [_cut_off(), HumanMessage(content=f"{td.CUT_OFF_MARKER} ...")]
    history.append(_cut_off())
    assert _gate(history, todos=None) is None, "must not nudge forever"
    # A new user turn re-arms it.
    assert _gate([*history, HumanMessage(content="again"), _cut_off()], todos=None) is not None


def test_doing_real_work_re_arms_the_truncation_budget() -> None:
    """The budget is for cut-offs IN A ROW, not for a whole unattended run.

    A Terminal-Bench trial hit the cap four times across 129 turns with
    productive work between each; counted per user turn, the fourth ended the
    trial with minutes left on the clock.
    """
    history: list = [HumanMessage(content="go")]
    for _ in range(td.MAX_CUT_OFF_NUDGES):
        history += [_cut_off(), HumanMessage(content=f"{td.CUT_OFF_MARKER} ...")]
    history += [
        AIMessage(content="", tool_calls=[{"id": "c", "name": "bash", "args": {}}]),
        ToolMessage(content="ok", tool_call_id="c"),
        _cut_off(),
    ]
    out = _gate(history, todos=None)
    assert out is not None and out["jump_to"] == "model"


def test_an_internal_context_message_does_not_re_arm_the_budget() -> None:
    """Another middleware's `Internal context` message is not a user turn."""
    from novacode_cli.skills.refreshing_middleware import SUGGESTION_MARKER

    history: list = [HumanMessage(content="go")]
    for _ in range(td.MAX_CUT_OFF_NUDGES):
        history += [_cut_off(), HumanMessage(content=f"{td.CUT_OFF_MARKER} ...")]
    history += [HumanMessage(content=f"{SUGGESTION_MARKER} ..."), _cut_off()]
    assert _gate(history, todos=None) is None


def test_the_truncation_nudge_is_hidden_from_the_transcript() -> None:
    from novacode_cli.core.streaming import is_internal_context_text

    assert is_internal_context_text(td.CUT_OFF_MARKER + " Your last response was empty")


# ── the protocol in the prompt ──────────────────────────────────────────────


def test_the_end_state_must_be_checked_and_left_clean() -> None:
    """Working code is not the whole job.

    On Terminal-Bench, `polyglot-rust-c` produced a correct answer and still
    scored 0: the agent left the binaries it had compiled next to the source,
    and the task required that directory to hold only the one source file.
    """
    from novacode_cli.prompts import render_template

    text = render_template("core_agent_system.jinja")
    assert "Understand → Change → Verify → Leave it clean." in text
    assert "which files should and should not exist" in text
    for scratch in ("compiled binaries", "temp scripts", "debug output"):
        assert scratch in text, scratch


def test_numeric_requirements_guidance_reaches_a_model_in_an_agent_graph() -> None:
    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentMiddleware
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from novacode_cli.prompts import render_template

    class _Capture(AgentMiddleware):
        seen: str = ""

        def wrap_model_call(self, request, handler) -> object:  # noqa: ANN001
            self.seen = str(request.system_message.content)
            return handler(request)

    capture = _Capture()
    model = GenericFakeChatModel(messages=iter([AIMessage(content="Done")]))
    result = create_agent(
        model,
        middleware=[capture],
        system_prompt=render_template("core_agent_system.jinja"),
    ).invoke({"messages": [HumanMessage(content="Compute the inclusive amount")]})
    assert result["messages"][-1].content == "Done"
    assert "what each source number includes or excludes" in capture.seen
    assert "check the final aggregate" in capture.seen
    assert "ambiguous items against the full category definition" in capture.seen


def test_unverifiable_is_a_last_resort_not_an_exit() -> None:
    """ "I cannot verify" must come after trying to get the means, not instead.

    A Terminal-Bench trial ran `apt-get install python3 >/dev/null 2>&1` once,
    never saw why it failed, declared the sandbox had "no network/apt access"
    and shipped untested code 49 seconds into a 15-minute budget. An earlier run
    had installed Python in that same image.
    """
    from novacode_cli.prompts import render_template

    text = render_template("core_agent_system.jinja")
    assert "get them first" in text
    assert "never assume there is no network or package manager" in text
    assert "If you cannot verify, say so plainly." not in text


def test_todos_no_longer_stall_for_approval() -> None:
    from novacode_cli.prompts import render_template

    text = render_template("core_agent_system.jinja")
    assert "without stopping for approval" in text
    assert "3+ steps → write todos, present plan, **wait for approval**" not in text
    assert "Understand → Change → Verify" in text
    for size in ("Trivial", "Standard", "Large or risky"):
        assert size in text


# ── empty tool results ──────────────────────────────────────────────────────


def test_empty_tool_results_are_filled_before_the_model_sees_them() -> None:
    from novacode_cli.agents.tool_call_repair import EMPTY_TOOL_RESULT, RepairToolCallsEachStep

    call = AIMessage(content="", tool_calls=[{"id": "c", "name": "serena_read_file", "args": {}}])
    empty = ToolMessage(content=[{"type": "text", "text": ""}], tool_call_id="c", id="t")
    out = RepairToolCallsEachStep().before_model(
        {"messages": [HumanMessage(content="x"), call, empty]}, None
    )
    assert out["messages"][-1].content == EMPTY_TOOL_RESULT
    assert out["messages"][-1].tool_call_id == "c"


def test_a_healthy_history_is_left_alone() -> None:
    from novacode_cli.agents.tool_call_repair import RepairToolCallsEachStep

    call = AIMessage(content="", tool_calls=[{"id": "c", "name": "ls", "args": {}}])
    ok = ToolMessage(content="a.py", tool_call_id="c")
    assert RepairToolCallsEachStep().before_model({"messages": [call, ok]}, None) is None
