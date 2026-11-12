#!/usr/bin/env python3
"""§237 box-side remote actions for the mail trigger (stdlib only, no repo imports).

Runs ON the EC2 box. mail_trigger.py (WSL2) reaches it through
`janus_ec2.sh remote -- <action> ...`, which streams the origin/main copy of
this file into `python3 -` (so the newest version works even while the box's
checkout is pinned by a running flow). Never runs a shell; every argument is
validated here (the authoritative check).

Actions:
  kill [--hold] [--dry-run]   stop the running flow: janus-autorun-* tmux
                              sessions, then SIGTERM (20 s later SIGKILL) the
                              process groups of janus_autorun.sh / janus_agent.py /
                              make DESIGN_CONFIG / openroad / yosys / klayout.
                              The idle watcher is left alone, so the box stops
                              ~IDLE_MIN later unless --hold (touch ~/.janus_no_autostop)
                              or a new run is launched.
  status                      flow processes, latest autorun log tail, newest logs
  ls                          report + log files (newest first)
  hold [N] [FILE]             worst N min paths, condensed (default 4_cts_pre_repair.rpt)
  setup [N] [FILE]            worst N max paths, condensed (default janus_06_cts_paths.rpt)
  paths FILE [min|max] [N]    condensed paths from any report
  hist [FILE] [min|max] [DEPTH]  start/endpoint histogram by hierarchy prefix
                              (count + worst slack) over the paths in FILE
  hooks [PREFIX]              JANUS_* hook lines from logs PREFIX*.log (default all)
  grep REGEX FILE [MAX]       Python-regex lines (FILE = name or glob, no '/')
  tail FILE [N]               last N lines

FILE is a basename (globs allowed) looked up in the block's reports dir, then
logs dir. Block / platform / variant: --block (janus_soc_top) / env.
"""
import fnmatch
import os
import re
import signal
import subprocess
import sys
import time
from collections import OrderedDict
from pathlib import Path

ACTIONS = ("kill", "status", "ls", "hold", "setup", "paths", "hist", "hooks", "grep", "tail")
MAX_OUT = 60000
HOLD_FILE = Path.home() / ".janus_no_autostop"
KILL_PATS = [re.compile(p) for p in (
    r"janus_autorun\.sh", r"janus_agent\.py", r"janus_autorun/\S+\.run\.sh",
    r"\bmake\b.*DESIGN_CONFIG", r"(^|/)openroad(\s|$)", r"(^|/)yosys(\s|$)",
    r"(^|/)klayout(\s|$)")]
KEEP_PATS = [re.compile(p) for p in (r"idle_stop\.py", r"mail_trigger\.py", r"janus_remote\.py",
                                     r"python3 -\s+kill")]


class UsageError(Exception):
    pass


# ------------------------------------------------------------ locations ---
def repo():
    r = os.environ.get("JANUS_REPO") or os.getcwd()
    return Path(r).expanduser().resolve()


def flow_dir():
    cands = []
    if os.environ.get("JANUS_FLOW_DIR"):          # set by the agent for post scripts
        cands.append(Path(os.environ["JANUS_FLOW_DIR"]))
    if os.environ.get("ORFS_DIR"):
        cands.append(Path(os.environ["ORFS_DIR"]) / "flow")
    r = repo()
    cands += [r / "asic_flow/OpenROAD-flow-scripts/flow", r.parent / "OpenROAD-flow-scripts/flow",
              Path.home() / "OpenROAD-flow-scripts/flow"]
    for c in cands:
        if (c / "logs").is_dir() or (c / "reports").is_dir():
            return c
    raise UsageError("ORFS flow dir not found (tried: " + ", ".join(map(str, cands)) + ")")


def block_dirs(block, platform="asap7", variant="base"):
    f = flow_dir()
    out = []
    for kind in ("reports", "logs"):
        d = f / kind / platform / block / variant
        out.append(d if d.is_dir() else None)
    return out


def safe_name(name):
    if not name or "/" in name or "\\" in name or name.startswith(".") or "\0" in name:
        raise UsageError(f"file must be a plain basename (no '/', not hidden): {name!r}")
    return name


