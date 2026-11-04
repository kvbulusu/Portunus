#!/usr/bin/env python3
"""§196 EC2 idle auto-stop watchdog (stdlib only, runs as root from a systemd timer).

Every 5 min (janus-idle-stop.timer) this decides BUSY or IDLE:
  BUSY if any of
    hold      HOLD_FILE exists (default /home/ubuntu/.janus_no_autostop)
    proc      a flow process is alive (janus_agent.py, openroad, yosys, make
              DESIGN_CONFIG=..., verilator, klayout, rsync/scp, apt/dpkg, ...)
    tty       someone typed in a terminal (ssh / tmux / VS Code terminal) within
              IDLE_MINUTES (atime of /dev/pts/N = last input, same as `w`)
    load      5-min load average > LOAD_MAX
  IDLE otherwise. The time of the last BUSY check is kept in STATE_DIR (on /run,
  a tmpfs, so every boot starts a fresh grace period). Once idle for
  IDLE_MINUTES - WARN_MINUTES a `wall` warning is sent; at IDLE_MINUTES it emails
  (if SMTP_* configured) and runs STOP_CMD (default `shutdown -h now`). On EC2 an
  OS shutdown = instance STOP when instanceInitiatedShutdownBehavior=stop (the
  default). If the AWS CLI can read that attribute and it says `terminate`, the
  stop is REFUSED (would delete the box) unless ALLOW_TERMINATE=1.

Config: KEY=VALUE file (default /etc/janus/idle_stop.conf), env vars override.
CLI: idle_stop.py [--conf F] [--dry-run] [--status]

§207 --watch: the same decision in a user-space loop (no systemd needed), armed
by `janus_agent.py --auto-stop-idle MIN` / janus_autorun.sh at launch. Ticks
every --interval s (default 60) with its own STATE_DIR/LOG_FILE under the
user's home/tmp, is a singleton (pid file), and runs `sudo -n shutdown -h now`
when not root. Exits after STOP, REFUSED or DISABLED.
"""
import argparse
import json
import os
import re
import shlex
import smtplib
import socket
import subprocess
import sys
import time
import urllib.request
from email.mime.text import MIMEText

DEFAULTS = {
    "IDLE_MINUTES": "30",
    "WARN_MINUTES": "5",
    "LOAD_MAX": "1.0",
    "HOLD_FILE": "/home/ubuntu/.janus_no_autostop",
    "STATE_DIR": "/run/janus-idle-stop",
    "LOG_FILE": "/var/log/janus-idle-stop.log",
    "STOP_CMD": "shutdown -h now",
    "ALLOW_TERMINATE": "0",
    "EXTRA_BUSY_PATTERNS": "",
    "PROC_ROOT": "/proc",
    "PTS_DIR": "/dev/pts",
    "ENABLED": "1",
    "NOTIFY_EMAIL": "", "SMTP_HOST": "", "SMTP_PORT": "587", "SMTP_USER": "", "SMTP_PASS": "",
}

# regexes on the full command line (args joined by spaces)
BUSY_PATTERNS = [
    r"janus_agent\.py",
    r"(^|/)openroad(\s|$)",
    r"(^|/)yosys(\s|$)",
    r"(^|/)sta(\s|$)",
    r"(^|/)klayout(\s|$)",
    r"(^|/)(verilator|verilator_bin|iverilog|vvp)(\s|$)",
    r"(^|/)make\s.*DESIGN_CONFIG",
    r"golden_promote\.sh|restore_block_from_golden\.sh|save_run_reports\.sh",
    r"(^|/)(rsync|scp|sftp-server)(\s|$)",
    r"(^|/)(apt|apt-get|dpkg)(\s|$)",
    r"(^|/)pytest(\s|$)|-m\s+pytest",
    r"(^|/)(tar|gzip|pigz|zstd)(\s|$)",
    r"(^|/)(pip|pip3)\s+install",
    r"(^|/)git\s+(clone|fetch|pull|push)",
]


def load_conf(path):
    c = dict(DEFAULTS)
    lines = []
    if path and os.path.isfile(path):
        try:   # §207: the root-only 600 conf is unreadable for the --watch user
            with open(path) as fh:
                lines = fh.readlines()
        except OSError:
            lines = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        c[k] = v
    for k in list(c):
        if os.environ.get("JIS_" + k) is not None:      # test / one-off override
            c[k] = os.environ["JIS_" + k]
    return c


