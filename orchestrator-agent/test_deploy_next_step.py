"""The deploy's closing hint is the request the page tells the room to send.

October 13 live walk: the deploy still printed `./ask.sh "Use preset=team-game."` after
part 4 had moved to a request with the attendee's own creative direction.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "orchestrator"))

import progress  # noqa: E402


def test_the_printed_request_is_the_pages():
    script = (HERE / "deploy-coordinator.sh").read_text()
    printed = re.findall(r'^echo "  (\./ask\.sh .*)"$', script, re.M)
    assert printed, "deploy-coordinator.sh no longer prints the next request"
    assert printed[-1].replace('\\"', '"') == progress.CMD_ASK[-1]
