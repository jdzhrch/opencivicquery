from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ACTOR_EMAIL = re.compile(r"^[^@\s\"]+@[^@\s\"]+$")
_MAX_KEY_BYTES = 64 * 1024


def _usage(args: argparse.Namespace) -> int:
    if args.hours < 1 or args.limit < 1 or args.limit > 1000:
        raise SystemExit("hours must be positive and limit must be between 1 and 1000")
    if not _PROJECT_ID.fullmatch(args.project):
        raise SystemExit("Invalid Google Cloud project ID")
    since = datetime.now(UTC) - timedelta(hours=args.hours)
    filters = [
        'resource.type="cloud_run_revision"',
        'jsonPayload.event="opencivicquery_usage"',
        f'timestamp>="{since.isoformat()}"',
    ]
    if args.actor:
        actor = args.actor.strip().lower()
        if actor != "anonymous" and not _ACTOR_EMAIL.fullmatch(actor):
            raise SystemExit("Invalid actor: use an email address or 'anonymous'")
        filters.append(f'jsonPayload.actor_id="{actor}"')
    command = [
        "gcloud",
        "logging",
        "read",
        " AND ".join(filters),
        "--project",
        args.project,
        "--format=json",
        f"--limit={args.limit}",
        "--order=desc",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "Unable to read OpenCivicQuery usage logs")
    events = json.loads(result.stdout or "[]")
    print(json.dumps(events, indent=2, ensure_ascii=False))
    return 0


def _key_client(text: str) -> Any:
    """A storage client signed in with a service account key given as JSON text.

    Errors never repeat the text, which holds a private key.
    """
    from google.cloud import storage
    from google.oauth2 import service_account

    refused = "--key-stdin: standard input did not hold a JSON service account key"
    try:
        info = json.loads(text)
    except ValueError:
        raise SystemExit(refused) from None
    if not isinstance(info, dict) or info.get("type") != "service_account":
        raise SystemExit(refused)
    try:
        credentials = service_account.Credentials.from_service_account_info(info)
    except Exception:
        # Whatever the key's flaw, the message must not carry any of it.
        raise SystemExit(refused) from None
    return storage.Client(project=info.get("project_id"), credentials=credentials)


async def _run_sfgov_copy(args: argparse.Namespace, client: Any = None) -> int:
    # Imported here so the usage command never loads the source modules.
    from opencivicquery.sources.base import SourceError
    from opencivicquery.sources.sfgov import SfGov
    from opencivicquery.sources.sfgov_copy import (
        DEFAULT_AGENCIES,
        GcsCopyStore,
        LocalCopyStore,
        as_json,
        decode_copy,
        encode_copy,
        plan_copy,
        plan_summary,
        sync_copy,
    )

    store = (
        GcsCopyStore(args.bucket, client=client) if args.bucket else LocalCopyStore(Path(args.path))
    )
    names = args.agency or list(DEFAULT_AGENCIES)
    data = await store.read()
    previous = None
    if data is not None:
        try:
            previous = decode_copy(data)
        except SourceError as exc:
            # A damaged or older-format copy must not stop every later run: read in full.
            print(f"The earlier copy could not be read ({exc}); this run reads everything again.")
    source = SfGov()
    try:
        if args.plan:
            rows = await plan_copy(source, names, previous)
            print(as_json(rows))
            print(plan_summary(rows))
            print(f"Plan requests sent: {source.requests_sent}")
            return 0
        copy, outcomes = await sync_copy(source, names, previous, sample=args.sample)
    finally:
        await source.close()
    for outcome in outcomes:
        line = (
            f"{outcome.name}: {outcome.status}; news {outcome.news} of {outcome.news_total} "
            f"({outcome.text_requests} text reads, {outcome.text_failures} failed); meeting "
            f"pages {outcome.meetings} of {outcome.meetings_total}"
        )
        print(line + (f"; {outcome.detail}" if outcome.detail else ""))
    print(f"Requests sent: {source.requests_sent}")
    if copy is None:
        print("No agency could be read and there is no earlier copy, so nothing was written.")
        return 1
    location = await store.write(encode_copy(copy))
    print(
        f"Wrote the copy to {location} (run finished {copy.copied_at.isoformat()}): "
        f"{len(copy.news)} news pages, {len(copy.meetings)} meeting pages, "
        f"{len(copy.documents)} documents."
    )
    return 0 if all(outcome.status == "read" for outcome in outcomes) else 1


def _sfgov_copy(args: argparse.Namespace) -> int:
    if bool(args.path) == bool(args.bucket):
        raise SystemExit("Give either --path or --bucket")
    if args.sample is not None and not 1 <= args.sample <= 50:
        raise SystemExit("--sample must be between 1 and 50")
    if args.sample is not None and args.bucket:
        # A sample copy is partial; it must never replace the copy a server answers from.
        raise SystemExit("A sample run writes only to a local --path")
    client = None
    if args.key_stdin:
        if not args.bucket:
            raise SystemExit("--key-stdin is for --bucket")
        client = _key_client(sys.stdin.read(_MAX_KEY_BYTES))
    return asyncio.run(_run_sfgov_copy(args, client))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencivicquery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    usage = subparsers.add_parser("usage", help="Read private OpenCivicQuery usage records")
    usage.add_argument("--project", required=True)
    usage.add_argument("--hours", type=int, default=168)
    usage.add_argument("--limit", type=int, default=100)
    usage.add_argument("--actor")
    usage.set_defaults(handler=_usage)
    copy = subparsers.add_parser(
        "sfgov-copy",
        help="Read SF.gov news and meeting pages into the daily copy the server answers from",
    )
    copy.add_argument("--path", help="Local snapshot folder; the copy goes under sfgov-copy/")
    copy.add_argument("--bucket", help="Snapshot bucket; the copy goes under sfgov-copy/")
    copy.add_argument(
        "--agency",
        action="append",
        help="An agency's exact SF.gov name; repeat for several. Default: the built-in list.",
    )
    copy.add_argument(
        "--sample",
        type=int,
        help="Read at most this many news and meeting pages per agency, for a test run",
    )
    copy.add_argument(
        "--plan",
        action="store_true",
        help="Only count what a full read would fetch, and write nothing",
    )
    copy.add_argument(
        "--key-stdin",
        action="store_true",
        help=(
            "Write to the bucket with a service account key read as JSON from standard input, "
            "so the key never has to sit in a file. Default: Application Default Credentials."
        ),
    )
    copy.set_defaults(handler=_sfgov_copy)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
