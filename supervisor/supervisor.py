#!/usr/bin/env python3
"""supervisor.py — drive a night-shift run non-interactively, watch it, escalate.

The host process that turns the /night-shift skill from "a thing you babysit"
into "a thing that runs itself". It:

  1. launches Claude headless in supervised mode — locally OR in a container,
  2. watches the run's own on-disk artifacts for a progress heartbeat,
  3. escalates via ntfy on stall / blocker / completion,
  4. kills a hung run so it can't idle for hours, and leaves its state clean.

Progress is read from the artifacts the agent already writes as its source of
truth — `.night-shift/runs/*/state.json` and `.night-shift/enforce.log`. Those
land in the (possibly volume-mounted) repo, so monitoring is identical whether
the run is local or containerized.

Usage (local):
  supervisor.py --repo /path/to/project --objective "build milestone m2 ..." \
                [--ntfy-topic ns-foo] [--branch ns/staging] [--database-url ...]

Usage (container):
  supervisor.py --repo /path/to/project --objective "..." \
                --container night-shift-agent:latest \
                [--db-image postgres:16] [--net ns-net] [--db-name weather_app]

Requires: claude on PATH (local) or docker + a built image (container).
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
DEVNULL = subprocess.DEVNULL


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


def latest_state(repo, ignore_dirs=frozenset()):
    # ignore_dirs excludes run directories that already existed before THIS
    # supervised run launched — otherwise a prior run's terminal state.json
    # (e.g. a previous "completed") is the newest by mtime and the monitor would
    # mistake it for this run finishing the instant it starts.
    states = sorted(
        (p for p in runs_dir(repo).glob("*/state.json") if str(p.parent) not in ignore_dirs),
        key=lambda p: p.stat().st_mtime,
    )
    if not states:
        return None, None
    p = states[-1]
    try:
        return p, json.loads(p.read_text())
    except Exception:  # noqa: BLE001 - a half-written file just means "no update yet"
        return p, None


def heartbeat_mtime(repo):
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
    return sum(1 for k in krs if k.get("status") == "completed"), len(krs)


def active_run_exists(repo):
    for p in runs_dir(repo).glob("*/state.json"):
        try:
            if json.loads(p.read_text()).get("status") == "running":
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


TERMINAL = {"completed", "hard-capped", "interrupted", "failed"}


# --- docker helpers ----------------------------------------------------------
def _docker(*args, check=True, capture=False):
    r = subprocess.run(
        ["docker", *args], text=True,
        stdout=(subprocess.PIPE if capture else DEVNULL),
        stderr=(subprocess.PIPE if capture else DEVNULL),
    )
    if check and r.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {(r.stderr or '').strip() or r.returncode}")
    return (r.stdout or "").strip()


def ensure_network(net):
    if net not in _docker("network", "ls", "--format", "{{.Name}}", capture=True, check=False).split():
        _docker("network", "create", net)
        log(f"created docker network {net}")


def ensure_db(net, project, db_image, db_name):
    name = f"ns-db-{project}"
    running = name in _docker("ps", "--format", "{{.Names}}", capture=True, check=False).split()
    if not running:
        _docker("rm", "-f", name, check=False)
        _docker("run", "-d", "--name", name, "--network", net,
                "-e", "POSTGRES_PASSWORD=dev", "-e", f"POSTGRES_DB={db_name}", db_image)
        log(f"started sibling DB {name} ({db_image})")
    for _ in range(30):
        if subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                          stdout=DEVNULL, stderr=DEVNULL).returncode == 0:
            break
        time.sleep(2)
    return f"postgresql://postgres:dev@{name}:5432/{db_name}"


# --- launch (local or container) ---------------------------------------------
def _base_env(objective, branch, database_url):
    env = dict(os.environ)
    env.update(NIGHT_SHIFT_SUPERVISED="1", NIGHT_SHIFT_OBJECTIVE=objective,
               NIGHT_SHIFT_BRANCH=branch)
    if database_url:
        env["DATABASE_URL"] = database_url
    return env


def launch_local(repo, objective, branch, database_url, model, log_path):
    # --model overrides the host's ~/.claude/settings.json model (which is the
    # operator's interactive preference, not what an autonomous shift should use).
    cmd = ["claude", "-p", "--dangerously-skip-permissions",
           "--output-format", "stream-json", "--verbose"]
    if model:
        cmd += ["--model", model]
    cmd.append("/night-shift")
    logf = open(log_path, "wb")
    proc = subprocess.Popen(
        cmd, cwd=repo, env=_base_env(objective, branch, database_url),
        stdout=logf, stderr=subprocess.STDOUT, stdin=DEVNULL, start_new_session=True,
    )
    return {"kind": "local", "proc": proc}, logf


def launch_container(image, repo, objective, branch, database_url, model, net, log_path):
    name = f"ns-run-{int(time.time())}"
    home = str(Path.home())
    args = ["run", "--rm", "--name", name,
            "-v", f"{repo}:{repo}",
            "-v", f"{home}/.claude:/host/.claude:ro",
            "-v", f"{home}/.codex:/host/.codex:ro",
            "-e", f"NS_REPO={repo}",
            "-e", "NIGHT_SHIFT_SUPERVISED=1",
            "-e", f"NIGHT_SHIFT_OBJECTIVE={objective}",
            "-e", f"NIGHT_SHIFT_BRANCH={branch}"]
    if model:
        args += ["-e", f"NIGHT_SHIFT_MODEL={model}"]
    if net:
        args += ["--network", net]
    if database_url:
        args += ["-e", f"DATABASE_URL={database_url}"]
    args.append(image)
    logf = open(log_path, "wb")
    proc = subprocess.Popen(["docker", *args], stdout=logf, stderr=subprocess.STDOUT,
                            stdin=DEVNULL, start_new_session=True)
    log(f"launched container {name} ({image})")
    return {"kind": "container", "proc": proc, "name": name}, logf


def terminate(handle):
    if handle["kind"] == "container":
        subprocess.run(["docker", "kill", handle["name"]], stdout=DEVNULL, stderr=DEVNULL)
        return
    try:
        os.killpg(os.getpgid(handle["proc"].pid), signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(3)
    if handle["proc"].poll() is None:
        try:
            os.killpg(os.getpgid(handle["proc"].pid), signal.SIGKILL)
        except Exception:  # noqa: BLE001
            pass


def mark_interrupted(repo, reason):
    p, state = latest_state(repo)
    if p and state and state.get("status") == "running":
        state["status"] = "interrupted"
        state["interrupted_reason"] = reason
        try:
            p.write_text(json.dumps(state, indent=2))
            log(f"marked {p.name} interrupted")
        except Exception as e:  # noqa: BLE001
            log(f"could not mark interrupted ({e})")


# --- monitor -----------------------------------------------------------------
def monitor(repo, handle, topic, stall_min, kill_mult, max_hours, ignore_dirs=frozenset()):
    proc = handle["proc"]
    stall_s, kill_s, max_s = stall_min * 60, stall_min * 60 * kill_mult, max_hours * 3600
    started = time.time()
    last_beat, last_beat_at = heartbeat_mtime(repo), time.time()
    last_head, last_done, stall_alerted = git_head(repo), -1, False

    def stop(kind, msg, tags):
        log(msg)
        ntfy(topic, f"night-shift: {kind}", f"{Path(repo).name}: {msg}", priority="high", tags=tags)
        terminate(handle)
        mark_interrupted(repo, msg)

    while True:
        rc = proc.poll()
        now = time.time()

        beat = heartbeat_mtime(repo)
        if beat > last_beat:
            last_beat, last_beat_at, stall_alerted = beat, now, False

        head = git_head(repo)
        if head and head != last_head:
            log(f"new commit {head[:8]}")
            last_head = head
        _, state = latest_state(repo, ignore_dirs)
        done, total = kr_progress(state)
        if total and done != last_done:
            if last_done >= 0 and done > last_done:
                ntfy(topic, "night-shift: progress",
                     f"{Path(repo).name}: {done}/{total} key results complete", tags="white_check_mark")
            last_done = done

        status = (state or {}).get("status")
        if status in TERMINAL:
            log(f"run reached terminal status: {status}")
            return status
        if rc is not None:
            log(f"launch process exited rc={rc}")
            _, state = latest_state(repo, ignore_dirs)
            return (state or {}).get("status") or f"exited-rc-{rc}"

        idle = now - last_beat_at
        if idle > kill_s:
            stop("KILLED (hung)", f"no progress for {int(idle//60)}m; killed", "skull")
            return "killed-hung"
        if idle > stall_s and not stall_alerted:
            log(f"stall: no progress for {int(idle)}s — alerting")
            ntfy(topic, "night-shift: stalled",
                 f"{Path(repo).name}: no progress for {int(idle//60)}m. "
                 f"Will kill at {int(kill_s//60)}m.", priority="high", tags="warning")
            stall_alerted = True
        if now - started > max_s:
            stop("max runtime hit", f"exceeded {max_hours}h wall clock; killed", "hourglass")
            return "killed-maxtime"

        time.sleep(POLL_SECONDS)


def read_manifest(path):
    try:
        import yaml  # provided by the enforcement install (python3-yaml)
    except ImportError:
        sys.exit("PyYAML unavailable; pass --repo/--ntfy-topic instead of --manifest")
    m = yaml.safe_load(Path(path).read_text())
    return {"repo": m.get("repo"), "ntfy": (m.get("notify") or {}).get("ntfy_topic")}


def main():
    ap = argparse.ArgumentParser(description="Supervise a night-shift run non-interactively.")
    ap.add_argument("--manifest", help="project manifest (for repo + ntfy topic)")
    ap.add_argument("--repo", help="project repo path (overrides manifest)")
    ap.add_argument("--objective", required=True, help="the shift objective, verbatim")
    ap.add_argument("--branch", default="ns/staging")
    ap.add_argument("--model", default="claude-opus-4-8",
                    help="model for the shift (empty string = account default)")
    ap.add_argument("--ntfy-topic", help="ntfy topic (overrides manifest)")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--stall-min", type=int, default=20, help="alert if no progress for N min")
    ap.add_argument("--kill-mult", type=int, default=2, help="kill at stall-min * this")
    ap.add_argument("--max-hours", type=float, default=9.0, help="wall-clock backstop")
    # container mode
    ap.add_argument("--container", metavar="IMAGE", help="run in this docker image instead of locally")
    ap.add_argument("--net", default="ns-net", help="docker network for container runs")
    ap.add_argument("--db-image", help="bring up a sibling Postgres on --net (e.g. postgres:16)")
    ap.add_argument("--db-name", help="database name for the sibling DB (default: derived from repo)")
    args = ap.parse_args()

    repo, topic = args.repo, args.ntfy_topic
    if args.manifest:
        mf = read_manifest(args.manifest)
        repo, topic = repo or mf["repo"], topic or mf["ntfy"]
    if not repo:
        sys.exit("need --repo or --manifest with repo:")
    repo = str(Path(repo).resolve())
    project = Path(repo).name

    if not (Path(repo) / ".night-shift" / "scope.yaml").exists():
        sys.exit(f"{repo} is not onboarded (no root-owned .night-shift/scope.yaml)")
    active = active_run_exists(repo)
    if active:
        sys.exit(f"a run is already active ({active}); finish or mark it interrupted first.")

    database_url = args.database_url
    if args.container:
        ensure_network(args.net)
        if args.db_image:
            db_name = args.db_name or project.replace("-", "_")
            database_url = ensure_db(args.net, project, args.db_image, db_name)
            log(f"sibling DB ready: DATABASE_URL points at ns-db-{project}")

    # Snapshot run dirs that already exist so the monitor ignores prior runs'
    # terminal state and only tracks the one this invocation is about to start.
    preexisting = {str(p.parent) for p in runs_dir(repo).glob("*/state.json")}

    log_path = Path(repo) / ".night-shift" / f"supervisor-{int(time.time())}.log"
    where = f"container {args.container}" if args.container else "locally"
    log(f"launching supervised run {where} in {repo}")
    log(f"  objective: {args.objective}")
    log(f"  branch: {args.branch} | model: {args.model or '(account default)'} "
        f"| ntfy: {topic or '(none)'} | log: {log_path}")
    ntfy(topic, "night-shift: started", f"{project}: {args.objective[:120]}", tags="new_moon")

    if args.container:
        handle, logf = launch_container(args.container, repo, args.objective, args.branch,
                                        database_url, args.model, args.net, log_path)
    else:
        handle, logf = launch_local(repo, args.objective, args.branch, database_url,
                                    args.model, log_path)

    try:
        outcome = monitor(repo, handle, topic, args.stall_min, args.kill_mult,
                          args.max_hours, preexisting)
    except KeyboardInterrupt:
        log("interrupted by operator — terminating run")
        terminate(handle)
        mark_interrupted(repo, "operator ctrl-c")
        outcome = "operator-abort"
    finally:
        logf.close()

    _, state = latest_state(repo, preexisting)
    done, total = kr_progress(state)
    hp = runs_dir(repo) / (state or {}).get("run_id", "") / "handoff.md"
    log(f"DONE — outcome={outcome} | key results {done}/{total} | handoff: {hp}")
    prio = "default" if outcome == "completed" else "high"
    ntfy(topic, f"night-shift: {outcome}",
         f"{project}: {done}/{total} key results. See handoff.md.",
         priority=prio, tags=("tada" if outcome == "completed" else "warning"))
    return 0 if outcome == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
