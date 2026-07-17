#!/usr/bin/env bash
# install.sh — set up night-shift enforcement so that hooks and the scope
# contract are ROOT-OWNED and cannot be modified or disabled by the agent.
#
# Subcommands:
#   sudo ./install.sh system                 machine-level: deps, /opt copy, managed hooks
#   sudo ./install.sh onboard <repo> [opts]  per-project: root-owned .night-shift/scope.yaml
#   sudo ./install.sh verify                 re-check installed file hashes vs the manifest
#
# PREREQUISITE (do this first, once): remove passwordless sudo for your user, so
# that root-owned really means the agent cannot touch it. Check with:
#     sudo -n true 2>&1        # if this succeeds with NO password, fix it:
#     sudo visudo             # remove the NOPASSWD entry for your user
#
# onboard options:
#   --ntfy-topic <topic>   set notify.ntfy_topic in the new scope.yaml
#   --group <group>        group that owns .night-shift/ (default: the sudo user's group)

set -euo pipefail

GITLEAKS_VERSION="8.18.4"
OPT_DIR="/opt/night-shift"
MANAGED_DIR="/etc/claude-code"
MANAGED_FILE="$MANAGED_DIR/managed-settings.json"
MANIFEST="$OPT_DIR/MANIFEST.sha256"

SRC_ENFORCEMENT="$(cd "$(dirname "$0")" && pwd)"   # .../enforcement

log()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install WARN]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[install FATAL]\033[0m %s\n' "$*" >&2; exit 1; }

require_root() { [ "$(id -u)" -eq 0 ] || die "run with sudo (need root to own the enforcement files)."; }

sudo_user() { printf '%s' "${SUDO_USER:-root}"; }
sudo_group() { id -gn "$(sudo_user)" 2>/dev/null || echo root; }

# ---------------------------------------------------------------------------
install_deps() {
  log "installing OS deps (jq, python3-yaml, curl)…"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq || warn "apt-get update failed; continuing"
    apt-get install -y jq python3-yaml curl ca-certificates >/dev/null || warn "apt install partial"
  else
    warn "no apt-get; ensure jq, python3 + PyYAML, and curl are installed."
  fi
  python3 -c 'import yaml' 2>/dev/null || die "PyYAML not importable after install — the gate needs it."
}

install_gitleaks() {
  if command -v gitleaks >/dev/null 2>&1; then
    log "gitleaks present: $(gitleaks version 2>/dev/null || echo '?')"; return 0
  fi
  log "installing gitleaks v$GITLEAKS_VERSION…"
  local arch tarch tmp url sums
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) tarch="x64" ;;
    aarch64|arm64) tarch="arm64" ;;
    *) warn "unknown arch $arch; skipping gitleaks (set require_secret_scan:false or install manually)"; return 0 ;;
  esac
  tmp="$(mktemp -d)"
  url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_linux_${tarch}.tar.gz"
  sums="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_checksums.txt"
  if ! curl -fsSL "$url" -o "$tmp/gl.tar.gz"; then
    warn "could not download gitleaks; secret scan will block until installed (or disable it in scope.yaml)."; rm -rf "$tmp"; return 0
  fi
  if curl -fsSL "$sums" -o "$tmp/sums.txt" 2>/dev/null; then
    local want got
    want="$(grep "linux_${tarch}.tar.gz" "$tmp/sums.txt" | awk '{print $1}' | head -1)"
    got="$(sha256sum "$tmp/gl.tar.gz" | awk '{print $1}')"
    if [ -n "$want" ] && [ "$want" != "$got" ]; then
      warn "gitleaks checksum mismatch (want $want got $got); NOT installing."; rm -rf "$tmp"; return 0
    fi
    log "gitleaks checksum verified."
  else
    warn "could not fetch gitleaks checksums; installing without verification."
  fi
  tar -xzf "$tmp/gl.tar.gz" -C "$tmp" gitleaks 2>/dev/null || { warn "gitleaks extract failed"; rm -rf "$tmp"; return 0; }
  install -m 0755 "$tmp/gitleaks" /usr/local/bin/gitleaks
  rm -rf "$tmp"
  log "gitleaks installed: $(gitleaks version 2>/dev/null || echo '?')"
}

