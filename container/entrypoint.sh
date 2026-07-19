#!/usr/bin/env bash
# ns-entrypoint — runs as ROOT to set up root-owned state that the agent must not
# control, then drops to the non-root agent (node) to run the supervised shift.
#
# Required env (from `docker run -e`):
#   NS_REPO                 mounted repo path (must be onboarded: root-owned scope.yaml)
#   NIGHT_SHIFT_OBJECTIVE   the shift objective, verbatim
# Optional:
#   NIGHT_SHIFT_BRANCH (default ns/staging), DATABASE_URL
# Mounts expected:
#   <repo>:<repo>                 (rw)  — the onboarded project; artifacts land here
#   ~/.claude:/host/.claude:ro          — Claude credentials source
#   ~/.codex:/host/.codex:ro            — Codex auth source
set -euo pipefail

: "${NS_REPO:?set NS_REPO to the mounted repo path}"
[ -f "$NS_REPO/.night-shift/scope.yaml" ] || {
  echo "FATAL: $NS_REPO is not onboarded (no root-owned .night-shift/scope.yaml)"; exit 3; }

# 1. Register the mounted repo so enforcement RESOLVES it. The registry is the
#    authoritative 'is this repo enforced?' signal; without this the gate would
#    fail OPEN (resolve to 'none' -> passthrough). Only root can write it.
real="$(cd "$NS_REPO" && pwd -P)"
touch /opt/night-shift/onboarded.list
grep -qxF "$real" /opt/night-shift/onboarded.list || echo "$real" >> /opt/night-shift/onboarded.list
chown root:root /opt/night-shift/onboarded.list; chmod 644 /opt/night-shift/onboarded.list

# 2. Inject host auth into the agent's WRITABLE home (the CLIs also write their own
#    state — sessions, logs — so the source can't just be mounted read-only in place).
AGENT_HOME=/home/node
inject() {
  local src="$1" dst="$2"; shift 2
  [ -d "$src" ] || return 0
  mkdir -p "$dst"
  for f in "$@"; do [ -e "$src/$f" ] && cp "$src/$f" "$dst/$f" || true; done
  chown -R node:node "$dst"
}
inject /host/.claude "$AGENT_HOME/.claude" .credentials.json settings.json
inject /host/.codex  "$AGENT_HOME/.codex"  auth.json config.toml
[ -f "$AGENT_HOME/.claude/.credentials.json" ] || {
  echo "FATAL: no Claude credentials injected — mount ~/.claude at /host/.claude:ro"; exit 3; }
[ -f "$AGENT_HOME/.codex/auth.json" ] || \
  echo "WARN: no Codex auth injected — the commit gate's review will fail closed."

# 3. Drop to the non-root agent and run the supervised shift. NIGHT_SHIFT_SUPERVISED
#    is forced on: a containerized run is headless by definition.
cd "$NS_REPO"
export NIGHT_SHIFT_SUPERVISED=1
exec gosu node env \
  HOME="$AGENT_HOME" \
  NIGHT_SHIFT_SUPERVISED=1 \
  NIGHT_SHIFT_OBJECTIVE="${NIGHT_SHIFT_OBJECTIVE:-}" \
  NIGHT_SHIFT_BRANCH="${NIGHT_SHIFT_BRANCH:-ns/staging}" \
  ${DATABASE_URL:+DATABASE_URL="$DATABASE_URL"} \
  PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright \
  PATH="/usr/local/bin:/usr/bin:/bin:/usr/local/lib/node_modules/.bin" \
  claude -p --dangerously-skip-permissions --output-format stream-json --verbose /night-shift
