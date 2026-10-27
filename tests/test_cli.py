from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from opencivicquery.cli import _usage


def test_usage_builds_structured_log_filter(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    captured: list[list[str]] = []

    def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    args = argparse.Namespace(
        project="opencivicquery-demo",
        hours=24,
        limit=20,
        actor="Reporter@Example.org",
    )

    assert _usage(args) == 0
    assert json.loads(capsys.readouterr().out) == []
    filter_value = captured[0][3]
    assert 'jsonPayload.event="opencivicquery_usage"' in filter_value
    assert 'jsonPayload.actor_id="reporter@example.org"' in filter_value
    assert captured[0][captured[0].index("--project") + 1] == "opencivicquery-demo"


def test_usage_can_filter_on_the_open_mode_actor(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    captured: list[list[str]] = []

    def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    args = argparse.Namespace(
        project="opencivicquery-demo", hours=24, limit=20, actor=" Anonymous "
    )

    assert _usage(args) == 0
    assert 'jsonPayload.actor_id="anonymous"' in captured[0][3]


@pytest.mark.parametrize(
    ("project", "actor"),
    [
        ("INVALID", None),
        ("opencivicquery-demo", 'bad" OR true'),
        ("opencivicquery-demo", 'anonymous" OR true'),
        ("opencivicquery-demo", "nobody"),
    ],
)
def test_usage_rejects_filter_injection(project: str, actor: str | None) -> None:
    args = argparse.Namespace(project=project, hours=24, limit=20, actor=actor)

    with pytest.raises(SystemExit, match="Invalid"):
        _usage(args)