install_opt() {
  log "installing enforcement to $OPT_DIR (root-owned)…"
  mkdir -p "$OPT_DIR/enforcement"
  # copy source tree
  cp -a "$SRC_ENFORCEMENT/." "$OPT_DIR/enforcement/"
  # ownership + perms: root owns everything; scripts executable, rest read-only.
  chown -R root:root "$OPT_DIR"
  find "$OPT_DIR" -type d -exec chmod 755 {} +
  find "$OPT_DIR" -type f -exec chmod 644 {} +
  find "$OPT_DIR/enforcement/hooks" -name '*.sh' -exec chmod 755 {} +
  chmod 755 "$OPT_DIR/enforcement/install.sh" "$OPT_DIR/enforcement/verify-integrity.sh" 2>/dev/null || true
  # Root-owned registry of onboarded repos — the authoritative "is this repo
  # enforced?" signal (so renaming .night-shift cannot make a repo look
  # un-onboarded; it fails closed instead).
  [ -f "$OPT_DIR/onboarded.list" ] || : > "$OPT_DIR/onboarded.list"
  chown root:root "$OPT_DIR/onboarded.list"; chmod 644 "$OPT_DIR/onboarded.list"
  # Empty, root-owned hooks dir. Onboarded repos point core.hooksPath here so
  # repo-local git hooks neither run during commits (a pre-commit hook could
  # inject content past the gate) nor trip the gate's hook check.
  mkdir -p "$OPT_DIR/githooks"; chown root:root "$OPT_DIR/githooks"; chmod 755 "$OPT_DIR/githooks"
  log "verifying scope matcher…"
  python3 "$OPT_DIR/enforcement/lib/scope_match.py" --selftest || die "scope matcher selftest failed."
}

install_managed() {
  mkdir -p "$MANAGED_DIR"
  if [ -f "$MANAGED_FILE" ]; then
    if grep -q '/opt/night-shift/enforcement/hooks' "$MANAGED_FILE" 2>/dev/null; then
      log "managed-settings already references night-shift hooks; refreshing from template."
      cp "$SRC_ENFORCEMENT/managed-settings.template.json" "$MANAGED_FILE"
    else
      local bak="$MANAGED_FILE.pre-night-shift.$(date +%s)"
      cp "$MANAGED_FILE" "$bak"
      warn "existing $MANAGED_FILE backed up to $bak."
      warn "It has other content — MERGE the hooks + allowManagedHooksOnly from"
      warn "  $SRC_ENFORCEMENT/managed-settings.template.json  into it by hand."
      warn "Not overwriting automatically to avoid clobbering your policy."
      return 0
    fi
  else
    cp "$SRC_ENFORCEMENT/managed-settings.template.json" "$MANAGED_FILE"
  fi
  chown root:root "$MANAGED_FILE"
  chmod 644 "$MANAGED_FILE"
  python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$MANAGED_FILE" || die "managed-settings.json is not valid JSON."
  log "managed hooks installed at $MANAGED_FILE (allowManagedHooksOnly)."
}

write_manifest() {
  log "writing integrity manifest…"
  {
    find "$OPT_DIR/enforcement" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.json' \) -print0 \
      | sort -z | xargs -0 sha256sum
    sha256sum "$MANAGED_FILE" 2>/dev/null || true
  } > "$MANIFEST"
  chown root:root "$MANIFEST"; chmod 644 "$MANIFEST"
  log "manifest: $MANIFEST ($(wc -l < "$MANIFEST") files)"
}

cmd_system() {
  require_root
  install_deps
  install_gitleaks
  install_opt
  install_managed
  write_manifest
  cat <<EOF

$(log "system install complete.")
Next:
  1. Confirm passwordless sudo is OFF for your user:   sudo -n true   (should ask for a password)
  2. Onboard a project:   sudo $OPT_DIR/enforcement/install.sh onboard /path/to/repo --ntfy-topic <topic>
  3. Edit its scope.yaml (sudo) to set allow_paths, deny_paths, and gate.fast_checks.
  4. Launch an autonomous run with --dangerously-skip-permissions; the hooks enforce automatically.
EOF
}