def find_file(name, block):
    safe_name(name)
    for d in block_dirs(block):
        if d is None:
            continue
        hits = sorted((p for p in d.iterdir() if p.is_file() and fnmatch.fnmatch(p.name, name)),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return hits[0]
    raise UsageError(f"{name}: not found in reports/logs of {block} (try: ls)")


def as_int(v, what, lo=1, hi=1000):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise UsageError(f"{what} must be an integer: {v!r}")
    if not lo <= n <= hi:
        raise UsageError(f"{what} must be in [{lo}, {hi}]: {n}")
    return n


# --------------------------------------------------------- STA reports ---
_PIN = re.compile(r"^\s*(?:\d+\s+)?(?:[-\d.]+\s+){0,4}([-\d.]+)\s+([-\d.]+)\s+[\^v]\s+(\S+)\s+\((\S+)\)")


def split_paths(text):
    """-> list of dicts {lines, start, end, type, group, slack} (OpenSTA report_checks)."""
    chunks = re.split(r"(?m)^(?=Startpoint:)", text)
    out = []
    for c in chunks:
        if not c.startswith("Startpoint:"):
            continue
        g = lambda rx: (re.search(rx, c, re.M) or [None, None])[1]  # noqa: E731
        sl = re.search(r"(?m)^\s*([-\d.]+)\s+slack\b", c)
        out.append({"lines": c.rstrip().splitlines(),
                    "start": (g(r"^Startpoint:\s*(\S+)") or "?"),
                    "end": (g(r"^Endpoint:\s*(\S+)") or "?"),
                    "type": (g(r"^Path Type:\s*(\S+)") or "?"),
                    "group": g(r"^Path Group:\s*(\S+)"),
                    "corner": g(r"^Corner:\s*(\S+)"),
                    "slack": float(sl.group(1)) if sl else None})
    return out


def _run_key(inst, cell):
    return (re.sub(r"\d+", "#", inst), cell)


def condense(lines, min_run=4):
    """Drop '(net)' lines; collapse >= min_run consecutive pins of the same
    instance pattern + cell into one summary line (delay of the run)."""
    body, out, i = [l for l in lines if not l.rstrip().endswith("(net)")], [], 0
    while i < len(body):
        m = _PIN.match(body[i])
        if not m:
            out.append(body[i]); i += 1; continue
        key = _run_key(m.group(3).split("/")[0], m.group(4))
        j = i
        while j + 1 < len(body):
            m2 = _PIN.match(body[j + 1])
            if not m2 or _run_key(m2.group(3).split("/")[0], m2.group(4)) != key:
                break
            j += 1
        if j - i + 1 >= min_run:
            t0 = float(_PIN.match(body[i]).group(2)) - float(m.group(1))
            t1 = float(_PIN.match(body[j]).group(2))
            out.append(body[i])
            out.append(f"      ... {j - i - 1} more {key[0]} ({key[1]}) ... "
                       f"run {j - i + 1} pins, {t1 - t0:+.1f} ps")
            out.append(body[j])
        else:
            out.extend(body[i:j + 1])
        i = j + 1
    return out


def q_paths(block, fname, ptype, n):
    p = find_file(fname, block)
    ps = [x for x in split_paths(p.read_text(errors="replace")) if ptype in (None, x["type"])]
    if not ps:
        return f"{p.name}: no {ptype or ''} paths"
    hdr = f"== {p.name} ({time.ctime(p.stat().st_mtime)}): {len(ps)} {ptype or ''} path(s), showing {min(n, len(ps))}"
    return "\n\n".join([hdr] + ["\n".join(condense(x["lines"])) for x in ps[:n]])


def prefix(name, depth):
    parts = re.split(r"[/.]", name)
    return "/".join(parts[:depth]) if len(parts) > depth else name


def q_hist(block, fname, ptype, depth):
    p = find_file(fname, block)
    ps = [x for x in split_paths(p.read_text(errors="replace")) if ptype in (None, x["type"])]
    if not ps:
        return f"{p.name}: no {ptype or ''} paths"
    out = [f"== {p.name}: {len(ps)} {ptype or ''} path(s) (only what the report holds), depth {depth}"]
    for side in ("end", "start"):
        agg = OrderedDict()
        for x in ps:
            k = prefix(x[side], depth)
            c, w = agg.get(k, (0, None))
            s = x["slack"]
            agg[k] = (c + 1, s if w is None or (s is not None and s < w) else w)
        out.append(f"\n-- {side}points --\n  count  worst(ps)  prefix")
        for k, (c, w) in sorted(agg.items(), key=lambda kv: -kv[1][0])[:25]:
            out.append(f"  {c:5d}  {('%.1f' % w) if w is not None else '?':>9}  {k}")
    return "\n".join(out)


# ------------------------------------------------------------- misc --------
def q_hooks(block, pfx):
    _, logs = block_dirs(block)
    if logs is None:
        raise UsageError("logs dir not found")
    pfx = safe_name(pfx) if pfx else ""
    out = []
    for f in sorted(logs.glob(f"{pfx}*.log")):
        hits = [l.rstrip() for l in f.read_text(errors="replace").splitlines() if l.startswith("JANUS_")]
        if hits:
            out.append(f"== {f.name} ({len(hits)})")
            out += hits[-150:]
    return "\n".join(out) or f"no JANUS_ lines in {pfx}*.log"


def q_grep(block, rx, fname, mx):
    try:
        r = re.compile(rx)
    except re.error as e:
        raise UsageError(f"bad regex {rx!r}: {e}")
    p = find_file(fname, block)
    hits = [l.rstrip() for l in p.read_text(errors="replace").splitlines() if r.search(l)]
    return f"== {p.name}: {len(hits)} match(es), showing {min(mx, len(hits))}\n" + "\n".join(hits[:mx])


def q_tail(block, fname, n):
    p = find_file(fname, block)
    return f"== {p.name}\n" + "\n".join(p.read_text(errors="replace").splitlines()[-n:])


def q_ls(block):
    out = []
    for d in block_dirs(block):
        if d is None:
            continue
        fs = sorted((p for p in d.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        out.append(f"== {d}")
        out += [f"  {time.strftime('%m-%d %H:%M', time.localtime(p.stat().st_mtime))} "
                f"{p.stat().st_size:>11,d}  {p.name}" for p in fs[:80]]
    return "\n".join(out)


def ps_rows():
    r = subprocess.run(["ps", "-eo", "pid=,pgid=,etimes=,args="], capture_output=True, text=True)
    rows = []
    for l in r.stdout.splitlines():
        f = l.split(None, 3)
        if len(f) == 4:
            rows.append((int(f[0]), int(f[1]), int(f[2]), f[3]))
    return rows


def ancestors():
    a, p = set(), os.getpid()
    while p > 1 and p not in a:
        a.add(p)
        try:
            p = int(Path(f"/proc/{p}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except Exception:
            break
    return a


def flow_procs():
    me, mypg = ancestors(), os.getpgrp()
    return [r for r in ps_rows()
            if r[0] not in me and r[1] != mypg
            and any(p.search(r[3]) for p in KILL_PATS)
            and not any(p.search(r[3]) for p in KEEP_PATS)]


def fmt_procs(rows):
    return "\n".join(f"  pid {p:>7} pgid {g:>7} up {e // 3600}h{e % 3600 // 60:02d}m  {a[:160]}"
                     for p, g, e, a in rows) or "  (none)"


def q_status(block):
    out = ["== flow processes", fmt_procs(flow_procs())]
    lg = Path.home() / "janus_autorun/latest.log"
    if lg.exists():
        out += [f"\n== {lg.resolve().name} (tail)"] + lg.read_text(errors="replace").splitlines()[-20:]
    out.append(f"\nhold file {'PRESENT (no auto-stop)' if HOLD_FILE.exists() else 'absent'}")
    _, logs = block_dirs(block)
    if logs is not None:
        fs = sorted(logs.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
        out += ["\n== newest stage logs"] + [
            f"  {time.strftime('%m-%d %H:%M', time.localtime(p.stat().st_mtime))} {p.name}" for p in fs]
    return "\n".join(out)


def tmux_sessions():
    try:
        r = subprocess.run(["tmux", "ls", "-F", "#S"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [s for s in r.stdout.split() if s.startswith("janus-autorun-")]


def q_kill(hold=False, dry=False, grace=20):
    rows, sess = flow_procs(), tmux_sessions()
    out = ["== flow processes before kill", fmt_procs(rows),
           f"tmux sessions: {' '.join(sess) or '(none)'}"]
    if dry:
        return "\n".join(out + ["(dry-run: nothing killed)"])
    if hold:
        HOLD_FILE.touch()
        out.append(f"touched {HOLD_FILE} (box will NOT auto-stop; rm it when done)")
    if not rows and not sess:
        return "\n".join(out + ["nothing to kill"])
    for s in sess:
        subprocess.run(["tmux", "kill-session", "-t", s], capture_output=True)
    pgs = sorted({g for _, g, _, _ in rows})
    for g in pgs:
        try:
            os.killpg(g, signal.SIGTERM)
        except OSError:
            pass
    t0 = time.time()
    while time.time() - t0 < grace and flow_procs():
        time.sleep(1)
    left = flow_procs()
    for p, g, _, _ in left:
        for fn, x in ((os.killpg, g), (os.kill, p)):
            try:
                fn(x, signal.SIGKILL)
            except OSError:
                pass
    time.sleep(1)
    rest = flow_procs()
    out += [f"SIGTERM -> {len(pgs)} process group(s); SIGKILL after {grace}s -> {len(left)} left",
            "== after", fmt_procs(rest), "KILLED" if not rest else "WARNING: processes survived"]
    if not hold:
        out.append("idle watcher untouched: box stops ~IDLE_MIN min from now unless a run is launched")
    return "\n".join(out)


# ------------------------------------------------------------- dispatch ---
def run(argv):
    block = "janus_soc_top"
    if len(argv) >= 2 and argv[0] == "--block":
        block = safe_name(argv[1]); argv = argv[2:]
    if not argv or argv[0] not in ACTIONS:
        raise UsageError(f"action must be one of: {' '.join(ACTIONS)}")
    a, rest = argv[0], argv[1:]
    if a == "kill":
        bad = [x for x in rest if x not in ("--hold", "--dry-run")]
        if bad:
            raise UsageError(f"kill: unknown option(s) {bad}")
        return q_kill("--hold" in rest, "--dry-run" in rest)
    if a == "status":
        return q_status(block)
    if a == "ls":
        return q_ls(block)
    if a in ("hold", "setup"):
        n = as_int(rest[0], "N", 1, 20) if rest else 1
        f = rest[1] if len(rest) > 1 else ("4_cts_pre_repair.rpt" if a == "hold" else "janus_06_cts_paths.rpt")
        return q_paths(block, f, "min" if a == "hold" else "max", n)
    if a == "paths":
        if not rest:
            raise UsageError("paths FILE [min|max] [N]")
        t = rest[1] if len(rest) > 1 and rest[1] in ("min", "max") else None
        n = as_int(rest[2 if t else 1], "N", 1, 20) if len(rest) > (2 if t else 1) else 2
        return q_paths(block, rest[0], t, n)
    if a == "hist":
        f = rest[0] if rest else "4_cts_pre_repair.rpt"
        t = rest[1] if len(rest) > 1 else "min"
        if t not in ("min", "max"):
            raise UsageError("hist type must be min or max")
        d = as_int(rest[2], "DEPTH", 1, 8) if len(rest) > 2 else 2
        return q_hist(block, f, t, d)
    if a == "hooks":
        return q_hooks(block, rest[0] if rest else "")
    if a == "grep":
        if len(rest) < 2:
            raise UsageError("grep REGEX FILE [MAX]")
        return q_grep(block, rest[0], rest[1], as_int(rest[2], "MAX", 1, 2000) if len(rest) > 2 else 200)
    if a == "tail":
        if not rest:
            raise UsageError("tail FILE [N]")
        return q_tail(block, rest[0], as_int(rest[1], "N", 1, 2000) if len(rest) > 1 else 60)
    raise UsageError(a)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        out = run(argv)
        rc = 0
    except UsageError as e:
        out, rc = f"ERROR: {e}", 2
    except Exception as e:
        out, rc = f"ERROR: {type(e).__name__}: {e}", 1
    if len(out) > MAX_OUT:
        out = out[:MAX_OUT] + f"\n... [truncated at {MAX_OUT} chars]"
    print(out)
    return rc


if __name__ == "__main__":
    sys.exit(main())
