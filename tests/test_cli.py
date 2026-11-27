from __future__ import annotations

import argparse
import json
import subprocess

import pytest

from opencivicquery import cli
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


SECRET = "fake key material: not-a-real-key"


def _copy_args(*extra: str) -> argparse.Namespace:
    return cli.build_parser().parse_args(["sfgov-copy", *extra])


def test_a_key_on_standard_input_signs_in_the_bucket_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from google.cloud import storage
    from google.oauth2 import service_account

    key = {"type": "service_account", "project_id": "demo-project", "private_key": SECRET}
    seen: dict[str, object] = {}

    def from_info(info: dict[str, object]) -> str:
        seen["info"] = info
        return "signed-in"

    def client(**kwargs: object) -> str:
        seen["client"] = kwargs
        return "client"

    async def run(args: argparse.Namespace, client: object = None) -> int:
        seen["store_client"] = client
        return 0

    monkeypatch.setattr(service_account.Credentials, "from_service_account_info", from_info)
    monkeypatch.setattr(storage, "Client", client)
    monkeypatch.setattr(cli, "_run_sfgov_copy", run)
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(key)))

    assert cli._sfgov_copy(_copy_args("--bucket", "b", "--key-stdin")) == 0
    assert seen["info"] == key
    assert seen["client"] == {"project": "demo-project", "credentials": "signed-in"}
    assert seen["store_client"] == "client"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json " + SECRET,
        json.dumps({"type": "authorized_user", "private_key": SECRET}),
        json.dumps({"type": "service_account", "private_key": SECRET}),
    ],
)
def test_a_bad_key_is_refused_without_repeating_it(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(text))

    with pytest.raises(SystemExit) as caught:
        cli._sfgov_copy(_copy_args("--bucket", "b", "--key-stdin"))

    message = str(caught.value)
    assert message.startswith("--key-stdin:")
    assert "not-a-real-key" not in message
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None or caught.value.__suppress_context__


def test_a_key_on_standard_input_needs_a_bucket() -> None:
    with pytest.raises(SystemExit) as caught:
        cli._sfgov_copy(_copy_args("--path", "x", "--key-stdin"))

    assert "--bucket" in str(caught.value)
