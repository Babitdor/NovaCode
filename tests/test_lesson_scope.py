"""Learning must not turn one project's facts into advice for every other.

Found by running the learning loop across unrelated Terminal-Bench tasks with a
shared memory. In thirteen minutes it had written 23 lessons and 14 skills, and
nearly all of it was one task's answer filed as general knowledge
(`build-rust-cpp-polyglot`), the same subject under several names
(`polyglot-rust-cpp` / `rust-cpp-polyglot`, three `setup-python-torch-*`), or an
approach recorded before anything had shown it to work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from novacode_cli.hermes import memory_tiers as mt
from novacode_cli.hermes.skill_manager import SkillManager, _similar_skill

BULLET = "- `apt-get install` needs `apt-get update` first in a fresh container"


def _topics(directory: Path) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.md") if p.name != "INDEX.md")


# ── scope ──────────────────────────────────────────────────────────────────


def test_scope_is_parsed_whatever_the_attribute_order() -> None:
    text = (
        '<lesson topic="apt" scope="general">\n- a b c\n</lesson>'
        '<lesson scope="project" topic="layout">\n- d e f\n</lesson>'
        '<lesson topic="old-style">\n- g h i\n</lesson>'
    )
    lessons = mt.parse_review_response(text)["lessons"]
    assert [(lesson["topic"], lesson["scope"]) for lesson in lessons] == [
        ("apt", "general"),
        ("layout", "project"),
        ("old-style", ""),
    ]


def test_a_lesson_is_private_until_a_second_project_confirms_it(tmp_path: Path) -> None:
    """The review calling a lesson general is a claim; a second project is evidence.

    Told to keep task answers private, the reviewer still marked the internals
    of one task's decoder, and four single-task skills, as general.
    """
    shared = mt.lesson_dir(tmp_path)
    mt.update_from_review(
        tmp_path, "", [{"topic": "apt", "bullets": BULLET, "scope": "general"}], project="task-a"
    )
    assert _topics(mt.lesson_dir(tmp_path, "task-a")) == ["apt"]
    assert _topics(shared) == [], "one project's say-so must not reach the shared pool"

    # A different project arrives at the same fact, in its own words.
    reworded = "- In a bare container `apt-get install` fails until `apt-get update` has run first"
    mt.update_from_review(
        tmp_path, "", [{"topic": "tooling", "bullets": reworded, "scope": "general"}], project="task-b"
    )
    assert _topics(shared) == ["tooling"]
    assert "apt-get update" in (shared / "tooling.md").read_text(encoding="utf-8")


def test_the_same_project_cannot_confirm_itself(tmp_path: Path) -> None:
    for _ in range(3):
        mt.update_from_review(
            tmp_path, "", [{"topic": "apt", "bullets": BULLET, "scope": "general"}], project="task-a"
        )
    assert _topics(mt.lesson_dir(tmp_path)) == []


def test_project_and_unmarked_lessons_are_never_shared(tmp_path: Path) -> None:
    fact = "- `/app/run.py` implements run_tasks with a bounded asyncio semaphore runner"
    for project in ("task-a", "task-b"):
        for scope in ("project", ""):
            mt.update_from_review(
                tmp_path, "", [{"topic": "run-tasks", "bullets": fact, "scope": scope}], project=project
            )
    assert _topics(mt.lesson_dir(tmp_path)) == []
    assert _topics(mt.lesson_dir(tmp_path, "task-a")) == ["run-tasks"]


def test_only_the_confirmed_bullets_of_a_lesson_are_shared(tmp_path: Path) -> None:
    mt.update_from_review(
        tmp_path, "", [{"topic": "apt", "bullets": BULLET, "scope": "general"}], project="task-a"
    )
    task_answer = "- the decoder computes `fraction = fraction*255 + (b-1)` on every renorm step"
    both = f"{BULLET}\n{task_answer}"
    mt.update_from_review(
        tmp_path, "", [{"topic": "apt", "bullets": both, "scope": "general"}], project="task-b"
    )
    body = (mt.lesson_dir(tmp_path) / "apt.md").read_text(encoding="utf-8")
    assert "apt-get update" in body and "fraction" not in body


# ── the same fact in other words ───────────────────────────────────────────


def test_a_reworded_fact_is_not_recorded_twice(tmp_path: Path) -> None:
    """One run filed this lesson four times in one topic, each worded differently."""
    for wording in (
        "- `apt-get install` reports 404 \"Failed to fetch\" on stale package URLs in a fresh container until `apt-get update` has run first.",
        "- In a bare container, `apt-get install` fails with 404/`Unable to locate package` until `apt-get update` has run first.",
        "- In a fresh container, `apt-get install` can fail with 404 \"Failed to fetch\" on package URLs; running `apt-get update` first fixes it.",
    ):
        mt.record_lesson(tmp_path, "tooling", wording)
    body = (mt.lesson_dir(tmp_path) / "tooling.md").read_text(encoding="utf-8")
    assert len(mt._bullets(body)) == 1, body


def test_different_facts_about_the_same_tool_are_all_kept(tmp_path: Path) -> None:
    for fact in (
        "- `dist.all_gather` is NOT autograd-aware: gathered tensors have no grad_fn and backward raises",
        "- `all_gather` requires contiguous inputs; call `.contiguous()` first",
        "- `dist.all_reduce` with ReduceOp.SUM is differentiable and propagates gradients correctly",
    ):
        mt.record_lesson(tmp_path, "torch-distributed", fact)
    body = (mt.lesson_dir(tmp_path) / "torch-distributed.md").read_text(encoding="utf-8")
    assert len(mt._bullets(body)) == 3, body


def test_without_a_project_everything_is_general_as_before(tmp_path: Path) -> None:
    mt.update_from_review(tmp_path, "", [{"topic": "x", "bullets": BULLET, "scope": "project"}])
    assert _topics(mt.lesson_dir(tmp_path)) == ["x"]


def test_project_keys_separate_sandboxes_and_same_named_folders(tmp_path: Path) -> None:
    assert mt.project_memory_key("/app", "task-a__1") != mt.project_memory_key("/app", "task-b__2")
    assert mt.project_memory_key("/app", "task-a__1").startswith("sandbox-")
    one, two = tmp_path / "x" / "backend", tmp_path / "y" / "backend"
    assert mt.project_memory_key(one) != mt.project_memory_key(two)
    assert mt.project_memory_key(one) == mt.project_memory_key(one)
    assert mt.project_memory_key(one).startswith("backend-")
    assert mt.project_memory_key(None) == ""


def test_a_project_reads_its_own_lessons_and_not_another_projects(tmp_path: Path) -> None:
    from novacode_cli.memory.agent_memory import AgentMemoryMiddleware

    mt.record_lesson(tmp_path, "apt", BULLET)
    mt.record_lesson(tmp_path, "layout-a", "- project A keeps code in `/app/src`", project="proj-a")
    mt.record_lesson(tmp_path, "layout-b", "- project B keeps code in `/srv/code`", project="proj-b")

    def corpus(project: str) -> set[str]:
        mw = AgentMemoryMiddleware.__new__(AgentMemoryMiddleware)
        mw.agent_dir, mw._project_key = tmp_path, project
        mw._corpus_cache, mw._corpus_sig = None, None
        return set(mw._load_memory_corpus())

    assert corpus("proj-a") == {"apt", "layout-a"}
    assert corpus("proj-b") == {"apt", "layout-b"}
    assert corpus("") == {"apt"}


# ── duplicate topics ───────────────────────────────────────────────────────


def test_a_reworded_topic_name_lands_in_the_existing_file(tmp_path: Path) -> None:
    mt.record_lesson(tmp_path, "polyglot-rust-cpp", "- first fact about the polyglot")
    mt.record_lesson(tmp_path, "rust-cpp-polyglot", "- second fact about the polyglot")
    assert _topics(mt.lesson_dir(tmp_path)) == ["polyglot-rust-cpp"]
    body = (mt.lesson_dir(tmp_path) / "polyglot-rust-cpp.md").read_text(encoding="utf-8")
    assert "first fact" in body and "second fact" in body


def test_related_but_different_topics_stay_apart(tmp_path: Path) -> None:
    mt.record_lesson(tmp_path, "build-and-test", "- the build is run with make")
    mt.record_lesson(tmp_path, "build-and-deploy", "- deploys go through the release script")
    assert _topics(mt.lesson_dir(tmp_path)) == ["build-and-deploy", "build-and-test"]


def test_the_reviewer_is_shown_the_topics_it_can_reuse(tmp_path: Path) -> None:
    mt.record_lesson(tmp_path, "apt", BULLET)
    mt.record_lesson(tmp_path, "layout", "- code lives in `/app/src`", project="proj-a")
    mt.record_lesson(tmp_path, "other", "- something about project B", project="proj-b")
    assert set(mt.existing_topics(tmp_path, "proj-a")) == {"apt", "layout"}

    from novacode_cli.prompts import render_template

    text = render_template(
        "nova_review.jinja",
        tool_call_count=10,
        prior_lessons="",
        recovered_from_error=False,
        clean_win=False,
        existing_topics="apt, layout",
    )
    assert "Topics already on file: apt, layout" in text


# ── outcome ────────────────────────────────────────────────────────────────


def test_the_review_only_records_what_was_shown_to_work() -> None:
    from novacode_cli.prompts import render_template

    text = render_template(
        "nova_review.jinja",
        tool_call_count=10,
        prior_lessons="",
        recovered_from_error=False,
        clean_win=False,
        existing_topics="",
    )
    assert "only if a check in this window actually passed" in text
    assert "do NOT write down your current approach as if it" in text
    assert 'scope="general|project"' in text
    assert "the solution to one specific problem" in text


# ── skills ─────────────────────────────────────────────────────────────────

_SKILL = (
    "<skill><name>{name}</name>{scope}"
    "<description>Use when doing the thing.</description>"
    "<body>## When to Use\nx\n## Procedure\n1. do it\n</body></skill>"
)


def _create(tmp_path: Path, project: str, name: str, scope: str = "") -> list[str]:
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    manager = SkillManager(None, skills_dir=skills, enabled=True, project=project)  # type: ignore[arg-type]
    manager._log_refinement = lambda *a, **k: None  # no ledger in this test
    asyncio.run(manager.maybe_create_from_review(_SKILL.format(name=name, scope=scope)))
    return sorted(p.name for p in skills.iterdir() if p.is_dir())


GENERAL = "<scope>general</scope>"


def test_a_sandbox_never_keeps_a_skill_it_did_not_call_general(tmp_path: Path) -> None:
    assert _create(tmp_path, "sandbox-task-a", "build-rust-cpp-polyglot") == []
    assert _create(tmp_path, "sandbox-task-a", "build-x", "<scope>project</scope>") == []


def test_a_general_skill_waits_for_a_second_sandbox_to_propose_it(tmp_path: Path) -> None:
    """`write-arithmetic-coder-encoder` was marked general; only one task ever wanted it."""
    assert _create(tmp_path, "sandbox-task-a", "setup-python-torch-bare-container", GENERAL) == []
    # The same sandbox proposing it again is not a second opinion.
    assert _create(tmp_path, "sandbox-task-a", "setup-python-torch-bare-container", GENERAL) == []
    # A different sandbox wanting much the same skill is.
    kept = _create(tmp_path, "sandbox-task-b", "setup-python-torch-container", GENERAL)
    assert kept == ["setup-python-torch-container"]


def test_an_unrelated_general_skill_does_not_ride_on_another(tmp_path: Path) -> None:
    _create(tmp_path, "sandbox-task-a", "setup-python-torch-bare-container", GENERAL)
    assert _create(tmp_path, "sandbox-task-b", "write-arithmetic-coder-encoder", GENERAL) == []


def test_a_real_project_still_gets_its_project_skills(tmp_path: Path) -> None:
    assert _create(tmp_path, "backend-1a2b3c4d", "add-tui-slash-command") == ["add-tui-slash-command"]


def test_a_reworded_skill_name_is_not_a_second_skill(tmp_path: Path) -> None:
    _create(tmp_path, "backend-1a2b3c4d", "setup-python-torch-container")
    after = _create(tmp_path, "backend-1a2b3c4d", "setup-python-torch-env")
    assert after == ["setup-python-torch-container"]

    skills = tmp_path / "skills"
    assert _similar_skill(skills, "setup-python-torch-env") == "setup-python-torch-container"
    assert _similar_skill(skills, "setup-python-torch-container") is None  # itself: the update path
    assert _similar_skill(skills, "add-tui-slash-command") is None


def test_a_confirmed_fact_is_shared_once_whatever_topic_it_arrives_under(tmp_path: Path) -> None:
    """Seen live: one `apt-get update` lesson landed in the shared pool under two topics."""
    wordings = {
        "task-a": ("apt", BULLET),
        "task-b": ("apt-in-containers", "- `apt-get install` reports \"Unable to locate package\" in a fresh container until `apt-get update` has run first"),
        "task-c": ("bare-container-python", "- In a fresh container `apt-get install python3` fails with \"Unable to locate package\" until `apt-get update` has run first"),
    }
    for project, (topic, bullet) in wordings.items():
        mt.update_from_review(
            tmp_path, "", [{"topic": topic, "bullets": bullet, "scope": "general"}], project=project
        )
    shared = [
        b
        for f in mt.lesson_dir(tmp_path).glob("*.md")
        if f.name != "INDEX.md"
        for b in mt._bullets(f.read_text(encoding="utf-8"))
    ]
    assert len(shared) == 1, shared
