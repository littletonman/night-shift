#!/usr/bin/env bash
# bootstrap.sh — stand up a project for the night shift, from its manifest.
#
#   sudo /opt/night-shift/enforcement/bootstrap.sh <manifest.yaml>
#
# This is the PRE-AGENT setup, and it belongs to the operator/system — NOT to
# the night-shift skill's pre-flight. The agent cannot bootstrap its own
# guardrails: onboarding writes root-owned files and the scope contract must
# exist BEFORE the agent runs (else the agent would define its own boundary).
#
# It does, idempotently:
#   1. create the repo (empty, user-owned) if it doesn't exist
#   2. onboard it (register + root-owned .night-shift + core.hooksPath)
#   3. generate its scope.yaml FROM the manifest (root-owned)
#   4. copy the plan (manifest + sibling *.md) into <repo>/docs/planning/
#   5. bring up the dev DB per dev_env.db
#   6. print the exact launch command
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MANIFEST="${1:-}"
[ -n "$MANIFEST" ] && [ -f "$MANIFEST" ] || { echo "usage: sudo bootstrap.sh <manifest.yaml>" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "run with sudo (writes the root-owned scope contract)" >&2; exit 2; }
MANIFEST="$(readlink -f "$MANIFEST")"

USER_NAME="${SUDO_USER:-root}"
as_user() { sudo -u "$USER_NAME" "$@"; }
log() { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }

field() {
  python3 -c '
import sys,yaml
d=yaml.safe_load(open(sys.argv[1])) or {}
cur=d
for k in sys.argv[2].split("."):
    cur = cur.get(k) if isinstance(cur,dict) else None
sys.stdout.write("" if cur is None else str(cur))
' "$MANIFEST" "$1"
}

REPO="$(field repo)";       [ -n "$REPO" ] || { echo "manifest has no 'repo:'" >&2; exit 2; }
PROJECT="$(field project)"; PROJECT="${PROJECT:-app}"
NTFY="$(field notify.ntfy_topic)"
DB="$(field dev_env.db)"
DBNAME="$(printf '%s' "$PROJECT" | tr -c 'a-zA-Z0-9' '_')"

log "project=$PROJECT  repo=$REPO"

# 1. create the repo if missing (user-owned) --------------------------------
if [ ! -d "$REPO/.git" ]; then
  as_user mkdir -p "$REPO"
  as_user git -C "$REPO" init -q
  as_user git -C "$REPO" commit -q --allow-empty -m "chore: init empty repo"
  log "created empty repo"
else
  log "repo already exists — leaving it"
fi

# 2. onboard (register + root-owned .night-shift) ---------------------------
if [ -n "$NTFY" ]; then
  bash "$HERE/install.sh" onboard "$REPO" --ntfy-topic "$NTFY"
else
  bash "$HERE/install.sh" onboard "$REPO"
fi

# 3. generate scope.yaml FROM the manifest (root-owned) ---------------------
python3 -c '
import sys,yaml
m=yaml.safe_load(open(sys.argv[1])) or {}
scope=m.get("scope",{}) or {}
out={
  "allow_paths":      scope.get("allow_paths",["**"]),
  "deny_paths":       scope.get("deny_paths",[]),
  "deny_commands":    scope.get("deny_commands",[]),
  "migration_policy": m.get("migration_policy","deny"),
  "dependency_policy":m.get("dependency_policy","propose-only"),
  "gate":             m.get("gate",{"fast_checks":["true"],"full_checks":[],
                                     "codex_effort":"high","require_secret_scan":True}),
  "push":             m.get("push",{"enabled":False,"allow_refs":["ns/"]}),
  "notify":           m.get("notify",{}) or {},
}
yaml.safe_dump(out, open(sys.argv[2],"w"), sort_keys=False, default_flow_style=False)
' "$MANIFEST" "$REPO/.night-shift/scope.yaml"
chown root:root "$REPO/.night-shift/scope.yaml"; chmod 644 "$REPO/.night-shift/scope.yaml"
log "wrote scope.yaml from the manifest"

