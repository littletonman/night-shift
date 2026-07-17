#!/usr/bin/env bash
# verify-integrity.sh — confirm the installed enforcement files and managed
# hooks still match the manifest written at install time. The Phase-2 supervisor
# runs this BEFORE launching each session; run it any time by hand with sudo.
#
# Exit 0 = intact. Exit 1 = drift (a file changed / is missing). On drift it
# fires an ntfy alert if NIGHT_SHIFT_NTFY_TOPIC is set.
set -uo pipefail

OPT_DIR="/opt/night-shift"
MANIFEST="$OPT_DIR/MANIFEST.sha256"
MANAGED_FILE="/etc/claude-code/managed-settings.json"

fail() { printf '\033[1;31m[verify FAIL]\033[0m %s\n' "$*" >&2; }
ok()   { printf '\033[1;32m[verify OK]\033[0m %s\n' "$*"; }

[ -r "$MANIFEST" ] || { fail "no manifest at $MANIFEST — run install.sh system."; exit 1; }

# Ownership sanity: everything under /opt/night-shift must be root-owned.
NONROOT="$(find "$OPT_DIR" ! -user root -print 2>/dev/null | head -5)"
if [ -n "$NONROOT" ]; then
  fail "non-root-owned files under $OPT_DIR (tamper risk):"
  printf '%s\n' "$NONROOT" >&2
fi
if [ -e "$MANAGED_FILE" ] && [ "$(stat -c '%U' "$MANAGED_FILE" 2>/dev/null)" != "root" ]; then
  fail "$MANAGED_FILE is not root-owned."
  NONROOT="x$NONROOT"
fi

# Hash check against the manifest.
DRIFT="$(sha256sum -c "$MANIFEST" 2>/dev/null | grep -v ': OK$' || true)"

if [ -z "$DRIFT" ] && [ -z "$NONROOT" ]; then
  ok "enforcement integrity verified ($(grep -c '' "$MANIFEST") files)."
  exit 0
fi

fail "enforcement integrity check FAILED:"
[ -n "$DRIFT" ] && printf '%s\n' "$DRIFT" >&2

TOPIC="${NIGHT_SHIFT_NTFY_TOPIC:-}"
if [ -n "$TOPIC" ] && command -v curl >/dev/null 2>&1; then
  curl -fsS --max-time 8 -H "Title: night-shift TAMPER ALERT" -H "Priority: urgent" \
    -H "Tags: rotating_light" \
    -d "Enforcement integrity check failed on $(hostname). Do not launch shifts until investigated." \
    "https://ntfy.sh/$TOPIC" >/dev/null 2>&1 || true
fi
exit 1