def busy_processes(proc_root, patterns, self_pid=None):
    """[(pid, cmdline)] of processes matching any pattern."""
    rx = [re.compile(p) for p in patterns if p]
    hits = []
    try:
        pids = [d for d in os.listdir(proc_root) if d.isdigit()]
    except OSError:
        return hits
    me = str(self_pid if self_pid is not None else os.getpid())
    for pid in pids:
        if pid == me:
            continue
        try:
            with open(os.path.join(proc_root, pid, "cmdline"), "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if not cmd or "idle_stop.py" in cmd:
            continue
        if any(r.search(cmd) for r in rx):
            hits.append((int(pid), cmd[:160]))
    return hits


def tty_idle_seconds(pts_dir, now):
    """Smallest idle time over open ptys (None if no pty)."""
    best = None
    try:
        names = [n for n in os.listdir(pts_dir) if n.isdigit()]
    except OSError:
        return None
    for n in names:
        try:
            st = os.stat(os.path.join(pts_dir, n))
        except OSError:
            continue
        idle = max(0.0, now - st.st_atime)
        best = idle if best is None else min(best, idle)
    return best


def load5(proc_root):
    try:
        with open(os.path.join(proc_root, "loadavg")) as fh:
            return float(fh.read().split()[1])
    except (OSError, ValueError, IndexError):
        return 0.0


def busy_reasons(c, now):
    out = []
    if os.path.exists(c["HOLD_FILE"]):
        out.append(f"hold file {c['HOLD_FILE']}")
    pats = BUSY_PATTERNS + [p for p in c["EXTRA_BUSY_PATTERNS"].split("|") if p.strip()]
    procs = busy_processes(c["PROC_ROOT"], pats)
    if procs:
        out.append(f"{len(procs)} flow process(es), e.g. pid {procs[0][0]}: {procs[0][1]}")
    idle_min = float(c["IDLE_MINUTES"])
    ti = tty_idle_seconds(c["PTS_DIR"], now)
    if ti is not None and ti < idle_min * 60:
        out.append(f"terminal input {ti / 60:.0f} min ago")
    la = load5(c["PROC_ROOT"])
    if la > float(c["LOAD_MAX"]):
        out.append(f"load5 {la:.2f} > {c['LOAD_MAX']}")
    return out


def _state(c, name, value=None):
    p = os.path.join(c["STATE_DIR"], name)
    if value is None:
        try:
            with open(p) as fh:
                return fh.read().strip()
        except OSError:
            return None
    os.makedirs(c["STATE_DIR"], exist_ok=True)
    with open(p, "w") as fh:
        fh.write(str(value))
    return value


def log(c, msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line)
    try:
        lf = c["LOG_FILE"]
        if os.path.isfile(lf) and os.path.getsize(lf) > (1 << 20):
            os.replace(lf, lf + ".1")
        with open(lf, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _imds(path, timeout=2):
    try:
        req = urllib.request.Request("http://169.254.169.254/latest/api/token", method="PUT",
                                     headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        tok = urllib.request.urlopen(req, timeout=timeout).read().decode()
        req = urllib.request.Request("http://169.254.169.254/latest/" + path,
                                     headers={"X-aws-ec2-metadata-token": tok})
        return urllib.request.urlopen(req, timeout=timeout).read().decode().strip()
    except Exception:
        return ""


def shutdown_behavior():
    """'stop' | 'terminate' | '' (unknown: no AWS CLI / no IAM permission)."""
    iid = _imds("meta-data/instance-id")
    region = _imds("meta-data/placement/region")
    if not iid or not region:
        return ""
    try:
        out = subprocess.run(["aws", "--region", region, "ec2", "describe-instance-attribute",
                              "--instance-id", iid, "--attribute", "instanceInitiatedShutdownBehavior",
                              "--output", "json"], capture_output=True, text=True, timeout=20)
        if out.returncode != 0:
            return ""
        return json.loads(out.stdout)["InstanceInitiatedShutdownBehavior"]["Value"]
    except Exception:
        return ""


def send_email(c, subject, body):
    if not (c["NOTIFY_EMAIL"] and c["SMTP_HOST"] and c["SMTP_USER"] and c["SMTP_PASS"]):
        return "email not configured"
    try:
        msg = MIMEText(body)
        msg["Subject"], msg["From"], msg["To"] = subject, c["SMTP_USER"], c["NOTIFY_EMAIL"]
        port = int(c["SMTP_PORT"] or 587)
        if port == 465:
            s = smtplib.SMTP_SSL(c["SMTP_HOST"], port, timeout=30)
        else:
            s = smtplib.SMTP(c["SMTP_HOST"], port, timeout=30)
            s.starttls()
        s.login(c["SMTP_USER"], c["SMTP_PASS"])
        s.sendmail(c["SMTP_USER"], [c["NOTIFY_EMAIL"]], msg.as_string())
        s.quit()
        return "email sent"
    except Exception as e:  # never block the stop on mail
        return f"email failed: {type(e).__name__}: {e}"


def wall(msg):
    try:
        subprocess.run(["wall", msg], timeout=10, capture_output=True)
    except Exception:
        pass


def check(c, now=None, dry_run=False, behavior_fn=shutdown_behavior):
    """One timer tick. Returns (action, detail): BUSY|IDLE|WARN|STOP|REFUSED|DISABLED."""
    now = time.time() if now is None else now
    if c["ENABLED"] != "1":
        return "DISABLED", "ENABLED!=1"
    idle_s = float(c["IDLE_MINUTES"]) * 60
    warn_s = max(0.0, idle_s - float(c["WARN_MINUTES"]) * 60)
    reasons = busy_reasons(c, now)
    last = _state(c, "last_busy")
    if reasons or last is None:          # busy, or first tick after boot (grace)
        _state(c, "last_busy", now)
        _state(c, "warned", 0)
        _state(c, "last_reason", "; ".join(reasons) or "boot grace")
        return "BUSY", "; ".join(reasons) or "first check since boot (grace period starts)"
    idle_for = now - float(last)
    detail = f"idle {idle_for / 60:.1f} min (last busy: {_state(c, 'last_reason') or '?'})"
    if idle_for < warn_s:
        return "IDLE", detail
    if idle_for < idle_s:
        if _state(c, "warned") != "1":
            _state(c, "warned", 1)
            if not dry_run:
                wall(f"JANUS idle auto-stop: this EC2 box stops in ~{(idle_s - idle_for) / 60:.0f} min "
                     f"(idle {idle_for / 60:.0f} min). Cancel: touch {c['HOLD_FILE']}")
        return "WARN", detail
    beh = behavior_fn()
    if beh == "terminate" and c["ALLOW_TERMINATE"] != "1":
        return "REFUSED", (detail + " -- instanceInitiatedShutdownBehavior=terminate: an OS shutdown would "
                           "DELETE the instance. Run `janus_ec2.sh idle-alarm` (sets it to stop) or "
                           "ALLOW_TERMINATE=1.")
    if dry_run:
        return "STOP", detail + f" (dry-run: would run '{c['STOP_CMD']}', shutdown behavior '{beh or 'unknown'}')"
    host = socket.gethostname()
    m = send_email(c, f"[JANUS] EC2 {host} auto-stopped after {idle_for / 60:.0f} min idle",
                   f"{detail}\nShutdown behavior: {beh or 'unknown (assumed stop)'}\n"
                   f"Start again from WSL: ./janus_ec2.sh start\n"
                   f"Disable: touch {c['HOLD_FILE']}  or  ENABLED=0 in /etc/janus/idle_stop.conf\n")
    _state(c, "last_busy", now)       # never loop on a failed stop
    log(c, f"STOP {detail}; {m}; running: {c['STOP_CMD']}")
    wall("JANUS idle auto-stop: stopping this EC2 instance NOW.")
    subprocess.run(shlex.split(c["STOP_CMD"]), timeout=60)
    return "STOP", detail


def _watch_defaults(c):
    """User-space paths + sudo stop for the --watch loop (unless overridden
    via JIS_* env). /tmp is wiped at boot, so each boot starts fresh."""
    uid = os.getuid()
    if not os.environ.get("JIS_STATE_DIR"):
        c["STATE_DIR"] = f"/tmp/janus-idle-watch-{uid}"
    if not os.environ.get("JIS_LOG_FILE"):
        c["LOG_FILE"] = os.path.expanduser("~/.janus_idle_watch.log")
    if not os.environ.get("JIS_STOP_CMD") and uid != 0:
        c["STOP_CMD"] = "sudo -n shutdown -h now"
    return c


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return b"idle_stop.py" in fh.read()
    except OSError:
        return True


def watch(c, interval=60.0, dry_run=False, max_ticks=None, sleep=time.sleep,
          behavior_fn=shutdown_behavior):
    """Loop check() until STOP / REFUSED / DISABLED (or max_ticks). Returns
    (exit_code, last_action). Singleton via STATE_DIR/watch.pid."""
    os.makedirs(c["STATE_DIR"], exist_ok=True)
    pidf = os.path.join(c["STATE_DIR"], "watch.pid")
    try:
        old = int(open(pidf).read().strip())
    except (OSError, ValueError):
        old = 0
    if old and old != os.getpid() and _pid_alive(old):
        print(f"idle watcher already running (pid {old}); not starting another")
        return 0, "ALREADY"
    with open(pidf, "w") as fh:
        fh.write(str(os.getpid()))
    if c["STOP_CMD"].startswith("sudo -n") and not dry_run:
        r = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=10)
        if r.returncode != 0:
            log(c, "WARN watch: passwordless sudo unavailable -- the STOP will fail; "
                   "run as root or add a sudoers rule for shutdown")
    log(c, f"WATCH start pid {os.getpid()}: stop after {c['IDLE_MINUTES']} min idle, "
           f"tick {interval:.0f}s, stop cmd '{c['STOP_CMD']}'")
    prev, ticks = None, 0
    try:
        while True:
            act, det = check(c, dry_run=dry_run, behavior_fn=behavior_fn)
            if act != prev or act in ("WARN", "STOP", "REFUSED"):
                log(c, f"WATCH {act} {det}")
            prev = act
            if act == "REFUSED":
                return 2, act
            if act in ("STOP", "DISABLED"):
                return 0, act
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return 0, act
            sleep(interval)
    finally:
        try:
            if open(pidf).read().strip() == str(os.getpid()):
                os.remove(pidf)
        except OSError:
            pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--conf", default="/etc/janus/idle_stop.conf")
    ap.add_argument("--dry-run", action="store_true", help="decide and print, never stop/wall")
    ap.add_argument("--status", action="store_true", help="show busy reasons + idle time (read-only)")
    ap.add_argument("--watch", action="store_true",
                    help="§207: loop in user space until the box is idle, then stop it")
    ap.add_argument("--idle-minutes", type=float, default=None, help="override IDLE_MINUTES")
    ap.add_argument("--interval", type=float, default=60.0, help="--watch tick seconds")
    a = ap.parse_args(argv)
    c = load_conf(a.conf)
    if a.idle_minutes is not None:
        c["IDLE_MINUTES"] = str(a.idle_minutes)
        if float(c["WARN_MINUTES"]) >= a.idle_minutes:
            c["WARN_MINUTES"] = str(max(0.0, a.idle_minutes / 3))
    if a.watch:
        _watch_defaults(c)
        return watch(c, interval=a.interval, dry_run=a.dry_run)[0]
    if a.status:
        now = time.time()
        r = busy_reasons(c, now)
        last = _state(c, "last_busy")
        print(f"enabled      : {c['ENABLED']}  (idle limit {c['IDLE_MINUTES']} min, load max {c['LOAD_MAX']})")
        print(f"busy now     : {'; '.join(r) if r else 'NO'}")
        print(f"last busy    : {time.ctime(float(last)) if last else 'never (timer not run since boot)'}"
              + (f"  ({(now - float(last)) / 60:.1f} min ago)" if last else ""))
        print(f"hold file    : {c['HOLD_FILE']} ({'present' if os.path.exists(c['HOLD_FILE']) else 'absent'})")
        return 0
    act, det = check(c, dry_run=a.dry_run)
    if act in ("WARN", "STOP", "REFUSED") or a.dry_run:
        log(c, f"{act} {det}")
    else:
        print(f"{act} {det}")
    return 2 if act == "REFUSED" else 0


if __name__ == "__main__":
    sys.exit(main())
