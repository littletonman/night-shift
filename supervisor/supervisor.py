#!/usr/bin/env python3
"""supervisor.py — drive a night-shift run non-interactively, watch it, escalate.

This is the host process that turns the /night-shift skill from "a thing you
babysit" into "a thing that runs itself". It:

  1. launches Claude headless in supervised mode (no interactive pre-flight),
  2. watches the run's own on-disk artifacts for a progress heartbeat,
  3. escalates via ntfy on stall / blocker / completion,
  4. kills a hung run so it can't idle for hours, and leaves its state clean.

Progress is read from the artifacts the agent already writes as its source of
truth — `.night-shift/runs/*/state.json` and `.night-shift/enforce.log` — so it
does not depend on parsing Claude's output stream.

Usage:
  supervisor.py --repo /path/to/project --objective "build milestone m2 ..." \
                [--ntfy-topic ns-foo] [--branch ns/staging] \
                [--database-url postgresql://...] \
                [--stall-min 20] [--kill-mult 2] [--max-hours 9]

Requires: claude on PATH; the repo already onboarded (root-owned scope.yaml).
Stdlib only (PyYAML optional, only for --manifest).
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

POLL_SECONDS = 30


def log(msg):
    ts = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
    print(f"[supervisor {ts}] {msg}", flush=True)


def ntfy(topic, title, message, priority="default", tags=""):
    """Best-effort push notification. Never raises."""
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # noqa: BLE001 - notifications must never break the run
        log(f"ntfy failed ({e}); continuing")


# --- run artifacts -----------------------------------------------------------
def runs_dir(repo):
    return Path(repo) / ".night-shift" / "runs"


def latest_state(repo):
    """Return (path, dict) of the newest run's state.json, or (None, None)."""
    states = sorted(runs_dir(repo).glob("*/state.json"), key=lambda p: p.stat().st_mtime)
    if not states:
        return None, None
    p = states[-1]
    try:
        return p, json.loads(p.read_text())
    except Exception:  # noqa: BLE001 - a half-written file just means "no update yet"
        return p, None


def heartbeat_mtime(repo):
    """Newest mtime across all run state + the enforce log = 'agent did something'."""
    latest = 0.0
    enforce = Path(repo) / ".night-shift" / "enforce.log"
    if enforce.exists():
        latest = enforce.stat().st_mtime
    for p in runs_dir(repo).glob("*/state.json"):
        latest = max(latest, p.stat().st_mtime)
    return latest