# 4. copy the plan into the repo (user-owned) -------------------------------
# Manifest + the docs it DECLARES in plan_docs (fall back to all *.md). This
# avoids sweeping in operator-only files (runbooks etc.).
PLANDIR="$(dirname "$MANIFEST")"
as_user mkdir -p "$REPO/docs/planning"
as_user cp "$MANIFEST" "$REPO/docs/planning/"
PLAN_DOCS="$(python3 -c '
import sys,yaml
m=yaml.safe_load(open(sys.argv[1])) or {}
d=m.get("plan_docs")
print("\n".join(d) if isinstance(d,list) else "")
' "$MANIFEST")"
if [ -n "$PLAN_DOCS" ]; then
  while IFS= read -r d; do
    [ -n "$d" ] && [ -f "$PLANDIR/$d" ] && as_user cp "$PLANDIR/$d" "$REPO/docs/planning/"
  done <<< "$PLAN_DOCS"
else
  for f in "$PLANDIR"/*.md; do [ -f "$f" ] && as_user cp "$f" "$REPO/docs/planning/"; done
fi
log "copied the plan -> docs/planning/"

# 5. dev DB (user-owned; docker) on a FREE host port ------------------------
# Each project gets its own port (5432 may be taken by another project's DB).
DBURL=""
if [ -n "$DB" ]; then
  CNAME="${DBNAME}-db"
  STATE="$(as_user docker inspect -f '{{.State.Status}}' "$CNAME" 2>/dev/null || true)"
  if [ "$STATE" = "running" ]; then
    HOSTPORT="$(as_user docker port "$CNAME" 5432/tcp 2>/dev/null | sed 's/.*://' | head -1)"
    log "db container $CNAME already running on host port ${HOSTPORT:-?}"
  else
    [ -n "$STATE" ] && as_user docker rm -f "$CNAME" >/dev/null 2>&1 || true
    HOSTPORT=""
    for p in $(seq 5432 5480); do
      ss -ltn 2>/dev/null | grep -q ":$p " || { HOSTPORT="$p"; break; }
    done
    [ -n "$HOSTPORT" ] || { echo "[bootstrap] no free host port 5432-5480 for the DB" >&2; exit 1; }
    if as_user docker run -d --name "$CNAME" \
        -e POSTGRES_PASSWORD=dev -e POSTGRES_DB="$DBNAME" \
        -p "$HOSTPORT:5432" "$DB" >/dev/null; then
      log "started db container $CNAME ($DB) on host port $HOSTPORT"
    else
      echo "[bootstrap] FAILED to start db container $CNAME" >&2; exit 1
    fi
  fi
  DBURL="postgresql://postgres:dev@localhost:$HOSTPORT/$DBNAME"
fi

# 5b. browser runtime for UI verification. Only the system libraries go in here:
# they need apt-get (root, which this script has). But a modern playwright needs
# a modern Node, and on many dev boxes root's system node (/usr/bin/node) is far
# older than the invoking user's nvm node — so we resolve npx from the USER's
# toolchain (login shell, to load nvm) and run it with the Node dir on PATH,
# still as root so its apt-get needs no sudo. In a container where root already
# has a modern Node, this is a no-op difference. The agent adds @playwright/test
# + the browser binary + the e2e test per-project (user-writable).
BROWSER="$(field dev_env.browser)"
if [ -n "$BROWSER" ]; then
  NS_NPX="$(as_user bash -lc 'command -v npx' 2>/dev/null || true)"
  [ -n "$NS_NPX" ] || NS_NPX="npx"
  if PATH="$(dirname "$NS_NPX"):$PATH" "$NS_NPX" --yes playwright install-deps "$BROWSER" >/dev/null 2>&1; then
    log "$BROWSER system libs ready (agent adds @playwright/test + tests + browser binary)"
  else
    log "WARN: could not install $BROWSER system libs. The agent cannot apt-get,"
    log "      so UI verification needs these libs — install them manually with:"
    log "        sudo env PATH=\"$(dirname "$NS_NPX"):\$PATH\" $NS_NPX --yes playwright install-deps $BROWSER"
  fi
fi

# 6. launch instructions -----------------------------------------------------
cat <<EOF

$(log "READY.  Launch the night shift with:")
  cd $REPO
  ${DBURL:+export DATABASE_URL="$DBURL"}
  claude --dangerously-skip-permissions
  # then:  /night-shift
  #   objective: build milestone m1; the full plan is in docs/planning/
EOF
