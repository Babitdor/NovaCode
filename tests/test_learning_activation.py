import json

import pytest

from novacode_cli.hermes.activation import ActivationManager, active_artifacts, content_hash
from novacode_cli.hermes.ledger import LearningLedger, LearningProposal, LearningRecord


def test_approval_is_pending_until_boundary_and_rollback_restores_prior(tmp_path):
    ledger = LearningLedger(tmp_path / "learning.sqlite3")
    manager = ActivationManager(tmp_path, "project")
    proposal = ledger.propose(content="First lesson", project="project")
    assert isinstance(proposal, LearningProposal)
    assert LearningProposal is not LearningRecord
    approved = ledger.transition(proposal.reference, "approve")
    assert approved.approval_reference == proposal.reference
    assert ledger.list(project="project", active=True) == []
    captured = manager.apply_pending()
    assert captured == (("lesson", "", "First lesson"),)
    applied = ledger.list(project="project", active=True)[0]
    assert applied.status == "applied"
    ledger.transition(applied.reference, "rollback")
    assert manager.capture() == captured
    assert manager.apply_pending() == ()
    assert ledger.list()[0].status == "rolled_back"


def test_invalid_target_hash_and_template_do_not_activate(tmp_path, monkeypatch):
    import novacode_cli.prompts as prompts

    source = tmp_path / "templates"
    source.mkdir()
    template = source / "test.jinja"
    template.write_text("{{ value }}")
    monkeypatch.setattr(prompts, "TEMPLATES_DIR", source)
    manager = ActivationManager(tmp_path, "project")
    for target, body, digest in [
        ("../unsafe.jinja", "{{ value }}", ""),
        ("test.jinja", "{{ value }}", "changed"),
        ("test.jinja", "{{ unrelated }}", content_hash(template.read_bytes())),
        ("test.jinja", "{% broken", content_hash(template.read_bytes())),
    ]:
        record = manager.ledger.propose(
            content=body, kind="prompt", target=target, source_hash=digest, project="project"
        )
        manager.ledger.transition(record.reference, "approve")
    assert manager.apply_pending() == ()
    assert {r.status for r in manager.ledger.list()} == {"failed"}


def test_publication_failure_keeps_pending_and_immutable_artifacts_recover(tmp_path, monkeypatch):
    import novacode_cli.hermes.activation as activation

    manager = ActivationManager(tmp_path, "project")
    record = manager.ledger.propose(content="lesson", project="project")
    manager.ledger.transition(record.reference, "approve")
    real_publish = activation.publish

    def fail_manifest(path, content):
        if path == manager.path:
            raise OSError("failed publication")
        real_publish(path, content)

    monkeypatch.setattr(activation, "publish", fail_manifest)
    with pytest.raises(OSError, match="failed publication"):
        manager.apply_pending()
    assert manager.ledger.list()[0].status == "approved"
    assert not manager.path.exists()
    monkeypatch.setattr(activation, "publish", real_publish)
    assert manager.apply_pending() == (("lesson", "", "lesson"),)
    assert manager.apply_pending() == (("lesson", "", "lesson"),)


def test_rollback_conflict_preserves_manual_artifact(tmp_path):
    manager = ActivationManager(tmp_path, "project")
    record = manager.ledger.propose(content="lesson", project="project")
    manager.ledger.transition(record.reference, "approve")
    manager.apply_pending()
    entry = next(iter(json.loads(manager.path.read_text())["entries"].values()))
    from pathlib import Path

    artifact = Path(entry["path"])
    artifact.write_text("manual changes")
    manager.ledger.transition(manager.ledger.list()[0].reference, "rollback")
    before = manager.path.read_bytes()
    with pytest.raises(ValueError, match="conflict"):
        manager.apply_pending()
    assert manager.path.read_bytes() == before
    assert artifact.read_text() == "manual changes"


