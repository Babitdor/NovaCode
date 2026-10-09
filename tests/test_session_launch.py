import pytest

from novacode_cli.sessions.launch import prepare_launch


def test_prepare_launch_resolves_relative_path_and_defaults_name(tmp_path):
    project = tmp_path / "Prøject With Spaces"
    project.mkdir()

    request = prepare_launch(project.name, None, "inspect tests", str(tmp_path))

    assert request["folder"] == str(project.resolve())
    assert request["name"] == project.name
    assert request["task"] == "inspect tests"
    assert len(request["request_id"]) == 12


def test_prepare_launch_rejects_missing_directory(tmp_path):
    with pytest.raises(FileNotFoundError):
        prepare_launch("missing", None, "", str(tmp_path))


def test_prepare_launch_rejects_file(tmp_path):
    file = tmp_path / "project.txt"
    file.write_text("not a folder")
    with pytest.raises(ValueError, match="Not a directory"):
        prepare_launch(str(file), None, "", str(tmp_path))