cmd_onboard() {
  require_root
  local repo="" ntfy="" grp=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --ntfy-topic) ntfy="$2"; shift 2 ;;
      --group) grp="$2"; shift 2 ;;
      *) [ -z "$repo" ] && repo="$1"; shift ;;
    esac
  done
  [ -n "$repo" ] || die "usage: install.sh onboard <repo> [--ntfy-topic X] [--group G]"
  repo="$(cd "$repo" 2>/dev/null && pwd)" || die "no such directory."
  git -C "$repo" rev-parse --show-toplevel >/dev/null 2>&1 || die "$repo is not a git repo."
  repo="$(git -C "$repo" rev-parse --show-toplevel)"
  [ -n "$grp" ] || grp="$(sudo_group)"
  local user; user="$(sudo_user)"

  local nsdir="$repo/.night-shift"
  local scope="$nsdir/scope.yaml"
  mkdir -p "$nsdir/runs"
  # runs/ is agent-writable; .night-shift/ is root-owned + sticky so the agent
  # can create run files but cannot delete the root-owned scope.yaml.
  chown "$user:$grp" "$nsdir/runs"
  chmod 775 "$nsdir/runs"

  if [ -f "$scope" ]; then
    warn "scope.yaml already exists at $scope — leaving it as is."
  else
    cp "$SRC_ENFORCEMENT/scope.yaml.template" "$scope"
    if [ -n "$ntfy" ]; then
      python3 - "$scope" "$ntfy" <<'PY'
import sys,re
p,topic=sys.argv[1],sys.argv[2]
s=open(p).read()
s=re.sub(r'ntfy_topic:\s*""', 'ntfy_topic: "%s"' % topic, s, count=1)
open(p,'w').write(s)
PY
    fi
    log "created $scope (EDIT IT: set allow_paths, deny_paths, gate.fast_checks)."
  fi
  # Root owns the contract + its directory; sticky bit protects it from the agent.
  chown root:root "$scope"; chmod 644 "$scope"
  chown "root:$grp" "$nsdir"; chmod 1775 "$nsdir"

  # Register the repo in the ROOT-OWNED registry. This — not the presence of
  # .night-shift/ — is what marks the repo enforced. If the agent later renames
  # .night-shift, the repo is still registered and enforcement fails CLOSED.
  local reg="$OPT_DIR/onboarded.list" real
  real="$(cd "$repo" && pwd -P)"
  mkdir -p "$OPT_DIR"; touch "$reg"
  grep -qxF "$real" "$reg" 2>/dev/null || echo "$real" >> "$reg"
  chown root:root "$reg"; chmod 644 "$reg"
  log "registered $real in $reg"

  # Neutralize repo-local git hooks: point core.hooksPath at the empty root-owned
  # dir so a planted .git/hooks/pre-commit cannot run during the commit. The
  # classifier + commit gate stop the agent from re-enabling .git/hooks.
  if [ -d "$OPT_DIR/githooks" ]; then
    git -C "$repo" config core.hooksPath "$OPT_DIR/githooks" 2>/dev/null \
      && log "set core.hooksPath -> $OPT_DIR/githooks (repo hooks neutralized)" \
      || warn "could not set core.hooksPath for $repo"
  fi

  # gitignore .night-shift/ as the original skill does (run the edit as the user).
  if ! grep -qxF '.night-shift/' "$repo/.gitignore" 2>/dev/null; then
    printf '\n.night-shift/\n' >> "$repo/.gitignore"
    chown "$user:$grp" "$repo/.gitignore" 2>/dev/null || true
    log "added .night-shift/ to .gitignore"
  fi

  cat <<EOF

$(log "onboarded $repo")
  scope.yaml: $scope   (root-owned; edit with sudo)
  Set at minimum: allow_paths, deny_paths, gate.fast_checks (the project's real lint/typecheck).
  To push to staging later, set push.enabled: true after configuring branch protection.
EOF
}

cmd_verify() {
  require_root
  "$OPT_DIR/enforcement/verify-integrity.sh"
}

case "${1:-}" in
  system) shift; cmd_system "$@" ;;
  onboard) shift; cmd_onboard "$@" ;;
  verify) shift; cmd_verify "$@" ;;
  *) cat >&2 <<EOF
night-shift enforcement installer
  sudo $0 system                 machine setup (deps, /opt copy, managed hooks, manifest)
  sudo $0 onboard <repo> [opts]  create a root-owned .night-shift/scope.yaml for a project
  sudo $0 verify                 re-check installed file hashes against the manifest
EOF
    exit 2 ;;
esac