def test_turn_captures_prompt_configuration_and_candidate_never_renders(tmp_path, monkeypatch):
    import novacode_cli.prompts as prompts
    from jinja2 import Environment, FileSystemLoader

    templates = tmp_path / "package"
    templates.mkdir()
    (templates / "test.jinja").write_text("package {{ value }}")
    monkeypatch.setattr(prompts, "_env", Environment(loader=FileSystemLoader(str(templates))))
    monkeypatch.setattr(prompts, "PROMPT_HISTORY_DIR", tmp_path / "overrides")
    candidate = prompts.PROMPT_HISTORY_DIR / "test" / "candidate.jinja"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("unapproved")
    monkeypatch.setattr(prompts, "_choose_variant", lambda _: "candidate")
    assert prompts.render_template("test.jinja", value="x") == "package x"
    token = active_artifacts.set((("prompt", "test.jinja", "approved {{ value }}"),))
    try:
        assert prompts.render_template("test.jinja", value="x") == "approved x"
        assert prompts.current_variant("test.jinja") is None
    finally:
        active_artifacts.reset(token)


def test_crash_after_manifest_before_ledger_recovers_once(tmp_path, monkeypatch):
    manager = ActivationManager(tmp_path, "project")
    proposal = manager.ledger.propose(content="recover exactly once", project="project")
    manager.ledger.transition(proposal.reference, "approve")
    transition = manager.ledger.transition

    def crash(reference, action, **kwargs):
        if action == "apply":
            raise OSError("crash after activation")
        return transition(reference, action, **kwargs)

    monkeypatch.setattr(manager.ledger, "transition", crash)
    with pytest.raises(OSError, match="crash after activation"):
        manager.apply_pending()
    assert manager.ledger.list()[0].status == "approved"
    manifest_before = manager.path.read_bytes()
    assert manager.capture() == (("lesson", "", "recover exactly once"),)
    monkeypatch.setattr(manager.ledger, "transition", transition)
    manager.apply_pending()
    assert manager.ledger.list()[0].status == "applied"
    assert manager.path.read_bytes() == manifest_before
    assert len(manager.manifest()["operations"]) == 1


def test_skill_versions_rollback_to_previous_and_project_isolation(tmp_path):
    manager = ActivationManager(tmp_path, "project")
    bodies = ["---\nname: generated\ndescription: Safe\n---\n" + value for value in ("one", "two")]
    for body in bodies:
        record = manager.ledger.propose(
            content=body, kind="skill", target="generated", project="project"
        )
        manager.ledger.transition(record.reference, "approve")
        manager.apply_pending()
    assert manager.capture() == (("skill", "generated", bodies[1]),)
    assert ActivationManager(tmp_path, "other").capture() == ()
    applied = next(r for r in manager.ledger.list() if r.content == bodies[1])
    manager.ledger.transition(applied.reference, "rollback")
    assert manager.apply_pending() == (("skill", "generated", bodies[0]),)
    assert next(r for r in manager.ledger.list() if r.content == bodies[1]).status == "rolled_back"


@pytest.mark.parametrize(
    "body",
    [
        "---\nname: [unterminated\ndescription: Broken\n---\nbody",
        "---\nname: other\ndescription: Valid YAML, wrong target\n---\nbody",
        "---\nname: generated\n---\nbody",
        "---\nname: generated\ndescription: [one, two]\n---\nbody",
        "---\nname: [generated]\ndescription: Valid\n---\nbody",
        "---\nname: generated\ndescription: 42\n---\nbody",
    ],
)
def test_malformed_generated_skill_cannot_be_marked_applied(tmp_path, body):
    manager = ActivationManager(tmp_path, "project")
    record = manager.ledger.propose(
        content=body, kind="skill", target="generated", project="project"
    )
    manager.ledger.transition(record.reference, "approve")
    assert manager.apply_pending() == ()
    assert manager.ledger.list()[0].status == "failed"
    assert manager.manifest()["entries"] == {}


@pytest.mark.parametrize("with_hash", [False, True])
def test_existing_skill_requires_matching_reviewed_target_hash(tmp_path, with_hash):
    manager = ActivationManager(tmp_path, "project")
    existing = tmp_path / "skills" / "generated" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("manual skill", encoding="utf-8")
    digest = content_hash(existing.read_bytes()) if with_hash else ""
    proposal = manager.ledger.propose(
        content="---\nname: generated\ndescription: Reviewed\n---\nreplacement",
        kind="skill",
        target="generated",
        source_hash=digest,
        project="project",
    )
    manager.ledger.transition(proposal.reference, "approve")
    if with_hash:
        existing.write_text("manual edit after review", encoding="utf-8")
    assert manager.apply_pending() == ()
    assert manager.ledger.list()[0].status == "failed"
    assert existing.read_text(encoding="utf-8").startswith("manual")