def git_head(repo):
    try:
        return subprocess.check_output(
            ["git", "-C", repo, "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
    except Exception:  # noqa: BLE001
        return None


def kr_progress(state):
    krs = (state or {}).get("key_results") or []
    done = sum(1 for k in krs if k.get("status") == "completed")
    return done, len(krs)


def active_run_exists(repo):
    """A run is active if any state.json says status:running (the pre-flight guard)."""
    for p in runs_dir(repo).glob("*/state.json"):
        try:
            if json.loads(p.read_text()).get("status") == "running":
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


TERMINAL = {"completed", "hard-capped", "interrupted", "failed"}


# --- launch + monitor --------------------------------------------------------
def launch(repo, objective, branch, database_url, log_path):
    env = dict(os.environ)
    env["NIGHT_SHIFT_SUPERVISED"] = "1"
    env["NIGHT_SHIFT_OBJECTIVE"] = objective
    env["NIGHT_SHIFT_BRANCH"] = branch
    if database_url:
        env["DATABASE_URL"] = database_url
    logf = open(log_path, "wb")
    proc = subprocess.Popen(
        ["claude", "-p", "--dangerously-skip-permissions",
         "--output-format", "stream-json", "--verbose", "/night-shift"],
        cwd=repo, env=env, stdout=logf, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    return proc, logf


def mark_interrupted(repo, reason):
    """Flip the running state to interrupted so the next run isn't blocked."""
    p, state = latest_state(repo)
    if p and state and state.get("status") == "running":
        state["status"] = "interrupted"
        state["interrupted_reason"] = reason
        try:
            p.write_text(json.dumps(state, indent=2))
            log(f"marked {p.name} interrupted")
        except Exception as e:  # noqa: BLE001
            log(f"could not mark interrupted ({e})")


def monitor(repo, proc, topic, stall_min, kill_mult, max_hours):
    stall_s = stall_min * 60
    kill_s = stall_s * kill_mult
    max_s = max_hours * 3600
    started = time.time()
    last_beat = heartbeat_mtime(repo)
    last_beat_at = time.time()
    last_head = git_head(repo)
    last_done = -1
    stall_alerted = False

    while True:
        rc = proc.poll()
        now = time.time()

        beat = heartbeat_mtime(repo)
        if beat > last_beat:
            last_beat, last_beat_at, stall_alerted = beat, now, False

        # milestone signals: a new commit, or a newly-completed key result
        head = git_head(repo)
        if head and head != last_head:
            log(f"new commit {head[:8]}")
            last_head = head
        _, state = latest_state(repo)
        done, total = kr_progress(state)
        if total and done != last_done:
            if last_done >= 0 and done > last_done:
                ntfy(topic, "night-shift: progress",
                     f"{Path(repo).name}: {done}/{total} key results complete",
                     tags="white_check_mark")
            last_done = done

        # terminal state on disk?
        status = (state or {}).get("status")
        if status in TERMINAL:
            log(f"run reached terminal status: {status}")
            return status

        # process exited
        if rc is not None:
            log(f"claude exited rc={rc}")
            _, state = latest_state(repo)
            return (state or {}).get("status") or f"exited-rc-{rc}"

        # stall detection
        idle = now - last_beat_at
        if idle > kill_s:
            log(f"HUNG: no progress for {int(idle)}s (> kill threshold) — killing")
            ntfy(topic, "night-shift: KILLED (hung)",
                 f"{Path(repo).name}: no progress for {int(idle//60)}m; killed. "
                 f"Check the run and relaunch.", priority="high", tags="skull")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass
            mark_interrupted(repo, f"supervisor killed after {int(idle)}s idle")
            return "killed-hung"
        if idle > stall_s and not stall_alerted:
            log(f"stall: no progress for {int(idle)}s — alerting")
            ntfy(topic, "night-shift: stalled",
                 f"{Path(repo).name}: no progress for {int(idle//60)}m. "
                 f"Still watching; will kill at {int(kill_s//60)}m.",
                 priority="high", tags="warning")
            stall_alerted = True

        # wall-clock backstop (the agent self-caps at 8h; this catches a runaway)
        if now - started > max_s:
            log(f"max wall-clock {max_hours}h exceeded — killing")
            ntfy(topic, "night-shift: max runtime hit",
                 f"{Path(repo).name}: exceeded {max_hours}h wall clock; killed.",
                 priority="high", tags="hourglass")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass
            mark_interrupted(repo, f"supervisor killed at {max_hours}h wall clock")
            return "killed-maxtime"

        time.sleep(POLL_SECONDS)


def read_manifest(path):
    try:
        import yaml  # provided by the enforcement install (python3-yaml)
    except ImportError:
        sys.exit("PyYAML not available; pass --repo/--ntfy-topic explicitly instead of --manifest")
    m = yaml.safe_load(Path(path).read_text())
    return {
        "repo": m.get("repo"),
        "ntfy": (m.get("notify") or {}).get("ntfy_topic"),
    }


def main():
    ap = argparse.ArgumentParser(description="Supervise a night-shift run non-interactively.")
    ap.add_argument("--manifest", help="project manifest (for repo + ntfy topic)")
    ap.add_argument("--repo", help="project repo path (overrides manifest)")
    ap.add_argument("--objective", required=True, help="the shift objective, verbatim")
    ap.add_argument("--branch", default="ns/staging")
    ap.add_argument("--ntfy-topic", help="ntfy topic (overrides manifest)")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--stall-min", type=int, default=20, help="alert if no progress for N min")
    ap.add_argument("--kill-mult", type=int, default=2, help="kill at stall-min * this")
    ap.add_argument("--max-hours", type=float, default=9.0, help="wall-clock backstop")
    args = ap.parse_args()

    repo, topic = args.repo, args.ntfy_topic
    if args.manifest:
        mf = read_manifest(args.manifest)
        repo = repo or mf["repo"]
        topic = topic or mf["ntfy"]
    if not repo:
        sys.exit("need --repo or --manifest with repo:")
    repo = str(Path(repo).resolve())

    # pre-checks
    if not (Path(repo) / ".night-shift" / "scope.yaml").exists():
        sys.exit(f"{repo} is not onboarded (no root-owned .night-shift/scope.yaml)")
    active = active_run_exists(repo)
    if active:
        sys.exit(f"a run is already active ({active}); refusing to start a second. "
                 f"Finish or mark it interrupted first.")

    log_path = Path(repo) / ".night-shift" / f"supervisor-{int(time.time())}.log"
    log(f"launching supervised run in {repo}")
    log(f"  objective: {args.objective}")
    log(f"  branch: {args.branch} | ntfy: {topic or '(none)'} | log: {log_path}")
    ntfy(topic, "night-shift: started",
         f"{Path(repo).name}: {args.objective[:120]}", tags="new_moon")

    proc, logf = launch(repo, args.objective, args.branch, args.database_url, log_path)
    try:
        outcome = monitor(repo, proc, topic, args.stall_min, args.kill_mult, args.max_hours)
    except KeyboardInterrupt:
        log("interrupted by operator — terminating run")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:  # noqa: BLE001
            pass
        mark_interrupted(repo, "operator ctrl-c")
        outcome = "operator-abort"
    finally:
        logf.close()

    # final report
    _, state = latest_state(repo)
    done, total = kr_progress(state)
    hp = (Path(repo) / ".night-shift" / "runs" /
          (state or {}).get("run_id", "") / "handoff.md")
    log(f"DONE — outcome={outcome} | key results {done}/{total} | handoff: {hp}")
    prio = "default" if outcome in ("completed",) else "high"
    tag = "tada" if outcome == "completed" else "warning"
    ntfy(topic, f"night-shift: {outcome}",
         f"{Path(repo).name}: {done}/{total} key results. See handoff.md.",
         priority=prio, tags=tag)
    return 0 if outcome == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
