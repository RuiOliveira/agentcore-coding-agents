#!/usr/bin/env bash
# Talk to the deployed coordinator in one conversation you can come back to.
#
#   ./ask.sh "Use preset=web-app."         # the first message starts the conversation
#   ./ask.sh "How is my build going?"      # later messages continue the same one
#   ./ask.sh --new "Use preset=web-app."   # start a fresh conversation
#
# The conversation is an AgentCore Runtime session of the coordinator. Its ID is kept in
# ~/.agentcore/coordinator-session, so a new terminal continues it. A build does not
# depend on it: the engine runs as an async task and saves its record to S3, which
# watch_run.py reads with no session at all.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
project="$here/../CodingAgents"
state="$HOME/.agentcore/coordinator-session"

if [ "${1:-}" = "--new" ]; then rm -f "$state"; shift; fi
if [ $# -lt 1 ] || [ -z "$*" ]; then
  echo "usage: ./ask.sh [--new] \"your message\"" >&2
  exit 2
fi
if [ ! -f "$project/agentcore/agentcore.json" ]; then
  echo "No coordinator yet. Deploy it first: ./deploy-coordinator.sh" >&2
  exit 1
fi
mkdir -p "$(dirname "$state")"
# AgentCore rejects a session ID shorter than 33 characters; a UUID is 36.
[ -s "$state" ] || python3 -c 'import uuid; print(uuid.uuid4())' > "$state"
session_id="$(cat "$state")"
echo "Coordinator session: $session_id"
cd "$project"
exec agentcore invoke --session-id "$session_id" --stream "$*"
