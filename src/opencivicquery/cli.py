from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime, timedelta

_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_ACTOR_EMAIL = re.compile(r"^[^@\s\"]+@[^@\s\"]+$")


def _usage(args: argparse.Namespace) -> int:
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opencivicquery")
    subparsers = parser.add_subparsers(dest="command", required=True)
    usage = subparsers.add_parser("usage", help="Read private OpenCivicQuery usage records")
    usage.add_argument("--project", required=True)
    usage.add_argument("--hours", type=int, default=168)
    usage.add_argument("--limit", type=int, default=100)
    usage.add_argument("--actor")
    usage.set_defaults(handler=_usage)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.hours < 1 or args.limit < 1 or args.limit > 1000:
        raise SystemExit("hours must be positive and limit must be between 1 and 1000")
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
