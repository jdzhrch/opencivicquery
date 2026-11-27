"""Start OpenCivicQuery's SF.gov tools for Claude Desktop, over stdio.

Claude Desktop runs this file with uv from the unpacked extension folder. The server offers
the SF.gov tools and the two snapshot tools, saves its evidence snapshots under
~/.opencivicquery/snapshots, and reads SF.gov live from this computer.
"""

import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("OPENCIVICQUERY_CONFIG_PATH", str(HERE / "deployment.yaml"))
os.environ["OPENCIVICQUERY_TRANSPORT"] = "stdio"
# The snapshot tools come along because every answer's rules point to them.
os.environ["OPENCIVICQUERY_TOOLS"] = (
    "find_sfgov_news,find_sfgov_meetings,search_sfgov,pin_snapshot,get_snapshot"
)

from opencivicquery.mcp_server import main  # noqa: E402

main()
