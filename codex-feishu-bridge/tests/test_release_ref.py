import json
from pathlib import Path
import runpy
import subprocess

import pytest


verify = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/verify-release-ref.py")
)["verify"]


@pytest.fixture
def published(tmp_path):
    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Tester",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "tag.gpgsign=false",
                *args,
            ],
            text=True,
        ).strip()

    git("init", "-q")
    (tmp_path / "deploy").mkdir()
    (tmp_path / "codex-feishu-bridge").mkdir()
    spec = {
        "tag": "bridge-v0.1.0-rc.1",
        "bridge_version": "0.1.0rc1",
        "codex_version": "0.0.1",
    }
    (tmp_path / "deploy/stable.json").write_text(json.dumps(spec))
    (tmp_path / "codex-feishu-bridge/pyproject.toml").write_text(
        '[project]\nversion = "0.1.0rc1"\n'
    )
    git("add", "--", "deploy", "codex-feishu-bridge")
    git("commit", "-qm", "test release")
    git("tag", "-a", spec["tag"], "-m", "immutable")
    spec["sha"] = git("rev-parse", "HEAD")
    (tmp_path / "deploy/candidate.json").write_text(json.dumps(spec))
    return tmp_path, spec


def test_both_channels_resolve_annotated_tag_without_using_network(published):
    root, spec = published
    assert verify(root, "stable") == {**spec, "channel": "stable"}
    assert verify(root, "candidate") == {**spec, "channel": "candidate"}


@pytest.mark.parametrize(
    "field,bad",
    [
        ("sha", None),
        ("sha", "1" * 40),
        ("bridge_version", "0.2"),
        ("codex_version", "99.9"),
        ("tag", "HEAD"),
    ],
)
def test_refused_inconsistent_candidate(published, field, bad):
    root, spec = published
    (root / "deploy/candidate.json").write_text(json.dumps({**spec, field: bad}))
    with pytest.raises(ValueError):
        verify(root, "candidate")