def test_activation_rejects_missing_required_variable_and_artifact_edit(tmp_path, monkeypatch):
    import novacode_cli.prompts as prompts

    templates = tmp_path / "templates"
    templates.mkdir()
    source = templates / "test.jinja"
    source.write_text("{{ value }}", encoding="utf-8")
    monkeypatch.setattr(prompts, "TEMPLATES_DIR", templates)
    manager = ActivationManager(tmp_path, "project")
    missing = manager.ledger.propose(
        content="{{ value }}",
        kind="prompt",
        target="test.jinja",
        project="project",
        source_hash=content_hash(source.read_bytes()),
        required_variables=("approval_id",),
    )
    manager.ledger.transition(missing.reference, "approve")
    assert manager.apply_pending() == ()
    assert manager.ledger.list()[0].status == "failed"

    valid = manager.ledger.propose(
        content="reviewed {{ value }}",
        kind="prompt",
        target="test.jinja",
        project="project",
        source_hash=content_hash(source.read_bytes()),
    )
    manager.ledger.transition(valid.reference, "approve")
    assert manager.apply_pending() == (("prompt", "test.jinja", "reviewed {{ value }}"),)
    entry = next(iter(manager.manifest()["entries"].values()))
    from pathlib import Path

    Path(entry["path"]).write_text("edited outside review", encoding="utf-8")
    with pytest.raises(ValueError, match="Activation conflict"):
        manager.capture()


def test_activation_rejects_unsupported_manifest_version(tmp_path):
    manager = ActivationManager(tmp_path, "project")
    manager.path.parent.mkdir(parents=True)
    manager.path.write_text('{"version":99,"entries":{},"operations":[]}', encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported activation manifest"):
        manager.capture()


def test_unknown_artifact_kind_cannot_be_activated(tmp_path):
    manager = ActivationManager(tmp_path, "project")
    proposal = manager.ledger.propose(content="unrecognized", kind="unknown", project="project")
    manager.ledger.transition(proposal.reference, "approve")
    assert manager.apply_pending() == ()
    assert manager.ledger.list()[0].status == "failed"


def test_published_version_conflict_preserves_manual_edit(tmp_path, monkeypatch):
    from novacode_cli.hermes import activation

    manager = ActivationManager(tmp_path, "project")
    proposal = manager.ledger.propose(content="reviewed", project="project")
    manager.ledger.transition(proposal.reference, "approve")
    publish = activation.publish

    def fail_manifest(path, data):
        if path == manager.path:
            raise OSError("manifest unavailable")
        publish(path, data)

    monkeypatch.setattr(activation, "publish", fail_manifest)
    with pytest.raises(OSError, match="manifest unavailable"):
        manager.apply_pending()
    version = manager.root / "versions" / proposal.id / "2" / "content"
    assert version.read_text(encoding="utf-8") == "reviewed"
    version.write_text("manual edit", encoding="utf-8")
    monkeypatch.setattr(activation, "publish", publish)
    with pytest.raises(ValueError, match="Immutable artifact conflict"):
        manager.apply_pending()
    assert version.read_text(encoding="utf-8") == "manual edit"
    assert manager.ledger.list()[0].status == "approved"


def test_activated_prompt_detects_changed_package_target(tmp_path, monkeypatch):
    import novacode_cli.prompts as prompts

    templates = tmp_path / "templates"
    templates.mkdir()
    target = templates / "test.jinja"
    target.write_text("{{ value }}", encoding="utf-8")
    monkeypatch.setattr(prompts, "TEMPLATES_DIR", templates)
    manager = ActivationManager(tmp_path, "project")
    proposal = manager.ledger.propose(
        content="reviewed {{ value }}",
        kind="prompt",
        target="test.jinja",
        source_hash=content_hash(target.read_bytes()),
        project="project",
    )
    manager.ledger.transition(proposal.reference, "approve")
    assert manager.apply_pending() == (("prompt", "test.jinja", "reviewed {{ value }}"),)
    target.write_text("manually changed {{ value }}", encoding="utf-8")
    with pytest.raises(ValueError, match="prompt target changed"):
        manager.capture()
