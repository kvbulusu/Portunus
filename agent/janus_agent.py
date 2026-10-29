#!/usr/bin/env python3
"""
janus_agent.py — Autonomous ASIC synthesis orchestrator for the JANUS SoC.

WHAT THIS DOES
--------------
Drives OpenROAD-flow-scripts (ORFS) synthesis + place-and-route for the JANUS
blocks, respecting hierarchical dependencies. It runs the flow LOCALLY (on your
WSL2 machine). It only calls the Abacus LLM when a block FAILS — to diagnose the
log and propose a fix — so you spend credits on errors, not on routine runs.

Loop per block:
    make (ORFS)  ->  parse log
        ok      ->  mark DONE, unblock dependents
        fail    ->  send a small log excerpt to the LLM  ->  apply fix  ->  retry
                    (up to --max-retries; rollback the fix if the retry is no better)

IMPORTANT — HONESTY NOTE
------------------------
This tool has NOT yet been run against JANUS RTL on real hardware/PDK by the
author of this repo. Every clock period / utilization / area number you see in
configs or reports is a DESIGN-INTENT TARGET, not a measured silicon result.
The agent records whatever ORFS actually reports; if a metric is missing it is
shown as "n/a" rather than guessed. Do not treat any number here as a verified
PPA result until you have run the flow yourself and inspected the ORFS outputs.

USAGE
-----
    python3 janus_agent.py --estimate                 # plan only, no runs, no LLM
    python3 janus_agent.py --dry-run                  # print the make commands
    python3 janus_agent.py                            # run all blocks, in order
    python3 janus_agent.py --blocks security noc      # run a subset
    python3 janus_agent.py --parallel 2               # run independent blocks together
    python3 janus_agent.py --resume                   # skip blocks already DONE
    python3 janus_agent.py --skip llm                 # never call the LLM (manual mode)
    python3 janus_agent.py --skip golden signoff      # disable optional stages (see --skip)
    python3 janus_agent.py --adaptive auto            # bounded variant rerun on hard blocks
    python3 janus_agent.py --interactive              # prompt on the shell before LLM/fixes
    python3 janus_agent.py --git-push                 # also push each committed fix to origin

HIERARCHICAL ABSTRACTS
----------------------
After a block hardens (reaches DONE), the agent runs the ORFS abstract target
(auto-detected from flow/Makefile; canonically `generate_abstract`) to emit that
block's <name>.lef + <name>.lib. janus_soc_top consumes those as hard macros for
bottom-up SoC assembly (peak RAM = the largest single block, not the whole SoC).
Disable with --skip abstract. The top level itself is skipped (it is not a macro).

SELF-HEALING + GIT (the "fix once, git pull, rerun" loop)
---------------------------------------------------------
On a REAL RTL/config failure the agent asks the LLM, applies a minimal fix, and
commits it to the CURRENT branch (disable with --skip git-commit; also push with
--git-push). config/SDC fixes are MIRRORED into the repo source
(asic_flow/configs/...) so they survive a scripts/03 re-stamp — otherwise the
next re-stamp would silently revert them. Your loop then becomes:

    (agent) fix + commit   ->   (you) git pull  ->  source the block configs /
    scripts/03_setup_janus_configs.sh  ->  re-run with --resume

Environment/infra errors (missing .so, OOM, missing tool) are NEVER sent to the
LLM — they are triaged deterministically with a precise manual instruction, so
credits are spent only on genuine RTL/config bugs.

Requires the env file written by the install scripts:
    source ~/janus_asic/janus_asic_env.sh
(or pass --orfs / --rtl-root explicitly).
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                wait, FIRST_COMPLETED)
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# --- make sibling (agent/) and ../utils importable, however we're invoked -----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)              # .../asic_flow
# §145: hardened-block abstract outputs (results leaf); never seeded/backfilled
# from a superseded leaf, regenerated after an adaptive promotion.
_ABSTRACT_EXTS = (".lef", ".lib", ".lib.gz")
# §145: default per-pass bound for --adaptive auto. A pass re-runs 3_place ->
# 6_final; detailed route alone takes 24-48 h on the big blocks, so 72 h.
# JANUS_ADAPTIVE_TIMEOUT_S overrides (seconds; 0 = no limit).
_ADAPTIVE_TIMEOUT_DEFAULT_S = 72 * 3600


def adaptive_timeout_s() -> Optional[int]:
    """Per-pass timeout in seconds, or None for no limit."""
    raw = os.environ.get("JANUS_ADAPTIVE_TIMEOUT_S", "").strip()
    try:
        v = int(float(raw)) if raw else _ADAPTIVE_TIMEOUT_DEFAULT_S
    except ValueError:
        v = _ADAPTIVE_TIMEOUT_DEFAULT_S
    return v if v > 0 else None
for _p in (_HERE, os.path.join(_ROOT, "utils")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dependency_manager import DependencyManager, Status, DEFAULT_GRAPH  # noqa: E402
from log_analyzer import (  # noqa: E402
    LogAnalyzer, classify_failure, read_block_timing, timing_verdict)
from llm_interface import AbacusLLM                                      # noqa: E402
from fix_applier import FixApplier                                       # noqa: E402
from git_ops import GitOps                                               # noqa: E402
from physical_analyzer import (detect_macro_fit_failure,                 # noqa: E402
                               analyze_and_recommend, MacroGeom)
from reasoning_engine import reason as reason_failure                    # noqa: E402
from reasoning_engine import reason_with_reports                         # noqa: E402
from dependency_manager import DEFAULT_GRAPH as _DEP_GRAPH               # noqa: E402
from checkpoint import Checkpoint                                        # noqa: E402
from flow_state import FlowState                                        # noqa: E402
from reporter import Reporter                                           # noqa: E402
from notification import NotificationManager                            # noqa: E402
from milestones import (MilestoneTracker, milestones_enabled,            # noqa: E402
                        config_uses_io_shadow)
from timing_classifier import classify_routed_odb, render as tc_render   # noqa: E402
from incremental_opt import (run_eco_pass, decide_timing, decide_drc,     # noqa: E402
                             adopt_odb, discard_odb, ECOResult,
                             parse_eco, parse_eco_pre)
try:
    from dashboard import Dashboard, DashboardState  # noqa: E402
    _HAS_DASHBOARD = True
except Exception:  # pragma: no cover — dashboard is optional
    _HAS_DASHBOARD = False

# rich is optional — degrade gracefully to plain prints if it's missing.
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    _RICH = True
except Exception:  # pragma: no cover
    _RICH = False


# ---------------------------------------------------------------------------- #
# Console helper (works with or without rich)
# ---------------------------------------------------------------------------- #
class UI:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.console = Console() if _RICH else None
        self._lock = threading.Lock()
        # When the live dashboard owns the terminal we suppress routine
        # scrolling output (info/good/step/debug) so only errors/warnings
        # scroll above the pinned dashboard. Toggled by the agent.
        self.quiet = False

    def _emit(self, msg: str, style: str = "") -> None:
        with self._lock:
            if self.console:
                self.console.print(msg, style=style)
            else:
                print(msg)

    def info(self, msg: str) -> None:
        if self.quiet:
            return
        self._emit(msg)

    def good(self, msg: str) -> None:
        if self.quiet:
            return
        self._emit(msg, "bold green")

    def warn(self, msg: str) -> None:
        self._emit(msg, "yellow")

    def err(self, msg: str) -> None:
        self._emit(msg, "bold red")

    def step(self, msg: str) -> None:
        if self.quiet:
            return
        self._emit(f"[*] {msg}", "bold cyan")

    def debug(self, msg: str) -> None:
        if self.verbose:
            self._emit(f"    {msg}", "dim")

    def panel(self, title: str, body: str) -> None:
        with self._lock:
            if self.console:
                self.console.print(Panel(body, title=title, expand=False))
            else:
                print(f"\n=== {title} ===\n{body}\n")

    def table(self, rows: List[Dict]) -> None:
        if not rows:
            return
        with self._lock:
            if self.console:
                t = Table(title="JANUS block status  (ASAP7 slacks in ps; -ve = violation)",
                          show_lines=False)
                # setup WNS and hold WS are BOTH worst-slack in picoseconds.
                for col in ("block", "status", "tries",
                            "setup WNS(ps)", "hold WS(ps)", "DRC", "note"):
                    t.add_column(col)
                for r in rows:
                    t.add_row(
                        str(r.get("block", "")),
                        str(r.get("status", "")),
                        str(r.get("tries", 0)),
                        _fmt(r.get("wns_ns")),
                        _fmt(r.get("hold_ps")),
                        _fmt(r.get("drc_violations")),
                        str(r.get("note", ""))[:48],
                    )
                self.console.print(t)
            else:
                print("\n# ASAP7 slacks in picoseconds (ps); negative = violation")
                print("block           status     tries  setupWNS(ps) holdWS(ps) DRC   note")
                for r in rows:
                    print("{:<15} {:<10} {:<6} {:<12} {:<10} {:<5} {}".format(
                        r.get("block", ""), r.get("status", ""), r.get("tries", 0),
                        _fmt(r.get("wns_ns")), _fmt(r.get("hold_ps")),
                        _fmt(r.get("drc_violations")),
                        str(r.get("note", ""))[:48]))


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:+.3f}" if v < 0 or v > 0 else "0.000"
    return str(v)


# ---------------------------------------------------------------------------- #
# System resource monitor  (no psutil — pure /proc parsing)
# ---------------------------------------------------------------------------- #
class SystemMonitor:
    """Sample system-wide CPU utilisation and memory from /proc only.

    CPU%: busy-vs-total jiffy delta in /proc/stat between consecutive
    ``cpu_percent()`` calls.  Memory: MemTotal / MemAvailable from
    /proc/meminfo.  Every reader is wrapped so a missing or unreadable
    /proc (non-Linux, container restriction, …) degrades to ``None`` —
    which the dashboard renders as "N/A" — instead of raising.
    """

    def __init__(self) -> None:
        self._prev = self._read_cpu_sample()

    @staticmethod
    def _read_cpu_sample():
        """Return (idle_jiffies, total_jiffies) from /proc/stat, or None."""
        try:
            with open("/proc/stat") as fh:
                for line in fh:
                    if line.startswith("cpu "):
                        parts = [int(x) for x in line.split()[1:]]
                        if len(parts) < 4:
                            return None
                        # idle + iowait count as "not busy".
                        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
                        total = sum(parts)
                        return idle, total
        except (OSError, ValueError):
            return None
        return None

    def cpu_percent(self):
        """System CPU busy % since the previous call, or None if unavailable."""
        cur = self._read_cpu_sample()
        if cur is None or self._prev is None:
            self._prev = cur
            return None
        idle0, total0 = self._prev
        idle1, total1 = cur
        self._prev = cur
        dt = total1 - total0
        di = idle1 - idle0
        if dt <= 0:
            return None
        return max(0.0, min(100.0, 100.0 * (dt - di) / dt))

    @staticmethod
    def mem_info():
        """Return (used_gb, total_gb, used_pct) or None if unavailable."""
        try:
            vals = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    if key in ("MemTotal", "MemAvailable"):
                        vals[key] = int(rest.strip().split()[0])  # kB
                    if len(vals) == 2:
                        break
            total_kb = vals.get("MemTotal")
            if not total_kb:
                return None
            avail_kb = vals.get("MemAvailable", 0)
            used_kb = max(0, total_kb - avail_kb)
            gb = 1024.0 * 1024.0
            pct = 100.0 * used_kb / total_kb if total_kb else 0.0
            return used_kb / gb, total_kb / gb, pct
        except (OSError, ValueError, IndexError):
            return None


# ---------------------------------------------------------------------------- #
# Live in-place dashboard  (rich.live if available, else raw-ANSI fallback)
# ---------------------------------------------------------------------------- #
class DashboardUI:
    """htop-style in-place dashboard for a (possibly parallel) agent run.

    Reads the agent's shared live state (per-block stage / progress / elapsed /
    WNS registered by ``_run_make``) plus a :class:`SystemMonitor` CPU+memory
    row and repaints a single status table in place every ``refresh`` seconds.
    Routine scrolling output is suppressed while this is active (see
    ``UI.quiet``); only errors/warnings still scroll (rich prints them above
    the live region).  The existing final summary table is untouched — it is
    printed by ``_finalize`` after :meth:`stop`.

    Rendering backends, in order of preference:
      * rich.live.Live  (auto-refreshing renderable) when ``rich`` is present;
      * a raw-ANSI (cursor-reposition + clear) repaint thread otherwise.
    The caller only starts this on a real TTY; a piped/redirected stdout keeps
    the classic scrolling output.
    """

    _STAGE_TOTAL = 6

    def __init__(self, agent, refresh: float = 2.5) -> None:
        self.agent = agent
        self.refresh = max(1.0, float(refresh))
        self.sysmon = SystemMonitor()
        self.console = agent.ui.console if _RICH else None
        self._rich = bool(_RICH and self.console is not None)
        self._live = None
        self._thread = None
        self._stop = threading.Event()
        self._started = False
        # throttled system-resource cache (so a fast rich refresh does not
        # shrink the CPU sampling window to a jumpy sub-second value)
        self._last_sample = 0.0
        self._cpu_cache = None
        self._mem_cache = None

    # ------------------------------------------------------------------ data
    def _sys_strings(self):
        now = time.time()
        if (now - self._last_sample) >= self.refresh or self._last_sample == 0.0:
            self._cpu_cache = self.sysmon.cpu_percent()
            self._mem_cache = self.sysmon.mem_info()
            self._last_sample = now
        cpu = self._cpu_cache
        cpu_s = f"{cpu:.0f}%" if cpu is not None else "N/A"
        if self._mem_cache is not None:
            used, total, pct = self._mem_cache
            mem_s = f"{used:.1f}/{total:.1f} GB ({pct:.0f}%)"
        else:
            mem_s = "N/A"
        return cpu_s, mem_s

    def _rows(self):
        a = self.agent
        rows = []
        for block in a.dep.graph:
            r = a.results.get(block)
            raw = (r.status if r else "PENDING") or "PENDING"
            # BlockResult.status is seeded "PENDING" and only promoted to
            # DONE/FAILED at completion — it is NEVER set to RUNNING. The live
            # RUNNING state lives in the scheduler (dep.status), so while a
            # result still reads PENDING we consult dep.status; otherwise an
            # in-flight block is mislabeled WAITING and 'running' shows 0 even
            # though its route job is pinning cores.
            if raw == "PENDING":
                sv = a.dep.status.get(block)
                if sv is not None:
                    raw = sv.value
            status = {"PENDING": "WAITING", "BLOCKED": "WAITING",
                      "ABORTED": "FAILED"}.get(raw, raw)
            stage, idx, elapsed = "-", 0, ""
            live = a._live_blocks.get(block)
            if live is not None:
                stt, sl = live.get("state"), live.get("state_lock")
                if stt is not None and sl is not None:
                    with sl:
                        stage = stt.get("stage", "-")
                        idx = stt.get("stage_idx", 0)
                    idx = max(idx, a._stage_major_index(stage))
                t0 = live.get("t_start")
                if live.get("active") and t0:
                    elapsed = a._fmt_elapsed(time.time() - t0)
                elif live.get("final_elapsed") is not None:
                    elapsed = a._fmt_elapsed(live["final_elapsed"])
            wns = _fmt(r.wns_ns) if r else "n/a"
            # DONE means the FLOW ran to completion (6/6 stages + GDS + DRC
            # gate) — NOT that timing closed. Flag whether setup AND hold worst
            # slack are both >= 0 so the renderer can show CLOSED vs DONE⚠
            # (ran-with-violations) instead of a reassuring green DONE for e.g.
            # a -126 ps block.
            verdict = timing_verdict(r.wns_ns, r.hold_ps) if r else "UNKNOWN"
            timing_known = verdict != "UNKNOWN"
            # §71: macros loaded without liberty => macro-pin paths untimed;
            # the slack numbers are incomplete, so never show CLOSED.
            nolib = a._macro_lib_missing(block) if status == "DONE" else []
            timing_closed = verdict == "CLOSED" and not nolib
            # "final" = numbers from 6_finish (RCX signoff); "est" = janus_08
            # GRT-RC fallback, shown so an estimate is never mistaken for signoff.
            src = (r.timing_source if r else None) or ""
            src_tag = "" if not src else ("final" if src == "6_finish" else "est")
            # Stage-label fix: the live `stage` string is scraped from the
            # running log and gets STUCK at whatever it last read (e.g.
            # "1_synth (yosys)") after the block finishes. status/progress are
            # authoritative — not the scraped stage text — so once a block is
            # DONE pin the stage to the final step and fill the progress bar
            # instead of showing a stale early-stage label next to 6/6 DONE.
            if status == "DONE":
                stage, idx = "6_final", self._STAGE_TOTAL
            # §75: after the build, the status column keeps the verdict
            # (CLOSED / DONE⚠ / FAILED) and the stage column shows the
            # post-build step in flight (abstract, signoff, slew triage...).
            post = a._post_step.get(block) if hasattr(a, "_post_step") else None
            if post and status in ("DONE", "FAILED"):
                stage = post
            # §134: an --adaptive auto variant pass is rebuilding this block
            # from 3_place. Show it as ADAPT (counted as running) with the
            # trigger reason + the variant's live stage; WNS/slew/cells stay
            # the BASE leaf's numbers until the pass is promoted (§145).
            ad = getattr(a, "_adapt_live", {}).get(block)
            if ad:
                status = "ADAPT"
                vst = ad.get("stage") or "3_place"
                stage = f"{ad.get('tag', 'adapt')}→{vst}"
                idx = a._stage_major_index(vst)
                if ad.get("t0"):
                    elapsed = "+" + a._fmt_elapsed(time.time() - ad["t0"])
            # Per-path-group worst slack (reg2reg/reg2out) for setup AND hold,
            # so the row can show which group closes vs violates.
            pg = r.path_groups if r else None
            setup_groups = (pg or {}).get("setup") or {}
            hold_groups = (pg or {}).get("hold") or {}
            rows.append({"block": block, "status": status, "stage": stage,
                         "idx": idx, "elapsed": elapsed, "wns": wns,
                         "timing_known": timing_known,
                         "timing_closed": timing_closed,
                         "src_tag": src_tag,
                         "nolib": nolib,
                         "setup_groups": setup_groups,
                         "hold_groups": hold_groups,
                         "slew": self._fmt_drv(r.max_slew_violations if r else None,
                                               r.max_cap_violations if r else None),
                         "slew_bad": bool(r and ((r.max_slew_violations or 0)
                                                 or (r.max_cap_violations or 0))),
                         "cells": self._fmt_cells(r.std_cells if r else None),
                         "util": (f"{r.utilization:.0f}%"
                                  if r and r.utilization is not None else "-")})
        return rows

    @staticmethod
    def _fmt_drv(slew: Optional[int], cap: Optional[int]) -> str:
        """'839/0' = max-slew / max-cap violation counts (6_finish); '-' unknown."""
        if slew is None and cap is None:
            return "-"
        return f"{'-' if slew is None else slew}/{'-' if cap is None else cap}"

    @staticmethod
    def _fmt_cells(n: Optional[int]) -> str:
        """155911 -> '155.9k'; 1.2M -> '1.20M'; '-' unknown."""
        if n is None:
            return "-"
        if n >= 1_000_000:
            return f"{n / 1e6:.2f}M"
        if n >= 1_000:
            return f"{n / 1e3:.1f}k"
        return str(n)

    # ----- compact per-path-group slack formatting --------------------------
    @staticmethod
    def _fmt_group_cell(groups: Dict[str, float], rich: bool) -> str:
        """Render {'reg2reg':274.0,'reg2out':-126.61} as 'r2r:+274 r2o:-127'.

        Shows the two signoff-critical groups the user asked for (reg2reg,
        reg2out). A violating (negative) group is coloured red / closing green
        when `rich`; plain text otherwise. '-' when a group has no path."""
        def one(label: str, key: str) -> str:
            v = groups.get(key)
            if v is None:
                return f"{label}:-"
            txt = f"{label}:{v:+.0f}"
            if rich:
                col = "red" if v < 0 else "green"
                return f"[{col}]{txt}[/]"
            return txt
        if not groups:
            return "-"
        return f"{one('r2r', 'reg2reg')} {one('r2o', 'reg2out')}"

    @staticmethod
    def _counts(rows):
        c = {"DONE": 0, "RUNNING": 0, "FAILED": 0, "WAITING": 0}
        for r in rows:
            # an adaptive pass is a real make job pinning cores -> running
            k = "RUNNING" if r["status"] == "ADAPT" else r["status"]
            c[k] = c.get(k, 0) + 1
        return c

    def _total_elapsed(self):
        a = self.agent
        return a._fmt_elapsed(time.time() - a._run_start) if a._run_start else "0:00"

    # --------------------------------------------------------------- rich UI
    def _render_group(self):
        from rich.table import Table
        from rich.panel import Panel
        from rich.console import Group
        a = self.agent
        cpu_s, mem_s = self._sys_strings()
        rows = self._rows()
        c = self._counts(rows)
        head = Table.grid(padding=(0, 2))
        head.add_column(justify="right", style="cyan")
        head.add_column()
        head.add_row("blocks", f"{len(rows)}  "
                     f"(done {c['DONE']} · running {c['RUNNING']} · "
                     f"failed {c['FAILED']} · waiting {c['WAITING']})")
        head.add_row("parallel / cores",
                     f"{a.args.parallel} / {a.args.num_cores}")
        head.add_row("total elapsed", self._total_elapsed())
        head.add_row("CPU / MEM", f"[bold]{cpu_s}[/bold]   ·   [bold]{mem_s}[/bold]")
        t = Table(show_lines=False, expand=False)
        for col in ("block", "status", "stage", "progress", "elapsed",
                    "WNS(ps)", "SETUP r2r/r2o", "HOLD r2r/r2o",
                    "SLEW/CAP", "CELLS", "UTIL"):
            t.add_column(col)
        style = {"RUNNING": "green", "DONE": "bold green",
                 "FAILED": "bold red", "WAITING": "yellow",
                 "ADAPT": "bold magenta"}
        for r in rows:
            idx = max(0, min(self._STAGE_TOTAL, r["idx"]))
            bar = "█" * idx + "░" * (self._STAGE_TOTAL - idx)
            st = r["status"]
            label, st_style = st, style.get(st, "")
            if st == "DONE" and r.get("timing_known"):
                # ran-to-completion but split closed vs violating so a negative
                # worst-slack block never reads as a clean green pass.
                if r.get("timing_closed"):
                    label, st_style = "CLOSED", "bold green"
                else:
                    label, st_style = "DONE⚠", "yellow"
                if r.get("nolib"):
                    label, st_style = "DONE⚠(nolib)", "bold red"
                if r.get("src_tag") == "est":
                    label += "(est)"
            t.add_row(r["block"],
                      f"[{st_style}]{label}[/]",
                      str(r["stage"]),
                      f"{bar} {idx}/{self._STAGE_TOTAL}",
                      r["elapsed"] or "", r["wns"],
                      self._fmt_group_cell(r.get("setup_groups") or {}, rich=True),
                      self._fmt_group_cell(r.get("hold_groups") or {}, rich=True),
                      (f"[red]{r['slew']}[/]" if r.get("slew_bad") else r["slew"]),
                      r["cells"], r["util"])
        return Group(Panel(head, title="JANUS Agent · live",
                           border_style="cyan", expand=False), t)

    # --------------------------------------------------------------- ANSI UI
    def _ansi_frame(self):
        a = self.agent
        cpu_s, mem_s = self._sys_strings()
        rows = self._rows()
        c = self._counts(rows)
        out = []
        out.append("=" * 132)
        out.append(f" JANUS Agent · live     total elapsed {self._total_elapsed()}")
        out.append(f" blocks={len(rows)}  done={c['DONE']} running={c['RUNNING']} "
                   f"failed={c['FAILED']} waiting={c['WAITING']}   "
                   f"parallel={a.args.parallel} cores={a.args.num_cores}")
        out.append(f" CPU={cpu_s}   MEM={mem_s}")
        out.append("-" * 132)
        out.append(f" {'block':<18}{'status':<9}{'stage':<20}"
                   f"{'prog':<7}{'elapsed':<9}{'WNS(ps)':<10}"
                   f"{'SETUP r2r/r2o':<22}{'HOLD r2r/r2o':<22}"
                   f"{'SLEW/CAP':<11}{'CELLS':<9}{'UTIL'}")
        for r in rows:
            idx = max(0, min(self._STAGE_TOTAL, r["idx"]))
            st = r["status"]
            if st == "DONE" and r.get("timing_known"):
                st = "CLOSED" if r.get("timing_closed") else "DONE!"
                if r.get("nolib"):
                    st = "NOLIB!"      # macros had no liberty: timing incomplete
                if r.get("src_tag") == "est":
                    st += "~"          # ~ = GRT-RC estimate, not 6_finish signoff
            setup_c = self._fmt_group_cell(r.get("setup_groups") or {}, rich=False)
            hold_c = self._fmt_group_cell(r.get("hold_groups") or {}, rich=False)
            out.append(f" {r['block'][:17]:<18}{st:<9}"
                       f"{str(r['stage'])[:19]:<20}{f'{idx}/6':<7}"
                       f"{(r['elapsed'] or ''):<9}{r['wns']:<10}"
                       f"{setup_c:<22}{hold_c:<22}"
                       f"{r['slew']:<11}{r['cells']:<9}{r['util']}")
        out.append("=" * 132)
        return "\n".join(out)

    def _ansi_loop(self):
        try:
            sys.stdout.write("\x1b[?25l")  # hide cursor
            sys.stdout.flush()
        except Exception:
            pass
        prev_lines = 0
        while not self._stop.wait(self.refresh):
            try:
                frame = self._ansi_frame()
                n = frame.count("\n") + 1
                buf = []
                if prev_lines:
                    buf.append(f"\x1b[{prev_lines}A")  # cursor up
                buf.append("\x1b[J")                   # clear to end of screen
                buf.append(frame + "\n")
                sys.stdout.write("".join(buf))
                sys.stdout.flush()
                prev_lines = n
            except Exception:
                break
        try:
            sys.stdout.write("\x1b[?25h")  # show cursor
            sys.stdout.flush()
        except Exception:
            pass

    # --------------------------------------------------------------- control
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        if self._rich:
            try:
                from rich.live import Live
                parent = self

                class _Renderable:
                    def __rich__(self_inner):
                        return parent._render_group()

                self._live = Live(_Renderable(), console=self.console,
                                  refresh_per_second=2, transient=False)
                self._live.start()
                return
            except Exception:
                self._live = None
                self._rich = False
        # ANSI fallback
        self._thread = threading.Thread(target=self._ansi_loop,
                                        name="janus-dashboard", daemon=True)
        self._thread.start()

    def refresh_now(self) -> None:
        if self._live is not None:
            try:
                self._live.refresh()
            except Exception:
                pass

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._started = False


# ---------------------------------------------------------------------------- #
# Per-block bookkeeping
# ---------------------------------------------------------------------------- #
@dataclass
class BlockResult:
    block: str
    status: str = "PENDING"
    tries: int = 0
    wns_ns: Optional[float] = None      # SETUP worst slack (ASAP7 → picoseconds)
    hold_ps: Optional[float] = None     # HOLD worst slack (picoseconds)
    tns_ns: Optional[float] = None      # SETUP TNS (picoseconds)
    drc_violations: Optional[int] = None
    area: Optional[float] = None
    power: Optional[float] = None
    utilization: Optional[float] = None
    log_path: str = ""
    note: str = ""
    llm_calls: int = 0
    timing_detail: str = ""             # rendered classified-timing breakdown
    # Worst slack per PATH GROUP, split setup vs hold (ASAP7 ps):
    #   {"setup": {"reg2reg": .., "reg2out": ..}, "hold": {...}}
    # Lets the dashboard show reg2reg vs reg2out so a block whose reg2reg closes
    # but reg2out violates is never read as a clean pass off the overall WNS.
    path_groups: Optional[Dict[str, Dict[str, float]]] = None
    # Which report the WNS/hold/path-group numbers came from: "6_finish"
    # (post-route OpenRCX signoff) or "janus_08" (GRT-RC estimate fallback).
    timing_source: Optional[str] = None
    # Final-STA design-rule counts (6_finish only; None when not available).
    max_slew_violations: Optional[int] = None
    max_cap_violations: Optional[int] = None
    # Standard logic cells from the final "Cell type report" (no macro/fill/tap).
    std_cells: Optional[int] = None

    def as_row(self) -> Dict:
        return {
            "block": self.block, "status": self.status, "tries": self.tries,
            "wns_ns": self.wns_ns, "hold_ps": self.hold_ps, "tns_ns": self.tns_ns,
            "drc_violations": self.drc_violations, "area": self.area,
            "power": self.power, "utilization": self.utilization,
            "log_path": self.log_path, "note": self.note, "llm_calls": self.llm_calls,
            "timing_source": self.timing_source,
            "max_slew_violations": self.max_slew_violations,
            "max_cap_violations": self.max_cap_violations,
            "std_cells": self.std_cells,
        }


# ---------------------------------------------------------------------------- #
# The orchestrator
# ---------------------------------------------------------------------------- #
class JANUSAutonomousAgent:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.ui = UI(verbose=args.verbose)
        self._stop = threading.Event()

        # --- resolve ORFS + RTL locations ------------------------------------
        self.orfs = args.orfs or os.environ.get("ORFS_DIR", "")
        self.rtl_root = args.rtl_root or os.environ.get(
            "JANUS_RTL_ROOT", os.path.dirname(_ROOT))  # default: repo root above asic_flow
        self.flow_dir = os.path.join(self.orfs, "flow") if self.orfs else ""
        self.design_root = os.path.join(self.flow_dir, "designs", "asap7", "janus") if self.flow_dir else ""

        # --- OpenROAD thread count (NUM_CORES) -------------------------------
        # Auto-detect if not specified: use nproc, capped at 24 to leave some
        # headroom for the OS. OpenROAD passes this as -threads to grt/drt/etc.
        if args.num_cores is None:
            try:
                detected = int(subprocess.check_output(["nproc"], text=True).strip())
                args.num_cores = min(detected, 24)
            except (subprocess.SubprocessError, ValueError, FileNotFoundError):
                args.num_cores = 8  # fallback if nproc unavailable

        # --- run directory (logs + reports live OUTSIDE the repo) ------------
        base = os.environ.get("JANUS_ASIC_HOME", os.path.expanduser("~/janus_asic"))
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = args.run_dir or os.path.join(base, "runs", ts)
        os.makedirs(self.run_dir, exist_ok=True)

        # --- components ------------------------------------------------------
        self.dep = DependencyManager()
        if args.blocks:
            self.dep = self.dep.restrict_to(args.blocks)
        self.analyzer = LogAnalyzer()
        self.llm = AbacusLLM(
            min_interval_s=args.llm_interval,
            max_retries=args.llm_retries,
            logger=self.ui.debug,
        )
        self.ckpt = Checkpoint(os.path.join(self.run_dir, "checkpoint.json"))
        self.ckpt.load()
        self.reporter = Reporter(self.run_dir)

        # --- run-completion notifications (email + optional Google Drive) ----
        # OFF unless --notify-email (or a channel) is given. Constructing it is
        # always safe; it simply does nothing when no channel is enabled. All
        # of its work is best-effort and can never crash a real ORFS run.
        self.notifier = NotificationManager(
            recipient=getattr(args, "notify_email", None),
            smtp_host=getattr(args, "smtp_host", None),
            smtp_port=getattr(args, "smtp_port", None),
            smtp_user=getattr(args, "smtp_user", None),
            smtp_pass=getattr(args, "smtp_pass", None),
            gdrive_enabled=getattr(args, "gdrive_upload", False),
            gdrive_creds=getattr(args, "gdrive_creds", None),
            logger=self._notify_log,
        )
        # §136: always state the notification config at startup — loudly, and
        # in <run_dir>/notify.log — so "no email" is diagnosable from a file
        # instead of only at --verbose (which is how it used to be).
        _np = self.notifier.config_problem()
        if not self.notifier.enabled:
            self._notify_log("notifications OFF (no --notify-email / $NOTIFY_EMAIL)")
        elif _np:
            self.ui.warn(f"email notifications will be SKIPPED: {_np}. Export them "
                         "in the shell that launches the agent (or pass --smtp-*).")
            self._notify_log(f"startup: email will be SKIPPED: {_np}")
        else:
            self._notify_log(f"startup: email -> {self.notifier.recipient} via "
                             f"{self.notifier.smtp_host}:{self.notifier.smtp_port} "
                             f"as {self.notifier.smtp_user}; attach cap "
                             f"{self.notifier.attach_max_bytes / 1048576:.0f} MB, "
                             f"bundle cap {self.notifier.max_tar_bytes / 1048576:.0f} MB "
                             f"uncompressed, format {self.notifier.archive_fmt}")
        # §138: Drive channel state (independent of email).
        if self.notifier.gdrive_enabled:
            _gp = self.notifier.gdrive_problem()
            if _gp:
                self.ui.warn(f"Google Drive upload will be SKIPPED: {_gp}")
                self._notify_log(f"startup: gdrive will be SKIPPED: {_gp}")
            else:
                self._notify_log(
                    "startup: gdrive -> " + self.notifier.gdrive_target_desc()
                    + f" (creds {self.notifier.gdrive_creds})")

        # --- GLOBAL flow-state manifest (outside all run-dirs / blocks) ------
        # A single JSON at <asic_flow>/.janus_state/janus_flow_state.json that
        # SURVIVES across timestamped --run-dir invocations. This is what makes
        # --resume actually resume: the per-run checkpoint.json lives INSIDE
        # run_dir, so a fresh --run-dir loads an EMPTY checkpoint and re-runs
        # everything from scratch. The manifest instead tracks, per block, the
        # stages completed, per-stage QoR, final completion status, and every
        # run/log/report dir ever used (for cleanup). It also drives STAGE-level
        # (not just block-level) resume through a *semantic* config diff.
        # Every manifest call is best-effort (try/except): a manifest bug must
        # NEVER crash or alter a real ORFS run.
        self.manifest: Optional[FlowState] = None
        try:
            mpath = (getattr(args, "manifest", None)
                     or FlowState.default_path(_ROOT))
            self.manifest = FlowState(mpath).load()
        except Exception as e:                       # pragma: no cover
            self.ui.warn(f"flow-state manifest unavailable ({e}); "
                         "continuing without it")

        # repo SOURCE configs (asic_flow/configs) — used to mirror LLM fixes back
        # so they survive a scripts/03 re-stamp and can be committed.
        self.configs_dir = os.path.join(_ROOT, "configs")
        # SRAM setup script (tracked source of truth for FakeRAM geometry) and
        # the dir the generated macros land in (ASAP7_SRAM_DIR). Used by the
        # deterministic macro-fit self-heal (MPL-0006 -> sram_reshape).
        self.sram_setup_script = os.path.join(_ROOT, "scripts", "04_setup_srams.sh")
        self.sram_out_dir = (os.environ.get("ASAP7_SRAM_DIR")
                             or os.path.join(_ROOT, "srams", "asap7"))
        # One-shot guard: before hardening any block we auto-repair fakeram LEFs
        # that still carry the FakeRAM2.0 blanket M4 (pin-layer) OBS which blocks
        # the macro power grid (PDN-0232 -> PDN-0233 on user_core_cva6). See
        # _ensure_fakeram_obs_fixed(). Set True once it has run this session.
        self._fakeram_obs_checked = False
        # branches a committed fix should be published to (keeps mirror branches
        # like main + v5.1_integration in lock-step). Empty => current branch.
        sync = list(getattr(args, "git_sync_branches", None) or [])
        # git auto-commit of applied fixes (on by default; push off by default).
        # Naming sync branches implies push (that's the whole point of syncing).
        self.git = GitOps(
            repo_root=self.rtl_root,
            logger=self.ui.debug,
            enable_commit=not args.no_git_commit,
            enable_push=args.git_push or bool(sync),
            sync_branches=sync,
        )
        # abstract (.lef/.lib) generation target — resolved lazily at first use.
        self._abstract_target: Optional[str] = None
        self._abstract_probed = False
        # interactive shell prompts only when asked AND stdin is a real TTY.
        self.interactive = bool(args.interactive) and sys.stdin.isatty()

        self.results: Dict[str, BlockResult] = {
            b: BlockResult(block=b) for b in self.dep.graph
        }
        self._results_lock = threading.Lock()

        # --- live in-place dashboard state -----------------------------------
        # Shared registry the live DashboardUI reads: block -> {state, lock,
        # t_start, log_path, active, final_elapsed}. _run_make registers a
        # block when it starts running and deregisters it (recording final
        # elapsed) when make exits, so the dashboard shows live stage/progress/
        # elapsed for every running block even under --parallel.
        self._run_start = None
        self._live_blocks: Dict[str, Dict] = {}
        self._live_lock = threading.Lock()
        self._dashboard_ui = None

        # --- resume: per-block "assume-old" file list ------------------------
        # On --resume, _plan_stage_resume() records here the absolute paths of
        # every REUSED stage artifact (the resume plan's touch list: the whole
        # {major}_* family — .rtlil/.v/.sdc/.odb/...). _make_cmd() turns each
        # into a GNU-make `-o <file>` (--old-file / --assume-old) flag so make
        # will NOT rebuild those files even if a prerequisite looks newer, AND
        # will not rebuild anything downstream on their account. This is the
        # DECISIVE defense against the spurious full re-synth: the synth make
        # chain (1_1_yosys_canonicalize.rtlil : $(YOSYS_DEPENDENCIES)) is forced
        # out-of-date by a rebuilt $(SDC_FILE_CLOCK_PERIOD) / re-stamped source,
        # which mtime-touching a target cannot beat but `-o` does. See
        # flow_state.ResumePlan.touch_paths.  Empty => no -o flags emitted.
        self._resume_old_files: Dict[str, List[str]] = {}
        # §75: post-build step per block shown in the dashboard stage column
        # once the main build is DONE/FAILED ("abstract", "signoff",
        # "slew triage running", "triage done"...). Absent => "6_final".
        self._post_step: Dict[str, str] = {}
        # §134: in-flight --adaptive auto pass per block, shown as status ADAPT
        # with the variant's live stage: {"tag", "stage", "t0"}.
        self._adapt_live: Dict[str, Dict] = {}

        # --- optional web dashboard (browsable live/standalone run view) -----
        # Zero-dep HTTP server (see dashboard.py). Off unless --dashboard. The
        # DashboardState is a pure mirror the agent pushes into; it also writes
        # run_dir/dashboard_state.json so a finished run can be reopened with
        # `python3 dashboard.py --run-dir <run_dir>`.
        self.dashboard = None
        self.dash_state = None
        if getattr(args, "dashboard", False) and _HAS_DASHBOARD:
            try:
                self.dash_state = DashboardState(self.run_dir)
                self.dash_state.set_meta(
                    blocks=list(self.dep.graph),
                    git_head=(self.git.head_sha()[:9] + " " + self.git.current_branch()
                              if self.git.available() else ""),
                    started=datetime.now().isoformat(timespec="seconds"),
                )
                extra_roots = []
                if self.flow_dir:
                    extra_roots += [os.path.join(self.flow_dir, "results"),
                                    os.path.join(self.flow_dir, "reports"),
                                    os.path.join(self.flow_dir, "logs")]
                if self.sram_out_dir:
                    extra_roots.append(self.sram_out_dir)
                self.dashboard = Dashboard(
                    self.run_dir,
                    port=int(getattr(args, "dashboard_port", 8080)),
                    host=getattr(args, "dashboard_host", "0.0.0.0"),
                    state=self.dash_state, extra_roots=extra_roots)
            except Exception as e:  # never let the dashboard break a run
                self.ui.warn(f"dashboard init failed ({e}); continuing headless")
                self.dashboard = None
        elif getattr(args, "dashboard", False) and not _HAS_DASHBOARD:
            self.ui.warn("dashboard requested but dashboard.py unavailable; "
                         "continuing headless")

    def _dash_update(self, block: str, **kw) -> None:
        """Best-effort push of one block's live state to the dashboard."""
        if self.dash_state is None:
            return
        try:
            self.dash_state.update_block(block, **kw)
        except Exception:
            pass

    # ----- live in-place dashboard: per-block state registry ----------------
    def _live_register(self, block: str, state: Dict, state_lock,
                       t_start: float, log_path: str) -> None:
        """Expose a running block's shared progress state to the DashboardUI."""
        with self._live_lock:
            self._live_blocks[block] = {
                "state": state, "state_lock": state_lock,
                "t_start": t_start, "log_path": log_path,
                "active": True, "final_elapsed": None,
            }

    def _live_deregister(self, block: str, final_elapsed=None) -> None:
        """Mark a block's live entry finished (freeze its elapsed time)."""
        with self._live_lock:
            e = self._live_blocks.get(block)
            if e:
                e["active"] = False
                if final_elapsed is not None:
                    e["final_elapsed"] = final_elapsed

    def _maybe_start_dashboard(self) -> None:
        """Start the live in-place dashboard when appropriate.

        Enabled for --progress {auto,dashboard} on a real TTY (not --verbose,
        which streams raw ORFS output and would fight the live region). For a
        piped/redirected stdout, or --progress {plain,quiet}, this is a no-op so
        the classic scrolling output is preserved. Fully best-effort: a failure
        here never breaks the run, it just falls back to scrolling output.
        """
        mode = getattr(self.args, "progress", "auto")
        if mode in ("quiet", "plain"):
            return
        if self.args.verbose or not sys.stdout.isatty():
            return
        try:
            self._dashboard_ui = DashboardUI(self)
            self._dashboard_ui.start()
            self.ui.quiet = True
        except Exception as e:  # never let the dashboard break a run
            self._dashboard_ui = None
            self.ui.quiet = False
            self.ui.warn(f"live dashboard unavailable ({e}); scrolling output")

    def _stop_dashboard(self) -> None:
        if self._dashboard_ui is not None:
            try:
                self._dashboard_ui.stop()
            except Exception:
                pass
            self._dashboard_ui = None
        self.ui.quiet = False

    # ----- DESIGN_CONFIG path for a block -----------------------------------
    def _config_path(self, block: str) -> str:
        return os.path.join(self.design_root, block, "config.mk")

    # ----- ORFS FLOW_VARIANT leaf -------------------------------------------
    def _leaf(self) -> str:
        """The ORFS results/reports/logs leaf directory for THIS run.

        ORFS writes under results|reports|logs/<platform>/<design>/<FLOW_VARIANT>
        and FLOW_VARIANT defaults to 'base'. For parallel variant campaigns we
        run the SAME block N times on ONE box, each with a distinct leaf so the
        ODB/report trees never collide on the shared disk. When --variant is
        unset (or 'base') this returns 'base', so single-run behaviour is
        byte-identical to the pre-variant agent.
        """
        v = getattr(self.args, "variant", None) or "base"
        return v

    def _block_log(self, block: str) -> str:
        return os.path.join(self.run_dir, f"{block}.log")

    _NOLIB_RE = re.compile(r"ORD-2011\]? LEF master (\S+) has no liberty cell"
                           r"|master (\S+) has signal pins but no liberty cell")

    def _macro_lib_missing(self, block: str) -> List[str]:
        """§71: macro masters OpenROAD loaded WITHOUT liberty (timing black
        boxes) per the block log. Non-empty => every macro-pin path was untimed,
        so the block's WNS/hold can NOT be called CLOSED. Cached on log mtime."""
        path = self._block_log(block)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            return []
        cache = self.__dict__.setdefault("_nolib_cache", {})
        hit = cache.get(block)
        if hit and hit[0] == mt:
            return hit[1]
        masters: List[str] = []
        try:
            with open(path, "r", errors="replace") as fh:
                for ln in fh:
                    if "liberty cell" in ln:
                        m = self._NOLIB_RE.search(ln)
                        if m:
                            nm = m.group(1) or m.group(2)
                            if nm not in masters:
                                masters.append(nm)
        except OSError:
            pass
        cache[block] = (mt, masters)
        return masters

    # ----- ORFS stage-report directory for a block ---------------------------
    def _reports_dir(self, block: str) -> Optional[str]:
        """Locate the ORFS reports dir for a block (holds 2_floorplan…6_finish).

        ORFS writes reports under flow/reports/<platform>/<design>/base/. The
        design folder is usually the block name or 'janus_<block>'. We glob a
        few known layouts and return the first that actually contains stage
        reports; None if nothing is found (Tier-1 then degrades to log-only).
        An explicit --reports-dir override wins if provided.
        """
        override = getattr(self.args, "reports_dir", None)
        if override:
            return override
        if not self.flow_dir:
            return None
        cands = [
            os.path.join(self.flow_dir, "reports", "asap7", block, self._leaf()),
            os.path.join(self.flow_dir, "reports", "asap7", f"janus_{block}", self._leaf()),
        ]
        # §154: no `*{block}*` glob — it matched sibling blocks (user_core ->
        # user_core_ara), so a failed user_core reported Ara's 6_finish QoR.
        cands.append(os.path.join(self.flow_dir, "reports", "asap7", "janus", block, self._leaf()))
        for d in cands:
            if os.path.isdir(d) and glob.glob(os.path.join(d, "*_*.rpt")):
                return d
        return None

    # ----- authoritative block timing: 6_finish (RCX) > janus_08 (GRT RC) -----
    def _authoritative_timing_from_rpt(self, block: str) -> Optional[Dict]:
        """Source-tagged signoff timing for a block (ASAP7 ps).

        Preference order (log_analyzer.read_block_timing):
          1. 6_finish.rpt — ORFS final STA AFTER OpenRCX extraction, multi-corner.
             This is the signoff authority.
          2. janus_08_detail_route_paths.rpt — written by the post-detail-route
             hook BEFORE 6_final extracts parasitics, i.e. GRT/estimated RC.
             Kept only as a fallback (e.g. a run that died between route and
             finish). On agent_domain 20261007_104052 it read hold reg2reg
             -122 ps while 6_finish read +9.95 ps / 0 hold violations — the
             false DONE⚠ this fixes.
        The run log's final STA print can be lost to a cosmetic dump-hook error,
        so neither source is ever taken from the log when a report exists.

        Returns {"setup_ws","setup_tns","hold_ws","path_groups","counts",
        "setup_worst","fmax_mhz","_source","source","final"} or None.
        """
        return read_block_timing(self._reports_dir(block))

    def _path_group_timing_from_rpt(
            self, block: str) -> Optional[Dict[str, Dict[str, float]]]:
        """Worst slack per path group (reg2reg/reg2out/gated clock) for setup
        AND hold, from the SAME source chosen by _authoritative_timing_from_rpt
        (6_finish preferred), so the per-group cells and the CLOSED/DONE⚠
        verdict can never disagree about which parasitic model they used."""
        t = self._authoritative_timing_from_rpt(block)
        groups = (t or {}).get("path_groups") or {}
        if not groups.get("setup") and not groups.get("hold"):
            return None
        return groups

    # ----- ORFS results (.odb) directory for a block -------------------------
    def _results_dir(self, block: str) -> Optional[str]:
        """Locate the ORFS *results* dir for a block (holds N_*.odb).

        ORFS writes stage databases under flow/results/<platform>/<design>/base.
        The DESIGN_NICKNAME the user runs with is the bare block name
        (confirmed by the log: results/asap7/user_core/base/5_2_route.odb) —
        NOT 'janus/<block>' as _find_routed_odb historically assumed. We glob a
        few known layouts and return the first that actually contains an .odb,
        so the manifest scan never depends on that latent path bug.
        """
        if not self.flow_dir:
            return None
        cands = [
            os.path.join(self.flow_dir, "results", "asap7", block, self._leaf()),
            os.path.join(self.flow_dir, "results", "asap7", f"janus_{block}", self._leaf()),
            os.path.join(self.flow_dir, "results", "asap7", "janus", block, self._leaf()),
        ]
        # §154: exact names only (no sibling-block glob).
        for d in cands:
            if os.path.isdir(d) and glob.glob(os.path.join(d, "*.odb")):
                return d
        return None

    def _synth_stat_path(self, block: str) -> Optional[str]:
        """synth_stat.txt lives in the ORFS logs tree (1_synth) for a block."""
        if not self.flow_dir:
            return None
        for pat in (
            os.path.join(self.flow_dir, "logs", "asap7", block, self._leaf(), "*synth_stat*"),
            os.path.join(self.flow_dir, "reports", "asap7", block, self._leaf(), "synth_stat.txt"),
            os.path.join(self.flow_dir, "logs", "asap7", f"janus_{block}", self._leaf(), "*synth_stat*"),
        ):
            hits = sorted(glob.glob(pat))
            if hits:
                return hits[-1]
        return None

    # ----- map an LLM fix to the remedy LAYER (for the evidence guardrail) ----
    # config keys that move the die/core geometry are FLOORPLAN; density/padding
    # is PLACE; clock period is SDC; RTL edits are RTL. This lets the Tier-1
    # guardrail veto a fix whose layer cannot explain the evidence (e.g. a
    # PLACE density tweak for a violation that already exists pre-placement).
    _CONFIG_KEY_LAYER = {
        "PLACE_DENSITY": "PLACE", "CELL_PAD_IN_SITES_GLOBAL_PLACEMENT": "PLACE",
        "CORE_UTILIZATION": "FLOORPLAN", "CORE_ASPECT_RATIO": "FLOORPLAN",
        "CORE_MARGIN": "FLOORPLAN", "CORE_AREA": "FLOORPLAN", "DIE_AREA": "FLOORPLAN",
    }

    @classmethod
    def _fix_layer(cls, fx: Dict) -> str:
        t = (fx.get("type") or "").lower()
        if t == "sdc_period":
            return "SDC"
        if t == "rtl":
            return "RTL"
        if t == "config":
            key = (fx.get("key") or fx.get("var") or "").upper()
            return cls._CONFIG_KEY_LAYER.get(key, "FLOORPLAN")
        return "UNKNOWN"

    # ----- the make command --------------------------------------------------
    def _make_cmd(self, block: str) -> List[str]:
        cmd = ["make", f"DESIGN_CONFIG={self._config_path(block)}"]
        if self.args.num_cores:
            cmd.append(f"NUM_CORES={self.args.num_cores}")

        # ---- Parallel-variant campaign: distinct ORFS leaf + extra make vars --
        # When this run is one leg of a parallel variant campaign (--variant set
        # to something other than 'base'), tell ORFS to write its results/reports/
        # logs under that leaf instead of 'base' (FLOW_VARIANT). This lets N
        # agents run the SAME block on ONE shared disk without clobbering each
        # other. --extra-make carries per-variant make assignments (highest GNU-
        # make precedence) — typically the lever enable for this leg, e.g.
        # JANUS_VLSU_LDU_RETIME2=1. The config.mk ifeq blocks translate the JANUS_*
        # knob into VERILOG_DEFINES, so no re-stamp is needed to flip a lever.
        leaf = self._leaf()
        if leaf != "base":
            cmd.append(f"FLOW_VARIANT={leaf}")
        for kv in (getattr(self.args, "extra_make", None) or []):
            cmd.append(kv)

        # ---- GLOBAL setup-signoff corner: WORST (slow) -----------------------
        # Design owner policy (2026-08-12): every step from synthesis through
        # route optimizes against the SLOW / WORST corner for SETUP. ASAP7 ships
        # three corners (platforms/asap7/config.mk):
        #     BC = FF / 0.77 V / 25 C   (fast  - optimistic, the platform DEFAULT
        #                                via `CORNER ?= BC`)
        #     TC = TT / 0.70 V / 0 C    (typical)
        #     WC = SS / 0.63 V / 100 C  (slow  - the setup-pessimistic corner)
        # The stock flow closes SETUP on BC, which is optimistic and hides real
        # setup violations. Forcing CORNER=WC makes synth (ABC target lib), place,
        # CTS and route repair_timing all read the SS/0.63V/100C .lib, so the WNS
        # the flow reports is the true worst-case setup number. Command-line
        # assignment (highest GNU-make precedence) overrides the platform's
        # `CORNER ?= BC` and every block config.mk. The multi-Vt OTHER_VT foreach
        # and the MBFF _MBFF_LIB_CORNER map both key off CORNER, so they will pull
        # the matching SS libs automatically (WC -> SS).
        # Keeping CORNER=WC ALSO matters for SYNTHESIS, which is inherently
        # single-corner: yosys maps flops from DFF_LIB_FILE = $(CORNER)_NLDM_
        # DFF_LIB_FILE and ABC targets the $(CORNER) libs. CORNER=WC therefore
        # makes synth itself optimize/flop-map against the SLOW (SS) corner,
        # while P&R + STA go full multi-corner (below). So synthesis is
        # pessimistic-on-slow and signoff spans both corners.
        cmd.append("CORNER=WC")

        # ---- MULTI-CORNER setup+hold signoff (SS + FF) -----------------------
        # Design owner (2026-08-12): "we need a multi-corner run given we are on
        # EC2" (the resized ~128 GB box can afford the extra lib sets). ORFS
        # multi-corner (scripts/read_liberty.tcl): when CORNERS is set, it calls
        # `define_corners {*}$CORNERS` and reads per-corner `<CORNER>_LIB_FILES`.
        # OpenSTA then analyzes SETUP as the worst *max* path across all corners
        # and HOLD as the worst *min* path across all corners automatically -- no
        # per-corner labels needed. We wire the two dominant corners:
        #     slow -> SS/0.63V/100C  ($(WC_NLDM_LIB_FILES)) : closes SETUP
        #     fast -> FF/0.77V/25C   ($(BC_NLDM_LIB_FILES)) : closes HOLD
        # (TT/typ intentionally omitted to hold peak DRAM to 2 corners x 3 VT
        # instead of 3 x 3; add "typ" + TYP_LIB_FILES=$(TC_NLDM_LIB_FILES) if you
        # want it.) The *_NLDM_LIB_FILES already include every enabled VT flavor
        # (the platform's OTHER_VT foreach appends LVT/SLVT to each corner list),
        # so multi-Vt is preserved per corner. The $(...) are passed literally in
        # argv (subprocess = no shell), and GNU make recursively expands them in
        # the ASAP7 platform context at env-export time (verified), where the
        # WC_/BC_NLDM_LIB_FILES are fully populated. This mirrors the stock
        # ihp-sg13g2 multi-corner design (CORNERS = slow fast + *_LIB_FILES).
        # >>> PD-UNTESTED / VALIDATE ON-BOX <<<  Multi-corner ~doubles STA/P&R
        # liberty memory vs single-corner; it is meant for the resized box. Set
        # JANUS_MULTICORNER=0 to fall back to single-corner CORNER=WC (setup
        # only; hold then needs a separate BC pass) on a memory-starved box.
        #
        # §71 FIX — MACRO LIBERTY WAS DROPPED IN MULTI-CORNER MODE. ORFS
        # read_liberty.tcl reads ONLY $<CORNER>_LIB_FILES when CORNERS is set;
        # the block config's `ADDITIONAL_LIBS += fakeram*.lib / <child>.lib`
        # only lands in LIB_FILES (single-corner path). Passing the bare
        # platform lists here therefore loaded the SRAM / hardened-child
        # macros as LEF-only black boxes in EVERY OpenROAD stage (proof:
        # agent_domain run 20261007_104052 log "[WARNING ORD-2011] LEF master
        # fakeram7_8192x8 has no liberty cell" + "master fakeram7_* has signal
        # pins but no liberty cell" at 6_report; slew_triage read_liberty list
        # had zero fakeram libs). Every path launched/captured at a macro pin
        # was untimed (slack INF) — never timing-repaired, never in WNS.
        # The macro .libs are single-corner; reading the same file into both
        # corners is the standard academic-PDK practice (ihp/sky130 do this).
        #
        # §99 FIX — PER-CORNER CHILD ABSTRACTS. A hardened child that emits
        # BOTH <blk>_fast.lib and <blk>_slow.lib (e.g. user_core_ara) was
        # globbed into BOTH corners. OpenSTA keeps the LAST same-named library
        # read into a corner (Sta::readLibertyAfter -> makeSceneMap overwrites
        # the corner cell; STA-1140 "library ... already exists"), and the
        # wildcard order is _fast then _slow, so the FAST (hold) corner timed
        # the child with its SLOW arcs (user_core log 2026-10-09: fast corner
        # reads user_core_ara_fast.lib then user_core_ara_slow.lib). Filter
        # the opposite-corner file out of each list; single-corner libs
        # (fakeram*, <blk>_typ.lib) still land in both corners unchanged.
        if os.environ.get("JANUS_MULTICORNER", "1") != "0":
            cmd += ["CORNERS=slow fast",
                    "SLOW_LIB_FILES=$(WC_NLDM_LIB_FILES) "
                    "$(filter-out %_fast.lib,$(ADDITIONAL_LIBS))",
                    "FAST_LIB_FILES=$(BC_NLDM_LIB_FILES) "
                    "$(filter-out %_slow.lib,$(ADDITIONAL_LIBS))"]

        # §147 FIX — KEPLER LEC DIES ON PER-CORNER CHILD LIBS. ORFS
        # formal_check.tcl:write_lec_script hands kepler-formal the single-corner
        # $LIB_FILES (= WC platform libs + the UNFILTERED ADDITIONAL_LIBS), so a
        # hierarchical parent with LEC_CHECK=1 passes BOTH <child>_fast.lib and
        # <child>_slow.lib. Kepler puts every lib cell into one SNL library keyed
        # by cell name and aborts on the second definition. Proof — user_core run
        # 20261016_202941, 4_1_cts.log tail:
        #   Loading library file: .../user_core_cva6/base/user_core_cva6_fast.lib
        #   Loading library file: .../user_core_cva6/base/user_core_cva6_slow.lib
        #   [critical] [KeplerFormal.cpp:1047] Netlist loading failed: NLLibrary
        #   user_core_cva6 contains already a SNLDesign named: user_core_cva6
        #   ... Peak memory: 2436936KB   (2.4 GB -> NOT the §140 OOM)
        # ORFS's own knob REMOVE_LIBS_FOR_LEC drops exact LIB_FILES entries from
        # the kepler list only (STA/resizer/synth unaffected). We drop the
        # _fast twins (LEC is functional; corner choice is irrelevant, and _slow
        # matches the setup corner above). Command-line only, so flow_state sees
        # no config.mk change -> --resume restarts at 4_cts, no re-synthesis.
        # Applies to every parent consuming per-corner child abstracts
        # (user_core, user_domain, janus_soc_top). JANUS_LEC_DEDUP_LIBS=0 opts out.
        if os.environ.get("JANUS_LEC_DEDUP_LIBS", "1") != "0":
            cmd.append("REMOVE_LIBS_FOR_LEC=$(filter %_fast.lib,$(ADDITIONAL_LIBS))")

        # ---- enable scan flops in the cell library (DFT) ---------------------
        # The ASAP7 platform FORBIDS the scan flops by default:
        #     platforms/asap7/config.mk:24  DONT_USE_CELLS  = *x1p*_ASAP7* *xp*_ASAP7*
        #     platforms/asap7/config.mk:25  DONT_USE_CELLS += SDF* ICG*
        # The `SDF*` pattern is exactly the scan D-flip-flops (SDFHx*/SDFLx* in
        # asap7sc7p5t_SEQ_*_nldm), so with the stock list scan_replace (dft_scan.tcl)
        # would find NO scan cells to swap in and DFT would be a silent no-op.
        # We rebuild the list WITHOUT SDF* (keeping the *x1p*/*xp* congestion
        # excludes and ICG*, which we are not using). The platform assigns
        # DONT_USE_CELLS with a plain `=` (not `?=`) and is read AFTER the block
        # config.mk, so a config.mk export would be clobbered -- the override MUST
        # come from the make command line (command-line assignment also suppresses
        # the platform's subsequent `+= SDF* ICG*`). Passed as ONE argv element:
        # subprocess runs make with no shell, so the embedded spaces are a single
        # value, not separate arguments. Gated by JANUS_DFT_SCAN (default on); set
        # JANUS_DFT_SCAN=0 to fall back to the stock (no-scan) cell list.
        if os.environ.get("JANUS_DFT_SCAN", "1") != "0":
            # §76 S4: JANUS_ARA_ALLOW_XP=1 (env, or `:= 1` in the block's
            # config.mk / *_local.mk) drops the `*xp*` pattern so the resizer
            # can use the intermediate (xp5/xp33/xp67) drive strengths for
            # sizeup on the WNS paths. *x1p* and ICG* stay excluded.
            allow_xp = os.environ.get("JANUS_ARA_ALLOW_XP") or self._janus_knob(block, "JANUS_ARA_ALLOW_XP") or "0"
            if allow_xp.strip() == "1":
                cmd.append("DONT_USE_CELLS=*x1p*_ASAP7* ICG*")
                self.ui.step(f"{block}: §76 S4 ALLOW_XP — *xp* cells enabled (DONT_USE_CELLS=*x1p*_ASAP7* ICG*)")
            else:
                cmd.append("DONT_USE_CELLS=*x1p*_ASAP7* *xp*_ASAP7* ICG*")

        # ---- OPTIONAL: cap detailed-route iterations -------------------------
        # TritonRoute's default DETAILED_ROUTE_END_ITERATION is 64. When set,
        # DRT stops after N optimization iterations even if DRC violations
        # remain, which BOUNDS runtime/cost on a run that is grinding many
        # iterations for a shrinking (or stuck) violation count. HONEST CAVEAT:
        # capping does NOT help convergence -- it just stops early with whatever
        # violations are left, so the route may be left DRC-DIRTY. Use it to
        # bound cost on exploratory runs, not for a signoff route. Off by
        # default (tool default 64 applies); set JANUS_DRT_MAX_ITER=<N> to cap.
        _drt_iter = os.environ.get("JANUS_DRT_MAX_ITER", "").strip()
        if _drt_iter:
            cmd.append(f"DETAILED_ROUTE_END_ITERATION={_drt_iter}")
        # ORFS variable-precedence workaround (verified against agent/user logs):
        # SYNTH_MOCK_LARGE_MEMORIES / SYNTH_MEMORY_MAX_BITS set via `export` in a
        # block's config.mk do NOT reach synth.tcl. In user_domain (4).log the
        # mock guard `if {$::env(SYNTH_MOCK_LARGE_MEMORIES)}` (synth.tcl:147) kept
        # evaluating FALSE (env value 0 = ORFS default winning), so the vendored
        # CVA6 multiport behavioral memories fell through to MEMORY_MAP and blew
        # the OPT_MUXTREE loop to 19.6 GB -> OOM (make Error 247) after ~48 min.
        # GNU-make command-line assignments carry the HIGHEST precedence and are
        # force-exported to the Yosys child env, which is exactly how ORFS's own
        # microwatt reference design turns the mock on (`make ...
        # SYNTH_MOCK_LARGE_MEMORIES=1`). Scoped to user_domain only: it is the
        # sole block with large un-mapped behavioral memories; every other block
        # binds its RAMs to fakeram macros and must NOT be mocked.
        # SYNTH_HIERARCHICAL=0 is REQUIRED for the mock to catch the big CVA6
        # cache memories. Proven in user_domain (5).log: with hierarchical=1 +
        # keep_hierarchy the mock DID fire (it printed "Memory ... has
        # dimensions" for the small proc_rom do_switch ROMs) but only saw
        # top-level $mem_v2 cells — the large wt_dcache_mem / tc_sram_wrapper
        # memories were locked inside kept submodules, invisible to the mock's
        # `select -list t:$mem_v2`, so none were shrunk and the final
        # `synth -run fine:` memory_map'd them into the 19.6 GB OOM. ORFS's mock
        # has no hierarchy-awareness (it is written/validated for the FLAT synth
        # path — exactly what the working microwatt reference uses). Flattening
        # before the mock exposes ALL memories (incl. the buried cache ones) as
        # top-level $mem_v2 so they cross the 4096-bit gate and get mocked.
        #
        # UPDATE (bottom-up hierarchy pivot): the CVA6/Ara caches no longer enter
        # the user_domain synthesis at all — they are bound to fakeram macros and
        # hardened inside the separate `user_core` block (ara_integration_wrapper),
        # which user_domain consumes as a blackbox macro. So user_domain's mock
        # now only shrinks the ONE remaining large behavioral memory, the NPU-L
        # 2RW byte-enabled scratchpad (FakeRAM is 1RW/no-mask, so it can't be
        # macro-bound yet). The force below is unchanged (mock on + flat) because
        # the flat mock is still the mechanism that catches that scratchpad, but
        # its blast radius is now tiny. user_core is NEVER mocked: its caches are
        # REAL macros (explicitly instantiated via the tc_sram funnel), so it
        # must run WITHOUT the mock to stay functionally faithful. Any other
        # block binds its RAMs to fakeram and must NOT be mocked either.
        #
        # user_core DOES need one forced knob, though: SYNTH_MEMORY_MAX_BITS.
        # (user_core.log 2026-07-18) ara_integration_wrapper leaves ~385 small
        # multiport structures as flops; the largest is a single 128x83 = 10624b
        # CVA6/Ara commit/LSU-class queue with no 1RW/no-mask FakeRAM target, so
        # it MUST synthesize as flops. ORFS's default 4096-bit inferred-memory
        # gate hard-FAILs on it (yosys synth check, ~1h48m in). Since ORFS does
        # NOT honor a config.mk export for this knob (same precedence issue as
        # the mock above -- synth.tcl reads $::env and sees ORFS's default), the
        # raise MUST come from the make command line. 16384 (2^14) clears 10624b
        # with headroom yet still trips if a real cache DATA way (>=32768b) ever
        # fell through to inference, so the guard stays meaningful. NO mock and
        # NO SYNTH_HIERARCHICAL override for user_core -- only the gate is raised.
        #
        # PARTITION MACROS (user_core_cva6 / user_core_ara): the flat CVA6+Ara
        # core OOM-killed DETAIL ROUTING at 27.3 GB (user_core (2).log), so
        # user_core is now split into two hardened partition macros consumed by
        # ara_integration_wrapper under -D JANUS_PARTITIONED. Each partition is
        # built flat (its config does NOT set JANUS_PARTITIONED) and inherits the
        # same SYNTH_MEMORY_MAX_BITS gate as user_core. The 128x83=10624b
        # commit/LSU-class flop queue is CVA6-side, so it lands in user_core_cva6;
        # user_core_ara carries the raise defensively (identical, meaningful gate
        # -- trips only if a wide Ara VRF way >=32768b ever fell through to
        # behavioral inference). NEITHER partition is ever mocked (both bind their
        # caches/VRF to real fakeram macros via the tc_sram funnel).
        force_vars = {
            "user_domain": ["SYNTH_MOCK_LARGE_MEMORIES=1",
                            "SYNTH_MEMORY_MAX_BITS=4096",
                            "SYNTH_HIERARCHICAL=0"],
            "user_core":      ["SYNTH_MEMORY_MAX_BITS=16384"],
            "user_core_cva6": ["SYNTH_MEMORY_MAX_BITS=16384"],
            # user_core_ara: JANUS_ARA_LANE_MACRO=1 is FORCED here, not left to a
            # sourced shell env, because a missing flag silently reverts ara.sv
            # to elaborating all 4 lanes FLAT (~620K inst) and OOM-kills detail
            # route at ~23 GB -- exactly the 8.5h wasted run in
            # 5_2_route.log (Error 247, peak 22,917,556 KB). With the flag,
            # ara.sv (rtl/external/ara/src/ara.sv:370, `ifdef
            # JANUS_PARTITIONED_LANE) instantiates the pre-hardened
            # user_core_ara_lane blackbox 4x instead, so the lane is routed ONCE.
            # The lane macro is guaranteed built first (dependency_manager.py
            # makes user_core_ara_lane a hard dep of user_core_ara). Command-line
            # assignment (highest GNU-make precedence) so config.mk's
            # `ifeq ($(JANUS_ARA_LANE_MACRO),1)` block reliably fires and its
            # ADDITIONAL_LEFS/LIBS/GDS pick up the lane abstract.
            # JANUS_ARA_LANE_MACRO defaults to 1 (lane-macro mode) because on a
            # memory-starved box flat Ara (~620K inst) OOM-kills detail route at
            # ~23 GB (5_2_route.log Error 247, peak 22,917,556 KB). BUT this is
            # now honored from the environment so a large-RAM box (e.g. 61 GB EC2)
            # can opt into FLAT Ara with `export JANUS_ARA_LANE_MACRO=0`, which
            # sidesteps the lane-abstract PDN saga entirely (no lane macros -> no
            # lane ElementGrid -> no PDN-0232). Command-line assignment still wins
            # over config.mk; we simply pass through whatever the env selected.
            #
            # ENABLE_RESISTANCE_AWARE=0 (GRT segfault fix): ASAP7's platform
            # config.mk sets `export ENABLE_RESISTANCE_AWARE ?= 1`, which makes
            # ORFS append `-resistance_aware` to `global_route`. That flag turns
            # on GRT-INTERNAL parasitic estimation inside the maze-route loop
            # (grt::FastRouteCore::CalculatePartialSlack ->
            # est::EstimateParasitics::estimateAllGlobalRouteParasitics ->
            # est::MakeWireParasitics::makeRouteParasitics -> layerRC), and on
            # the FLAT user_core_ara (JANUS_ARA_LANE_MACRO=0, 638,278 insts,
            # only M2-M7 usable signal layers, heavy congestion -> 30 GRT
            # iterations) that path SIGSEGVs during congestion "extra iteration
            # 3/30" (0924-1-user_core_ara.log: Signal 11 in layerRC, make
            # Error 245, peak 13.95 GB so NOT OOM). The custom calibrated RC
            # (configs/rc/user_core_ara_setRC.tcl) is NOT the trigger:
            # `estimate_parasitics -placement` ran 6x cleanly with setRC active
            # BEFORE GRT -- the crash is specific to the fragile res-aware GRT
            # feature on this flat/congested block. We disable it via a
            # command-line assignment (highest GNU-make precedence, beats the
            # platform `?= 1`); GRT falls back to standard congestion-driven
            # routing (res-aware is off-by-default on most platforms). For a
            # FREQUENCY-LOCK run this costs ~nothing: signoff WNS comes from
            # detailed route + RCX-extracted SPEF (6_final), not the GRT-internal
            # estimate, and setRC.tcl stays active for post-route repair
            # estimation (proven safe). Env-overridable: set
            # JANUS_ARA_RESIST_AWARE_GRT=1 to restore the platform default.
            # >>> PD-UNVALIDATED locally (no openroad/ASAP7 DB in sandbox);
            # on-box GRT re-run is the first exercise. <<<
            "user_core_ara":  ["SYNTH_MEMORY_MAX_BITS=16384",
                                f"JANUS_ARA_LANE_MACRO={os.environ.get('JANUS_ARA_LANE_MACRO', '1')}",
                                f"ENABLE_RESISTANCE_AWARE={os.environ.get('JANUS_ARA_RESIST_AWARE_GRT', '0')}"],
            # L0.25 lane sub-macro: same defensive gate as the Ara macro. The
            # lane VRF banks are 8 words deep (< asic_tc_sram MIN_MACRO_DEPTH=32)
            # so they infer flops, but keep the gate identical for determinism.
            "user_core_ara_lane": ["SYNTH_MEMORY_MAX_BITS=16384"],
        }
        cmd += force_vars.get(block, [])

        # ---- per-stage DEBUG DUMP hooks (timing rpts / netlists / images) ----
        # Wire the JANUS per-stage debug dumps unless --no-debug-dump. ORFS
        # sources an optional user Tcl at the end of each step via the
        # POST_<STEP>_TCL env-var mechanism (flow/scripts/util.tcl::
        # source_step_tcl -> source_env_var_if_exists). We pass the wrapper
        # paths as make command-line assignments (highest GNU-make precedence,
        # force-exported to the openroad recipe env -- same channel used for
        # CORNER/CORNERS above), so ORFS sources them with the design fully
        # loaded (liberty+LEF+ODB+SDC, STA live). Each hook body is catch-
        # guarded (asic_flow/scripts/hooks/janus_stage_dump.tcl) so a dump bug
        # can NEVER fail the step / abort the run. See hooks/README.md.
        # >>> PD-UNTESTED <<< authored from ORFS scripts on disk; the sandbox
        # has no openroad binary / ASAP7 DB. First on-box run is first exercise.
        if not getattr(self.args, "no_debug_dump", False):
            hooks_dir = os.path.join(_ROOT, "scripts", "hooks")
            step_wrappers = {
                "POST_FLOORPLAN_TCL":    "post_floorplan.tcl",
                "POST_MACRO_PLACE_TCL":  "post_macro_place.tcl",
                "POST_GLOBAL_PLACE_TCL": "post_global_place.tcl",
                "POST_RESIZE_TCL":       "post_resize.tcl",
                "POST_DETAIL_PLACE_TCL": "post_detail_place.tcl",
                "POST_CTS_TCL":          "post_cts.tcl",
                "POST_GLOBAL_ROUTE_TCL": "post_global_route.tcl",
                "POST_DETAIL_ROUTE_TCL": "post_detail_route.tcl",
                # §123: final_outputs.tcl `source_step_tcl POST FINAL_REPORT`
                # (after report_metrics 6 + SPEF) -> janus_09_final_debug.rpt
                # + final PNGs (worst path, clock tree, congestion, density).
                "POST_FINAL_REPORT_TCL": "post_final_report.tcl",
            }
            for env_var, fname in step_wrappers.items():
                cmd.append(f"{env_var}={os.path.join(hooks_dir, fname)}")
            # class toggles read by the dispatcher (default on inside the Tcl)
            cmd.append("JANUS_DUMP_REPORTS=1")
            cmd.append("JANUS_DUMP_NETLISTS=1")
            cmd.append(
                "JANUS_DUMP_IMAGES="
                + ("0" if getattr(self.args, "no_debug_images", False) else "1")
            )

        # ---- RESUME: mark reused stage artifacts "old" (--old-file) ----------
        # THE decisive fix for the recurring spurious full re-synth on --resume.
        # ORFS's synth make chain is rooted at
        #     $(RESULTS_DIR)/1_1_yosys_canonicalize.rtlil : $(YOSYS_DEPENDENCIES)
        # and $(YOSYS_DEPENDENCIES) (scripts/variables.mk) includes
        # $(SDC_FILE_CLOCK_PERIOD) — itself a FILE target rebuilt from the
        # (re-stamped) constraint SDC — plus the RTL/lib sources. When any of
        # those looks newer than the .rtlil, make rebuilds the .rtlil and the
        # WHOLE chain cascades (1_2_yosys.v -> 1_synth.odb -> 2_floorplan.odb ->
        # ...), re-running ~2h of synthesis even though the stage outputs are on
        # disk and valid. mtime-TOUCHING the outputs cannot win this race (a
        # phony/force or freshly-rebuilt prereq is *always* newer). GNU make's
        # `-o FILE` / `--old-file` / `--assume-old` is the correct tool: make is
        # told to treat FILE as older than every prerequisite, so it will (a)
        # never rebuild FILE, and (b) never rebuild anything downstream because
        # of FILE. We emit one `-o` per reused-stage artifact recorded by
        # _plan_stage_resume(). Because the recipes recurse via $(UNSET_AND_MAKE)
        # and the top make decides at each FILE target whether to run the recipe,
        # stopping the top make from rebuilding the .odb/.rtlil/.v also stops the
        # recursive sub-make from firing. Only files that still exist are passed.
        if getattr(self.args, "resume", False):
            old_files = self._resume_old_files.get(block) or []
            n_old = 0
            for p in old_files:
                if p and os.path.exists(p):
                    # CRITICAL bug fix: the -o/--old-file value MUST be the target
                    # name EXACTLY as ORFS's Makefile expresses it, which is
                    # RELATIVE to the make cwd (self.flow_dir):
                    #     results/asap7/<block>/base/1_synth.odb
                    # GNU make matches --old-file by literal target-name string,
                    # NOT by inode/realpath. _resume_old_files holds ABSOLUTE paths
                    # (flow_state emits os.path.abspath). Passing them verbatim
                    # matched NONE of make's relative targets, so every -o was a
                    # no-op and make silently re-ran the whole synth->...->route
                    # chain from scratch — the recurring "restarted even with
                    # --resume" bug (0924_2 fully re-synthesized despite 24 -o
                    # flags). Converting to a flow_dir-relative path makes the
                    # strings match so make actually treats the artifacts as old.
                    rel = os.path.relpath(p, self.flow_dir) if self.flow_dir else p
                    # Guard: if the artifact somehow lives outside flow_dir the
                    # relative form would escape with '..' and still not match a
                    # make target; keep the absolute path in that (never-observed)
                    # case rather than emit a broken '../..' flag.
                    flag = p if rel.startswith(os.pardir + os.sep) else rel
                    cmd += ["-o", flag]
                    n_old += 1
            if n_old:
                self.ui.step(f"{block}: resume — {n_old} reused artifact(s) "
                             f"passed to make as --old-file (relative target "
                             f"names; no spurious rebuild)")

        return cmd

    # ----- §75: make line for steps that run AFTER the block's build ---------
    def _post_build_make_cmd(self, block: str, targets: List[str]) -> List[str]:
        """_make_cmd(block) (same NUM_CORES / CORNER=WC / CORNERS / FLOW_VARIANT
        / --extra-make / resume -o) + `-o` for EVERY existing file under the
        block's results/, logs/ and objects/ leaf dirs, then `targets`.

        Post-build targets (generate_abstract, run) must never rebuild a stage:
        generate_abstract depends on 6_final.{gds,def,v,sdc}, whose chain goes
        back through $(LOG_DIR)/6_report.log ... to 1_1_yosys_canonicalize.rtlil
        : $(YOSYS_DEPENDENCIES) (incl. clock_period.txt <- constraint.sdc). Any
        re-stamped source would otherwise cascade a full re-synth. -o matches
        by literal target name, so paths are flow_dir-relative (like _make_cmd).
        The abstract's own outputs are not make targets (do-generate_abstract
        is phony), so marking them old cannot suppress their regeneration."""
        cmd = self._make_cmd(block)
        seen = {cmd[i + 1] for i, t in enumerate(cmd[:-1]) if t == "-o"}
        res = self._results_dir(block)
        extra: List[str] = []
        if res and self.flow_dir:
            res_root = os.path.join(self.flow_dir, "results")
            rel_leaf = os.path.relpath(res, res_root)
            if not rel_leaf.startswith(os.pardir):
                for sub in ("results", "logs", "objects"):
                    d = os.path.join(self.flow_dir, sub, rel_leaf)
                    if not os.path.isdir(d):
                        continue
                    for root, _dirs, files in os.walk(d):
                        for fn in sorted(files):
                            rel = os.path.relpath(os.path.join(root, fn), self.flow_dir)
                            if rel not in seen:
                                seen.add(rel)
                                extra += ["-o", rel]
        if extra:
            self.ui.debug(f"{block}: post-build make — {len(extra) // 2} existing "
                          "artifact(s) passed as --old-file (no stage rebuild)")
        return cmd + extra + list(targets)

    # ----- repo SOURCE config / sdc for a block ------------------------------
    # The stamped copies in the ORFS tree are regenerated by scripts/03 from
    # these. Config filename == block name (security.mk, noc.mk, ...); the SDC
    # lives under configs/constraints/<block>.sdc.
    def _repo_config_path(self, block: str) -> Optional[str]:
        p = os.path.join(self.configs_dir, f"{block}.mk")
        return p if os.path.isfile(p) else None

    def _repo_sdc_path(self, block: str) -> Optional[str]:
        p = os.path.join(self.configs_dir, "constraints", f"{block}.sdc")
        return p if os.path.isfile(p) else None

    # ----- stamp-freshness guard (prevents silent STOCK builds) --------------
    def _verify_stamp_fresh(self, block: str) -> Optional[str]:
        """Return an error string if the STAMPED ORFS config for `block` is out
        of sync with the repo SOURCE config, else None.

        WHY THIS EXISTS: `make DESIGN_CONFIG=<stamped>/config.mk` reads a COPY
        that scripts/03_setup_janus_configs.sh stamps from asic_flow/configs.
        §163: run() now re-stamps automatically at launch (_auto_restamp_all);
        this guard still catches the cases it skips, or an edit after launch.
        Before §163 agent.py did NOT auto-restamp, so if you `git pull` (or edit a source
        config / *_local.mk) and forget to re-run scripts/03, make silently uses
        the OLD stamped copy. Two real bugs hit exactly this: (1) scripts/03 not
        copying *_local.mk, and (2) config.mk's -include sitting below the
        VERILOG_DEFINES block. In BOTH cases the run built STOCK (no JANUS lever
        defines) while every surface signal looked fine. This guard makes that
        class of failure loud and CHEAP (abort before the multi-minute synth)
        instead of silent and expensive (a full ~$50/3h run that was stock).

        Checks, per block:
          * source configs/<blk>.mk must be byte-identical to the stamped
            <design_root>/<blk>/config.mk  (catches stale config.mk / ordering).
          * if source configs/<blk>_local.mk exists, the stamped
            <blk>_local.mk must exist and be byte-identical (catches missing or
            stale lever overrides).
          * if source <blk>_local.mk is ABSENT but a stamped one lingers, that
            is stale too (would apply levers the user removed).
          * source configs/constraints/<blk>.sdc (when present) must be
            byte-identical to the stamped <design_root>/<blk>/constraint.sdc —
            scripts/03 copies it verbatim. This is what previously let a STALE
            noc SDC through: the design dir kept a snapshot with the 600 MHz
            period but WITHOUT the §87 boundary-budget relief, so reg2out still
            read -126 ps while the repo SDC would have closed it. The .mk-only
            guard never noticed because the constraints live in the .sdc.
        """
        src_cfg = self._repo_config_path(block)
        if not src_cfg:
            return None  # block has no repo source config -> nothing to compare
        stamped_cfg = self._config_path(block)
        if not os.path.isfile(stamped_cfg):
            return None  # never stamped yet -> scripts/03 will create it; not our case

        def _same(a: str, b: str) -> bool:
            try:
                with open(a, "rb") as fa, open(b, "rb") as fb:
                    return fa.read() == fb.read()
            except OSError:
                return False

        problems: List[str] = []
        if not _same(src_cfg, stamped_cfg):
            problems.append(
                f"  config.mk DIFFERS: source {src_cfg}\n"
                f"                 vs stamped {stamped_cfg}")

        stamped_dir = os.path.dirname(stamped_cfg)
        src_local = os.path.join(self.configs_dir, f"{block}_local.mk")
        stamped_local = os.path.join(stamped_dir, f"{block}_local.mk")
        if os.path.isfile(src_local):
            if not os.path.isfile(stamped_local):
                problems.append(
                    f"  {block}_local.mk MISSING from stamped dir: {stamped_local}\n"
                    f"     (source overrides at {src_local} are NOT reaching make -> STOCK build)")
            elif not _same(src_local, stamped_local):
                problems.append(
                    f"  {block}_local.mk DIFFERS: source {src_local}\n"
                    f"                       vs stamped {stamped_local}")
        elif os.path.isfile(stamped_local):
            problems.append(
                f"  STALE {block}_local.mk in stamped dir (no source): {stamped_local}\n"
                f"     (a previous run's lever overrides would still apply)")

        # Constraints SDC. scripts/03 copies configs/constraints/<blk>.sdc verbatim
        # to <design_root>/<blk>/constraint.sdc (plain cp, no substitution), so a
        # byte-compare is valid. THIS IS THE GAP THAT LET A STALE noc SDC THROUGH:
        # the run carried the 600 MHz period but NOT the §87 boundary-budget relief
        # (uncertainty 60→40, out_frac 0.30→0.15) because the design dir held a
        # snapshot from between §86 and §87, and this guard only checked the .mk
        # files. A stale SDC silently signs off against the WRONG constraints.
        src_sdc = self._repo_sdc_path(block)
        if src_sdc:
            stamped_sdc = os.path.join(stamped_dir, "constraint.sdc")
            if not os.path.isfile(stamped_sdc):
                problems.append(
                    f"  constraint.sdc MISSING from stamped dir: {stamped_sdc}\n"
                    f"     (source constraints at {src_sdc} are NOT reaching make)")
            elif not _same(src_sdc, stamped_sdc):
                problems.append(
                    f"  constraint.sdc DIFFERS: source {src_sdc}\n"
                    f"                      vs stamped {stamped_sdc}\n"
                    f"     (run would sign off against STALE constraints — e.g. an "
                    f"old clock period / boundary budget)")

        if not problems:
            return None
        return (
            "STAMP OUT OF SYNC — the ORFS stamped config does not match the repo "
            "source, so this run would build with the WRONG (likely STOCK) JANUS "
            "levers.\n" + "\n".join(problems) +
            "\n\nFIX: re-stamp before running:\n"
            "    bash asic_flow/scripts/03_setup_janus_configs.sh\n"
            "then re-launch. (The launch-time auto re-stamp skipped this block or "
            "the source changed after launch. Override this guard "
            "with --skip-stamp-check only if you know the stamp is intentionally "
            "different.)")

    # ----- §163 automatic re-stamp at launch ----------------------------------
    # Same per-block copy scripts/03 step 1 does (configs/<blk>.mk -> config.mk,
    # <blk>_local.mk, constraints/<blk>.sdc -> constraint.sdc; plain cp). Runs
    # once at launch, BEFORE --force-blocks/--resume planning, so the semantic
    # config diff in flow_state sees the new values and picks the right restart
    # stage (comment-only edits invalidate nothing).
    #   restamped : stamp differs from the repo and nothing else owns the block
    #   fresh     : byte-identical -> no write (mtimes kept, no make churn)
    #   skipped   : --skip-stamp-check (deliberate hand edit); no repo config;
    #               never stamped (scripts/03 must run once for env/slang/cts
    #               setup); or another live process is building with this
    #               stamped config (re-stamping under it would re-synth it).
    # Mid-run (_run_make) never re-stamps: the resume plan was already made
    # from the launch-time stamp, so a later edit still aborts loudly there.
    def _stamp_pairs(self, block: str) -> List[Tuple[Optional[str], str]]:
        """(source or None, stamped dest) for every file scripts/03 stamps.
        Source None means the stamped copy must not exist."""
        bdir = os.path.join(self.design_root, block)
        src_local = os.path.join(self.configs_dir, f"{block}_local.mk")
        pairs: List[Tuple[Optional[str], str]] = [
            (self._repo_config_path(block), os.path.join(bdir, "config.mk")),
            (src_local if os.path.isfile(src_local) else None,
             os.path.join(bdir, f"{block}_local.mk"))]
        sdc = self._repo_sdc_path(block)
        if sdc:
            pairs.append((sdc, os.path.join(bdir, "constraint.sdc")))
        return pairs

    @staticmethod
    def _stamp_users(stamped_cfg: str) -> List[int]:
        """PIDs (other than us) whose command line references stamped_cfg."""
        me, pids = os.getpid(), []
        needle = stamped_cfg.encode()
        for d in glob.glob("/proc/[0-9]*"):
            try:
                pid = int(os.path.basename(d))
                if pid == me:
                    continue
                with open(os.path.join(d, "cmdline"), "rb") as fh:
                    if needle in fh.read():
                        pids.append(pid)
            except (OSError, ValueError):
                continue
        return pids

    def _auto_restamp(self, block: str) -> Tuple[str, str]:
        """Returns (state, detail); state in restamped|fresh|skipped."""
        if getattr(self.args, "skip_stamp_check", False):
            return "skipped", "--skip-stamp-check (stamp kept as-is)"
        if not self._repo_config_path(block):
            return "skipped", "no repo config"
        stamped_cfg = self._config_path(block)
        if not os.path.isfile(stamped_cfg):
            return "skipped", ("never stamped — run bash asic_flow/scripts/03_setup_janus_configs.sh "
                               "once (it also sets up the env file / slang / cts patch)")

        def _read(p: str) -> Optional[bytes]:
            try:
                with open(p, "rb") as fh:
                    return fh.read()
            except OSError:
                return None
        todo = []
        for src, dst in self._stamp_pairs(block):
            if src is None:
                if os.path.isfile(dst):
                    todo.append((None, dst))
            elif _read(src) != _read(dst):
                todo.append((src, dst))
        if not todo:
            return "fresh", ""
        users = self._stamp_users(stamped_cfg)
        if users:
            return "skipped", (f"stamp is stale but PID(s) {users[:5]} are building with "
                               f"{stamped_cfg}; not touching it")
        done = []
        for src, dst in todo:
            if src is None:
                os.remove(dst)
                done.append(f"removed {os.path.basename(dst)}")
                continue
            tmp = dst + ".janus_restamp.tmp"
            shutil.copyfile(src, tmp)
            os.replace(tmp, dst)
            done.append(os.path.basename(dst))
        return "restamped", ", ".join(done)

    def _auto_restamp_all(self) -> None:
        for b in list(self.dep.graph):
            try:
                state, detail = self._auto_restamp(b)
            except Exception as e:                  # never blocks the launch
                state, detail = "skipped", f"re-stamp failed: {e}"
            if state == "restamped":
                self.ui.good(f"{b}: stamp refreshed from repo ({detail})")
            elif state == "skipped" and detail != "no repo config":
                self.ui.warn(f"{b}: auto re-stamp skipped — {detail}")

    # ----- interactive shell prompt (no-op unless --interactive on a TTY) ----
    def _ask(self, question: str, choices: str, default: str) -> str:
        """Ask the user a one-key question on the shell. Returns `default`
        immediately when not interactive so autonomous runs never block."""
        if not self.interactive:
            return default
        try:
            with self.ui._lock:  # keep the prompt from interleaving with logs
                ans = input(f"\n[?] {question} [{choices}] (default={default}): ").strip().lower()
            return ans or default
        except (EOFError, KeyboardInterrupt):
            return default

    # ----- resolve the ORFS 'generate_abstract' make target at runtime -------
    def _detect_abstract_target(self) -> Optional[str]:
        """Find the make target that emits a block's .lef/.lib abstract.

        ORFS names it `generate_abstract` (confirmed against current master),
        but we grep the actual flow/Makefile so a renamed/older target is still
        picked up rather than hard-coding it. Returns None if the flow dir or a
        matching target cannot be found (then we simply skip abstract gen and
        say so, instead of running a bogus target)."""
        if self._abstract_probed:
            return self._abstract_target
        self._abstract_probed = True
        mkfile = os.path.join(self.flow_dir, "Makefile") if self.flow_dir else ""
        if not mkfile or not os.path.isfile(mkfile):
            self.ui.warn("abstract: flow/Makefile not found — skipping abstract "
                         "generation (blocks will still harden to GDS).")
            return None
        try:
            with open(mkfile, "r", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return None
        # Look for a rule/target whose name contains 'abstract' (e.g.
        # 'generate_abstract:' or a '.PHONY: ... generate_abstract' listing).
        cand = None
        for m in re.finditer(r"^([A-Za-z0-9_/.\-]*abstract[A-Za-z0-9_/.\-]*)\s*:",
                             text, re.M):
            name = m.group(1)
            if name.startswith("clean"):     # skip clean_abstract
                continue
            cand = name
            if name == "generate_abstract":  # strong preference for the canonical name
                break
        if cand is None and re.search(r"\bgenerate_abstract\b", text):
            cand = "generate_abstract"       # named only in .PHONY / a variable
        if cand:
            self._abstract_target = cand
            self.ui.debug(f"abstract: using make target '{cand}'")
        else:
            self.ui.warn("abstract: no 'abstract' target found in flow/Makefile "
                         "— this ORFS build may not support hierarchical "
                         "abstracts. Skipping (GDS is still produced).")
        return self._abstract_target

    # ----- generate the .lef/.lib abstract for a hardened block --------------
    def _run_abstract(self, block: str) -> None:
        """After a block reaches DONE, emit its abstract so the top level
        (janus_soc_top) can consume it as a macro. Best-effort: a failure here
        is logged but does NOT flip the block to FAILED (its GDS is already
        good)."""
        if self.args.no_abstract:
            return
        if block == "janus_soc_top":         # the top consumes abstracts; it isn't one
            return
        target = self._detect_abstract_target()
        if not target:
            return
        # §75: was a bare `make DESIGN_CONFIG=.. generate_abstract` — no -o
        # flags, no NUM_CORES/CORNER=WC/CORNERS. On --resume the re-stamped
        # config/SDC is newer than 1_1_yosys_canonicalize.rtlil, so that bare
        # make re-ran synth -> ... -> route single-threaded on the default
        # corner, overwriting the finished .odbs (agent_domain 2026-10-08).
        cmd = self._post_build_make_cmd(block, [target])
        self.ui.step(f"{block}: generating abstract (.lef/.lib) via '{target}'")
        log_path = self._block_log(block)
        try:
            with open(log_path, "ab", buffering=0) as logf:
                logf.write(f"\n===== {block} : {' '.join(cmd)} @ {datetime.now()} =====\n".encode())
                proc = subprocess.run(cmd, cwd=self.flow_dir, stdout=logf,
                                      stderr=subprocess.STDOUT)
        except (OSError, subprocess.SubprocessError) as e:
            self.ui.warn(f"{block}: abstract generation could not run: {e}")
            return
        if proc.returncode == 0:
            lef = self._find_abstract(block, ".lef")
            lib = self._find_abstract(block, ".lib")
            self.results[block].note += "  +abstract"
            self.ui.good(f"{block}: abstract OK — "
                         f"lef={os.path.basename(lef) if lef else 'n/a'} "
                         f"lib={os.path.basename(lib) if lib else 'n/a'}")
        else:
            self.ui.warn(f"{block}: abstract target rc={proc.returncode} "
                         f"(GDS is still valid; see {os.path.basename(log_path)}). "
                         "Top-level integration will need this .lef/.lib.")

    def _abstract_base_dirs(self, block: str) -> List[str]:
        """Candidate results/<...>/base dirs for a block, bare block name FIRST.

        ORFS writes to results/asap7/<DESIGN_NICKNAME>/base and DESIGN_NICKNAME is
        the bare block name (confirmed by 1_synth.log write_db paths) — NOT
        'janus/<block>'. We keep the legacy janus/<block> layout as a fallback so
        old result trees still resolve, mirroring _results_dir().
        """
        if not self.orfs:
            return []
        root = os.path.join(self.orfs, "flow", "results", "asap7")
        return [
            os.path.join(root, block, self._leaf()),
            os.path.join(root, f"janus_{block}", self._leaf()),
            os.path.join(root, "janus", block, self._leaf()),
        ]

    def _find_abstract(self, block: str, ext: str) -> Optional[str]:
        """Locate a freshly generated abstract file (.lef/.lib) for a block."""
        for base in self._abstract_base_dirs(block):
            hits = sorted(glob.glob(os.path.join(base, f"*{ext}")))
            if hits:
                return hits[0]
        return None

    def _find_routed_odb(self, block: str) -> Optional[str]:
        """Locate the final routed .odb for a block (prefer 6_final, then route)."""
        for base in self._abstract_base_dirs(block):
            for name in ("6_final.odb", "5_route.odb", "5_2_route.odb"):
                p = os.path.join(base, name)
                if os.path.exists(p):
                    return p
            # last resort: highest-numbered odb in the dir
            hits = sorted(glob.glob(os.path.join(base, "*.odb")))
            if hits:
                return hits[-1]
        return None

    def _classify_timing(self, block: str) -> None:
        """Run the read-only classified-timing probe on the routed .odb and
        attach the breakdown (authoritative setup WNS + hold WS + violating
        endpoint counts by path class) to the block result. Best-effort: if
        openroad or the .odb is unavailable it leaves prior metrics untouched.
        """
        if getattr(self.args, "no_physical", False):
            return
        odb = self._find_routed_odb(block)
        if not odb:
            self.ui.debug(f"{block}: no routed .odb found — skipping timing detail")
            return
        openroad = os.path.join(self.orfs, "tools", "install", "OpenROAD",
                                "bin", "openroad")
        if not os.path.isfile(openroad):
            openroad = "openroad"          # fall back to PATH
        ct = classify_routed_odb(odb, openroad_bin=openroad)
        if not ct.ok:
            self.ui.debug(f"{block}: classified timing unavailable — {ct.error}")
            return
        res = self.results[block]
        res.timing_detail = tc_render(ct)
        if res.timing_source == "6_finish":
            # 6_finish is RCX-extracted signoff; this re-time of the .odb is
            # not. Keep the breakdown as detail but never let it overwrite the
            # final numbers (that would re-introduce the false DONE⚠).
            self.ui.info(f"{block}: classified timing (detail only; WNS/hold "
                         f"kept from 6_finish) —\n{res.timing_detail}")
            return
        # Prefer these slacks over the stdout-scraped ones; this is where hold
        # stops being 'n/a' when no 6_finish report exists.
        if ct.setup_wns is not None:
            res.wns_ns = ct.setup_wns
        if ct.setup_tns is not None:
            res.tns_ns = ct.setup_tns
        if ct.hold_wns is not None:
            res.hold_ps = ct.hold_wns
        res.timing_detail = tc_render(ct)
        self.ui.info(f"{block}: classified timing —\n{res.timing_detail}")

    def _orfs_lib_env(self, block: str) -> Dict[str, str]:
        """Best-effort discovery of the liberty + SDC the ECO Tcl needs to
        re-time a saved .odb. If we can't find them the Tcl's timing queries
        return nothing -> ECO ok=False -> we keep the original (safe)."""
        env: Dict[str, str] = {}
        sdc = os.path.join(self.design_root, block, "constraint.sdc")
        if os.path.isfile(sdc):
            env["SDC_FILE"] = sdc
        libs: List[str] = []
        if self.orfs:
            # §149: ASAP7 std-cell libs live in lib/NLDM/ (box 6_report.log:
            # read_liberty -corner slow .../asap7/lib/NLDM/...). The old
            # lib/*.lib glob matched nothing -> std cells untimed in the ECO.
            for pat in ("flow/platforms/asap7/lib/NLDM/*.lib",
                        "flow/platforms/asap7/lib/NLDM/*.lib.gz",
                        "flow/platforms/asap7/lib/*.lib",
                        "flow/platforms/asap7/lib/*.lib.gz"):
                libs += sorted(glob.glob(os.path.join(self.orfs, pat)))
        # hardened child-block .lib abstracts + SRAM libs also matter for the top
        libs += sorted(glob.glob(os.path.join(self.sram_out_dir, "*.lib")))
        if libs:
            env["LIB_FILES"] = " ".join(libs)
        # §76 L3: give the ECO real parasitics + a wire-RC model. Without these
        # post_route_opt.tcl's estimate_parasitics has no RC (repair_timing then
        # optimises against zero wire delay) and never sees the routed SPEF.
        if self.orfs:
            pdir = os.path.join(self.orfs, "flow", "platforms", "asap7")
            if os.path.isdir(pdir):
                env["PLATFORM_DIR"] = pdir
        odb = self._find_routed_odb(block)
        if odb:
            spef = os.path.join(os.path.dirname(odb), "6_final.spef")
            if os.path.isfile(spef):
                env["JANUS_SPEF"] = spef
        rc_file = self._janus_knob(block, "JANUS_ARA_LAYER_RC_FILE")
        if rc_file and self._janus_knob(block, "JANUS_ARA_LAYER_RC") == "1":
            rc_file = os.path.expandvars(rc_file.replace("$(JANUS_RTL_ROOT)", self.rtl_root or os.path.dirname(_ROOT)))
            if os.path.isfile(rc_file):
                env["LAYER_PARASITICS_FILE"] = rc_file
        return env

    def _janus_knob(self, block: str, name: str) -> Optional[str]:
        """Read a JANUS_* make knob as the flow will see it: last assignment in
        <design>/<block>_local.mk wins over <design>/<block>/config.mk (the
        local file is `-include`d and uses `:=`). Returns the raw RHS string or
        None. Cheap text scan - does not evaluate make."""
        bdir = os.path.join(self.design_root, block)
        val: Optional[str] = None
        pat = re.compile(r"^\s*" + re.escape(name) + r"\s*[:?]?=\s*(.*?)\s*$")
        for fn in (os.path.join(bdir, "config.mk"), os.path.join(bdir, f"{block}_local.mk")):
            try:
                with open(fn, encoding="utf-8", errors="replace") as fh:
                    found = None
                    for line in fh:
                        m = pat.match(line.split("#", 1)[0])
                        if m:
                            found = m.group(1)
                    if found is not None:
                        val = found
            except OSError:
                continue
        return val

    def _openroad_bin(self) -> str:
        cand = os.path.join(self.orfs, "tools", "install", "OpenROAD",
                            "bin", "openroad") if self.orfs else ""
        return cand if cand and os.path.isfile(cand) else "openroad"

    def _post_route_opt(self, block: str) -> None:
        """Adaptive post-route ECO: try to close residual timing (hold first —
        it is functionally critical), then optionally clean a handful of DRCs.
        DISCARDS any ECO that degrades timing; the canonical .odb is only
        replaced when the ECO strictly improves it. Best-effort and fully
        reversible — never flips a passing block to FAILED."""
        if getattr(self.args, "no_post_route_opt", False):
            self.ui.step(f"{block}: post-route ECO skipped (--no-post-route-opt)")
            return
        res = self.results[block]
        setup = res.wns_ns          # authoritative setup WNS (ps) from _classify_timing
        hold = res.hold_ps          # authoritative hold WS (ps)
        drc = res.drc_violations
        has_setup_viol = setup is not None and setup < 0.0
        has_hold_viol = hold is not None and hold < 0.0
        timing_closed = (setup is not None and setup >= 0.0
                         and hold is not None and hold >= 0.0)
        # §76 L3: every skip path is now logged at step level (the §68 log had
        # NO JANUS_PR lines and no explanation why the ECO never ran).
        if setup is None and hold is None:
            self.ui.warn(f"{block}: post-route ECO skipped — _classify_timing produced no "
                         f"setup/hold numbers (routed .odb or openroad probe missing)")
            return
        # nothing to do: closed timing and no DRCs
        if not (has_setup_viol or has_hold_viol) and not (
                timing_closed and drc and drc > 0):
            self.ui.step(f"{block}: post-route ECO not needed (setup {setup} ps, hold {hold} ps, drc {drc})")
            return
        odb = self._find_routed_odb(block)
        if not odb:
            self.ui.warn(f"{block}: no routed .odb — skipping post-route ECO")
            return
        orb = self._openroad_bin()
        env = self._orfs_lib_env(block)
        tmpdir = os.path.dirname(odb)
        res_dir = self._results_dir(block)
        orfs_ok = bool(self.flow_dir and res_dir
                       and os.path.isfile(os.path.join(res_dir, "6_final.odb")))

        # ---- Stage 1 (§149, ORFS path): hold ECO, then optional setup ECO ----
        if (has_setup_viol or has_hold_viol) and orfs_ok:
            self._eco_log(block, f"post-route ECO start: setup WS {setup} ps, hold WS "
                                 f"{hold} ps (6_finish RCX)")
            if has_hold_viol:
                setup, hold = self._eco_pass_orfs(block, "hold", setup, hold)
            if setup is not None and setup < 0.0 and (hold is None or hold >= 0.0):
                setup, hold = self._eco_pass_orfs(block, "setup", setup, hold)
            elif setup is not None and setup < 0.0:
                self._eco_log(block, f"setup ECO NOT attempted — hold still {hold} ps "
                                     "(hold is closed first)")
            timing_closed = (setup is not None and setup >= 0.0
                             and hold is not None and hold >= 0.0)
            drc = res.drc_violations
        # ---- Stage 1 (legacy: no ORFS results dir): timing ECO ---------------
        elif has_setup_viol or has_hold_viol:
            crit = "HOLD (functionally critical)" if has_hold_viol else "setup"
            self.ui.step(f"{block}: residual {crit} violation — running post-route "
                         f"timing ECO (hold repaired first)")
            eco = run_eco_pass(odb, mode="timing", openroad_bin=orb,
                               extra_env=env, tmpdir=tmpdir)
            verdict, why = decide_timing(before_setup=setup, before_hold=hold, after=eco)
            if verdict == "adopt":
                if adopt_odb(eco.out_odb, odb):
                    self.ui.good(f"{block}: post-route ECO ADOPTED — {why}")
                    res.note += "  +eco(timing)"
                    self._classify_timing(block)   # refresh displayed metrics
                    # refresh locals for the DRC stage below
                    setup, hold, drc = res.wns_ns, res.hold_ps, res.drc_violations
                    timing_closed = (setup is not None and setup >= 0.0
                                     and hold is not None and hold >= 0.0)
                else:
                    discard_odb(eco.out_odb)
                    self.ui.warn(f"{block}: ECO adopt failed to swap .odb — kept original")
            else:
                discard_odb(eco.out_odb)
                self.ui.warn(f"{block}: post-route ECO DISCARDED — {why}")

        # ---- Stage 2: DRC-only cleanup (only if timing is closed) -----------
        if timing_closed and drc and 0 < drc:
            # dry gate first: is a DRC ECO even worth attempting? (uses an empty
            # ECOResult purely to evaluate the skip conditions on the *before*.)
            probe, why0 = decide_drc(timing_closed, drc, ECOResult(),
                                     handful=self.args.drc_handful)
            if probe == "skip":
                if "congestion" in why0:
                    self.ui.warn(f"{block}: {drc} DRCs — {why0}")
                return
            self.ui.step(f"{block}: timing closed, {drc} DRC(s) remain — DRC-only ECO")
            eco = run_eco_pass(odb, mode="drc", openroad_bin=orb,
                               extra_env=env, tmpdir=tmpdir)
            verdict, why = decide_drc(timing_closed, drc, eco,
                                      handful=self.args.drc_handful,
                                      pre_setup=setup, pre_hold=hold)
            if verdict == "adopt" and adopt_odb(eco.out_odb, odb):
                self.ui.good(f"{block}: DRC ECO ADOPTED — {why}")
                res.note += "  +eco(drc)"
                self._classify_timing(block)
            else:
                discard_odb(eco.out_odb)
                self.ui.warn(f"{block}: DRC ECO DISCARDED — {why}")

    # ----- §149: post-route timing ECO through ORFS `make run` ---------------
    # Why: the legacy ECO (openroad + read_liberty $LIB_FILES) globbed
    # platforms/asap7/lib/*.lib, but ASAP7 libs are in lib/NLDM/, so std cells
    # were untimed; it also ran on the FILLED 6_final.odb (no room for hold
    # buffers) with no RCX. ara 20261011 log has no ECO verdict at all and
    # 6_finish kept hold WS -4.55 ps / 9 violators. Through ORFS `make run` the
    # Tcl gets the block's real corners/libs/SDC/RCX_RULES/FILL_CELLS.
    _ECO_TIMEOUT_S = 14400        # ara 5_2_route took 12803 s from scratch
    _ECO_HOLD_SETUP_TOL_PS = 5.0  # setup give-back allowed when a hold pass closes hold

    def _eco_log(self, block: str, msg: str) -> None:
        """Verdicts go to the block log too (ui.* may not reach it)."""
        self.ui.step(f"{block}: {msg}")
        try:
            with open(self._block_log(block), "a", encoding="utf-8") as fh:
                fh.write(f"[janus-eco] {datetime.now():%H:%M:%S} {msg}\n")
        except OSError:
            pass

    def _eco_hold_margin(self, block: str) -> str:
        """ECO hold over-fix (ps). Default 10: the ECO is measured on fresh RCX
        after an INCREMENTAL reroute, so only new-net erosion remains (the
        in-flow margin must absorb the full GRT->RCX shift, §149: ~24.5 ps).
        Override per block with JANUS_ECO_HOLD_MARGIN_PS in config/local.mk."""
        v = self._janus_knob(block, "JANUS_ECO_HOLD_MARGIN_PS")
        try:
            return f"{float(v):g}" if v else "10"
        except ValueError:
            return "10"

    def _eco_outputs(self, res_dir: str, mode: str) -> Dict[str, str]:
        tag = f"janus_eco_{mode}"
        return {k: os.path.join(res_dir, f"{tag}.{k}") for k in ("odb", "spef", "def", "v")}

    def _run_eco_orfs(self, block: str, mode: str, in_odb: str = "6_final.odb",
                      in_spef: Optional[str] = None,
                      tag_mode: Optional[str] = None) -> Tuple[ECOResult, Dict[str, str]]:
        """One ECO pass (hold|setup) via ORFS `make run` on results/<in_odb>
        (default 6_final.odb; §163 chains a hold pass on janus_eco_setup.odb).
        Writes janus_eco_<tag_mode or mode>.{odb,spef,def,v} beside it; never
        touches the canonical files. Returns (parsed result, output paths)."""
        r = ECOResult(mode=mode)
        res_dir = self._results_dir(block)
        if not (self.flow_dir and res_dir):
            r.error = "no ORFS results dir"
            return r, {}
        tmode = tag_mode or mode
        outs = self._eco_outputs(res_dir, tmode)
        for p in outs.values():
            if os.path.exists(p):
                os.remove(p)                 # stale outputs of an earlier pass
        rpt_dir = self._reports_dir(block) or res_dir
        tag = f"janus_eco_{tmode}"
        tcl = os.path.join(_ROOT, "scripts", "post_route_opt.tcl")
        targets = ["run", f"RUN_SCRIPT={tcl}", f"RUN_LOG_NAME_STEM={tag}",
                   f"JANUS_PR_IN_ODB={in_odb}", "JANUS_PR_IN_SDC=6_final.sdc",
                   f"JANUS_PR_MODE={mode}",
                   f"JANUS_PR_HOLD_MARGIN={self._eco_hold_margin(block)}",
                   f"JANUS_OUT_ODB={outs['odb']}", f"JANUS_OUT_SPEF={outs['spef']}",
                   f"JANUS_OUT_DEF={outs['def']}", f"JANUS_OUT_V={outs['v']}",
                   f"JANUS_PR_RPT={os.path.join(rpt_dir, tag + '.rpt')}",
                   f"JANUS_PR_DRC_RPT={os.path.join(rpt_dir, tag + '_drc.rpt')}"]
        spef = in_spef or os.path.join(res_dir, "6_final.spef")
        if os.path.isfile(spef):
            targets.append(f"JANUS_SPEF={spef}")
        cmd = self._post_build_make_cmd(block, targets)
        if getattr(self.args, "dry_run", False):
            self.ui.info(f"{block}: [dry-run] {tag}: {' '.join(cmd)}")
            r.error = "dry-run"
            return r, outs
        keep: List[str] = []
        try:
            with open(self._block_log(block), "ab", buffering=0) as logf:
                logf.write(f"\n===== {block} : {tag} {' '.join(cmd)} "
                           f"@ {datetime.now()} =====\n".encode())
                proc = subprocess.Popen(cmd, cwd=self.flow_dir, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, start_new_session=True)
                killed: List[bool] = []

                def _kill() -> None:
                    killed.append(True)
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                timer = threading.Timer(self._ECO_TIMEOUT_S, _kill)
                timer.start()
                try:
                    for line in proc.stdout:
                        logf.write(line)
                        if line.startswith(b"JANUS_PR"):
                            keep.append(line.decode(errors="replace").rstrip())
                    proc.wait()
                finally:
                    timer.cancel()
                timed_out = bool(killed)
        except Exception as e:                       # never flips DONE
            r.error = f"could not run: {e}"
            return r, outs
        if timed_out:
            r.error = f"timed out after {self._ECO_TIMEOUT_S}s"
            return r, outs
        out = "\n".join(keep) + "\n"
        parsed = parse_eco(out, mode=mode, out_odb=outs["odb"])
        parsed.pre_setup, parsed.pre_hold = parse_eco_pre(out)
        if proc.returncode != 0 and parsed.ok:
            # e.g. openroad SIGSEGV after printing slacks: never trust it
            parsed.ok = False
            parsed.error = f"make rc={proc.returncode}"
        if not parsed.ok and not parsed.error:
            parsed.error = f"make rc={proc.returncode}, no JANUS_PR slacks/write"
        return parsed, outs

    def _adopt_eco_orfs(self, block: str, outs: Dict[str, str]) -> bool:
        """Swap janus_eco_* over 6_final.{odb,spef,def,v} (backups *.preeco) and
        re-merge 6_final.gds from the new def (ORFS do-gds-merged), so abstract,
        sign-off RCX/STA and the parent's GDS all see the ECO'd layout. Any
        failure restores every backup (all-or-nothing)."""
        res_dir = self._results_dir(block)
        missing = [k for k, p in outs.items() if not os.path.isfile(p)]
        if not res_dir or missing:
            self._eco_log(block, f"ECO not adopted — missing outputs {missing}")
            return False
        pairs = [(outs[k], os.path.join(res_dir, f"6_final.{k}"))
                 for k in ("odb", "spef", "def", "v")]
        gds = next((os.path.join(res_dir, f"6_final.{e}") for e in ("gds", "oas")
                    if os.path.isfile(os.path.join(res_dir, f"6_final.{e}"))), None)
        backups = [c for _n, c in pairs if os.path.isfile(c)] + ([gds] if gds else [])

        def _restore() -> None:
            for c in backups:
                if os.path.isfile(c + ".preeco"):
                    shutil.copy2(c + ".preeco", c)
        try:
            for c in backups:
                shutil.copy2(c, c + ".preeco")
            for new, canon in pairs:
                shutil.move(new, canon)
            if gds:
                merged = os.path.join(res_dir, "6_1_merged" + os.path.splitext(gds)[1])
                if os.path.isfile(merged):
                    os.remove(merged)
                cmd = self._post_build_make_cmd(block, ["do-gds-merged"])
                with open(self._block_log(block), "ab", buffering=0) as logf:
                    logf.write(f"\n===== {block} : eco gds {' '.join(cmd)} =====\n".encode())
                    rc = subprocess.run(cmd, cwd=self.flow_dir, stdout=logf,
                                        stderr=subprocess.STDOUT, timeout=7200).returncode
                if rc != 0 or not os.path.isfile(merged):
                    raise RuntimeError(f"do-gds-merged rc={rc}")
                shutil.copy2(merged, gds)
            return True
        except Exception as e:
            _restore()
            self._eco_log(block, f"ECO adopt failed ({e}) — restored pre-ECO 6_final.*")
            return False

    # §150: an adopted ECO replaces 6_final.* but NOT 6_finish.rpt, so every
    # 6_finish-based reader (adaptive base pass, resumed-QoR table) saw the
    # pre-ECO slack. ara 20261018_213427: ECO took setup -61.315 -> -10.837 ps,
    # adaptive compared its pass (-51.43, no ECO) against the stale -61.32 and
    # promoted it. The adopted numbers are persisted beside 6_finish.rpt.
    _ECO_ADOPTED_JSON = "janus_eco_adopted.json"

    def _eco_record_adopted(self, block: str, mode: str, eco: ECOResult) -> None:
        rep = self._reports_dir(block)
        if not rep:
            return
        body = {"block": block, "mode": mode, "setup_ws": eco.setup_wns,
                "setup_tns": eco.setup_tns, "hold_ws": eco.hold_wns,
                "hold_tns": eco.hold_tns, "drc": eco.drc,
                "source": f"post-route ECO RCX (janus_eco_{mode})",
                "at": datetime.now().isoformat(timespec="seconds")}
        try:
            with open(os.path.join(rep, self._ECO_ADOPTED_JSON), "w") as fh:
                json.dump(body, fh, indent=2)
        except OSError as e:
            self.ui.debug(f"{block}: {self._ECO_ADOPTED_JSON} write failed: {e}")

    def _eco_adopted_timing(self, block: str) -> Optional[dict]:
        """Timing of the ECO'd 6_final.* in force, or None (no ECO adopted, or
        6_finish.rpt is NEWER, i.e. the block was re-finished since)."""
        rep = self._reports_dir(block)
        if not rep:
            return None
        p = os.path.join(rep, self._ECO_ADOPTED_JSON)
        fin = os.path.join(rep, "6_finish.rpt")
        if not os.path.isfile(p):
            return None
        if os.path.isfile(fin) and os.path.getmtime(fin) > os.path.getmtime(p):
            return None
        try:
            with open(p) as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return None
        if d.get("setup_ws") is None and d.get("hold_ws") is None:
            return None
        return d

    def _eco_pass_orfs(self, block: str, mode: str, setup: Optional[float],
                       hold: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
        """Run + judge one pass. Before = JANUS_PR_PRE (same session, same
        6_final.spef) when present, else the 6_finish numbers. Returns the
        (setup, hold) in force afterwards."""
        res = self.results[block]
        self._eco_log(block, f"{mode} ECO via ORFS make run (hold margin "
                             f"{self._eco_hold_margin(block)} ps, incremental reroute + RCX)")
        eco, outs = self._run_eco_orfs(block, mode)
        b_setup = eco.pre_setup if eco.pre_setup is not None else setup
        b_hold = eco.pre_hold if eco.pre_hold is not None else hold
        verdict, why = decide_timing(before_setup=b_setup, before_hold=b_hold, after=eco)
        # Hold is the must-fix (user rule): a hold pass that CLOSES hold is kept
        # even if RCX moves setup down by <= _ECO_HOLD_SETUP_TOL_PS (repair_timing
        # -hold already refuses buffers that would break setup on their path;
        # the residual is re-extraction shift on neighbouring nets).
        if (verdict == "discard" and mode == "hold" and eco.ok
                and b_hold is not None and b_hold < 0.0
                and eco.hold_wns is not None and eco.hold_wns >= 0.0
                and b_setup is not None and eco.setup_wns is not None
                and b_setup - eco.setup_wns <= self._ECO_HOLD_SETUP_TOL_PS):
            verdict, why = "adopt", (f"hold CLOSED {b_hold:+.3f}->{eco.hold_wns:+.3f} ps; "
                                     f"setup {b_setup:+.3f}->{eco.setup_wns:+.3f} ps "
                                     f"(within {self._ECO_HOLD_SETUP_TOL_PS:g} ps tolerance)")
        # a setup pass must never open hold that was closed (noise band incl.)
        if (verdict == "adopt" and mode == "setup" and b_hold is not None
                and b_hold >= 0.0 and (eco.hold_wns is None or eco.hold_wns < 0.0)):
            verdict, why = "discard", f"setup ECO opened hold ({eco.hold_wns} ps)"
        # §163: a setup pass that gained setup but opened hold gets ONE hold
        # pass on top of its own output; the combined result is judged against
        # the original pre-ECO numbers with the same hold-first rules.
        if verdict == "discard" and self._eco_setup_gain_hold_lost(eco, b_setup, b_hold):
            first_why = why
            chained = self._eco_chain_hold(block, eco, outs, b_setup, b_hold)
            if chained is not None:
                eco, outs, verdict, why = chained
                why = f"{first_why}; {why}"
        # the incremental reroute must not leave new routing DRCs behind
        b_drc = res.drc_violations or 0
        if verdict == "adopt" and eco.drc is None:
            verdict, why = "discard", "ECO reroute DRC count unknown (no JANUS_PR drc)"
        if verdict == "adopt" and eco.drc is not None and eco.drc > b_drc:
            verdict, why = "discard", f"ECO reroute left {eco.drc} DRC(s) (before {b_drc})"
        if verdict == "adopt" and self._adopt_eco_orfs(block, outs):
            res.wns_ns, res.hold_ps = eco.setup_wns, eco.hold_wns
            if eco.setup_tns is not None:
                res.tns_ns = eco.setup_tns
            self._eco_record_adopted(block, eco.mode or mode, eco)
            res.note += f"  +eco({eco.mode or mode}) RCX WS {eco.setup_wns}/{eco.hold_wns}"
            self._eco_log(block, f"{mode} ECO ADOPTED — {why} (drc {eco.drc}); "
                                 "6_final.{odb,spef,def,v,gds} replaced, *.preeco kept")
            self.ui.good(f"{block}: post-route {mode} ECO ADOPTED — {why}")
            return eco.setup_wns, eco.hold_wns
        for p in outs.values():
            discard_odb(p)
        if verdict == "adopt":
            return setup, hold                     # adopt failed; already logged
        self._eco_log(block, f"{mode} ECO DISCARDED — {why}")
        return setup, hold

    @staticmethod
    def _eco_setup_gain_hold_lost(eco: ECOResult, b_setup: Optional[float],
                                  b_hold: Optional[float]) -> bool:
        """§163: setup ECO is worth a chained hold pass only when it produced
        trusted data, gained setup beyond noise, routed with a known DRC count
        and the sole problem is hold (now negative)."""
        return bool(eco.ok and eco.mode == "setup" and eco.drc is not None
                    and b_setup is not None and eco.setup_wns is not None
                    and eco.setup_wns > b_setup + 1.0
                    and eco.hold_wns is not None and eco.hold_wns < 0.0
                    and (b_hold is None or eco.hold_wns < b_hold))

    def _eco_chain_hold(self, block: str, s_eco: ECOResult, s_outs: Dict[str, str],
                        b_setup: Optional[float], b_hold: Optional[float]):
        """§163: run a hold pass on janus_eco_setup.{odb,spef}; return
        (eco, outs, verdict, why) for the combined result, or None (setup
        outputs are left for the caller to discard)."""
        if not (os.path.isfile(s_outs.get("odb", "")) and os.path.isfile(s_outs.get("spef", ""))):
            return None
        self._eco_log(block, f"setup ECO gained setup {b_setup}->{s_eco.setup_wns} ps but hold "
                             f"{b_hold}->{s_eco.hold_wns} ps — chaining a hold pass on its output")
        h_eco, h_outs = self._run_eco_orfs(block, "hold",
                                           in_odb=os.path.basename(s_outs["odb"]),
                                           in_spef=s_outs["spef"], tag_mode="setup_hold")
        for p in s_outs.values():
            discard_odb(p)                 # intermediate; the chain output replaces it
        verdict, why = decide_timing(before_setup=b_setup, before_hold=b_hold, after=h_eco)
        if verdict == "adopt" and b_hold is not None and b_hold >= 0.0 and (
                h_eco.hold_wns is None or h_eco.hold_wns < 0.0):
            verdict, why = "discard", f"setup+hold ECO still leaves hold {h_eco.hold_wns} ps"
        h_eco.mode = "setup_hold"
        return h_eco, h_outs, verdict, ("setup+hold chain: " + why)

    # ----- ORFS logs directory for a block -----------------------------------
    def _logs_dir(self, block: str) -> Optional[str]:
        """Locate the ORFS *logs* dir for a block (holds N_*.log per substage).

        Same layout uncertainty as _results_dir/_reports_dir: ORFS writes under
        flow/logs/<platform>/<design>/base and DESIGN_NICKNAME is usually the
        bare block name. Return the first candidate that actually holds logs.
        """
        if not self.flow_dir:
            return None
        cands = [
            os.path.join(self.flow_dir, "logs", "asap7", block, self._leaf()),
            os.path.join(self.flow_dir, "logs", "asap7", f"janus_{block}", self._leaf()),
        ]
        # §154: exact names only (no sibling-block glob).
        cands.append(os.path.join(self.flow_dir, "logs", "asap7", "janus", block, self._leaf()))
        for d in cands:
            if os.path.isdir(d) and glob.glob(os.path.join(d, "*.log")):
                return d
        return None

    # ----- golden archive of a converged block -------------------------------
    # Heavy design binaries: kept ON DISK in the golden snapshot (so it is a
    # complete restorable copy) but git-ignored by default so the shared repo
    # is not bloated with hundreds of MB per converged block. --golden-include
    # -heavy commits them too.
    _GOLDEN_HEAVY_EXT = (".gds", ".gds.gz", ".odb", ".def", ".spef", ".spef.gz")

    def _golden_converged(self, res: "BlockResult") -> "tuple[bool, str]":
        """Deterministic convergence gate for golden archiving.

        Golden-worthy == the block completed AND timing is met (or within the
        operator-set uncertainty margin) AND residual DRC is within the same
        'handful' bound the post-route ECO uses. Margins default to 0 (strict:
        timing must actually be met) so the gate NEVER silently masks a real
        violation — the operator must OPT IN to a margin via
        --golden-wns-margin-ps / --golden-hold-margin-ps, and whatever the
        gate decides, the TRUE QoR is always recorded verbatim in the manifest.
        Returns (ok, human_reason).
        """
        wns_m = float(getattr(self.args, "golden_wns_margin_ps", 0.0) or 0.0)
        hold_m = float(getattr(self.args, "golden_hold_margin_ps", 0.0) or 0.0)
        handful = int(getattr(self.args, "drc_handful", 20))
        setup, hold, drc = res.wns_ns, res.hold_ps, res.drc_violations
        # timing: unknown slack is NOT treated as converged (honest — we only
        # archive golden when we can prove closure within the stated margin).
        if setup is None:
            return False, "setup WNS unknown — not archiving golden"
        if setup < -wns_m:
            return False, (f"setup WNS {setup:.1f}ps < -{wns_m:.1f}ps margin "
                           "— not converged")
        if hold is not None and hold < -hold_m:
            return False, (f"hold WS {hold:.1f}ps < -{hold_m:.1f}ps margin "
                           "— not converged")
        if drc is not None and drc > handful:
            return False, (f"{drc} DRC > handful ({handful}) — not golden "
                           "(treat as congestion, not an ECO/archive)")
        margin_txt = (f"(within margins setup≤{wns_m:.0f}ps hold≤{hold_m:.0f}ps)"
                      if (wns_m or hold_m) else "(strict: timing met)")
        return True, (f"converged {margin_txt}: setup={setup:.1f}ps "
                      f"hold={hold if hold is None else round(hold,1)}ps "
                      f"drc={drc if drc is not None else 'n/a'}")

    def _signoff_dir(self, block: str) -> str:
        """Per-run sign-off output dir for a block (SPEF / LVS reports)."""
        return os.path.join(self.run_dir, "signoff", block)

    def _run_signoff(self, block: str) -> Optional[str]:
        """Run the ASAP7 sign-off extraction (OpenRCX -> SPEF) — and LVS once its
        collateral lands — on the block's routed .odb. Runs on EVERY completed
        block by default (staff-engineer discipline); skipped only with
        --no-signoff. Best-effort and fully defensive: a missing tool, missing
        asset, or any failure is logged and NEVER flips the block off DONE.

        RCX is real and available (the staged ASAP7 OpenRCX model produces a
        sign-off SPEF distinct from — and higher fidelity than — the flow's
        built-in 6_final.spef estimate). LVS assets are absent, so an --stage
        that includes LVS gracefully reports 'missing' and continues. Nothing is
        ever fabricated: run_signoff.py refuses (non-zero) rather than invent a
        result, and we treat that refusal as an informational skip."""
        if getattr(self.args, "no_signoff", False):
            return
        if getattr(self.args, "no_physical", False):
            return
        driver = os.path.join(_ROOT, "signoff", "asap7", "run_signoff.py")
        if not os.path.isfile(driver):
            self.ui.debug(f"{block}: signoff driver not found ({driver}) — skipping")
            return
        odb = self._find_routed_odb(block)
        if not odb:
            self.ui.debug(f"{block}: no routed .odb — skipping signoff")
            return
        stage = getattr(self.args, "signoff_stage", "rcx") or "rcx"
        timeout = int(getattr(self.args, "signoff_timeout", 1800) or 1800)
        out_dir = self._signoff_dir(block)
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            self.ui.warn(f"{block}: cannot create signoff out dir: {e}")
            return

        cmd = [sys.executable, driver, "--odb", odb, "--out", out_dir,
               "--stage", stage, "--corner", "nom"]
        # Make the ORFS-built openroad reachable on PATH for the extractor Tcl.
        env = dict(os.environ)
        orbin = self._openroad_bin()
        if os.path.isabs(orbin) and os.path.isfile(orbin):
            env["PATH"] = os.path.dirname(orbin) + os.pathsep + env.get("PATH", "")
        self.ui.step(f"{block}: sign-off (stage={stage}) on "
                     f"{os.path.basename(odb)}")
        log_path = self._block_log(block)
        try:
            with open(log_path, "ab", buffering=0) as logf:
                logf.write(f"\n===== {block} : signoff {' '.join(cmd)} "
                           f"@ {datetime.now()} =====\n".encode())
                proc = subprocess.run(cmd, cwd=os.path.dirname(driver), env=env,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, timeout=timeout)
                logf.write(proc.stdout or b"")
        except subprocess.TimeoutExpired:
            self.ui.warn(f"{block}: signoff timed out after {timeout}s "
                         "(block stays DONE)")
            return
        except (OSError, subprocess.SubprocessError) as e:
            self.ui.warn(f"{block}: signoff could not run: {e} (block stays DONE)")
            return

        out = (proc.stdout or b"").decode("utf-8", "replace")
        spefs = sorted(glob.glob(os.path.join(out_dir, "*.spef"))
                       + glob.glob(os.path.join(out_dir, "*.spef.gz")))
        low = out.lower()
        skip_markers = ("missing", "not ready", "skipped", "not available")
        status = "skip"
        # --- RCX ---------------------------------------------------------
        if stage in ("rcx", "both"):
            rcx_ok = "[rcx] ok" in low and bool(spefs)
            if rcx_ok:
                self.results[block].note += "  +signoff-rcx"
                self.ui.good(f"{block}: sign-off RCX OK — SPEF "
                             f"{os.path.basename(spefs[0])}")
                status = "ok"
                # §126: re-time on the sign-off SPEF (opt-out: --skip signoff-sta)
                if not getattr(self.args, "no_signoff_sta", False):
                    self._spef_sta(block, spefs[0], out_dir, timeout)
        # --- LVS (interconnect / black-box mode; LVS_INTERCONNECT_<verdict>) -
        if stage in ("lvs", "both"):
            rpt = os.path.join(out_dir, "lvs.report")
            if "LVS_INTERCONNECT_CLEAN" in out:
                self.results[block].note += "  +signoff-lvs-ic"
                self.ui.good(f"{block}: interconnect LVS CLEAN (std cells + macros "
                             f"black-boxed; NOT device-level FinFET) -> {rpt}")
                status = "ok" if status in ("ok", "skip") else status
            elif "LVS_INTERCONNECT_MISMATCH" in out:
                self.results[block].note += "  !lvs-ic-mismatch"
                self.ui.warn(f"{block}: interconnect LVS MISMATCH (block stays DONE) "
                             f"-> {rpt}")
                status = "fail"
            elif "LVS_INTERCONNECT_INCONCLUSIVE" in out:
                self.ui.warn(f"{block}: interconnect LVS INCONCLUSIVE (DEF coarser than "
                             f"tech LEF and tech-unit regen failed) -> {rpt}")
                status = "fail" if status != "fail" else status
        if status == "skip":
            if proc.returncode != 0 and not any(m in low for m in skip_markers):
                self.ui.warn(f"{block}: sign-off rc={proc.returncode} "
                             f"(block stays DONE; see {os.path.basename(log_path)})")
                status = "fail"
            else:
                # Asset(s)/tool absent: an honest, expected skip — never fabricated.
                self.ui.info(f"    {block}: sign-off skipped — asset/tool absent "
                             f"(see {os.path.basename(log_path)})")
        elif proc.returncode != 0 and status == "ok":
            # e.g. stage=both: one half passed, the other skipped/failed.
            self.ui.info(f"    {block}: sign-off partial (rc={proc.returncode}; "
                         f"see {os.path.basename(log_path)})")
        return status

    def _spef_sta(self, block: str, spef: str, out_dir: str,
                  timeout: int) -> Optional[dict]:
        """§126: STA back-annotated with the sign-off RCX SPEF (not the flow's
        6_final.spef) via hooks/janus_spef_sta.tcl through ORFS `make run` with
        the block's own corner env (_post_build_make_cmd: every existing stage
        artifact passed as -o, so nothing is rebuilt). Writes spef_sta.rpt /
        spef_sta.json in the signoff dir. Best-effort; never flips DONE.
        >>> PD-UNTESTED <<<"""
        if not self.flow_dir:
            return None
        import adaptive_closure as ac
        script = os.path.join(_ROOT, "scripts", "hooks", "janus_spef_sta.tcl")
        rpt = os.path.join(out_dir, "spef_sta.rpt")
        cmd = self._post_build_make_cmd(block, [
            "run", f"RUN_SCRIPT={script}", "RUN_LOG_NAME_STEM=janus_spef_sta",
            f"JANUS_SPEF={os.path.abspath(spef)}", f"JANUS_SPEF_STA_RPT={rpt}",
        ])
        if getattr(self.args, "dry_run", False):
            self.ui.info(f"{block}: [dry-run] SPEF STA: {' '.join(cmd)}")
            return None
        try:
            with open(self._block_log(block), "ab", buffering=0) as logf:
                logf.write(f"\n===== {block} : spef-sta {' '.join(cmd)} "
                           f"@ {datetime.now()} =====\n".encode())
                subprocess.run(cmd, cwd=self.flow_dir, stdout=logf,
                               stderr=subprocess.STDOUT, timeout=timeout)
            r = ac.parse_spef_sta(open(rpt, errors="replace").read()) if os.path.isfile(rpt) else {}
        except subprocess.TimeoutExpired:
            self.ui.warn(f"{block}: SPEF STA timed out after {timeout}s (ignored)")
            return None
        except Exception as e:
            self.ui.warn(f"{block}: SPEF STA could not run: {e} (ignored)")
            return None
        try:
            with open(os.path.join(out_dir, "spef_sta.json"), "w") as fh:
                json.dump(r, fh, indent=2)
        except OSError:
            pass
        if r.get("status") == "ok":
            ws, hs = r.get("worst_slack_max"), r.get("worst_slack_min")
            self.results[block].note += f"  +spef-sta WS {ws}/{hs}"
            self.ui.good(f"{block}: SPEF-annotated STA: setup WS {ws} ps, hold WS {hs} "
                         f"ps, TNS {r.get('tns_max')} -> {rpt}")
        else:
            self.ui.warn(f"{block}: SPEF STA status={r.get('status', 'no report')} "
                         f"(see {os.path.basename(self._block_log(block))})")
        return r

    def _signoff_only(self) -> int:
        """--signoff-only: run sign-off (--signoff-stage) on each --blocks
        block's EXISTING routed .odb. No stamp check, no build, no manifest /
        checkpoint update, no dashboard (mirrors --triage-slew)."""
        blocks = list(self.args.blocks or [])
        if not blocks:
            self.ui.err("--signoff-only needs --blocks <block> (e.g. --blocks agent_domain)")
            return 2
        self.args.no_signoff = False
        os.makedirs(self.run_dir, exist_ok=True)
        rc_all = 0
        for b in blocks:
            if b not in self.results:
                self.results[b] = BlockResult(block=b)
            if not self._find_routed_odb(b):
                self.ui.err(f"{b}: no routed .odb (block must have finished routing)")
                rc_all = rc_all or 3
                continue
            if self.args.dry_run:
                self.ui.info(f"  (dry-run) would sign off {b} "
                             f"stage={self.args.signoff_stage}")
                continue
            st = self._run_signoff(b)
            if st == "fail":
                rc_all = rc_all or 1
            elif st != "ok":
                rc_all = rc_all or 4
        return rc_all

    def _archive_golden(self, block: str, res: Optional["BlockResult"] = None) -> None:
        """Snapshot a CONVERGED block's logs/reports/final outputs into the
        in-repo golden area and git-commit it (surgically — only the golden
        subtree). Heavy binaries are copied to disk but git-ignored unless
        --golden-include-heavy. Never raises into the flow: any failure is
        logged and the block stays DONE."""
        if getattr(self.args, "no_golden", False):
            return
        if res is None:
            res = self.results.get(block) if hasattr(self.results, "get") else None
        if res is None:
            return
        ok, why = self._golden_converged(res)
        if not ok:
            self.ui.info(f"    {block}: golden archive SKIPPED — {why}")
            return
        self.ui.good(f"{block}: {why} — archiving golden")

        try:
            golden_root = getattr(self.args, "golden_dir", None) or os.path.join(
                _ROOT, "golden")
            dest = os.path.join(golden_root, "asap7", block)
            # deterministic: the golden area holds the LATEST converged snapshot
            # for a block (overwrite), so `golden/asap7/<block>` is always the
            # current sign-off copy. History is preserved by git commits.
            if os.path.isdir(dest):
                shutil.rmtree(dest)
            os.makedirs(dest, exist_ok=True)

            include_heavy = bool(getattr(self.args, "golden_include_heavy", False))
            copied: List[str] = []

            def _copy_into(src_dir: Optional[str], subdir: str,
                           only_text: bool) -> None:
                if not src_dir or not os.path.isdir(src_dir):
                    return
                out = os.path.join(dest, subdir)
                os.makedirs(out, exist_ok=True)
                for name in sorted(os.listdir(src_dir)):
                    sp = os.path.join(src_dir, name)
                    if not os.path.isfile(sp):
                        continue
                    is_heavy = name.lower().endswith(self._GOLDEN_HEAVY_EXT)
                    if only_text and is_heavy and not include_heavy:
                        # copy heavy final outputs to disk only when asked; the
                        # .gitignore keeps them out of git regardless.
                        continue
                    shutil.copy2(sp, os.path.join(out, name))
                    copied.append(os.path.join(subdir, name))

            # (1) ORFS per-substage logs + reports (text; the audit trail).
            _copy_into(self._logs_dir(block), "logs", only_text=False)
            _copy_into(self._reports_dir(block), "reports", only_text=False)
            # (2) the agent's UNIFIED block log (one file, whole run).
            ublog = self._block_log(block)
            if os.path.isfile(ublog):
                shutil.copy2(ublog, os.path.join(dest, "logs",
                                                 f"{block}_unified.log"))
                copied.append(f"logs/{block}_unified.log")
            # (3) final design outputs (6_final.*). Text (.v/.sdc) always;
            #     heavy (.gds/.def/.spef/.odb) to disk, git only with the flag.
            rdir = self._results_dir(block)
            if rdir:
                out = os.path.join(dest, "final")
                os.makedirs(out, exist_ok=True)
                for sp in sorted(glob.glob(os.path.join(rdir, "6_final.*"))):
                    name = os.path.basename(sp)
                    is_heavy = name.lower().endswith(self._GOLDEN_HEAVY_EXT)
                    if is_heavy and not include_heavy:
                        # still snapshot to disk for a complete restorable copy
                        shutil.copy2(sp, os.path.join(out, name))
                        continue
                    shutil.copy2(sp, os.path.join(out, name))
                    copied.append(f"final/{name}")

            # (3b) sign-off outputs (RCX SPEF / LVS reports) for this run. Text
            #      reports are tracked; the SPEF is heavy (in _GOLDEN_HEAVY_EXT)
            #      so it lands on disk but is git-ignored unless include_heavy.
            _copy_into(self._signoff_dir(block), "signoff", only_text=True)

            # (4) machine-readable QoR + human MANIFEST with FULL provenance.
            metrics = res.as_row()
            metrics["converged_reason"] = why
            metrics["timestamp"] = datetime.now().isoformat(timespec="seconds")
            metrics["git_sha"] = self.git.head_sha(short=False) if self.git.available() else None
            metrics["git_branch"] = self.git.current_branch() if self.git.available() else None
            metrics["make_cmd"] = " ".join(self._make_cmd(block))
            with open(os.path.join(dest, "metrics.json"), "w") as f:
                json.dump(metrics, f, indent=2, sort_keys=True)
            self._write_golden_manifest(dest, block, res, why, metrics)

            # (5) a per-golden-root .gitignore so heavy binaries never enter git
            #     unless the operator committed them on purpose (--golden-include
            #     -heavy writes NO ignore, letting them be staged).
            gi = os.path.join(golden_root, ".gitignore")
            if not include_heavy and not os.path.isfile(gi):
                os.makedirs(golden_root, exist_ok=True)
                with open(gi, "w") as f:
                    f.write("# Heavy PD binaries are snapshotted to disk but kept "
                            "OUT of git to protect repo size.\n"
                            "# Re-run with --golden-include-heavy to version them.\n")
                    for ext in ("*.gds", "*.gds.gz", "*.odb", "*.def",
                                "*.spef", "*.spef.gz"):
                        f.write(ext + "\n")

            self.ui.good(f"{block}: golden snapshot → {dest} "
                         f"({len(copied)} tracked file(s))")

            # (6) commit the golden subtree SURGICALLY (GitOps stages only these
            #     paths; .gitignore keeps heavy binaries out). Push only if the
            #     operator already enabled push/sync — mirrors fix-commit policy.
            if not getattr(self.args, "no_git_commit", False) and self.git.available():
                msg = (f"golden({block}): archive converged sign-off snapshot\n\n"
                       f"{why}\nsource: {metrics['git_branch']} "
                       f"{(metrics['git_sha'] or '')[:12]}\n"
                       f"make: {metrics['make_cmd']}")
                commit_paths = [gi, dest] if os.path.isfile(gi) else [dest]
                cok, cmsg = self.git.commit_paths(commit_paths, msg)
                if cok:
                    self.ui.good(f"{block}: golden committed — {cmsg}")
                    pok, pmsg = self.git.push()
                    if pok:
                        self.ui.good(f"{block}: golden {pmsg}")
                    else:
                        self.ui.info(f"    {block}: golden not pushed — {pmsg}")
                else:
                    self.ui.info(f"    {block}: golden commit skipped — {cmsg}")
        except (OSError, ValueError, shutil.Error) as exc:
            self.ui.warn(f"{block}: golden archive failed (block stays DONE): {exc}")

    def _write_golden_manifest(self, dest: str, block: str, res: "BlockResult",
                               why: str, metrics: Dict) -> None:
        """Human-readable sign-off record next to the machine metrics."""
        def _fmt(v, unit=""):
            return "n/a" if v is None else f"{v}{unit}"
        lines = [
            "=" * 70,
            f"JANUS GOLDEN SIGN-OFF SNAPSHOT — {block}",
            "=" * 70,
            f"  timestamp   : {metrics['timestamp']}",
            f"  git branch  : {_fmt(metrics.get('git_branch'))}",
            f"  git HEAD     : {_fmt(metrics.get('git_sha'))}",
            f"  make cmd    : {metrics['make_cmd']}",
            f"  status      : {res.status}   (tries={res.tries})",
            f"  convergence : {why}",
            "-" * 70,
            "  QoR (as archived — TRUE measured values, never masked):",
            f"    setup WNS : {_fmt(res.wns_ns, ' ps')}",
            f"    hold  WS  : {_fmt(res.hold_ps, ' ps')}",
            f"    setup TNS : {_fmt(res.tns_ns, ' ps')}",
            f"    DRC       : {_fmt(res.drc_violations)}",
            f"    area      : {_fmt(res.area)}",
            f"    power     : {_fmt(res.power)}",
            f"    util      : {_fmt(res.utilization)}",
            "=" * 70,
            "Contents: logs/ (ORFS per-substage + <block>_unified.log),",
            "  reports/ (ORFS stage reports), final/ (6_final.* outputs),",
            "  signoff/ (OpenRCX SPEF / LVS reports, when produced),",
            "  metrics.json (machine-readable QoR + provenance).",
            "Heavy binaries (.gds/.def/.spef/.odb) are on disk here but",
            "git-ignored unless the run used --golden-include-heavy.",
        ]
        with open(os.path.join(dest, "MANIFEST.txt"), "w") as f:
            f.write("\n".join(lines) + "\n")

    # ----- map a raw ORFS output line to a human-readable flow stage ---------
    # ORFS emits markers like "Running synth_odb.tcl, stage 1_synth" and step
    # banners; we translate those into a coarse stage label so the heartbeat can
    # tell the user *where* in the flow a long-running block currently is.
    # Order matters: put the MOST specific / earliest-in-flow markers first so a
    # long-running sub-phase (e.g. yosys read_slang) is named precisely instead
    # of falling through to a coarser label. Every marker below was observed in a
    # real ORFS (Yosys 0.64 + slang) security.log — the yosys/slang phase used to
    # show as "starting" for HOURS because none of these were recognized.
    _STAGE_PATTERNS = [
        # ---- 1_synth: yosys front-end (slang read is the long pole) ----------
        # read_slang parses ALL RTL; on big elaborations it can run for HOURS,
        # so name it explicitly and warn the user it is expected to be slow.
        (re.compile(r"Executing\s+SLANG\s+frontend", re.I),
                                                          "1_synth (yosys: SLANG parse — SLOW, can take minutes→hours)"),
        (re.compile(r"\bread_slang\b", re.I),             "1_synth (yosys: SLANG parse — SLOW)"),
        (re.compile(r"yosys[_-]?canonicalize", re.I),     "1_synth (yosys canonicalize / read RTL)"),
        (re.compile(r"Executing\s+SYNTH\s+pass", re.I),   "1_synth (yosys: synth/optimize)"),
        (re.compile(r"\bExecuting\s+ABC\b", re.I),        "1_synth (yosys: ABC tech-map)"),
        (re.compile(r"Executing\s+Liberty\s+frontend", re.I),
                                                          "1_synth (yosys: reading .lib)"),
        (re.compile(r"1_[0-9]+_yosys", re.I),             "1_synth (yosys)"),
        (re.compile(r"stage\s+1_synth", re.I),            "1_synth (synthesis)"),
        # ---- 2_floorplan .. 6_final -----------------------------------------
        (re.compile(r"stage\s+2_floorplan", re.I),        "2_floorplan"),
        (re.compile(r"stage\s+3_place", re.I),            "3_place"),
        (re.compile(r"stage\s+4_cts", re.I),              "4_cts (clock tree)"),
        (re.compile(r"stage\s+5_route", re.I),            "5_route"),
        (re.compile(r"stage\s+6_(final|report)", re.I),   "6_final"),
        (re.compile(r"\bglobal_place\b", re.I),           "3_place (global)"),
        (re.compile(r"\bdetailed_place\b", re.I),         "3_place (detailed)"),
        (re.compile(r"\bclock_tree_synthesis\b", re.I),   "4_cts"),
        (re.compile(r"\bglobal_route\b", re.I),           "5_route (global)"),
        (re.compile(r"\bdetailed_route\b", re.I),         "5_route (detailed)"),
        (re.compile(r"\bdetailed routing\b", re.I),       "5_route (detailed)"),
        (re.compile(r"Running\s+(\S+?)\.tcl", re.I),      None),   # fallback: script name
    ]

    @classmethod
    def _detect_stage(cls, line: str) -> Optional[str]:
        for pat, label in cls._STAGE_PATTERNS:
            m = pat.search(line)
            if m:
                return label if label is not None else f"{m.group(1)}"
        return None

    # Keyword -> canonical major-stage number (1..6). Used to draw the N/6
    # progress bar. ORFS emits many fine-grained sub-step labels (and the
    # `Running <name>.tcl` fallback yields bare script names such as "resize",
    # "repair_timing", "fillcell") that do NOT start with a stage digit — the
    # old code did `int(stg[0])` and silently fell back to 0, which is why an
    # in-flight place/resize showed "0/6". This maps every known sub-step to
    # the major stage it belongs to. Order matters: earliest stage a keyword
    # can appear in wins (monotonic tracking in the reader loop prevents a
    # multi-stage step like "repair_timing" from ever pulling the bar
    # backwards once a later numbered stage has been seen).
    _STAGE_KEYWORDS = [
        (1, ("1_synth", "synth", "yosys", "slang", "abc", "techmap")),
        (2, ("2_floorplan", "floorplan", "tapcell", "pdn", "macro_place",
             "ioplace", "io_place", "pin_place")),
        (3, ("3_place", "global_place", "gpl", "detailed_place", "dpl",
             "resize", "resizer", "repair_design", "repair_timing", "repair",
             "buffer", "place")),
        (4, ("4_cts", "cts", "clock_tree", "clock")),
        (5, ("5_route", "global_route", "grt", "detailed_route", "drt",
             "route", "fillcell", "antenna", "fill")),
        (6, ("6_final", "6_report", "final", "finish", "signoff", "report",
             "ir_drop", "drc", "density", "abstract", "gds")),
    ]

    @classmethod
    def _stage_major_index(cls, label: str) -> int:
        """Map any stage label (numbered OR bare sub-step name) to 1..6.

        Returns 0 only when nothing matches (e.g. 'starting'). This is a FLOOR:
        the reader loop keeps the running max so the bar never regresses.
        """
        if not label:
            return 0
        s = str(label).strip().lower()
        if s[:1].isdigit():
            try:
                n = int(s.split("_", 1)[0])
                if 1 <= n <= 6:
                    return n
            except ValueError:
                pass
        # CTS first: 'clock_tree_synthesis' contains 'synth' and would
        # otherwise mis-map to stage 1.
        if "clock" in s or "cts" in s:
            return 4
        for idx, keys in cls._STAGE_KEYWORDS:
            for k in keys:
                if k in s:
                    return idx
        return 0

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"

    # ----- run one make and capture the log ---------------------------------
    def _ensure_fakeram_obs_fixed(self) -> None:
        """Pre-flight self-enforcing guard against the fakeram M4-OBS gotcha.

        FakeRAM2.0 emits a full-macro blanket OBS covering M1-M4; the M4 sub-
        block sits on top of the macro's own M4 VDD/VSS pins, so pdngen cannot
        drop the M4->M5 vias the ASAP7 CORE_macro_grid_1 strategy needs. Result:
        55x [WARNING PDN-0232] "... does not contain any shapes or vias" then
        [ERROR PDN-0233] "Failed to generate full power grid" on user_core_cva6
        (the only block that instantiates fakeram cache tiles directly).

        The committed LEFs in srams/asap7 are already stripped (M1-M3 OBS only),
        and 04_setup_srams.sh step 5b re-strips after every regeneration. But a
        run that consumed a STALE LEF (partial pull / regen without the strip)
        silently reverts to the M4 OBS and the PDN failure recurs. This guard
        makes the fix impossible to bypass: right before hardening the first
        block it scans every consumed fakeram LEF and, if any still carries an
        M4 OBS sub-block, strips it in place using the same deterministic,
        idempotent scripts/fix_fakeram_obs.py. Loud WARN so the stale artifact
        is visible. Never raises — a guard must not be able to break the flow.
        """
        if self._fakeram_obs_checked:
            return
        self._fakeram_obs_checked = True
        try:
            lefs = sorted(glob.glob(os.path.join(self.sram_out_dir, "*.lef")))
            if not lefs:
                return
            # Import the shared stripper (single source of truth) from scripts/.
            scripts_dir = os.path.join(_ROOT, "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            try:
                from fix_fakeram_obs import fix_lef  # type: ignore
            except Exception as e:  # noqa
                self.ui.warn(f"fakeram OBS guard: cannot load fix_fakeram_obs "
                             f"({e}); skipping pre-flight check")
                return
            repaired = []
            for lef in lefs:
                try:
                    changed, _msg = fix_lef(lef)
                    if changed:
                        repaired.append(os.path.basename(lef))
                except Exception as e:  # noqa
                    self.ui.warn(f"fakeram OBS guard: {os.path.basename(lef)}: "
                                 f"{e}")
            if repaired:
                self.ui.warn(
                    "fakeram OBS guard: STALE M4 pin-layer OBS auto-stripped "
                    "from %d LEF(s) in %s before hardening — %s. This would "
                    "otherwise recur as PDN-0232/PDN-0233 on user_core_cva6. "
                    "Consumed LEFs were out of date; re-run "
                    "04_setup_srams.sh (or pull cleanly) to keep the source "
                    "artifacts in sync." % (
                        len(repaired), self.sram_out_dir, ", ".join(repaired))
                )
        except Exception as e:  # noqa — a guard must never break the flow
            self.ui.warn(f"fakeram OBS guard: skipped ({e})")

    def _ensure_lane_abstract_obs_fixed(self, block: str) -> None:
        """Pre-flight guard against the child-abstract power-pin OBS gotcha.

        ORFS generates every block's abstract with
            write_abstract_lef -bloat_occupied_layers   (generate_abstract.tcl)
        which bloats EVERY occupied layer into a full-macro OBS rectangle. The
        hardened user_core_ara_lane places its VDD/VSS power pins on its top
        layer M6, so the bloated M6 OBS blankets the M6 power pins. When
        user_core_ara then consumes the lane .lef and its macro-PDN strategy
        (ara_grid_strategy.tcl) tries to drop the ElementGrid {M5 M6} via onto
        those pins, the blanket M6 OBS blocks it -> 4x [WARNING PDN-0232] (one
        per lane) then [ERROR PDN-0233] at 2_4_floorplan_pdn (see
        user_core_ara (2).log / 2_4_floorplan_pdn (7).log).

        This is the exact analogue of the fakeram M4-OBS gotcha, and the remedy
        is identical: an abstract must NOT obstruct its own power-pin layer.
        Right before hardening user_core_ara (by which point its hard dependency
        user_core_ara_lane has already produced its abstract), scan the lane
        .lef and strip the OBS on whichever layer carries the VDD/VSS pins, using
        the shared, deterministic, idempotent scripts/fix_abstract_obs.py. Fires
        for user_core_ara (lane child) and, since §101, user_core (Ara + CVA6
        children); a no-op if a child .lef is absent (flat build) or already
        stripped. Never raises — a guard must not break the flow.
        """
        # §101: generalised from user_core_ara-only to every parent that
        # consumes hardened child abstracts. strip_above=True also removes OBS
        # on layers ABOVE the pin layer (PDN-0006: Ara's M7-M9 mesh / CVA6's
        # M7 signals bloat into OBS that fully covers the M6 power pins); the
        # parent's POST_PDN hook restore_macro_route_obs.tcl re-blocks those
        # layers for signal routing after pdngen. The lane keeps the original
        # behaviour (it is capped at M6, nothing above to strip). For user_core
        # the pin layer's own OBS is KEPT (keep_pin_layer) so the parent M6
        # straps stay off the macros; set JANUS_OBS_STRIP_PIN_LAYER=1 to fall
        # back to also stripping it (the lane's on-box-proven behaviour) if
        # this OpenROAD build ever reports PDN-0232 on the macros.
        children = {
            "user_core_ara": (("user_core_ara_lane",), False),
            "user_core": (("user_core_ara", "user_core_cva6"), True),
            # §164: user_domain consumes 2x the hardened user_core abstract
            # (VDD/VSS on M6, M7-M9 signal OBS) with the same PDN/OBS recipe
            # as user_core (user_core_grid_strategy + restore_macro_route_obs).
            "user_domain": (("user_core",), True),
        }.get(block)
        if not children:
            return
        for child in children[0]:
            self._strip_child_abstract_obs(
                block, child, children[1],
                keep_pin_layer=children[1] and os.environ.get(
                    "JANUS_OBS_STRIP_PIN_LAYER", "0") != "1")

    def _strip_child_abstract_obs(self, block: str, child: str,
                                  strip_above: bool,
                                  keep_pin_layer: bool = False) -> None:
        """Run fix_abstract_obs on one child's abstract .lef(s) (see above)."""
        try:
            lefs = []
            for base in self._abstract_base_dirs(child):
                lefs = sorted(glob.glob(os.path.join(base, "*.lef")))
                if lefs:
                    break
            if not lefs:
                return  # flat build (no child macro) or not yet generated
            scripts_dir = os.path.join(_ROOT, "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            try:
                from fix_abstract_obs import fix_lef  # type: ignore
            except Exception as e:  # noqa
                self.ui.warn(f"child-abstract OBS guard: cannot load "
                             f"fix_abstract_obs ({e}); skipping pre-flight check")
                return
            for lef in lefs:
                try:
                    changed, msg = fix_lef(lef, strip_above=strip_above,
                                           keep_pin_layer=keep_pin_layer)
                except Exception as e:  # noqa
                    self.ui.warn(f"child-abstract OBS guard: "
                                 f"{os.path.basename(lef)}: {e}")
                    continue
                if changed:
                    self.ui.warn(
                        "child-abstract OBS guard: power-pin OBS auto-stripped "
                        "from %s before hardening %s — %s. The bloated "
                        "abstract OBS would otherwise block the parent PDN "
                        "(PDN-0232/0233 or PDN-0006)." % (
                            os.path.basename(lef), block, msg))
        except Exception as e:  # noqa — a guard must never break the flow
            self.ui.warn(f"child-abstract OBS guard: skipped ({e})")

    def _render_live_panel(self, block, state, state_lock, t_start, log_path):
        """Build the in-place rich renderable for a serial-TTY live run.

        Called by rich.Live's auto-refresh, so it must be cheap and read the
        shared `state` under its lock. Shows block · status (with a stall hint
        when the tool has gone quiet) · stage N/6 with a text progress bar ·
        time-in-stage · elapsed · log-line count · log path.
        """
        from rich.panel import Panel
        from rich.table import Table
        with state_lock:
            stg = state["stage"]
            n = state["lines"]
            prev = state["prev_lines"]
            stage_at = state["stage_at"]
            idx = state.get("stage_idx", 0)
        el = self._fmt_elapsed(time.time() - t_start)
        # Robust major-stage index: prefer the monotonic value tracked by the
        # reader loop; fall back to a fresh lookup so the bar is still correct
        # even before the first transition has been recorded.
        idx = max(idx, self._stage_major_index(stg))
        total = 6
        filled = max(0, min(total, idx))
        bar = "█" * filled + "░" * (total - filled)
        stage_el = self._fmt_elapsed(time.time() - stage_at)
        stalled = (n == prev) and n > 0
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="right", style="cyan")
        t.add_column()
        t.add_row("block", f"[bold]{block}[/bold]")
        t.add_row("status",
                  "[yellow]running — waiting on tool (no new output)[/yellow]"
                  if stalled else "[green]running[/green]")
        t.add_row("stage", f"{stg}    [{bar}] {filled}/{total}")
        t.add_row("in stage", stage_el)
        t.add_row("elapsed", el)
        t.add_row("log lines", str(n))
        t.add_row("log", log_path)
        return Panel(t, title=f"JANUS · live · {block}",
                     border_style="yellow" if stalled else "cyan",
                     expand=False)

    def _run_make(self, block: str) -> int:
        # Self-enforcing pre-flight: repair any stale fakeram LEF that still
        # blocks the macro power grid, so PDN-0233 cannot silently recur.
        self._ensure_fakeram_obs_fixed()
        # And strip the bloated power-pin-layer OBS from the lane abstract before
        # user_core_ara consumes it (same PDN-0232/0233 class, on M6).
        self._ensure_lane_abstract_obs_fixed(block)
        log_path = self._block_log(block)
        # Stamp-freshness gate: abort a STOCK-by-accident run BEFORE synth burns
        # time/$. Skippable with --skip-stamp-check for deliberate stamp edits.
        if not getattr(self.args, "skip_stamp_check", False):
            stale = self._verify_stamp_fresh(block)
            if stale:
                banner = (
                    f"\n{'!' * 78}\n"
                    f"########## ABORT (pre-flight): {block} — {stale}\n"
                    f"{'!' * 78}\n")
                try:
                    with open(log_path, "ab") as _lf:
                        _lf.write(banner.encode())
                except OSError:
                    pass
                self.ui.err(f"{block}: {stale}")
                return 90  # nonzero -> treated as a build failure, no make run
        cmd = self._make_cmd(block)
        self.ui.debug(f"{block}: cwd={self.flow_dir} cmd={' '.join(cmd)}")
        if self.manifest is not None and not self.args.dry_run:
            try:   # §74: lets a later --resume attribute .odb from a killed run
                self.manifest.mark_inflight(
                    block, config_path=self._config_path(block),
                    sdc_path=os.path.join(self.design_root, block, "constraint.sdc"))
            except Exception as e:
                self.ui.debug(f"{block}: inflight mark skipped — {e}")

        # Shared state updated by the reader loop; read by the heartbeat thread,
        # the optional in-place live console panel, and the web dashboard.
        t_start = time.time()
        state = {"stage": "starting", "lines": 0, "last_msg": "",
                 "stage_at": t_start, "prev_lines": 0, "stage_idx": 0}
        state_lock = threading.Lock()
        hb_stop = threading.Event()

        # Expose this block's live progress to the in-place DashboardUI (if any)
        # so it shows real stage/progress/elapsed even under --parallel.
        self._live_register(block, state, state_lock, t_start, log_path)

        interval = max(5.0, float(getattr(self.args, "heartbeat", 30.0)))

        # In-place live status panel: only for a SERIAL run on a real TTY with
        # rich present and not --verbose (verbose streams raw ORFS output, which
        # would fight the panel). Parallel runs launch several _run_make threads
        # that cannot share one rich.Live, so they fall back to the throttled
        # one-line updates. The web dashboard (if any) is fed in every mode — it
        # is independent of the console.
        # The global in-place DashboardUI, when active, owns the terminal's
        # live region; a per-block rich.Live panel would collide with it, so
        # the serial panel is only used when NO global dashboard is running.
        use_live = bool(
            _RICH and self.ui.console is not None and sys.stdout.isatty()
            and int(getattr(self.args, "parallel", 1)) <= 1
            and not self.args.verbose
            and self._dashboard_ui is None
        )

        def _push_dash(status: str) -> None:
            if self.dash_state is None:
                return
            with state_lock:
                stg, n = state["stage"], state["lines"]
            # Merge live BlockResult metrics (WNS/util/area from last completed
            # stage, or final metrics if DONE/FAILED) so the dashboard shows them.
            row = self.results[block].as_row()
            # Extract ONLY the metric fields (not status/log_path/block/etc which
            # we pass explicitly) to avoid duplicate keyword arguments.
            metrics = {k: row.get(k) for k in (
                'wns_ns', 'tns_ns', 'hold_ps', 'utilization', 'area', 'drc_violations')}
            self._dash_update(
                block, status=status, stage=stg,
                elapsed=round(time.time() - t_start, 1), lines=n,
                log_path=log_path,
                results_dir=self._results_dir(block),
                reports_dir=self._reports_dir(block),
                sram_dir=self.sram_out_dir,
                **metrics,
            )

        def _heartbeat() -> None:
            # Throttled one-liner + dashboard feed. Fires even when ORFS is
            # silent for minutes (e.g. detailed_route) because it runs
            # independently of the blocking stdout reader. Under `use_live` the
            # rich panel owns the terminal, so this only feeds the dashboard
            # (at a slightly faster cadence) and prints nothing.
            wait_for = min(interval, 5.0) if use_live else interval
            while not hb_stop.wait(wait_for):
                if not use_live:
                    with state_lock:
                        stg, n, prev = (state["stage"], state["lines"],
                                        state["prev_lines"])
                        state["prev_lines"] = n
                    el = self._fmt_elapsed(time.time() - t_start)
                    hint = "  [no new output — still working]" if n == prev else ""
                    self.ui.info(
                        f"    … {block}: running — stage={stg}  elapsed={el}  "
                        f"({n} log lines){hint}  log: {os.path.basename(log_path)}"
                    )
                _push_dash("RUNNING")

        hb = threading.Thread(target=_heartbeat, name=f"hb-{block}", daemon=True)
        hb.start()

        # Optional in-place live panel (auto-refreshing; reads shared state).
        _live = None
        if use_live:
            try:
                from rich.live import Live
                _agent = self

                class _LivePanel:
                    def __rich__(self_p):
                        return _agent._render_live_panel(
                            block, state, state_lock, t_start, log_path)

                _live = Live(_LivePanel(), console=self.ui.console,
                             refresh_per_second=4, transient=False)
                _live.start()
            except Exception:  # never let the pretty console break a run
                _live = None

        with open(log_path, "ab", buffering=0) as logf:
            header = f"\n===== {block} : {' '.join(cmd)} @ {datetime.now()} =====\n"
            # Stamp git provenance at the top of every block log so any log
            # shared for triage is self-identifying (branch, HEAD sha+subject,
            # uncommitted tracked edits) — no reliance on branch names or
            # remembering "what was I running".
            if self.git.available():
                header += self.git.provenance() + "\n"
            logf.write(header.encode())
            ms = self._milestone_tracker(block)
            # bufsize=0 (unbuffered) avoids the "line buffering isn't supported
            # in binary mode" RuntimeWarning that bufsize=1 raised on every run.
            proc = subprocess.Popen(
                cmd, cwd=self.flow_dir, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0,
            )
            try:
                for raw in iter(proc.stdout.readline, b""):
                    if self._stop.is_set():
                        proc.terminate()
                        break
                    logf.write(raw)
                    line = raw.decode("utf-8", "replace").rstrip()
                    # Update shared progress state; announce stage transitions
                    # immediately so the user sees synth→floorplan→place→cts→route.
                    stg = self._detect_stage(line)
                    transitioned = False
                    with state_lock:
                        state["lines"] += 1
                        if stg and stg != state["stage"]:
                            state["stage"] = stg
                            state["stage_at"] = time.time()
                            # Track the canonical major-stage number as a
                            # monotonic max so the N/6 bar reflects real
                            # progress and never regresses on a sub-step (e.g.
                            # "resize" / "repair_timing") that lacks a digit
                            # prefix. Fixes the "0/6" display bug.
                            state["stage_idx"] = max(
                                state["stage_idx"],
                                self._stage_major_index(stg))
                            transitioned = True
                            el = self._fmt_elapsed(time.time() - t_start)
                            # Under the in-place live panel we do NOT print a
                            # scrolling step line (the panel shows the current
                            # stage); otherwise announce the transition inline.
                            if not use_live:
                                self.ui.step(f"{block}: → {stg}  (elapsed {el})")
                            # Stamp a MAJOR-STAGE banner INTO the unified block
                            # log itself (not just the console). This is what
                            # makes runs/<ts>/<block>.log a single, navigable
                            # log for the whole run: one greppable separator per
                            # major stage (1_synth, 2_floorplan, 3_place, 4_cts,
                            # 5_route, 6_final) instead of ORFS's dozen tiny
                            # per-substage files (2_1/2_2/2_3/2_4...). ORFS still
                            # writes its own per-substage logs under flow/logs/;
                            # this banner just lets you read/split ONE file by
                            # `grep -n 'MAJOR STAGE'` or awk on the marker.
                            banner = (
                                f"\n{'#' * 78}\n"
                                f"########## MAJOR STAGE: {stg}"
                                f"   (block={block}, elapsed {el})\n"
                                f"{'#' * 78}\n"
                            )
                            logf.write(banner.encode())
                    if transitioned:
                        _push_dash("RUNNING")
                    if ms is not None:
                        self._send_milestones(block, ms.feed(line, state["stage"]))
                    if self.args.verbose:
                        self.ui.debug(f"{block}| {line}")
                proc.stdout.close()
            finally:
                proc.wait()
                if ms is not None:
                    self._send_milestones(block, ms.finish())
                hb_stop.set()
                hb.join(timeout=2.0)
                if _live is not None:
                    try:
                        _live.stop()
                    except Exception:
                        pass
        el = self._fmt_elapsed(time.time() - t_start)
        self._live_deregister(block, time.time() - t_start)
        self.ui.info(f"    {block}: make exited rc={proc.returncode} after {el}")
        return proc.returncode

    # ----- process one block end-to-end (with LLM retry loop) ---------------
    def _process_block(self, block: str) -> BlockResult:
        res = self.results[block]
        cfg = self._config_path(block)
        sdc = os.path.join(self.design_root, block, "constraint.sdc")
        applier = FixApplier(
            block_config_path=cfg, block_sdc_path=sdc,
            rtl_root=self.rtl_root, logger=self.ui.debug,
            repo_config_path=self._repo_config_path(block),
            repo_sdc_path=self._repo_sdc_path(block),
            sram_setup_script=(self.sram_setup_script
                               if os.path.isfile(self.sram_setup_script) else None),
            sram_out_dir=self.sram_out_dir,
            regen_srams=not self.args.no_sram_regen,
        )

        best_wns: Optional[float] = None
        for attempt in range(1, self.args.max_retries + 1):
            if self._stop.is_set():
                res.status = "ABORTED"
                return res
            res.tries = attempt
            self.ui.step(f"{block}: attempt {attempt}/{self.args.max_retries}")
            rc = self._run_make(block)
            report = self.analyzer.analyze_file(self._block_log(block), block, stage="flow")

            # absorb whatever metrics ORFS actually reported (may be None).
            # wns_ns/hold_ps/tns_ns are ASAP7 picoseconds; worst-slack, -ve = viol.
            res.wns_ns = report.wns_ns
            res.hold_ps = report.hold_ps
            res.tns_ns = report.tns_ns

            # Authoritative override: the run log's final STA print can be lost to
            # a cosmetic dump-hook error, leaving only placement estimates. The
            # detail-route .rpt header is the signoff-comparable timing — prefer
            # it whenever present so WNS/TNS/hold never come from an estimate.
            # 6_finish (RCX) is preferred; janus_08 (GRT RC) is the fallback.
            res.timing_source = None
            auth = self._authoritative_timing_from_rpt(block)
            if auth:
                if auth.get("setup_ws") is not None:
                    res.wns_ns = auth["setup_ws"]
                if auth.get("hold_ws") is not None:
                    res.hold_ps = auth["hold_ws"]
                if auth.get("setup_tns") is not None:
                    res.tns_ns = auth["setup_tns"]
                res.timing_source = auth.get("source")
                counts = auth.get("counts") or {}
                res.max_slew_violations = counts.get("max_slew")
                res.max_cap_violations = counts.get("max_cap")
                # Per-path-group breakdown from the SAME report (one parse).
                pg = auth.get("path_groups") or {}
                if pg.get("setup") or pg.get("hold"):
                    res.path_groups = pg
                self.ui.debug(
                    f"{block}: authoritative timing [{res.timing_source}] from "
                    f"{os.path.basename(auth['_source'])} setup_ws={res.wns_ns} "
                    f"tns={res.tns_ns} hold_ws={res.hold_ps} "
                    f"groups={res.path_groups} drv_counts={counts}")
            res.drc_violations = report.drc_violations
            res.area = report.area
            res.power = report.power
            res.utilization = report.utilization
            res.std_cells = report.std_cells
            res.log_path = self._block_log(block)

            if rc == 0 and not report.failed:
                res.status = "DONE"
                res.note = report.headline()
                self.ui.good(f"{block}: PASS — {report.headline()}")
                # Authoritative, read-only re-time on the routed .odb: fills the
                # hold column (no longer 'n/a') and prints the violating-endpoint
                # breakdown by path class (reg2reg / in2reg / reg2mem / ...).
                self._classify_timing(block)
                # Adaptive post-route ECO: close residual timing (hold first)
                # and/or clean a handful of DRCs — discarding any ECO that
                # degrades the result (the canonical .odb is only swapped on a
                # strict improvement). Runs BEFORE abstract so the abstract is
                # emitted from the adopted database.
                self._post_step[block] = "post-route opt"
                self._post_route_opt(block)
                _nolib = self._macro_lib_missing(block)
                if _nolib:
                    self.ui.err(f"{block}: §71 macros loaded WITHOUT liberty in OpenROAD "
                                f"({', '.join(_nolib)}) — macro-pin paths were UNTIMED; "
                                "WNS/hold are incomplete. Do NOT sign off this result.")
                # Emit the block's .lef/.lib abstract so janus_soc_top can
                # consume it as a hard macro (hierarchical bottom-up assembly).
                self._post_step[block] = "abstract"
                self._run_abstract(block)
                # Sign-off parasitic extraction (and, when its collateral lands,
                # LVS) on the routed .odb: runs the real ASAP7 OpenRCX model to
                # emit a signoff SPEF distinct from the flow's built-in estimate.
                # Runs on EVERY completed block by default (staff-engineer
                # discipline); disable with --no-signoff. Never flips the block
                # off DONE (fully wrapped in try/except inside). Runs BEFORE the
                # golden snapshot so its outputs are captured into golden/.
                self._post_step[block] = "signoff"
                self._run_signoff(block)
                self._post_step[block] = "golden"
                # Sign-off snapshot: if this block converged (timing met within
                # the configured margins AND DRC within a handful), copy its
                # logs/reports/final outputs + a QoR manifest into the golden
                # area and commit surgically. Never flips the block off DONE
                # (fully wrapped in try/except inside).
                self._archive_golden(block, res)
                self._post_step.pop(block, None)
                return res

            # --- failed -----------------------------------------------------
            self.ui.warn(f"{block}: make rc={rc}; {report.headline()}")

            # Deterministic triage FIRST: if this is a known environment /
            # infrastructure error (missing .so, OOM, missing tool, etc.) the
            # LLM cannot fix it by editing RTL/config. Skip the LLM entirely,
            # save credits, and give a precise manual instruction.
            triage = classify_failure(report)
            if triage is not None:
                res.status = "NEEDS_MANUAL"
                res.note = f"[{triage.category}] {triage.message}"[:240]
                self.ui.err(f"{block}: environment/infra error "
                            f"[{triage.category}] — NOT sending to LLM (no credits spent).")
                self.ui.err(f"{block}: matched: {triage.matched_line}")
                self.ui.err(f"{block}: fix: {triage.message}")
                return res

            # PRINCIPAL-ENGINEER REASONING (deterministic, ADVISORY ONLY).
            # This does NOT edit anything. It classifies the failure into a
            # remedy LAYER (RTL/SDC/SYNTH_OPT/FLOORPLAN/PLACE/CTS/ROUTE/tool),
            # computes the cross-stage + cross-block blast radius, flags a
            # suspected tool bug, and produces a framed briefing. That briefing
            # is (a) printed so a human gets a real diagnosis even under
            # --no-llm, and (b) injected into the LLM consult so the model
            # reasons about root cause + cost instead of pattern-matching a log.
            # It never auto-applies a speculative change — the ONLY deterministic
            # auto-apply remains the geometry-exact macro-fit reshape below.
            reason_ctx = ""
            reason_tag = ""
            fused = None
            if getattr(self.args, "reason", False):
                # EVIDENCE-FIRST: fuse the single-log signature with the FULL
                # cross-stage report trajectory (Tier-1). The evidence-derived
                # layer overrides the log-only guess when they disagree — this
                # is what stops a route-abort being mislabelled "route" when the
                # timing was already broken pre-placement.
                rdir = self._reports_dir(block)
                fused = reason_with_reports(
                    report.error_lines, report.tail, block, _DEP_GRAPH,
                    report_dir=rdir, synth_stat_path=self._synth_stat_path(block),
                    stage_hint=None)
                rr = fused.base
                reason_ctx = fused.briefing
                rc = fused.root_cause
                if rc is not None:
                    self.ui.step(
                        f"{block}: reasoning → ROOT CAUSE {fused.root_layer} "
                        f"[{rc.confidence:.2f}] (cross-stage evidence); "
                        f"first violation @ {fused.evidence.first_violation_stage}")
                    self.ui.info(f"      {rc.summary}")
                    self.ui.info(f"      remedy    : {rc.fix_hint}")
                    reason_tag = f"[reason:{fused.root_layer} {rc.confidence:.2f}/evidence] "
                    res.note = (reason_tag + (res.note or ""))[:240]
                elif rr.top is not None:
                    t = rr.top
                    ripple = (f"; ripples to {', '.join(t.ripple_blocks)}"
                              if t.ripple_blocks else "")
                    self.ui.step(
                        f"{block}: reasoning → likely {t.layer} "
                        f"[{t.confidence:.2f}] ({t.signature}); "
                        f"re-runs {', '.join(t.downstream)}{ripple}"
                        + ("  [TOOL-BUG suspected]" if rr.tool_bug_suspected else ""))
                    self.ui.info(f"      root cause: {t.root_cause}")
                    self.ui.info(f"      remedy    : {t.remedy_hint}")
                    reason_tag = f"[reason:{t.layer}/{t.signature} {t.confidence:.2f}] "
                    res.note = (reason_tag + (res.note or ""))[:240]
                else:
                    self.ui.step(f"{block}: reasoning → novel failure "
                                 "(no signature matched); reason from first principles")
                if self.args.verbose:
                    self.ui.panel(f"{block}: reasoning briefing", fused.briefing)

            # Deterministic PHYSICAL self-heal (no LLM): an RTL-MP "macro does
            # not fit in the core" (MPL-0006) failure is almost always a
            # pathological FakeRAM aspect ratio. Analyze the SRAM geometry the
            # same way a human would, reshape the offending macro(s) via the
            # tracked setup script, regenerate + commit — all without spending a
            # credit. Runs even under --no-llm (it isn't an LLM path).
            phys_ctx = ""
            if not self.args.no_physical:
                handled, phys_ctx = self._macro_fit_selfheal(block, attempt, applier, report)
                if handled:
                    self._snapshot(block)
                    if attempt == self.args.max_retries:
                        res.status = "NEEDS_MANUAL"
                        res.note = ("SRAM reshape applied on final attempt — "
                                    "re-run with --resume to retry floorplan")
                        self.ui.warn(f"{block}: {res.note}")
                        return res
                    continue   # retry make with the reshaped macros

            if self.args.no_llm or not self.llm.available():
                res.status = "FAILED"
                res.note = reason_tag + "failed; LLM disabled — see log: " + res.log_path
                self.ui.err(f"{block}: stopping (no LLM). Inspect {res.log_path}")
                return res

            if attempt == self.args.max_retries:
                res.status = "FAILED"
                res.note = reason_tag + f"failed after {attempt} attempts — see {res.log_path}"
                self.ui.err(f"{block}: exhausted retries.")
                return res

            # Interactive gate (no-op unless --interactive on a TTY): give the
            # user the choice to spend an LLM credit, skip, or abort.
            choice = self._ask(f"{block} failed on a real RTL/config bug. "
                               "Consult the LLM to diagnose & auto-fix?",
                               "y=yes / s=skip block / a=abort run", "y")
            if choice.startswith("a"):
                self._stop.set()
                res.status = "ABORTED"
                res.note = "user aborted at LLM prompt"
                return res
            if choice.startswith("s"):
                res.status = "NEEDS_MANUAL"
                res.note = "user skipped LLM diagnosis — inspect log: " + res.log_path
                self.ui.warn(f"{block}: skipped by user; see {res.log_path}")
                return res

            # --- ask the LLM for a fix --------------------------------------
            excerpt = self.analyzer.build_llm_excerpt(report)
            self.ui.step(f"{block}: consulting Abacus LLM (attempt {attempt})")
            llm_res = self.llm.request_fix(excerpt, physical=phys_ctx,
                                           reasoning=reason_ctx)
            res.llm_calls += 1
            if not llm_res.ok or not llm_res.fixes:
                res.status = "FAILED"
                res.note = (reason_tag + f"LLM no actionable fix: {llm_res.error or llm_res.diagnosis}")[:200]
                self.ui.err(f"{block}: LLM returned no fix. {res.note}")
                return res

            self.ui.info(f"{block}: LLM diagnosis [{llm_res.confidence}]: {llm_res.diagnosis[:160]}")
            # In interactive mode, show the proposed fixes and let the user
            # approve before we touch any file.
            if self.interactive:
                self.ui.panel(f"{block}: proposed fixes (attempt {attempt})",
                              "\n".join(f"  - [{fx.get('type')}] {fx.get('reason', fx)}"
                                        for fx in llm_res.fixes))
                ok = self._ask("Apply these fixes?", "y=apply / n=skip", "y")
                if not ok.startswith("y"):
                    res.status = "NEEDS_MANUAL"
                    res.note = "user declined the LLM fixes — inspect log: " + res.log_path
                    self.ui.warn(f"{block}: fixes declined by user.")
                    return res

            # EVIDENCE GUARDRAIL (Tier-3 reconciler): before touching any file,
            # veto any proposed fix whose LAYER cannot explain the cross-stage
            # evidence — e.g. a PLACE/ROUTE density tweak when timing is already
            # violated pre-placement. This is the deterministic referee that
            # would have blocked the two reverted `PLACE_DENSITY` commits.
            if fused is not None and fused.evidence is not None:
                kept, vetoed = [], []
                for fx in llm_res.fixes:
                    layer = self._fix_layer(fx)
                    ok_fix, why = fused.fix_allowed(layer)
                    (kept if ok_fix else vetoed).append((fx, layer, why))
                for fx, layer, why in vetoed:
                    self.ui.warn(f"{block}: VETOED {layer} fix — {why}")
                if vetoed and not kept:
                    # every proposed fix contradicts the evidence: do NOT apply a
                    # symptom fix. Surface the real root cause for review instead.
                    rc = fused.root_cause
                    res.status = "NEEDS_MANUAL"
                    res.note = (reason_tag + "all LLM fixes vetoed by evidence "
                                f"guardrail; root cause {fused.root_layer}: "
                                + (rc.summary if rc else ""))[:240]
                    self.ui.err(f"{block}: all proposed fixes contradict the "
                                f"evidence (root cause {fused.root_layer}). "
                                "Not applying a symptom fix.")
                    if rc and rc.fix_hint:
                        self.ui.info(f"      real remedy: {rc.fix_hint}")
                    return res
                llm_res.fixes = [fx for fx, _, _ in kept]

            applied = applier.apply(llm_res.fixes)
            for a in applied:
                tag = "ok" if a.ok else "skip"
                self.ui.debug(f"{block}: fix[{a.type}] {tag}: {a.detail}")
            if not applier.applied_any(applied):
                # nothing we could safely apply — likely a manual fix
                manual = "; ".join(a.detail for a in applied if a.type == "manual")
                res.status = "NEEDS_MANUAL"
                res.note = (manual or "LLM suggested only manual/unsupported fixes")[:160]
                self.ui.err(f"{block}: needs manual intervention: {res.note}")
                return res

            # Commit the applied fix to git so the user just `git pull`s it (and
            # it survives a scripts/03 re-stamp because config/sdc edits were
            # mirrored into the repo source). Best-effort: never blocks the run.
            self._commit_fixes(block, attempt, llm_res, applied)

            # remember whether this attempt was an improvement; if the *next*
            # run is worse, roll the fix back so we never regress.
            if best_wns is None or (report.wns_ns is not None and report.wns_ns > best_wns):
                best_wns = report.wns_ns
            self._snapshot(block)
            # loop continues -> retry make with the applied fix

        res.status = "FAILED"
        return res

    # ----- checkpoint snapshot ----------------------------------------------
    def _snapshot(self, block: str) -> None:
        r = self.results[block]
        self.ckpt.set_block(block, status=r.status, tries=r.tries,
                            wns_ns=r.wns_ns, hold_ps=r.hold_ps, drc=r.drc_violations,
                            note=r.note, log=r.log_path)
        self._manifest_snapshot(block)

    def _manifest_snapshot(self, block: str) -> None:
        """Update the GLOBAL flow-state manifest from what actually landed on
        disk for this block: which stage .odbs exist (+ fingerprints), the
        semantic config/sdc snapshot, top-level QoR, and this run's
        log/run/report dirs. Best-effort — a manifest failure never touches the
        real flow result."""
        if self.manifest is None:
            return
        try:
            r = self.results[block]
            results_dir = self._results_dir(block)
            top_qor = {
                # NOTE: ASAP7 reports time in PICOSECONDS. The legacy *_ns keys
                # actually carry ps (historical misnomer); the *_ps keys below
                # are the correctly-labelled values. Both are emitted so older
                # readers / --resume keep working while new consumers can use
                # the honest names. See log_analyzer.py header.
                "wns_ns": r.wns_ns, "hold_ps": r.hold_ps, "tns_ns": r.tns_ns,
                "wns_ps": r.wns_ns, "tns_ps": r.tns_ns,
                "drc": r.drc_violations, "area": r.area, "power": r.power,
                "util": r.utilization, "status": r.status, "note": r.note,
                "timing_source": r.timing_source,
                "max_slew_violations": r.max_slew_violations,
                "std_cells": r.std_cells,
                "max_cap_violations": r.max_cap_violations,
            }
            self.manifest.update_block_from_scan(
                block,
                results_dir or "",
                config_path=self._config_path(block),
                sdc_path=os.path.join(self.design_root, block, "constraint.sdc"),
                top_qor=top_qor,
                final_done=(r.status == "DONE"),
            )
            reached = self.manifest.last_completed_stage(block)
            self.manifest.record_run(
                block,
                run_dir=self.run_dir,
                log=r.log_path or self._block_log(block),
                reports_dir=self._reports_dir(block),
                result=r.status,
                reached_stage=reached,
            )
        except Exception as e:                       # pragma: no cover
            self.ui.debug(f"{block}: manifest snapshot skipped — {e}")

    def _plan_stage_resume(self, block: str) -> None:
        """On --resume, if the manifest has partial stages for ``block``,
        compute + apply a stage-level resume plan (semantic config/sdc diff):
        keep valid stages (touch their .odb so a re-stamp can't force a redo),
        remove the invalidated ones. The block still runs, but ORFS make now
        skips the reused stages instead of restarting from synthesis.
        Best-effort — never blocks the run."""
        if self.manifest is None:
            return
        try:
            results_dir = self._results_dir(block)
            if not results_dir:
                self.ui.info(f"{block}: resume — no prior results on disk; "
                             "full run from synthesis")
                return
            cfg = self._config_path(block)
            sdc = os.path.join(self.design_root, block, "constraint.sdc")
            plan = self.manifest.resume_plan(block, results_dir,
                                             config_path=cfg, sdc_path=sdc,
                                             force_from=getattr(self.args, "resume_from", None))
            for n in plan.notes:
                self.ui.info(f"{block}: resume — {n}")
            summary = self.manifest.apply_resume_plan(plan, config_path=cfg)
            if plan.full_rebuild:
                # nothing reused — make must redo from synthesis; no -o flags.
                self._resume_old_files.pop(block, None)
                self.ui.info(f"{block}: resume — full rebuild from synthesis "
                             f"({plan.reason})")
            else:
                # Record the reused artifact family so _make_cmd() can pass each
                # as `make -o <file>` (--old-file). This is the mechanism that
                # actually stops ORFS from re-running the reused synth/floorplan
                # chain: those files are declared old, so make neither rebuilds
                # them (despite a forced-newer $(YOSYS_DEPENDENCIES) prereq) nor
                # anything downstream on their account. The mtime-touch above is
                # kept as belt-and-suspenders but is NOT sufficient on its own.
                self._resume_old_files[block] = list(plan.touch_paths)
                self.ui.info(
                    f"{block}: resume — reuse [{', '.join(plan.reuse) or 'none'}], "
                    f"rerun from {plan.rerun_from or 'finish'} "
                    f"(touched {len(summary.get('touched', []))}, "
                    f"removed {len(summary.get('removed', []))}). {plan.reason}")
            for err in summary.get("errors", []):
                self.ui.warn(f"{block}: resume fs action failed — {err}")
        except Exception as e:                       # pragma: no cover
            self.ui.debug(f"{block}: stage-resume planning skipped — {e}")

    # ----- force a clean re-run (--force-blocks / --clean) -------------------
    def _force_targets(self) -> List[str]:
        """Resolve the set of blocks to force-clean from --clean / --force-blocks.

        --clean           -> every block targeted by this run.
        --force-blocks ...-> the named blocks (space- OR comma-separated).
        Restricted to blocks actually in this run's dependency graph so a typo
        can never wipe an unrelated block's results. De-duplicated, order-stable.
        """
        want: List[str] = []
        if getattr(self.args, "clean", False):
            want.extend(self.dep.graph)
        for tok in (getattr(self.args, "force_blocks", None) or []):
            want.extend(x for x in re.split(r"[,\s]+", tok) if x)
        out: List[str] = []
        seen = set()
        for b in want:
            if b in seen:
                continue
            seen.add(b)
            if b in self.dep.graph:
                out.append(b)
            else:
                self.ui.warn(f"--force-blocks: '{b}' is not in this run's blocks "
                             f"({', '.join(self.dep.graph)}); ignoring")
        return out

    def _orfs_state_dirs(self, block: str) -> List[str]:
        """Every on-disk ORFS state dir that belongs to ``block`` across the
        known ORFS layouts (<block>, janus_<block>, janus/<block>) and ALL
        FLOW_VARIANT leaves. We remove the whole per-block dir under each kind
        (results/reports/logs/objects) — the decisive equivalent of
        `make clean_<block>` — so no stale .odb can make ORFS make short-circuit
        and no stale report can be mis-read as a fresh pass.

        §153: EXACT names only. FLOW_VARIANT leaves live INSIDE <block>/
        (results/asap7/<block>/<leaf>), so removing <block> already covers
        them. The old `*{block}*` glob also matched SIBLING blocks —
        `--force-blocks user_core` wiped user_core_cva6/ user_core_ara/
        user_core_ara_lane (their .lib/.lef abstracts), and user_core synth
        then failed with "unknown module 'user_core_cva6'"."""
        if not self.flow_dir:
            return []
        out: List[str] = []
        seen = set()
        for kind in ("results", "reports", "logs", "objects"):
            base = os.path.join(self.flow_dir, kind, "asap7")
            cands = [
                os.path.join(base, block),
                os.path.join(base, f"janus_{block}"),
                os.path.join(base, "janus", block),
            ]
            for d in cands:
                if d not in seen:
                    seen.add(d)
                    out.append(d)
        return out

    def _force_clean_blocks(self) -> None:
        """Honor --force-blocks / --clean: wipe ALL on-disk ORFS state for the
        targeted blocks AND their flow-state manifest entries, BEFORE anything is
        scheduled, so each one genuinely re-runs from 1_synth.

        WHY THIS EXISTS
        ---------------
        Deleting janus_flow_state.json by hand does NOT force a re-run. The state
        that makes a block look "already built" is the on-disk
        results/asap7/<block>/base/*.odb tree — ORFS `make` is mtime-driven and
        skips any stage whose .odb is newer than its prerequisites. Worse, on
        --resume with NO stored config snapshot (exactly what you get right after
        deleting the JSON), resume_plan() falls back to "reuse on-disk stages
        as-is" and TOUCHES every .odb newer than config.mk, so make jumps
        straight to 6_final and the block reports DONE instantly. Removing the
        on-disk results/reports/logs/objects dirs (and the manifest record) is
        the only thing that reliably defeats both paths."""
        targets = self._force_targets()
        if not targets:
            return
        self.ui.step(f"force-clean: wiping ORFS state for "
                     f"{', '.join(targets)} (will re-run from 1_synth)")
        for block in targets:
            removed: List[str] = []
            for d in self._orfs_state_dirs(block):
                try:
                    if os.path.isdir(d):
                        shutil.rmtree(d)
                        removed.append(d)
                except OSError as e:
                    self.ui.warn(f"{block}: force-clean could not remove {d}: {e}")
            # drop the manifest record so --resume can't reuse a stale stage
            # ledger or the old config/sdc snapshot.
            if self.manifest is not None:
                try:
                    if self.manifest.forget_block(block):
                        self.ui.debug(f"{block}: force-clean cleared manifest entry")
                except Exception as e:                # pragma: no cover
                    self.ui.warn(f"{block}: force-clean could not clear manifest "
                                 f"entry — {e}")
            # keep the live dashboard / results map honest
            if block in self.results:
                self.results[block].status = "PENDING"
                self.results[block].note = "force-clean (rebuild from synthesis)"
            if removed:
                self.ui.good(f"{block}: force-clean removed {len(removed)} ORFS "
                             f"state dir(s)")
                for d in removed:
                    self.ui.debug(f"   rm -rf {d}")
            else:
                self.ui.info(f"{block}: force-clean — no on-disk ORFS state found "
                             "(already clean)")

    # ----- git commit of an applied LLM fix ----------------------------------
    def _commit_fixes(self, block: str, attempt: int, llm_res, applied) -> None:
        """Commit the repo-source files an LLM fix touched (config/sdc mirrors
        + RTL edits). No-op when commits are disabled or git is unavailable —
        the fix is always still on disk. Optionally pushes if --git-push."""
        if self.args.no_git_commit:
            return
        paths = sorted({a.repo_file for a in applied if a.ok and a.repo_file})
        if not paths:
            self.ui.debug(f"{block}: no repo-source files to commit "
                          "(fix applied only to stamped ORFS copy)")
            return
        # Concise, honest commit message — makes clear this is an automated,
        # not-yet-revalidated fix the user should review on `git pull`.
        types = ",".join(sorted({a.type for a in applied if a.ok}))
        diag = (llm_res.diagnosis or "").strip().replace("\n", " ")[:100]
        msg = (f"janus-agent: auto-fix {block} (attempt {attempt}, {types}) "
               f"[{llm_res.confidence}]\n\n"
               f"Diagnosis: {diag}\n"
               f"Applied by janus_agent.py on ORFS failure. NOT yet revalidated "
               f"— review before trusting.")
        self._publish(block, paths, msg)

    # ----- commit + (optional) push shared by all fix paths ------------------
    def _publish(self, block: str, paths: List[str], msg: str) -> None:
        """Commit `paths` with `msg`, then push if pushing is enabled. Honors
        --git-sync-branches (publishes HEAD to every mirror branch). Best-effort:
        never blocks the run — the fix is always on disk regardless."""
        ok, info = self.git.commit_paths(paths, msg)
        if not ok:
            self.ui.warn(f"{block}: git commit skipped — {info} "
                         "(fix is on disk; commit manually if desired).")
            return
        self.ui.good(f"{block}: {info} — files: "
                     + ", ".join(os.path.relpath(p, self.rtl_root) for p in paths))
        # push() is a no-op (returns disabled) unless --git-push or sync branches
        # were given; when sync branches are set it publishes HEAD to each.
        pok, pinfo = self.git.push()
        if "disabled" not in pinfo:
            (self.ui.good if pok else self.ui.warn)(f"{block}: {pinfo}")

    # ----- deterministic macro-fit (MPL-0006) self-heal ----------------------
    def _render_proposals(self, mfit, proposals) -> str:
        """Human/LLM-readable summary of the macro-fit analysis + reshapes."""
        lines = ["[ERROR MPL-0006] macro does not fit in the core.",
                 f"  failing: {mfit.instance or mfit.master_hint or mfit.matched_line}"]
        for p in proposals:
            oa = MacroGeom.aspect(*p.old_dims)
            na = MacroGeom.aspect(*p.new_dims)
            lines.append(
                f"  reshape {p.macro}: banks {p.old_banks}->{p.new_banks} "
                f"(col_mux {p.col_mux}): {p.old_dims[0]}x{p.old_dims[1]}um "
                f"({oa:.1f}:1) -> {p.new_dims[0]}x{p.new_dims[1]}um ({na:.1f}:1) "
                f"— same bitcell count & pins.")
        return "\n".join(lines)

    def _macro_fit_selfheal(self, block: str, attempt: int, applier, report):
        """Detect an MPL-0006 macro-fit failure and, if so, reshape the
        offending FakeRAM macro(s) deterministically (no LLM), regenerate, and
        commit. Returns (handled, phys_ctx):
          handled=True  -> a reshape was applied+committed; caller should retry.
          handled=False -> not a macro-fit issue (or nothing actionable); the
                           returned phys_ctx (possibly "") is fed to the LLM.
        """
        mfit = detect_macro_fit_failure(report.error_lines, report.tail)
        if mfit is None:
            return False, ""
        if not applier.sram_setup_script:
            # detected, but we don't have the setup script to act on — hand the
            # LLM a precise hint instead of guessing.
            return False, (f"[ERROR MPL-0006] macro-fit at "
                           f"{mfit.instance or mfit.matched_line}; SRAM setup "
                           f"script not found so no auto-reshape was computed.")
        proposals = analyze_and_recommend(
            applier.sram_setup_script, self.sram_out_dir, issue=mfit)
        if not proposals:
            return False, (f"[ERROR MPL-0006] macro-fit at "
                           f"{mfit.instance or mfit.matched_line}; current SRAM "
                           f"aspect ratios already look square — inspect the log.")
        phys = self._render_proposals(mfit, proposals)
        self.ui.step(f"{block}: MPL-0006 macro-fit — deterministic SRAM reshape "
                     f"of {len(proposals)} macro(s), NO LLM credit spent")
        self.ui.info(phys)

        if self.interactive:
            if not self._ask("Apply these SRAM reshapes?",
                             "y=apply / n=send to LLM instead", "y").startswith("y"):
                return False, phys

        fixes = [p.to_fix(applier.sram_setup_script) for p in proposals]
        applied = applier.apply(fixes)
        for a in applied:
            self.ui.debug(f"{block}: fix[{a.type}] {'ok' if a.ok else 'skip'}: {a.detail}")
        if not applier.applied_any(applied):
            # A reshape was refused or its regen failed (edit already rolled
            # back). Do NOT commit — surface the exact reason so the human/LLM
            # can act instead of silently retrying an unbuildable macro.
            why = "; ".join(a.detail for a in applied if not a.ok) or "no fix applied"
            self.ui.err(f"{block}: SRAM reshape NOT applied — {why}")
            return False, (phys + f"\n[MACRO-FIT SELF-HEAL ABORTED] {why}\n"
                           "The reshape did not produce buildable macros; a "
                           "different fold (banks in {1,2,4} x column_mux 1/2/4/8) "
                           "or a larger core may be required.")

        paths = sorted({a.repo_file for a in applied if a.ok and a.repo_file})
        reshaped = ", ".join(f"{p.macro}(banks {p.old_banks}->{p.new_banks})"
                             for p in proposals)
        msg = (f"janus-agent: fix {block} MPL-0006 macro-fit via SRAM reshape "
               f"(attempt {attempt}) [deterministic]\n\n"
               f"Reshaped: {reshaped}. FakeRAM banks/column_mux folded so deep "
               f"macros are ~square (fit the core); bitcell count and pin "
               f"interface unchanged. Regenerated via scripts/04_setup_srams.sh. "
               f"Auto-derived by janus_agent.py — review before trusting.")
        self._publish(block, paths, msg)
        return True, phys

    # ----- planning / estimate ----------------------------------------------
    def _print_plan(self) -> None:
        order = []
        dep = DependencyManager()
        if self.args.blocks:
            dep = dep.restrict_to(self.args.blocks)
        # topological-ish display via repeated ready()
        seen = set()
        while not dep.all_settled():
            ready = [b for b in dep.ready() if b not in seen]
            if not ready:
                break
            order.append(ready)
            for b in ready:
                seen.add(b)
                dep.mark(b, Status.DONE)
        body = []
        wave = 1
        for group in order:
            body.append(f"  wave {wave}: {', '.join(group)}")
            wave += 1
        deps = "\n".join(f"    {k} <- {', '.join(sorted(v)) or '(leaf)'}"
                        for k, v in DEFAULT_GRAPH.items()
                        if (not self.args.blocks or k in self.args.blocks))
        self.ui.panel(
            "Execution plan (waves run in order; blocks in a wave can parallelize)",
            "\n".join(body) + "\n\n  dependencies:\n" + deps)

    def _print_estimate(self) -> None:
        n = len(self.dep.graph)
        self.ui.panel(
            "Cost / time estimate (ROUGH — not a guarantee)",
            (f"  blocks to run        : {n}\n"
             f"  LLM calls            : 0 unless a block FAILS (then up to "
             f"{self.args.max_retries - 1} per failing block)\n"
             f"  approx tokens / call : ~2-4k in + ~0.5-1k out (small log excerpt)\n"
             f"  local compute        : synthesis+PnR per block can take "
             f"minutes to hours\n"
             f"                         depending on block size and your CPU.\n\n"
             "  NOTE: the agent does NOT call the LLM on success. Credits are\n"
             "  only spent diagnosing real failures. All PPA targets in the\n"
             "  configs are DESIGN-INTENT, not measured results."))

    # ----- read-only slew triage on an already-finished block ----------------
    def _triage_slew_cmd(self, block: str, script: str) -> List[str]:
        """The block's EXACT build make line (same DESIGN_CONFIG, FLOW_VARIANT,
        CORNER/CORNERS lib sets, NUM_CORES, --extra-make) + `run RUN_SCRIPT=`.
        Reusing _make_cmd guarantees the triage loads the same liberty corners
        the 6_finish STA used, so its slew numbers are comparable."""
        return self._post_build_make_cmd(block, ["run", f"RUN_SCRIPT={script}"])

    def _triage_slew(self, blocks: Optional[List[str]] = None,
                     post_flow: bool = False) -> int:
        """--triage-slew: run asic_flow/scripts/slew_triage.tcl on each block's
        existing 6_final.odb. READ-ONLY: no stamp check, no re-stamp, no build,
        no manifest/checkpoint update, no dashboard. Safe while another block
        (e.g. user_core_ara) is running — it uses one OpenROAD process.

        post_flow=True (--post-triage-slew): called after the build loop for
        the blocks that finished DONE in this run; every JANUS_SLEW_* line
        (incl. per-net rows) is also appended to <run-dir>/<block>.log so the
        unattended run carries its own triage."""
        script = os.path.join(_ROOT, "scripts", "slew_triage.tcl")
        blocks = list(blocks if blocks is not None else (self.args.blocks or []))
        if not blocks:
            self.ui.err("--triage-slew needs --blocks <block> (e.g. --blocks agent_domain)")
            return 2
        if not os.path.isfile(script):
            self.ui.err(f"slew triage script not found: {script}")
            return 2
        if not self.flow_dir or not os.path.isdir(self.flow_dir):
            self.ui.err(f"ORFS flow dir not found: {self.flow_dir or '(unset — pass --orfs)'}")
            return 2
        os.makedirs(self.run_dir, exist_ok=True)
        rc_all = 0
        for b in blocks:
            res = self._results_dir(b) if hasattr(self, "_results_dir") else None
            odb = os.path.join(res, "6_final.odb") if res else None
            if not odb or not os.path.exists(odb):
                self.ui.err(f"{b}: no 6_final.odb found (looked in {res or 'n/a'}) — "
                            "the block must have finished 6_final first; skipping.")
                rc_all = rc_all or 3
                continue
            if not os.path.exists(os.path.join(res, "6_final.spef")):
                self.ui.warn(f"{b}: 6_final.spef missing — triage will fall back to "
                             "GRT-estimated parasitics (numbers won't match 6_finish).")
            cmd = self._triage_slew_cmd(b, script)
            if post_flow:
                self._post_step[b] = "slew triage running"
            log_path = os.path.join(self.run_dir, f"slew_triage_{b}.log")
            self.ui.step(f"{b}: slew triage (read-only) on {odb}")
            self.ui.info(f"  cd {self.flow_dir} && {' '.join(cmd)}")
            if self.args.dry_run:
                continue
            with open(log_path, "wb", buffering=0) as logf:
                proc = subprocess.run(cmd, cwd=self.flow_dir, stdout=logf,
                                      stderr=subprocess.STDOUT)
            janus_lines = []
            try:
                with open(log_path, "r", errors="replace") as fh:
                    janus_lines = [ln.rstrip() for ln in fh if ln.startswith("JANUS_SLEW_")]
            except OSError:
                pass
            for ln in [l for l in janus_lines
                       if not l.startswith(("JANUS_SLEW_NET ", "JANUS_SLEW_CONST"))]:
                self.ui.info(f"  {ln}")
            if post_flow:
                try:
                    with open(self._block_log(b), "a") as bl:
                        bl.write(f"\n===== {b} : POST-FLOW SLEW TRIAGE (rc={proc.returncode}, "
                                 f"full log {log_path}) @ {datetime.now()} =====\n")
                        bl.write("\n".join(janus_lines) + "\n")
                except OSError as e:
                    self.ui.warn(f"{b}: could not append triage to block log ({e})")
            n_net = sum(1 for l in janus_lines if l.startswith("JANUS_SLEW_NET"))
            if post_flow:
                self._post_step[b] = ("triage done" if proc.returncode == 0
                                      else f"triage rc={proc.returncode}")
            msg = f"{b}: triage rc={proc.returncode}, {n_net} net rows -> {log_path}"
            (self.ui.good if proc.returncode == 0 else self.ui.warn)(msg)
            if proc.returncode != 0:
                rc_all = rc_all or proc.returncode
        return rc_all

    def run(self) -> int:
        if getattr(self.args, "triage_slew", False):
            return self._triage_slew()
        if getattr(self.args, "signoff_only", False):
            return self._signoff_only()
        self._preflight()
        if self.dashboard is not None:
            try:
                url = self.dashboard.start()
                self.ui.good(f"◈ dashboard live at {url}  "
                             f"(open in a browser; per-block logs, reports, "
                             f"results & floorplan snapshot are all clickable)")
            except Exception as e:
                self.ui.warn(f"dashboard could not start ({e}); continuing headless")
                self.dashboard = None
        if self.args.estimate:
            self._print_plan()
            self._print_estimate()
            return 0
        self._print_plan()
        if self.args.dry_run:
            self.ui.step("DRY RUN — commands that would execute:")
            for b in self.dep.graph:
                self.ui.info(f"  ({b}) cd {self.flow_dir} && " + " ".join(self._make_cmd(b)))
            return 0

        # §163: refresh stale stamps from the repo before any resume decision.
        self._auto_restamp_all()

        # honor --force-blocks / --clean FIRST (before resume or scheduling):
        # wipe the on-disk ORFS state + manifest entry for the forced blocks so
        # they genuinely rebuild from 1_synth. This must precede --resume, which
        # would otherwise reuse/touch the very .odb we are about to remove.
        self._force_clean_blocks()

        # honor --resume: consult the GLOBAL manifest (survives a fresh
        # --run-dir), fall back to the per-run checkpoint. A block that is fully
        # DONE is skipped; a block with partial stages is STAGE-resumed — we
        # keep the stages a semantic config/sdc diff says are still valid
        # (touching their .odb so a scripts/03 re-stamp can't force a redo) and
        # remove the invalidated ones, so ORFS make picks up mid-flow instead of
        # restarting from synthesis.
        if self.args.resume:
            for b in list(self.dep.graph):
                done = self.ckpt.is_done(b)
                if not done and self.manifest is not None:
                    try:
                        done = self.manifest.is_done(b)
                    except Exception:
                        done = self.ckpt.is_done(b)
                why_skip = ""
                if done and self.manifest is not None:
                    # §73: a DONE block whose stamped config/sdc changed since its
                    # last run (e.g. HOLD_SLACK_MARGIN) must be STAGE-resumed, not
                    # skipped — otherwise --resume silently ignores the edit.
                    try:
                        res_dir = self._results_dir(b)
                        if not res_dir:
                            # §143: used to be silent -> "already DONE" with no hint
                            why_skip = ("no ORFS results dir with .odb found, so the "
                                        "config-change check could NOT run")
                        if res_dir:
                            pv = self.manifest.resume_plan(
                                b, res_dir, config_path=self._config_path(b),
                                sdc_path=os.path.join(self.design_root, b, "constraint.sdc"),
                                force_from=getattr(self.args, "resume_from", None))
                            if pv.rerun_from and (pv.config_changed or pv.remove_paths
                                                  or getattr(self.args, "resume_from", None)):
                                self.ui.info(f"{b}: was DONE but needs a rerun from "
                                             f"{pv.rerun_from} (changed: "
                                             f"{', '.join(pv.changed_keys) or 'n/a'})")
                                done = False
                            elif not why_skip:
                                why_skip = (f"stamped {self._config_path(b)} matches the "
                                            "last DONE run (no config/sdc change); "
                                            "re-stamp with scripts/03 if you edited "
                                            "configs/*.mk")
                    except Exception as e:
                        why_skip = f"config-change check FAILED: {e}"
                if done:
                    self.dep.mark(b, Status.DONE)
                    self.results[b].status = "DONE"
                    self.results[b].note = "resumed (already DONE)"
                    self._load_resumed_qor(b)
                    self.ui.info(f"{b}: skipped (resume — already DONE)"
                                 + (f" — {why_skip}" if why_skip else ""))
                    continue
                self._plan_stage_resume(b)

        start = time.time()
        self._run_start = start
        # Live in-place dashboard (htop-style): per-block status + CPU/mem row.
        # No-op when piped/redirected or --progress {plain,quiet}; see
        # _maybe_start_dashboard. Stopped in finally so the final summary table
        # (printed by _finalize) renders cleanly below it.
        self._maybe_start_dashboard()
        try:
            self._main_loop()
            if getattr(self.args, "post_triage_slew", False) and not self.args.dry_run:
                done_blocks = [b for b in self.dep.graph
                               if self.results[b].status == "DONE"
                               and "already DONE" not in (self.results[b].note or "")]
                if done_blocks:
                    self.ui.step(f"post-flow slew triage: {', '.join(done_blocks)}")
                    try:
                        self._triage_slew(done_blocks, post_flow=True)
                    except Exception as e:      # triage must never fail the run
                        self.ui.warn(f"post-flow slew triage failed: {e}")
        except KeyboardInterrupt:
            self._stop.set()
            self.ui.err("\nInterrupted — saving checkpoint and writing report...")
        finally:
            self._stop_dashboard()
            elapsed = time.time() - start
            self._finalize(elapsed)
        # exit code: 0 if every targeted block is DONE
        failed = [b for b, r in self.results.items()
                  if r.status not in ("DONE",) and b in self.dep.graph]
        return 0 if not failed else 1

    def _refresh_schedule_ui(self) -> None:
        """Repaint the live dashboard, or print the scrolling status table when
        no live dashboard owns the terminal (otherwise it scrolls under the
        pinned dashboard)."""
        if self._dashboard_ui is not None:
            self._dashboard_ui.refresh_now()
        else:
            self.ui.table([self.results[b].as_row() for b in self.dep.graph])

    def _main_loop(self) -> None:
        """Rolling (slot-filling) scheduler.

        Previously this was a WAVE BARRIER: it computed the whole set of ready
        blocks, launched them, and blocked until EVERY block in that wave had
        finished before recomputing readiness. A freed worker slot therefore sat
        idle until the slowest block in the wave drained — e.g. while `security`
        alone kept routing, noc's released cores (and any spare --parallel slot)
        did nothing, and blocks that security would unblock could not start.

        The rolling scheduler keeps ONE persistent pool. Each time ANY block
        finishes it frees a slot that is IMMEDIATELY refilled with whatever just
        became ready (its deps now satisfied). Only blocks that are actually
        in-flight are marked RUNNING, so the dashboard's running-count stays
        honest. Dependency semantics are unchanged — a block still starts only
        once all of its deps are DONE, and a failed dep still propagates BLOCKED.
        """
        parallel = max(1, self.args.parallel)

        # Sequential fast-path: one block at a time in dependency order. Keeps
        # the simplest possible control flow (and stack traces) for --parallel 1.
        if parallel == 1:
            while not self.dep.all_settled() and not self._stop.is_set():
                ready = self.dep.ready()
                if not ready:
                    break
                b = ready[0]
                self.dep.mark(b, Status.RUNNING)
                self._process_and_mark(b)
                self._refresh_schedule_ui()
            return

        # Parallel rolling scheduler.
        with ThreadPoolExecutor(max_workers=parallel) as ex:
            futs: Dict = {}            # Future -> block name (in-flight only)
            while not self._stop.is_set():
                # Fill every free slot with blocks that are ready RIGHT NOW.
                # ready()/mark() touch self.dep.status, which workers also mutate
                # on completion, so take the same lock they use.
                free = parallel - len(futs)
                launch: List[str] = []
                if free > 0:
                    with self._results_lock:
                        launch = self.dep.ready()[:free]
                        for b in launch:
                            self.dep.mark(b, Status.RUNNING)
                for b in launch:
                    futs[ex.submit(self._process_and_mark, b)] = b
                if launch:
                    self._refresh_schedule_ui()

                if not futs:
                    break  # nothing running and nothing ready -> settled/blocked

                # Wake as soon as the NEXT block finishes (not the whole wave),
                # free its slot, and loop to refill it immediately.
                # §134: ALSO re-poll every few seconds. _process_and_mark marks a
                # block DONE (unblocking its dependents) and only THEN runs the
                # notifier + --adaptive auto pass in the same worker; that pass
                # can take hours, so waiting purely on future completion left
                # free slots idle while ampo/agent_domain/cva6 were already
                # ready (seen: security DONE at 2:12, nothing launched by 3:23).
                done, _ = wait(set(futs), timeout=5.0, return_when=FIRST_COMPLETED)
                for f in done:
                    futs.pop(f, None)
                if done:  # a timeout tick must not reprint the plain table
                    self._refresh_schedule_ui()

    def _process_and_mark(self, block: str) -> None:
        try:
            res = self._process_block(block)
        except Exception as e:  # never let one block kill the run
            res = self.results[block]
            res.status = "FAILED"
            res.note = f"agent exception: {e}"[:160]
            self.ui.err(f"{block}: agent exception: {e}")
        # §145: adaptive closure runs BEFORE the block is marked DONE: under
        # --adaptive auto the extra pass may be PROMOTED to become the block's
        # result, so dependents must not start on (and the email/Drive bundle
        # must not report) the pre-promotion base. advise is instant.
        adaptive = self._adaptive_advise(block, res)
        if adaptive:
            self._adaptive_auto(block, res, adaptive)
        status = Status.DONE if res.status == "DONE" else Status.FAILED
        # Push the settled QoR to the dashboard (final status + measured slacks,
        # DRC, area, util, tries) and the artifact dirs so its detail view / the
        # floorplan snapshot resolve. Best-effort; never blocks the run.
        self._dash_update(
            block, status=res.status, tries=res.tries,
            wns_ns=res.wns_ns, hold_ps=res.hold_ps, tns_ns=res.tns_ns,
            drc_violations=res.drc_violations, area=res.area,
            power=res.power, utilization=res.utilization,
            note=res.note, log_path=res.log_path or self._block_log(block),
            results_dir=self._results_dir(block),
            reports_dir=self._reports_dir(block),
            sram_dir=self.sram_out_dir,
        )
        with self._results_lock:
            self.dep.mark(block, status)
            self._snapshot(block)
            # reflect dependency-driven BLOCKED state in our result table
            for b, st in self.dep.summary().items():
                if st == Status.BLOCKED.value and self.results[b].status == "PENDING":
                    self.results[b].status = "BLOCKED"
                    self.results[b].note = "skipped — a dependency failed"

        # Fire the completion/failure notification OUTSIDE the results lock so
        # the (possibly slow) email/Drive I/O never serialises other blocks.
        # Fully best-effort inside notify(); a failure here cannot affect the run.
        self._notify_settled(block, res)
        if adaptive:
            # notify() rewrites qor.json; re-attach the plan to it.
            self._adaptive_write(block, adaptive)

    def _notify_settled(self, block: str, res: "BlockResult") -> None:
        """Email (and optionally Drive-upload) the settled block's logs+reports.

        No-op unless a notification channel was enabled on the CLI/env. Derives
        the failing stage (best-effort) and a short QoR summary, then hands off
        to NotificationManager.notify(), which swallows all of its own errors.
        """
        try:
            if not getattr(self, "notifier", None) or not self.notifier.enabled:
                return
            # Best-effort failing stage: the last stage the manifest recorded as
            # completed (the block died at/after it). DONE runs pass no stage.
            stage = ""
            if res.status != "DONE" and getattr(self, "manifest", None):
                try:
                    stage = self.manifest.last_completed_stage(block) or ""
                except Exception:
                    stage = ""
            # §120: hand the notifier THIS block's dirs only. run_dir is
            # display-only now — the old code passed the shared agent run dir
            # and the notifier recursively globbed it, so under --parallel
            # every block's agent log + signoff/* landed in each tarball.
            def _leafdir(kind: str, found: Optional[str]) -> Optional[str]:
                if found:
                    return found
                if not self.flow_dir:
                    return None
                for nm in (block, f"janus_{block}"):
                    d = os.path.join(self.flow_dir, kind, "asap7", nm, self._leaf())
                    if os.path.isdir(d):
                        return d
                return None
            reports_dir = _leafdir("reports", self._reports_dir(block))
            signoff = os.path.join(self.run_dir, "signoff", block)
            if res.status != "DONE":
                self._debug_rerun_on_fail(block)
            # §121: the email QoR (path groups, DRV counts, DRC from
            # 5_route_drc.rpt, skew, clock/fmax, triage) is rebuilt from the
            # reports by utils/report_bundle.py with the dashboard's parsers;
            # qor.json lands in the reports dir and inside the bundle.
            qor = self.notifier.notify(
                block=block,
                status=res.status,
                stage=stage,
                run_dir=self.run_dir,
                results_dir=_leafdir("results", self._results_dir(block)),
                reports_dir=reports_dir,
                logs_dir=_leafdir("logs", self._logs_dir(block)),
                log_path=res.log_path or self._block_log(block),
                signoff_dir=signoff if os.path.isdir(signoff) else None,
                tries=res.tries or None,
                note=res.note or "",
                qor_out_dir=reports_dir,
                bundle_dir=os.path.join(self.run_dir, "bundles", block),
                run_table=self._run_table(),
                drive_subfolder=self._drive_run_stamp(),
            )
            out = getattr(self.notifier, "last_outcome", "") or "no outcome recorded"
            (self.ui.good if out.startswith("email SENT") and "[" not in out
             else self.ui.warn)(f"{block}: {out}")
            if qor and qor.get("triage"):
                tr = qor["triage"]
                self.ui.warn(f"{block}: triage {tr.get('category')}: "
                             f"{(tr.get('first_error') or '')[:160]}")
        except Exception as e:  # never let notification affect the run
            self.ui.warn(f"{block}: notification hook error (ignored): {e}")
            self._notify_log(f"{block}: notification hook error: {e}")

    def _milestone_tracker(self, block: str):
        """§157: per-make-run milestone parser, or None when milestone emails
        are off (no SMTP config, or JANUS_NOTIFY_MILESTONES excludes block)."""
        try:
            if not (self.notifier.email_configured and milestones_enabled(block)):
                return None
            return MilestoneTracker(
                block, expect_io_shadow=config_uses_io_shadow(self._config_path(block)))
        except Exception:
            return None

    def _send_milestones(self, block: str, events) -> None:
        """§157: send each milestone as a short summary-only email on a daemon
        thread so SMTP latency never stalls the ORFS log reader. Never raises."""
        for key, subject, body in events or ():
            text = (f"{body}\n\nblock: {block}\nrun: {self.run_dir}\n"
                    f"log: {self._block_log(block)}\n"
                    f"time: {datetime.now().isoformat(timespec='seconds')}")
            self._notify_log(f"milestone {block}/{key}: {subject}")

            # Own shallow copy: keeps the main notifier's last_outcome (used by
            # the DONE/FAILED email path) free of milestone-thread races.
            if getattr(self, "_ms_notifier", None) is None:
                import copy
                self._ms_notifier = copy.copy(self.notifier)
            mn = self._ms_notifier

            def _go(s=subject, t=text):
                try:
                    mn.send_email(s, t, [])
                except Exception:
                    pass
            threading.Thread(target=_go, name=f"ms-{block}-{key}", daemon=True).start()

    def _notify_log(self, msg: str) -> None:
        """§136: every notification line -> <run_dir>/notify.log (+ debug UI).
        Never raises."""
        self.ui.debug(msg)
        try:
            with open(os.path.join(self.run_dir, "notify.log"), "a") as fh:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
        except Exception:
            pass

    def _debug_rerun_on_fail(self, block: str) -> None:
        """§123: opt-in (JANUS_DEBUG_ON_FAIL=1) debug dump on the last good ODB.

        POST_* hooks never fire for a step that errored, so a failed block has
        no janus_*_debug.rpt for the failing region. This reruns
        hooks/janus_debug_rerun.tcl through ORFS's own `make ... run` target
        (same DESIGN_CONFIG / FLOW_VARIANT / CORNER as the block) under a hard
        subprocess timeout (JANUS_DEBUG_TIMEOUT_S, default 900 s). Every error
        and the timeout are swallowed: it can never change the run outcome.
        """
        on = (getattr(self.args, "debug_on_fail", False)
              or os.environ.get("JANUS_DEBUG_ON_FAIL", "0") == "1")
        if not on or not self.flow_dir:
            return
        try:
            timeout = int(os.environ.get("JANUS_DEBUG_TIMEOUT_S", "900"))
        except ValueError:
            timeout = 900
        script = os.path.join(_ROOT, "scripts", "hooks", "janus_debug_rerun.tcl")
        # _post_build_make_cmd marks every existing results/logs/objects file
        # old (-o) so the `run` target can never trigger a stage rebuild.
        cmd = self._post_build_make_cmd(block, [
            "run", f"RUN_SCRIPT={script}",
            "RUN_LOG_NAME_STEM=janus_debug_rerun",
            "JANUS_DUMP_IMAGES=0",
        ])
        try:
            self.ui.debug(f"{block}: debug rerun on last good ODB: {' '.join(cmd)}")
            subprocess.run(cmd, cwd=self.flow_dir, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.ui.warn(f"{block}: debug rerun timed out after {timeout}s (ignored)")
        except Exception as e:
            self.ui.debug(f"{block}: debug rerun error (ignored): {e}")

    # ----- §126 adaptive closure: advise + bounded auto rerun ---------------
    # adaptive_closure.py is pure (classify / plan / knobs). This section owns
    # every side effect. Invariants: the base leaf is NEVER cleaned or written
    # (only read + copied), protected blocks never rerun (plan()), passes are
    # capped by --adaptive-max-passes. §145: a pass that beats the base with
    # no signoff regression is PROMOTED in place (_adaptive_promote) BEFORE
    # the block is marked DONE / emailed; the old leaf is kept, renamed.
    _ADAPT_VARIANT_RE = re.compile(r"[\w.-]*adapt\d+_[a-z_]+")

    def _adaptive_mode(self) -> str:
        # NB: independent of --no-physical (that only disables the macro-fit
        # SRAM reshape self-heal); --adaptive off is the sole kill switch.
        return getattr(self.args, "adaptive", None) or "advise"

    def _adaptive_thresholds(self) -> dict:
        th: dict = {}
        spec = getattr(self.args, "adaptive_thresholds", None)
        if spec:
            th.update(json.load(open(spec)) if os.path.isfile(spec) else json.loads(spec))
        mp = getattr(self.args, "adaptive_max_passes", None)
        if mp is not None:
            th["max_extra_passes"] = int(mp)
        return th

    def _adaptive_write(self, block: str, rec: dict,
                        reports_dir: Optional[str] = None) -> None:
        """adaptive.json + qor.json['adaptive'] in the block's reports dir."""
        d = reports_dir or rec.get("_reports_dir")
        if not d or not os.path.isdir(d):
            return
        body = {k: v for k, v in rec.items() if not k.startswith("_")}
        try:
            with open(os.path.join(d, "adaptive.json"), "w") as fh:
                json.dump(body, fh, indent=2, default=str)
            qp = os.path.join(d, "qor.json")
            if os.path.isfile(qp):
                with open(qp) as fh:
                    q = json.load(fh)
                q["adaptive"] = body
                with open(qp, "w") as fh:
                    json.dump(q, fh, indent=2, default=str)
        except (OSError, ValueError) as e:
            self.ui.debug(f"{block}: adaptive.json write failed (ignored): {e}")

    def _adaptive_advise(self, block: str, res: "BlockResult") -> Optional[dict]:
        """Classify the settled DONE block (qor rebuilt by report_bundle with
        the dashboard parsers) and plan the next action. Never raises."""
        mode = self._adaptive_mode()
        if mode == "off" or res.status != "DONE" or getattr(self.args, "dry_run", False):
            return None
        try:
            import adaptive_closure as ac
            import report_bundle as rb
            reports = self._reports_dir(block)
            if not reports:
                return None
            qor = rb.build_qor(block, res.status, self._logs_dir(block), reports,
                               self._results_dir(block))
            fq = ac.from_qor(qor)
            eco = self._eco_adopted_timing(block)   # §150: base = what ships
            if eco:
                for k_fq, k_eco in (("wns_ps", "setup_ws"), ("hold_wns_ps", "hold_ws"),
                                    ("tns_ps", "setup_tns")):
                    if eco.get(k_eco) is not None:
                        fq[k_fq] = float(eco[k_eco])
            th = self._adaptive_thresholds()
            rec = ac.plan(block, ac.classify(fq, th), mode, 0, th)
            rec["input"] = fq
            rec["passes"] = [ac.pass_summary(self._leaf(), fq)]
            rec["passes"][0]["timing_source"] = eco["source"] if eco else "6_finish.rpt"
            rec["_reports_dir"] = reports
            self._adaptive_write(block, rec)
            conf = rec.get("confidence")
            tag = f"adaptive[{mode}] {rec['class']}->{rec['action']}"
            if rec.get("blocked"):
                tag += f" (blocked: {rec['blocked']})"
            elif rec.get("rerun"):
                tag += f" (rerun {rec['flow_variant']})"
            res.note = (res.note + "  " if res.note else "") + tag
            self.ui.info(f"{block}: {tag} conf={conf} — "
                         f"{'; '.join(rec.get('evidence') or [])}")
            return rec
        except Exception as e:
            self.ui.debug(f"{block}: adaptive advise error (ignored): {e}")
            return None

    @staticmethod
    def _read_text(path: str) -> Optional[str]:
        try:
            with open(path, errors="ignore") as fh:
                return fh.read()
        except OSError:
            return None

    def _place_density(self, block: str) -> Optional[float]:
        try:
            txt = open(self._config_path(block)).read()
        except OSError:
            return None
        m = re.findall(r"^\s*(?:export\s+)?PLACE_DENSITY\s*[:?]?=\s*([0-9.]+)\s*$", txt, re.M)
        try:
            return float(m[-1]) if m else None
        except ValueError:
            return None

    @staticmethod
    def _adaptive_seedable(sub: str, rel: str) -> bool:
        """Stage <=2 artifacts (+ non-stage inputs like clock_period.txt) are
        copied into the variant leaf; 3_* and later are what the pass rebuilds.
        objects/ (merged libs, LEF caches) is copied whole."""
        if sub == "objects":
            return True
        name = os.path.basename(rel)
        if name.startswith(("1_", "2_")):
            return True
        if sub == "reports":
            return name == "synth_stat.txt"
        if sub == "results" and name.endswith(_ABSTRACT_EXTS):
            return False  # §145: the base's abstract describes the BASE layout
        return not name[:1].isdigit() and not name.startswith(("janus_", "qor.json", "adaptive"))

    def _adaptive_leaf_dirs(self, base_res: str, variant: str) -> Dict[str, tuple]:
        res_root = os.path.join(self.flow_dir, "results")
        rel = os.path.relpath(base_res, res_root)          # asap7/<design>/<leaf>
        if rel.startswith(os.pardir):
            raise ValueError(f"results dir {base_res} outside {res_root}")
        parent = os.path.dirname(rel)
        return {sub: (os.path.join(self.flow_dir, sub, rel),
                      os.path.join(self.flow_dir, sub, parent, variant))
                for sub in ("results", "logs", "reports", "objects")}

    def _adaptive_seed(self, base_res: str, variant: str) -> List[str]:
        """Copy the base leaf's <=2_floorplan state into a FRESH variant leaf.
        Only the variant leaf (name-validated) is ever deleted. Returns the
        flow_dir-relative seeded results/logs/objects files (make -o list)."""
        if not self._ADAPT_VARIANT_RE.fullmatch(variant):
            raise ValueError(f"refusing non-adaptive variant leaf {variant!r}")
        seeded: List[str] = []
        for sub, (src, dst) in self._adaptive_leaf_dirs(base_res, variant).items():
            if os.path.basename(dst) != variant or os.path.realpath(dst) == os.path.realpath(src):
                raise ValueError(f"variant leaf {dst} would alias the base leaf")
            if os.path.isdir(dst):
                shutil.rmtree(dst)
            os.makedirs(dst, exist_ok=True)
            if not os.path.isdir(src):
                continue
            for root, _dirs, files in os.walk(src):
                for fn in sorted(files):
                    rel = os.path.relpath(os.path.join(root, fn), src)
                    if not self._adaptive_seedable(sub, rel):
                        continue
                    out = os.path.join(dst, rel)
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    shutil.copy2(os.path.join(root, fn), out)
                    if sub != "reports":
                        seeded.append(os.path.relpath(out, self.flow_dir))
        return seeded

    def _adaptive_variant_cmd(self, block: str, variant: str, knobs: dict,
                              seeded: List[str], overlay: Optional[str],
                              restore: Optional[str] = None) -> List[str]:
        """The block's own make line (NUM_CORES / CORNER=WC / CORNERS / libs /
        DONT_USE / force vars / debug hooks / --extra-make) re-pointed at the
        variant leaf: base -o's and FLOW_VARIANT dropped, the class knobs and
        -o for every seeded artifact appended (later command-line assignment
        wins in GNU make, so knobs override block force vars)."""
        base = self._make_cmd(block)
        cmd: List[str] = []
        skip = False
        for tok in base:
            if skip:
                skip = False
                continue
            if tok == "-o":
                skip = True
                continue
            if tok.startswith("FLOW_VARIANT="):
                continue
            cmd.append(tok)
        cmd.append(f"FLOW_VARIANT={variant}")
        cmd += list(knobs.get("vars") or [])
        if overlay:
            hooks = os.path.join(_ROOT, "scripts", "hooks")
            pre = os.path.join(hooks, "janus_adaptive_pre.tcl")
            cmd.append(f"JANUS_CLOCK_LATENCY_TCL={overlay}")
            cmd += [f"{v}={pre}" for v in
                    ("PRE_GLOBAL_PLACE_TCL", "PRE_RESIZE_TCL", "PRE_DETAIL_PLACE_TCL")]
            if restore:
                # detail_place.tcl writes 3_place.sdc right after POST_DETAIL_PLACE
                # and CTS loads it: undo the uncertainty bump there, chaining
                # whatever POST_DETAIL_PLACE_TCL (debug hook) the base line set.
                chain = ""
                for i, tok in enumerate(cmd):
                    if tok.startswith("POST_DETAIL_PLACE_TCL="):
                        chain = tok.split("=", 1)[1]
                        del cmd[i]
                        break
                cmd += [f"JANUS_CLOCK_RESTORE_TCL={restore}",
                        f"JANUS_ADAPT_CHAIN_POST={chain}",
                        f"POST_DETAIL_PLACE_TCL={os.path.join(hooks, 'janus_adaptive_post.tcl')}"]
        for s in seeded:
            cmd += ["-o", s]
        return cmd

    # §134: 1-2 word trigger reason for the dashboard / warn line.
    _ADAPT_REASON = {"DRV_LIMITED": "slew/cap", "MODERATE": "setup",
                     "CLOCK_SKEW_LIMITED": "clk skew",
                     "CONGESTION_LIMITED": "congestion"}

    @classmethod
    def _adaptive_reason_tag(cls, klass: str, fq: Optional[dict]) -> str:
        fq = fq or {}
        word = cls._ADAPT_REASON.get(klass, (klass or "adapt").lower())
        try:
            if klass == "DRV_LIMITED":
                n = int(fq.get("slew_viol") or 0) + int(fq.get("cap_viol") or 0)
                return f"{word} {n}"
            if klass == "MODERATE":
                ws = [float(g["wns_ps"]) for g in (fq.get("groups") or {}).values()
                      if g.get("wns_ps") is not None]
                if fq.get("wns_ps") is not None:
                    ws.append(float(fq["wns_ps"]))
                if ws:
                    return f"{word} {min(ws):+.0f}ps"
        except (TypeError, ValueError):
            pass
        return word

    def _adaptive_stage_poll(self, logs_dir: str, live: dict,
                             stop: threading.Event) -> None:
        """Track the variant pass's current ORFS stage from the newest
        <n>_<step>.log in its leaf logs dir (dashboard only; never raises)."""
        names = {3: "3_place", 4: "4_cts", 5: "5_route", 6: "6_final"}
        while not stop.wait(5.0):
            try:
                logs = [f for f in os.listdir(logs_dir)
                        if f.endswith(".log") and f[:1].isdigit()]
                if not logs:
                    continue
                newest = max(logs, key=lambda f: os.path.getmtime(os.path.join(logs_dir, f)))
                n = self._stage_major_index(newest)
                if n in names:
                    live["stage"] = names[n]
            except OSError:
                continue

    def _adaptive_run_variant(self, block: str, base_res: str, variant: str,
                              knobs: dict, cls: str = "",
                              fq: Optional[dict] = None) -> dict:
        """One bounded pass from 3_place in its own FLOW_VARIANT leaf.
        >>> PD-UNTESTED <<< (seed + make line are unit-tested in the sandbox)."""
        import adaptive_closure as ac
        import report_bundle as rb
        run = {"variant": variant, "knobs": knobs, "status": "NOT_RUN"}
        seeded = self._adaptive_seed(base_res, variant)
        dirs = self._adaptive_leaf_dirs(base_res, variant)
        overlay = None
        if knobs.get("overlay"):
            overlay = os.path.join(dirs["results"][1], "janus_adaptive_latency.tcl")
            with open(overlay, "w") as fh:
                fh.write(knobs["overlay"])
        restore = None
        if overlay and knobs.get("restore"):  # always set alongside an overlay
            restore = os.path.join(dirs["results"][1], "janus_adaptive_restore.tcl")
            with open(restore, "w") as fh:
                fh.write(knobs["restore"])
        cmd = self._adaptive_variant_cmd(block, variant, knobs, seeded, overlay, restore)
        run["cmd"] = " ".join(cmd)
        run["seeded"] = len(seeded)
        if getattr(self.args, "dry_run", False):
            self.ui.info(f"{block}: [dry-run] adaptive pass: {run['cmd']}")
            run["status"] = "DRY_RUN"
            return run
        timeout = adaptive_timeout_s()
        tag = self._adaptive_reason_tag(cls, fq)
        self.ui.warn(f"{block}: adaptive auto kicked in [{tag}] -> pass {variant} "
                     f"({'; '.join(knobs.get('notes') or [])}) "
                     f"— {len(seeded)} seeded artifact(s) from {os.path.basename(base_res)}")
        live = {"tag": tag, "stage": "3_place", "t0": time.time()}
        self._adapt_live[block] = live
        stop = threading.Event()
        poller = threading.Thread(target=self._adaptive_stage_poll,
                                  args=(dirs["logs"][1], live, stop), daemon=True)
        poller.start()
        try:
            with open(self._block_log(block), "ab", buffering=0) as logf:
                logf.write(f"\n===== {block} : adaptive [{tag}] {variant} {run['cmd']} "
                           f"@ {datetime.now()} =====\n".encode())
                rc = subprocess.run(cmd, cwd=self.flow_dir, stdout=logf,
                                    stderr=subprocess.STDOUT, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            rc = -1
            run["error"] = f"timeout {timeout}s"
        finally:
            stop.set()
            self._adapt_live.pop(block, None)
        run["rc"] = rc
        vres, vrep, vlog = dirs["results"][1], dirs["reports"][1], dirs["logs"][1]
        done = rc == 0 and os.path.isfile(os.path.join(vres, "6_final.odb"))
        run["status"] = "DONE" if done else "FAILED"
        qor = rb.build_qor(block, run["status"], vlog, vrep, vres)
        try:
            with open(os.path.join(vrep, "qor.json"), "w") as fh:
                json.dump(qor, fh, indent=2, default=str)
        except OSError:
            pass
        run["input"] = ac.from_qor(qor)
        return run

    def _adaptive_auto(self, block: str, res: "BlockResult", rec: dict) -> None:
        """--adaptive auto: run at most max_extra_passes variant passes while
        plan() says rerun; if a pass beats the base (pick_promotion: no
        signoff regression + a strict improvement) it is PROMOTED (§145)."""
        if not rec or rec.get("mode") != "auto" or not rec.get("rerun"):
            return
        try:
            import adaptive_closure as ac
            th = self._adaptive_thresholds()
            base_res = self._results_dir(block)
            if not base_res or not self.flow_dir:
                return
            leaf = self._leaf()
            cur, done_n = rec, 0
            rec["runs"] = []
            while cur.get("rerun"):
                variant = cur["flow_variant"] if leaf == "base" else f"{leaf}_{cur['flow_variant']}"
                knobs = ac.class_knobs(cur["class"], cur["input"], self._place_density(block),
                                       self._read_text(os.path.join(base_res, "2_floorplan.sdc")))
                if not knobs["vars"] and not knobs["overlay"]:
                    rec["runs"].append({"variant": variant, "status": "SKIPPED",
                                        "reason": "; ".join(knobs["notes"]) or "no safe knobs"})
                    break
                run = self._adaptive_run_variant(block, base_res, variant, knobs,
                                                 cur.get("class", ""), cur.get("input"))
                done_n += 1
                rec["runs"].append(run)
                if run["status"] != "DONE":
                    break
                rec["passes"].append(ac.pass_summary(variant, run["input"]))
                cur = ac.plan(block, ac.classify(run["input"], th), "auto", done_n, th)
                cur["input"] = run["input"]
                run["next"] = {k: cur.get(k) for k in ("class", "action", "rerun", "blocked")}
            if len(rec["passes"]) > 1:
                best, why = ac.pick_promotion(rec["passes"])
                rec["best"] = best["name"] if best else leaf
                rec["promotion"] = {"pass": best["name"] if best else None, "reason": why}
                if best:
                    self._adaptive_promote(block, res, rec, best, why)
                else:
                    res.note += f"  adaptive: kept base ({why[:120]})"
                    self.ui.info(f"{block}: adaptive: base kept — {why}")
            self._adaptive_write(block, rec)
        except Exception as e:
            self.ui.warn(f"{block}: adaptive auto error (ignored; base untouched): {e}")
            self._adaptive_write(block, rec)

    def _adaptive_promote(self, block: str, res: "BlockResult", rec: dict,
                          best: dict, why: str) -> bool:
        """§145: make pass *best* the block's result IN PLACE.

        The pass leaf is swapped into the run's leaf (results/logs/reports/
        objects) and the old leaf is kept as <leaf>_superseded_<ts>, so every
        consumer that keys on the leaf — --status, the manifest/--resume,
        dependents' hier inputs, the email and Drive bundle, the dashboard —
        sees the promoted pass with no --variant rerun. Stage 1-2 artifacts the
        pass did not rebuild are backfilled from the old leaf first (mtimes
        preserved). The abstract is regenerated. All-or-nothing: a failed
        rename is rolled back and the base stays the result."""
        import adaptive_closure as ac
        variant = best["name"]
        base_res = self._results_dir(block)
        if not base_res or not self._ADAPT_VARIANT_RE.fullmatch(variant):
            return False
        leaf = self._leaf()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        keep = f"{leaf}_superseded_{ts}"
        dirs = self._adaptive_leaf_dirs(base_res, variant)
        try:
            for sub, (src, dst) in dirs.items():
                if not os.path.isdir(src) or not os.path.isdir(dst):
                    continue
                for root, _d, files in os.walk(src):
                    for fn in files:
                        if not fn.startswith(("1_", "2_")) and not (
                                sub == "reports" and fn.startswith("synth")):
                            continue
                        rel = os.path.relpath(os.path.join(root, fn), src)
                        out = os.path.join(dst, rel)
                        if not os.path.exists(out):
                            os.makedirs(os.path.dirname(out), exist_ok=True)
                            shutil.copy2(os.path.join(root, fn), out)
                if sub == "results":  # stale (base-layout) abstract, if any
                    for fn in os.listdir(dst):
                        if fn.endswith(_ABSTRACT_EXTS) and not fn[:1].isdigit():
                            os.remove(os.path.join(dst, fn))
        except OSError as e:
            self.ui.warn(f"{block}: adaptive promote backfill failed ({e}); base kept")
            res.note += f"  adaptive: promote of {variant} FAILED (backfill: {e})"
            return False
        moved: List[tuple] = []
        try:
            for sub, (src, dst) in dirs.items():
                if not os.path.isdir(dst):
                    continue
                if os.path.isdir(src):
                    arch = os.path.join(os.path.dirname(src), keep)
                    os.rename(src, arch)
                    moved.append((arch, src))
                os.rename(dst, src)
                moved.append((src, dst))
        except OSError as e:
            for a, b in reversed(moved):   # roll back
                try:
                    os.rename(a, b)
                except OSError:
                    pass
            self.ui.warn(f"{block}: adaptive promote rename failed ({e}); base kept")
            res.note += f"  adaptive: promote of {variant} FAILED (rename: {e})"
            return False
        rec["promotion"].update({"promoted": True, "superseded_leaf": keep,
                                 "at": datetime.now().isoformat(timespec="seconds")})
        # refresh the block's QoR from the promoted leaf (same parser as the
        # main flow), DRC from the pass qor.
        auth = self._authoritative_timing_from_rpt(block)
        if auth:
            res.wns_ns = auth.get("setup_ws", res.wns_ns)
            res.hold_ps = auth.get("hold_ws", res.hold_ps)
            res.tns_ns = auth.get("setup_tns", res.tns_ns)
            res.timing_source = auth.get("source")
            counts = auth.get("counts") or {}
            res.max_slew_violations = counts.get("max_slew")
            res.max_cap_violations = counts.get("max_cap")
            pg = auth.get("path_groups") or {}
            if pg.get("setup") or pg.get("hold"):
                res.path_groups = pg
        else:
            res.wns_ns = best.get("wns_ps")
            res.hold_ps = best.get("hold_wns_ps")
            res.tns_ns = best.get("tns_ps")
            res.max_slew_violations = best.get("slew_viol")
        if best.get("drc") is not None:
            res.drc_violations = best.get("drc")
        res.note += f"  adaptive PROMOTED {variant} ({why[:120]}); old leaf -> {keep}"
        self.ui.good(f"{block}: adaptive pass {variant} PROMOTED to {leaf} — {why} "
                     f"(previous result kept as {keep})")
        try:
            with open(os.path.join(dirs["reports"][0], "adaptive_promoted.json"), "w") as fh:
                json.dump({"block": block, "promoted": variant, "reason": why,
                           "superseded_leaf": keep, "passes": rec.get("passes")},
                          fh, indent=2, default=str)
        except OSError:
            pass
        # §150: the promoted leaf is a fresh 6_final with no post-route ECO
        # (the old base's ECO lives in the superseded leaf). Give it the same
        # post-route steps a normal DONE block gets — ECO, abstract, signoff —
        # so the shipped layout, abstract and signoff SPEF all match.
        try:
            self._post_route_opt(block)
        except Exception as e:
            self.ui.warn(f"{block}: post-promotion ECO error (promoted leaf kept as-is): {e}")
        self._run_abstract(block)
        try:
            self._run_signoff(block)
        except Exception as e:
            self.ui.warn(f"{block}: post-promotion signoff error (ignored): {e}")
        self._archive_golden(block, res)   # golden = promoted leaf (never raises)
        return True

    # ----- §145 run-wide status table (every email) -------------------------
    def _drive_run_stamp(self) -> str:
        """Drive subfolder for THIS run: YYYY-MM-DD_HH-MM-SS of the run start
        (parsed from the run dir name, else first use)."""
        if getattr(self, "_drive_stamp", None):
            return self._drive_stamp
        m = re.search(r"(\d{8})_(\d{6})", os.path.basename(str(self.run_dir or "")))
        if m:
            d, t = m.groups()
            st = f"{d[:4]}-{d[4:6]}-{d[6:]}_{t[:2]}-{t[2:4]}-{t[4:]}"
        else:
            st = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._drive_stamp = st
        return st

    def _run_table(self) -> List[dict]:
        """One row per block in the run graph with the latest state:
        DONE/FAILED/BLOCKED rows carry QoR, in-flight rows show RUNNING or
        ADAPT (<stage>), not-started rows PENDING. Never raises."""
        rows: List[dict] = []
        try:
            dep = {}
            try:
                dep = self.dep.summary()
            except Exception:
                pass
            live = getattr(self, "_adapt_live", {}) or {}
            for b in self.dep.graph:
                r = self.results.get(b)
                st = (r.status if r else "PENDING") or "PENDING"
                if st in ("PENDING", "RUNNING") and dep.get(b) == Status.RUNNING.value:
                    st = "RUNNING"
                if b in live:
                    st = f"ADAPT ({live[b].get('stage', '?')})"
                settled = st in ("DONE", "FAILED", "NEEDS_MANUAL", "ABORTED")
                rows.append({
                    "block": b, "status": st,
                    "setup_ps": r.wns_ns if (r and settled) else None,
                    "hold_ps": r.hold_ps if (r and settled) else None,
                    "drc": r.drc_violations if (r and settled) else None,
                    "slew": r.max_slew_violations if (r and settled) else None,
                    "note": ("adaptive PROMOTED" if r and "PROMOTED" in (r.note or "")
                             else ""),
                })
        except Exception as e:
            self.ui.debug(f"run table error (ignored): {e}")
        return rows

    def _load_resumed_qor(self, block: str) -> None:
        """§145: a resume-skipped DONE block still shows its on-disk QoR in the
        all-blocks table (same parsers as a fresh run; DRC from
        5_route_drc.rpt). Never raises; missing reports leave fields None."""
        try:
            res = self.results[block]
            auth = self._authoritative_timing_from_rpt(block)
            if auth:
                res.wns_ns = auth.get("setup_ws")
                res.hold_ps = auth.get("hold_ws")
                res.tns_ns = auth.get("setup_tns")
                res.timing_source = auth.get("source")
                counts = auth.get("counts") or {}
                res.max_slew_violations = counts.get("max_slew")
                res.max_cap_violations = counts.get("max_cap")
            eco = self._eco_adopted_timing(block)   # §150
            if eco:
                if eco.get("setup_ws") is not None:
                    res.wns_ns = eco["setup_ws"]
                if eco.get("hold_ws") is not None:
                    res.hold_ps = eco["hold_ws"]
                if eco.get("setup_tns") is not None:
                    res.tns_ns = eco["setup_tns"]
                res.timing_source = eco.get("source")
            rep = self._reports_dir(block)
            if rep:
                from report_bundle import parse_route_drc
                drc = parse_route_drc(os.path.join(rep, "5_route_drc.rpt"))
                if drc is not None:
                    res.drc_violations = drc.get("total")
                if os.path.isfile(os.path.join(rep, "adaptive_promoted.json")):
                    res.note += "  adaptive PROMOTED (earlier run)"
        except Exception as e:
            self.ui.debug(f"{block}: resumed QoR load skipped — {e}")

    def _notify_run_summary(self) -> None:
        """§145: one final email at run end with the full block table."""
        try:
            if not getattr(self, "notifier", None) or not self.notifier.email_configured:
                return
            rows = self._run_table()
            ok = sum(1 for r in rows if r["status"] == "DONE")
            self.notifier.send_summary(
                f"JANUS run complete: {ok}/{len(rows)} DONE ({os.path.basename(self.run_dir)})",
                rows, run_dir=self.run_dir)
            self._notify_log(f"run summary: {self.notifier.last_outcome}")
        except Exception as e:
            self._notify_log(f"run summary email error: {e}")

    # ----- kepler-formal shared-library auto-fix -----------------------------
    def _ensure_kepler_libpath(self) -> None:
        """Make ORFS's logic-equivalence checker (kepler-formal) loadable.

        Symptom we are defending against (seen on the user's WSL2 install):
            cts.tcl, 83 ... kepler-formal: error while loading shared libraries:
            libnaja_snl_pyloader.so: cannot open shared object file

        kepler-formal ships its own .so deps (the 'naja' SNL python loader) but
        ORFS does NOT add their directory to LD_LIBRARY_PATH, so the binary
        cannot start and the *whole* flow dies at CTS — even though the equiv
        check is OPTIONAL.  This is a pure environment/packaging problem (NOT an
        RTL/config bug), so we fix it ourselves instead of burning an LLM call.

        Strategy (version-independent — does not depend on how a given ORFS
        release gates the check):
          1. Locate libnaja*.so anywhere under <ORFS>/tools/install.
          2. Prepend each containing dir (plus any kepler-formal '*/lib' dir) to
             this process's LD_LIBRARY_PATH.  _run_make()'s Popen inherits
             os.environ, so the child make/openroad/kepler-formal sees it.
        If the .so genuinely does not exist (incomplete install) we cannot fix
        it here — we warn, and rely on EQUIVALENCE_CHECK=0 in the block configs
        to skip the check entirely.
        """
        if not self.orfs:
            return
        install = os.path.join(self.orfs, "tools", "install")
        if not os.path.isdir(install):
            return
        lib_dirs = set()
        # primary: the naja loader libs that the error names explicitly
        for so in glob.glob(os.path.join(install, "**", "libnaja*.so*"),
                            recursive=True):
            lib_dirs.add(os.path.dirname(so))
        # secondary: any kepler-formal lib dir (other deps live alongside)
        for libdir in glob.glob(os.path.join(install, "**", "kepler-formal",
                                             "**", "lib"), recursive=True):
            if os.path.isdir(libdir):
                lib_dirs.add(libdir)
        if not lib_dirs:
            self.ui.warn(
                "kepler-formal libs (libnaja*.so) not found under "
                f"{install} — cannot auto-fix LD_LIBRARY_PATH. If a block dies "
                "at cts.tcl with 'libnaja_snl_pyloader.so: cannot open shared "
                "object file', the equiv-check is broken in this ORFS install; "
                "EQUIVALENCE_CHECK=0 in the block configs should skip it.")
            return
        current = os.environ.get("LD_LIBRARY_PATH", "")
        existing = current.split(os.pathsep) if current else []
        added = [d for d in sorted(lib_dirs) if d not in existing]
        if added:
            os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(added + existing)
            self.ui.good("kepler-formal: added to LD_LIBRARY_PATH so the "
                         "equiv-check can load instead of crashing CTS:")
            for d in added:
                self.ui.info(f"      {d}")
        else:
            self.ui.debug("kepler-formal lib dir(s) already on LD_LIBRARY_PATH")

    def _check_lec_dedup_support(self) -> None:
        """§147: confirm this ORFS checkout honours REMOVE_LIBS_FOR_LEC.

        Older formal_check.tcl versions ignore the variable, in which case the
        duplicate-child-lib Kepler abort comes back. Warn loudly (do not fail):
        the fallback is LEC_CHECK=0 for that parent block.
        """
        if not self.flow_dir or os.environ.get("JANUS_LEC_DEDUP_LIBS", "1") == "0":
            return
        fc = os.path.join(self.flow_dir, "scripts", "formal_check.tcl")
        try:
            with open(fc, "r", errors="replace") as fh:
                ok = "REMOVE_LIBS_FOR_LEC" in fh.read()
        except OSError:
            return  # no formal_check.tcl -> this ORFS has no Kepler LEC at all
        if ok:
            self.ui.debug("ORFS formal_check.tcl supports REMOVE_LIBS_FOR_LEC (§147)")
        else:
            self.ui.warn(
                f"{fc} does not reference REMOVE_LIBS_FOR_LEC: this ORFS will pass "
                "BOTH <child>_fast.lib and <child>_slow.lib to kepler-formal and "
                "LEC_CHECK=1 parents (user_core/user_domain/janus_soc_top) will "
                "abort in 4_1_cts with 'contains already a SNLDesign named'. "
                "Update ORFS or set LEC_CHECK=0 for those blocks.")

    # ----- preflight checks --------------------------------------------------
    def _preflight(self) -> None:
        problems = []
        # Fix the kepler-formal shared-lib path up front so the optional
        # equiv-check loads (or is cleanly skipped) instead of crashing CTS.
        self._ensure_kepler_libpath()
        self._check_lec_dedup_support()
        if not self.orfs:
            problems.append("ORFS_DIR not set (source ~/janus_asic/janus_asic_env.sh or pass --orfs)")
        elif not os.path.isdir(self.flow_dir):
            problems.append(f"ORFS flow dir not found: {self.flow_dir}")
        if not os.path.isdir(self.rtl_root):
            problems.append(f"RTL root not found: {self.rtl_root}")
        # config presence is only required for actual runs
        need_cfg = not (self.args.estimate or self.args.dry_run)
        if need_cfg and self.design_root and os.path.isdir(self.design_root):
            for b in self.dep.graph:
                if not os.path.isfile(self._config_path(b)):
                    problems.append(f"missing config for '{b}': {self._config_path(b)} "
                                    "(run scripts/03_setup_janus_configs.sh)")
        # Verify the OpenROAD binary actually loads its shared libs BEFORE we
        # run any block. A broken install (e.g. missing libortools.so.9) would
        # otherwise fail every block and waste an LLM call per block.
        if need_cfg and self.orfs:
            orbin = os.path.join(self.orfs, "tools", "install", "OpenROAD",
                                 "bin", "openroad")
            if os.path.isfile(orbin):
                try:
                    import subprocess
                    proc = subprocess.run([orbin, "-version"],
                                          capture_output=True, text=True, timeout=30)
                    combined = (proc.stdout or "") + (proc.stderr or "")
                    if proc.returncode != 0 and (
                        "shared libraries" in combined
                        or "libortools" in combined
                        or "cannot open shared object" in combined):
                        problems.append(
                            "OpenROAD binary is installed but cannot start — "
                            "missing shared library. Run:\n"
                            f"      ldd {orbin} | grep 'not found'\n"
                            "    Likely libortools.so.9 (OR-Tools 9). Rebuild "
                            "OpenROAD or add its lib dir to LD_LIBRARY_PATH in "
                            "janus_asic_env.sh. Detail: " + combined.strip()[:200])
                except (OSError, subprocess.SubprocessError) as exc:
                    problems.append(f"could not exec OpenROAD binary {orbin}: {exc}")
            # if the binary is missing entirely, a block run would say so; we
            # don't hard-fail here because some setups use a PATH openroad.

        if problems and need_cfg:
            self.ui.err("Preflight failed:")
            for p in problems:
                self.ui.err(f"  - {p}")
            raise SystemExit(2)
        elif problems:
            for p in problems:
                self.ui.warn(f"(estimate/dry-run) note: {p}")
        git_state = "disabled"
        if not self.args.no_git_commit:
            if self.git.available():
                git_state = f"commit -> {self.git.current_branch()}"
                if self.git.sync_branches:
                    git_state += f" (+push sync: {', '.join(self.git.sync_branches)})"
                elif self.args.git_push:
                    git_state += " (+push origin)"
            else:
                git_state = "commit requested but repo not a git work tree"
        self.ui.panel("JANUS Autonomous ASIC Agent",
                      (f"  ORFS     : {self.orfs or '(unset)'}\n"
                       f"  RTL root : {self.rtl_root}\n"
                       f"  run dir  : {self.run_dir}\n"
                       f"  blocks   : {', '.join(self.dep.graph)}\n"
                       f"  parallel : {self.args.parallel}   max-retries: {self.args.max_retries}   "
                       f"num-cores: {self.args.num_cores}\n"
                       f"  LLM      : {'ENABLED' if (self.llm.available() and not self.args.no_llm) else 'disabled'}\n"
                       f"  reasoning: {'ON — deterministic layer+blast-radius triage, advisory only' if getattr(self.args, 'reason', False) else 'off (--reason to enable)'}\n"
                       f"  git      : {git_state}\n"
                       f"  abstracts: {'skipped (--no-abstract)' if self.args.no_abstract else 'generate .lef/.lib after each block'}\n"
                       f"  mode     : {'INTERACTIVE (shell prompts)' if self.interactive else 'autonomous'}"))

        # Persist a run manifest so the exact source state this run executed
        # against is recoverable from the run-dir alone (shared alongside logs).
        try:
            manifest = os.path.join(self.run_dir, "manifest.txt")
            with open(manifest, "w") as mf:
                mf.write(f"JANUS run manifest — {datetime.now()}\n")
                mf.write(f"run dir : {self.run_dir}\n")
                mf.write(f"blocks  : {', '.join(self.dep.graph)}\n\n")
                if self.git.available():
                    mf.write(self.git.provenance() + "\n")
        except OSError:
            pass

    # ----- finalize / report -------------------------------------------------
    def _finalize(self, elapsed: float) -> None:
        rows = [self.results[b].as_row() for b in self.dep.graph]
        usage = self.llm.usage()
        self.ckpt.record_usage(**usage)
        meta = {
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "orfs_dir": self.orfs,
            "rtl_root": self.rtl_root,
            "run_dir": self.run_dir,
            "elapsed_sec": round(elapsed, 1),
            "honesty_note": ("All PPA values are whatever ORFS reported during THIS "
                             "run, or 'n/a' if absent. Config TARGETS are design-intent, "
                             "not verified silicon results."),
        }
        paths = self.reporter.write_all(rows, usage, meta)
        self.ui.table(rows)
        self._notify_run_summary()
        done = sum(1 for r in self.results.values() if r.status == "DONE")
        # stamp final run meta into the dashboard so a post-run browse is exact
        if self.dash_state is not None:
            try:
                self.dash_state.set_meta(
                    finished=datetime.now().isoformat(timespec="seconds"),
                    done=done, total=len(rows),
                    elapsed_sec=round(elapsed, 1))
            except Exception:
                pass
        dash_line = ""
        if self.dashboard is not None:
            try:
                dash_line = (f"  dashboard   : http://localhost:{self.dashboard.port}/"
                             "  (still live — browse logs/reports/results/floorplan)\n")
            except Exception:
                dash_line = ""
        self.ui.panel(
            "Run complete",
            (f"  blocks DONE : {done}/{len(rows)}\n"
             f"  elapsed     : {elapsed/60:.1f} min\n"
             f"  LLM calls   : {usage.get('llm_calls', 0)}  "
             f"(~{usage.get('approx_tokens', 0)} tokens, ESTIMATE)\n"
             f"  report      : {paths['html']}\n"
             f"{dash_line}"
             f"  checkpoint  : {self.ckpt.path}\n\n"
             "  Reminder: numbers above are ORFS output for this run (or n/a).\n"
             "  No metric here is a guaranteed/verified silicon result."))


# ---------------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------------- #
# Consolidated --skip token -> (args.<attr> it forces True, one-line meaning).
# The attr is the legacy --no-* switch (still accepted, hidden from --help) so
# all downstream code stays unchanged. This table is the single source of
# truth for the --skip choices AND its help text.
_SKIP_MAP = {
    "llm":            ("no_llm", "never call the LLM; stop a block on first failure"),
    "git-commit":     ("no_git_commit", "do not git-commit LLM fixes"),
    "macro-reshape":  ("no_physical", "no deterministic macro-fit (MPL-0006) SRAM reshape self-heal"),
    "physical":       ("no_physical", "legacy alias of macro-reshape"),
    "sram":           ("no_sram_regen", "on reshape, edit 04_setup_srams.sh but do not regenerate macros"),
    "abstract":       ("no_abstract", "no .lef/.lib abstract after a block hardens"),
    "post-route-opt": ("no_post_route_opt", "no adaptive post-route hold/setup/DRC ECO (keep-if-better)"),
    "debug-dump":     ("no_debug_dump", "no per-stage POST_*_TCL debug hooks (timing rpts/netlists/.png)"),
    "debug-images":   ("no_debug_images", "keep debug rpts/netlists, skip the .png layout/clock-tree images"),
    "golden":         ("no_golden", "no golden sign-off archiving/commit"),
    "signoff":        ("no_signoff", "no sign-off (RCX/LVS) on DONE blocks"),
    "signoff-sta":    ("no_signoff_sta", "no STA re-timing on the sign-off RCX SPEF"),
    "stamp-check":    ("skip_stamp_check", "bypass the pre-flight stamp-freshness guard"),
}

# Read-only / exit-early modes: at most one per invocation.
_EXCLUSIVE_MODES = ("status", "gc", "gc_dry_run", "resume_preview", "estimate",
                    "signoff_only", "triage_slew")
_ADAPTIVE_MAX_PASSES_CAP = 5


def _apply_skips(args: argparse.Namespace) -> None:
    """Fold the consolidated --skip tokens into the individual --no-* booleans
    (OR-ing, so an explicit --no-* still wins). Idempotent."""
    for tok in (getattr(args, "skip", None) or []):
        ent = _SKIP_MAP.get(tok)
        if ent is not None:
            setattr(args, ent[0], True)


def _flag(dest: str) -> str:
    return "--" + dest.replace("_", "-")


def _validate_args(args: argparse.Namespace) -> Tuple[List[str], List[str]]:
    """Cross-flag consistency check, run after _apply_skips(). Returns
    (errors, warnings); any error aborts main() with exit code 2 BEFORE any
    work starts. Warnings flag flags that are silently ignored."""
    err: List[str] = []
    warn: List[str] = []
    g = lambda k, d=None: getattr(args, k, d)  # noqa: E731

    modes = [_flag(m) for m in _EXCLUSIVE_MODES if g(m)]
    if len(modes) > 1:
        err.append(f"{' / '.join(modes)} are mutually exclusive (each is a "
                   "separate read-only/exit-early mode)")
    if modes and g("post_triage_slew"):
        err.append(f"--post-triage-slew needs a build; it conflicts with {modes[0]}")
    if g("resume_from") and (g("clean") or g("force_blocks")):
        err.append("--resume-from trusts stages before STAGE on disk; it "
                   "conflicts with --clean/--force-blocks (which wipe them)")
    if g("clean") and g("force_blocks"):
        warn.append("--clean already forces every targeted block; "
                    "--force-blocks is redundant")
    if g("yes") and not g("gc"):
        warn.append("--yes only answers the --gc prompt; ignored")
    if g("no_git_commit") and (g("git_push") or g("git_sync_branches")):
        err.append("--git-push/--git-sync-branches publish committed fixes; "
                   "they conflict with --skip git-commit (--no-git-commit)")
    if g("no_golden") and g("golden_include_heavy"):
        err.append("--golden-include-heavy conflicts with --skip golden (--no-golden)")
    if g("no_golden") and g("golden_dir"):
        warn.append("--golden-dir ignored under --skip golden")
    if g("signoff_only") and g("no_signoff"):
        err.append("--signoff-only conflicts with --skip signoff (--no-signoff)")
    if g("interactive") and g("no_llm"):
        warn.append("--interactive with --skip llm only prompts before "
                    "deterministic fixes (no LLM calls will be made)")
    if g("post_triage_slew") and g("dry_run"):
        warn.append("--post-triage-slew does nothing under --dry-run")
    if g("variants") and g("variant", "base") != "base":
        err.append("--variant sets ONE leaf; --variants fans out and sets each "
                   "child's leaf itself — use one or the other")
    for kv in (g("extra_make") or []):
        if "=" not in kv or not kv.split("=", 1)[0].strip():
            err.append(f"--extra-make expects VAR=VAL, got {kv!r}")

    # ---- adaptive closure
    mode = g("adaptive") or "advise"
    if mode == "off" and (g("adaptive_max_passes") is not None
                          or g("adaptive_thresholds")):
        err.append("--adaptive-max-passes/--adaptive-thresholds need "
                   "--adaptive advise|auto (got off)")
    if mode == "auto" and g("dry_run"):
        err.append("--adaptive auto launches real reruns; it conflicts with --dry-run")
    mp = g("adaptive_max_passes")
    if mp is not None and not 0 <= mp <= _ADAPTIVE_MAX_PASSES_CAP:
        err.append(f"--adaptive-max-passes must be 0..{_ADAPTIVE_MAX_PASSES_CAP}, got {mp}")
    if mp and mode == "advise":
        warn.append("--adaptive-max-passes only bounds reruns under --adaptive auto")
    spec = g("adaptive_thresholds")
    if spec:
        try:
            import adaptive_closure as _ac
            th = (json.load(open(spec)) if os.path.isfile(spec)
                  else json.loads(spec))
            if not isinstance(th, dict):
                raise ValueError("must be a JSON object")
            bad = sorted(set(th) - set(_ac.DEFAULT_THRESHOLDS))
            if bad:
                raise ValueError(f"unknown key(s) {bad}; valid: "
                                 f"{sorted(_ac.DEFAULT_THRESHOLDS)}")
            if "max_extra_passes" in th and mp is not None:
                warn.append("--adaptive-max-passes overrides max_extra_passes "
                            "in --adaptive-thresholds")
        except Exception as e:  # noqa: BLE001
            err.append(f"--adaptive-thresholds: {e}")

    # ---- numeric ranges
    for dest, lo in (("parallel", 1), ("max_retries", 1), ("num_cores", 1),
                     ("drc_handful", 0), ("signoff_timeout", 1),
                     ("variants_reserve_cores", 0), ("llm_retries", 0),
                     ("golden_wns_margin_ps", 0), ("golden_hold_margin_ps", 0)):
        v = g(dest)
        if v is not None and v < lo:
            err.append(f"{_flag(dest)} must be >= {lo}, got {v}")
    port = g("dashboard_port")
    if port is not None and not 0 <= port <= 65535:
        err.append(f"--dashboard-port must be 0..65535, got {port}")
    return err, warn


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="janus_agent",
        description="Autonomous OpenROAD synthesis agent for the JANUS SoC "
                    "(calls the Abacus LLM only on failures).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--blocks", nargs="+", metavar="BLOCK",
                   help="subset of blocks to run (default: all). "
                        f"choices: {', '.join(DEFAULT_GRAPH)}")
    p.add_argument("--parallel", type=int, default=1,
                   help="max independent blocks to run at once (default 1)")
    p.add_argument("--num-cores", type=int, default=None,
                   help="OpenROAD thread count (NUM_CORES passed to ORFS make). "
                        "Default: auto-detect via nproc, capped at 24. "
                        "OpenROAD passes this as -threads to its internal tools "
                        "(global_route, detailed_route, etc.). Set to 1 for "
                        "single-threaded debugging, or match your CPU count for "
                        "max throughput. ORFS default (if unset) uses all cores.")
    p.add_argument("--max-retries", type=int, default=3,
                   help="max attempts per block including the first (default 3)")
    p.add_argument("--orfs", default=None,
                   help="path to OpenROAD-flow-scripts (default $ORFS_DIR)")
    p.add_argument("--rtl-root", default=None,
                   help="path to the JANUS repo root holding rtl/ (default $JANUS_RTL_ROOT)")
    p.add_argument("--run-dir", default=None,
                   help="override the run/log/report directory")
    # ---- Parallel-variant campaign (one box, N variants of the SAME block) ----
    p.add_argument("--variant", default="base", metavar="LEAF",
                   help="ORFS FLOW_VARIANT leaf for THIS run's results/reports/"
                        "logs (default 'base'). Set to a distinct name per leg so "
                        "parallel variant runs on one shared disk do not collide. "
                        "Usually set automatically by --variants fan-out.")
    p.add_argument("--extra-make", nargs="*", default=[], metavar="VAR=VAL",
                   help="extra make assignments appended to the ORFS make command "
                        "(highest GNU-make precedence) — typically the lever enable "
                        "for this variant, e.g. JANUS_VLSU_LDU_RETIME2=1. Can pass "
                        "several: --extra-make A=1 B=0.")
    p.add_argument("--variants", nargs="+", default=None, metavar="TAG[:VAR=VAL,...]",
                   help="PARENT fan-out: launch one parallel child run per spec, "
                        "each a full agent run of the SAME --blocks with its own "
                        "FLOW_VARIANT leaf (=TAG) and lever defines. Example: "
                        "--variants stock ldu_rt2:JANUS_VLSU_LDU_RETIME2=1 . The "
                        "--num-cores budget is split evenly across the N children "
                        "(reserve a few for the OS). Children run in parallel and "
                        "the parent aggregates their exit codes.")
    p.add_argument("--variants-reserve-cores", type=int, default=0, metavar="N",
                   help="cores to leave unallocated when splitting --num-cores "
                        "across --variants children (default 0).")
    p.add_argument("--resume", action="store_true",
                   help="resume using the GLOBAL flow-state manifest (survives a "
                        "fresh --run-dir): skip DONE blocks, and STAGE-resume "
                        "partial ones (reuse valid stages via a semantic config "
                        "diff, redo only what changed) instead of restarting "
                        "from synthesis")
    p.add_argument("--manifest", default=None, metavar="PATH",
                   help="path to the global flow-state manifest JSON "
                        "(default: <asic_flow>/.janus_state/janus_flow_state.json, "
                        "or $JANUS_MANIFEST). It lives OUTSIDE every run-dir so "
                        "stage/QoR/cleanup state persists across runs.")
    p.add_argument("--force-blocks", nargs="+", default=None, metavar="BLOCK",
                   help="force a clean re-run of these blocks from 1_synth. "
                        "Wipes ALL their on-disk ORFS state "
                        "(flow/{results,reports,logs,objects}/asap7/<block>/...) "
                        "AND their flow-state manifest entry, BEFORE scheduling. "
                        "This is what actually forces a rebuild: deleting "
                        "janus_flow_state.json alone does NOT, because ORFS make "
                        "is mtime-driven on the on-disk *.odb (and --resume "
                        "re-touches them when no config snapshot is stored). "
                        "Accepts space- or comma-separated names; only blocks in "
                        "this run's --blocks are affected.")
    p.add_argument("--clean", action="store_true",
                   help="force a clean re-run of EVERY targeted block (shorthand "
                        "for --force-blocks <all --blocks>). Wipes their on-disk "
                        "ORFS state + manifest entries so the run starts from "
                        "1_synth. Combine with --blocks to scope it.")
    p.add_argument("--resume-from", default=None, metavar="STAGE",
                   choices=["1_synth", "2_floorplan", "3_place", "4_cts", "5_route", "6_final"],
                   help="implies --resume. Manual override of the automatic "
                        "detection: TRUST every stage before STAGE on disk and "
                        "rebuild STAGE onward (its .odb are deleted for you). "
                        "Use when detection is too conservative, e.g. a killed "
                        "run left odbs of unknown provenance.")
    p.add_argument("--resume-preview", action="store_true",
                   help="READ-ONLY: show what --resume would do for each --blocks "
                        "block (skip / reused stages / rerun-from / files it "
                        "would delete) against --manifest, then exit.")
    p.add_argument("--status", action="store_true",
                   help="print the flow-state manifest (per-block stages done, "
                        "per-stage/final QoR, all run/log/report dirs, and "
                        "run-dirs safe to delete), then exit. Use with --blocks "
                        "to filter.")
    p.add_argument("--gc", action="store_true",
                   help="delete run-dirs the manifest marks as superseded "
                        "(every block in them has a newer run) and exit. "
                        "Prompts unless combined with --yes.")
    p.add_argument("--gc-dry-run", action="store_true",
                   help="list the run-dirs --gc would delete, then exit "
                        "(never removes anything).")
    p.add_argument("--yes", action="store_true",
                   help="answer 'yes' to the --gc deletion prompt (non-interactive).")
    p.add_argument("--skip-stamp-check", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--dry-run", action="store_true",
                   help="print the make commands but do not run anything")
    p.add_argument("--triage-slew", action="store_true",
                   help="READ-ONLY: run asic_flow/scripts/slew_triage.tcl on the "
                        "existing 6_final.odb of each --blocks block, using the "
                        "block's exact build make line (corners, FLOW_VARIANT, "
                        "NUM_CORES). No stamp check, no build, no manifest update. "
                        "Log -> <run-dir>/slew_triage_<block>.log. Combine with "
                        "--dry-run to only print the make command.")
    p.add_argument("--signoff-only", action="store_true",
                   help="READ-ONLY: run sign-off (--signoff-stage rcx|lvs|both) on "
                        "the EXISTING routed .odb of each --blocks block. No stamp "
                        "check, no build, no manifest update. Exit 0 = all ok, "
                        "1 = LVS mismatch/inconclusive or signoff error, "
                        "3 = no routed odb, 4 = skipped (asset/tool absent).")
    p.add_argument("--post-triage-slew", action="store_true",
                   help="after the build finishes, run the same read-only slew "
                        "triage on every block that ended DONE in this run "
                        "(output also appended to <run-dir>/<block>.log). Exit "
                        "code still reflects the build only.")
    p.add_argument("--estimate", action="store_true",
                   help="print the execution plan + rough cost estimate, then exit")
    p.add_argument("--no-llm", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--interactive", action="store_true",
                   help="prompt on the shell before spending an LLM call and "
                        "before applying fixes (only if stdin is a TTY; "
                        "otherwise runs fully autonomously)")
    p.add_argument("--no-git-commit", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--git-push", action="store_true",
                   help="after committing a fix, push the current branch to "
                        "origin (default: off; uses your existing git creds)")
    p.add_argument("--git-sync-branches", nargs="+", metavar="BRANCH",
                   help="publish each committed fix (HEAD) to EVERY named branch "
                        "on origin, keeping mirror branches in lock-step "
                        "(e.g. --git-sync-branches main v5.1_integration). "
                        "Implies push; fast-forward only, never --force.")
    p.add_argument("--reason", action="store_true",
                   help="enable the deterministic 'principal-engineer' reasoning "
                        "layer: on any failure, classify the remedy LAYER "
                        "(RTL/SDC/synth/floorplan/place/CTS/route/tool), compute "
                        "the cross-stage + cross-block blast radius, flag tool "
                        "bugs, and frame the LLM consult accordingly. ADVISORY "
                        "ONLY — it never auto-applies a change; the sole "
                        "deterministic auto-fix remains the macro-fit reshape. "
                        "Prints a real diagnosis even under --no-llm.")
    p.add_argument("--reports-dir", default=None,
                   help="override the ORFS stage-report dir used by the "
                        "cross-stage evidence analyzer (Tier-1). Normally "
                        "auto-detected under $ORFS/flow/reports/...; set this to "
                        "point at a saved reports dir (e.g. to replay a failure).")
    p.add_argument("--no-physical", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-sram-regen", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-abstract", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-post-route-opt", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-debug-dump", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-debug-images", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--drc-handful", type=int, default=20,
                   help="max residual DRC count the post-route DRC-only ECO will "
                        "attempt to clean (default 20). Above this the failure is "
                        "treated as global congestion needing a floorplan/route "
                        "strategy change, not an ECO.")
    # ----- golden sign-off archiving --------------------------------------
    p.add_argument("--no-golden", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--golden-dir", default=None,
                   help="root of the golden sign-off area (default "
                        "asic_flow/golden). Snapshots go to "
                        "<golden-dir>/asap7/<block>/ and OVERWRITE the previous "
                        "snapshot for that block — git history preserves versions.")
    p.add_argument("--golden-wns-margin-ps", type=float, default=0.0,
                   help="setup-WNS uncertainty margin (ps) tolerated when "
                        "deciding a block is golden-converged. Default 0 = "
                        "strict (setup timing must actually be met). Raising it "
                        "lets you archive results 'within the uncertainty "
                        "margin'; the TRUE WNS is always recorded in the "
                        "manifest — the margin never masks or rewrites it.")
    p.add_argument("--golden-hold-margin-ps", type=float, default=0.0,
                   help="hold-slack uncertainty margin (ps) for the golden gate "
                        "(default 0 = strict). Same non-masking guarantee as "
                        "--golden-wns-margin-ps.")
    p.add_argument("--golden-include-heavy", action="store_true",
                   help="also VERSION heavy PD binaries (.gds/.def/.spef/.odb) "
                        "in the golden commit. By default they are copied to "
                        "disk (complete restorable snapshot) but git-ignored to "
                        "protect repo size.")
    # ----- sign-off (parasitic extraction / LVS) --------------------------
    p.add_argument("--no-signoff", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--signoff-stage", choices=["rcx", "lvs", "both"],
                   default="rcx",
                   help="sign-off stage(s) to run (default rcx). 'rcx' is REAL "
                        "and available now: it runs the staged ASAP7 OpenRCX "
                        "model to write a sign-off SPEF distinct from — and "
                        "higher fidelity than — the flow's built-in 6_final.spef "
                        "estimate. 'lvs'/'both' run device-level FinFET LVS if its "
                        "collateral is staged, otherwise INTERCONNECT LVS: routed "
                        "metal of the GDS vs 6_final.v with std cells + macros "
                        "black-boxed (KLayout; DEF/GDS regenerated from the .odb "
                        "at tech units). Never fabricates a result.")
    p.add_argument("--signoff-timeout", type=int, default=1800,
                   help="per-block sign-off wall-clock budget in seconds "
                        "(default 1800). On timeout the block stays DONE.")
    p.add_argument("--llm-interval", type=float, default=3.0,
                   help="min seconds between LLM calls (rate limit, default 3)")
    p.add_argument("--llm-retries", type=int, default=3,
                   help="LLM network retry attempts per call (default 3)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="stream ORFS output and debug detail to the console")
    p.add_argument("--heartbeat", type=float, default=30.0,
                   help="seconds between 'still running' progress updates per "
                        "block (default 30; min 5). Lets you see a long block "
                        "is alive without --verbose. When stdout is a TTY the "
                        "agent renders an in-place live status panel/table "
                        "(no scroll spam); piped output falls back to one "
                        "throttled line per interval.")
    p.add_argument("--progress", choices=["auto", "dashboard", "plain", "quiet"],
                   default="auto",
                   help="console progress style. 'auto' (default) and "
                        "'dashboard' render an in-place live dashboard — a "
                        "per-block status table (status / stage / progress bar / "
                        "elapsed / latest WNS) plus a system CPU+memory row, "
                        "updating in place every ~2-3s — when stdout is a TTY, "
                        "and fall back to the classic scrolling output when "
                        "piped/redirected or under --verbose. 'plain' always "
                        "uses the classic scrolling output. 'quiet' suppresses "
                        "the live dashboard (only errors/warnings scroll; the "
                        "final summary table is still printed).")
    p.add_argument("--tmux", action="store_true",
                   help="launch this agent inside a new DETACHED tmux session "
                        "named janus-agent-<timestamp>, inheriting all other "
                        "args and the current environment, then print the attach "
                        "command and exit immediately. Lets you start a long run "
                        "and detach in one command; attach later with "
                        "`tmux attach -t janus-agent-<timestamp>` to watch the "
                        "live dashboard. Requires tmux on PATH.")

    # ----- run-completion notifications (email + optional Google Drive) ---
    #   OFF by default. See asic_flow/utils/notification.py for full SMTP /
    #   Google-Drive setup. All notification work is best-effort and fail-safe:
    #   a notification error never crashes or alters the ASIC run.
    notif = p.add_argument_group(
        "notifications",
        "email a tarball of logs + key reports (and optionally upload it to "
        "Google Drive) when a block completes or fails, so you don't have to "
        "poll a long EC2 run. SMTP creds may be given here or via the "
        "SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASS env vars (env is safer for "
        "the password).")
    notif.add_argument("--notify-email", default=None, metavar="ADDRESS",
                       help="recipient email address. Enables notifications; "
                            "requires SMTP config (flags below or env vars).")
    notif.add_argument("--smtp-host", default=None, metavar="HOST",
                       help="SMTP server host (or $SMTP_HOST), e.g. "
                            "smtp.gmail.com")
    notif.add_argument("--smtp-port", type=int, default=None, metavar="PORT",
                       help="SMTP port (or $SMTP_PORT, default 587). 587 uses "
                            "STARTTLS; 465 uses implicit SSL.")
    notif.add_argument("--smtp-user", default=None, metavar="USER",
                       help="SMTP username / from-address (or $SMTP_USER).")
    notif.add_argument("--smtp-pass", default=None, metavar="PASS",
                       help="SMTP password (or $SMTP_PASS). For Gmail use a "
                            "16-char App Password, NOT your login password. "
                            "Prefer the env var so it stays out of shell "
                            "history / ps.")
    notif.add_argument("--gdrive-upload", action="store_true",
                       help="also upload each block's bundle to Google Drive "
                            "at janus_ec2_runs/<block>/ (root must already "
                            "exist and be shared with the credential; "
                            "$JANUS_GDRIVE_ROOT / $JANUS_GDRIVE_ROOT_ID "
                            "override it). "
                            "Needs google-api-python-client + a credentials "
                            "file at ~/.config/janus_gdrive_credentials.json "
                            "(or --gdrive-creds / $JANUS_GDRIVE_CREDENTIALS); "
                            "gracefully skipped if either is missing.")
    notif.add_argument("--gdrive-creds", default=None, metavar="PATH",
                       help="path to the Google Drive credentials json "
                            "(service account preferred for headless runs). "
                            "Default: ~/.config/janus_gdrive_credentials.json "
                            "or $JANUS_GDRIVE_CREDENTIALS.")

    # ----- live web dashboard --------------------------------------------
    dash = p.add_argument_group(
        "live dashboard",
        "an EDA-style browsable web dashboard: live per-block status, "
        "click a block to see its real log / reports / results (with their "
        "on-disk paths) and a scale floorplan snapshot of the implemented "
        "design. Zero external deps; safe to leave running to browse a "
        "finished run.")
    dash.add_argument("--dashboard", action="store_true",
                      help="start the live web dashboard for this run and print "
                           "its URL. Also usable standalone on any finished "
                           "run: python3 dashboard.py --run-dir <run> "
                           "--flow-dir <orfs>/flow")
    dash.add_argument("--dashboard-port", type=int, default=8080,
                      help="port for the dashboard (default 8080; 0 = let the "
                           "OS pick a free port, printed at startup)")
    dash.add_argument("--dashboard-host", type=str, default="0.0.0.0",
                      help="host/IP to bind the dashboard server (default 0.0.0.0 "
                           "= all interfaces/LAN-reachable; use 127.0.0.1 for "
                           "localhost-only)")

    # ----- consolidated stage toggles ------------------------------------
    #   ONE flag replaces the individual --no-* switches. Those remain
    #   accepted as hidden aliases (scripts/docs keep working) but are no
    #   longer listed in --help. Effects are OR-ed with any legacy --no-*.
    p.add_argument("--no-signoff-sta", action="store_true", help=argparse.SUPPRESS)
    skip = p.add_argument_group(
        "stage toggles (--skip)",
        "tokens (legacy --no-* aliases still accepted, hidden from --help):\n"
        + "\n".join(f"  {k:<15} {v[1]}  [{_flag(v[0])}]"
                    for k, v in _SKIP_MAP.items()))
    skip.add_argument("--skip", action="extend", nargs="+", default=None,
                      metavar="TOKEN", choices=list(_SKIP_MAP),
                      help="disable optional stages/behaviours; repeatable "
                           "(`--skip sram abstract` == `--skip sram --skip "
                           "abstract`). Tokens listed above.")

    # ----- adaptive closure (§126) ----------------------------------------
    adapt = p.add_argument_group(
        "adaptive closure",
        textwrap.fill(
        "after a block reaches DONE, classify its QoR (EASY / MODERATE / "
        "CLOCK_SKEW_LIMITED / IO_BUDGET_LIMITED / CONGESTION_LIMITED / "
        "LOGIC_DEPTH_LIMITED / DRV_LIMITED) and recommend — or, with auto, "
        "run — a bounded extra pass from 3_place in a NEW FLOW_VARIANT leaf "
        "(<variant>_adaptN_<class>). Protected blocks never rerun. If a pass "
        "improves QoR with no signoff regression (setup/hold into violation, "
        "more DRC or slew) it is PROMOTED: it becomes the block's result "
        "(--status, reports, dependents, email, Drive) and the old leaf is "
        "kept as <leaf>_superseded_<ts>. The block is marked DONE and emailed "
        "only after its passes finish.", 78))
    adapt.add_argument("--adaptive", choices=["off", "advise", "auto"],
                       default="advise",
                       help="off = no classification; advise (default) = "
                            "classify + write the recommendation only; auto = "
                            "also run up to --adaptive-max-passes variant "
                            "reruns (CTS latency overlay for "
                            "CLOCK_SKEW_LIMITED, knob tweaks otherwise).")
    adapt.add_argument("--adaptive-max-passes", type=int, default=None,
                       metavar="N",
                       help="cap on extra passes per block under --adaptive "
                            f"auto (default 1, max {_ADAPTIVE_MAX_PASSES_CAP}). "
                            "Env JANUS_ADAPTIVE_TIMEOUT_S bounds each pass "
                            "in seconds (default 259200 = 72 h; 0 = no limit).")
    adapt.add_argument("--adaptive-thresholds", default=None,
                       metavar="FILE|JSON",
                       help="override classifier thresholds with a JSON object "
                            "(inline or a file path); keys must be from "
                            "adaptive_closure.DEFAULT_THRESHOLDS.")
    p.add_argument("--debug-on-fail", action="store_true",
                   help="on a block FAILURE, rerun hooks/janus_debug_rerun.tcl "
                        "on the last good ODB to dump debug timing reports "
                        "for the failing region (same as env "
                        "JANUS_DEBUG_ON_FAIL=1; JANUS_DEBUG_TIMEOUT_S bounds it).")
    return p


def _manifest_only_cli(args: argparse.Namespace) -> Optional[int]:
    """Handle the manifest-only sub-commands (--status / --gc / --gc-dry-run)
    WITHOUT constructing the full agent (which needs ORFS_DIR etc). Returns an
    exit code if it handled the invocation, else None."""
    if not (args.status or args.gc or args.gc_dry_run
            or getattr(args, "resume_preview", False)):
        return None
    mpath = args.manifest or FlowState.default_path(_ROOT)
    fs = FlowState(mpath).load()
    if getattr(args, "resume_preview", False):
        return _resume_preview(args, fs, mpath)
    if args.status:
        print(fs.render_status(args.blocks or None))
        return 0
    # --gc / --gc-dry-run
    cands = fs.gc_candidates()
    if not cands:
        print(f"flow-state manifest: {mpath}\n"
              "cleanup: no superseded run-dirs to delete.")
        return 0
    print(f"flow-state manifest: {mpath}")
    print(f"{len(cands)} run-dir(s) superseded and safe to delete:")
    for c in cands:
        print(f"   {c['run_dir']}  (blocks: {', '.join(c['blocks'])})")
    if args.gc_dry_run:
        print("\n(dry run — nothing removed)")
        return 0
    if not args.yes:
        try:
            ans = input("\ndelete these run-dirs? [y/N] ").strip().lower()
        except EOFError:
            ans = "n"
        if ans not in ("y", "yes"):
            print("aborted — nothing removed.")
            return 0
    import shutil
    removed = 0
    for c in cands:
        rd = c["run_dir"]
        try:
            if rd and os.path.isdir(rd):
                shutil.rmtree(rd)
                removed += 1
                print(f"   removed {rd}")
        except OSError as e:
            print(f"   FAILED to remove {rd}: {e}", file=sys.stderr)
    print(f"done — removed {removed}/{len(cands)} run-dir(s).")
    return 0


def _resume_preview(args: argparse.Namespace, fs: FlowState, mpath: str) -> int:
    """--resume-preview: print what `--resume` WOULD do per --blocks block
    (skip / reuse which stages / rerun from where / which files it would
    delete). Pure read: nothing is touched or removed."""
    orfs = (args.orfs or os.environ.get("ORFS_DIR", "")
            or os.path.join(_ROOT, "OpenROAD-flow-scripts"))
    flow = os.path.join(orfs, "flow")
    leaf = getattr(args, "variant", None) or "base"
    if not args.blocks:
        print("--resume-preview needs --blocks", file=sys.stderr)
        return 2
    print(f"flow-state manifest: {mpath}\nORFS flow dir: {flow}")
    for b in args.blocks:
        res = os.path.join(flow, "results", "asap7", b, leaf)
        cfg = os.path.join(flow, "designs", "asap7", "janus", b, "config.mk")
        sdc = os.path.join(flow, "designs", "asap7", "janus", b, "constraint.sdc")
        rec = fs.state.get("blocks", {}).get(b, {})
        print(f"\n[{b}] manifest status={rec.get('status', 'PENDING')}  "
              f"config snapshot={'yes' if rec.get('config_vars') is not None else 'NO'}")
        print(f"   results: {res}{'' if os.path.isdir(res) else '  (MISSING)'}")
        if not os.path.isfile(cfg):
            print(f"   stamped config missing: {cfg} — run scripts/03 first")
            continue
        ff = getattr(args, "resume_from", None)
        plan = fs.resume_plan(b, res, config_path=cfg, sdc_path=sdc, force_from=ff)
        for n in plan.notes:
            print(f"   note         : {n}")
        if fs.is_done(b) and not (plan.rerun_from and (plan.config_changed or plan.remove_paths or ff)):
            print("   -> SKIP (DONE, no config/sdc change)")
            continue
        print(f"   changed keys : {', '.join(plan.changed_keys) or 'none'}")
        print(f"   reuse        : {', '.join(plan.reuse) or 'nothing'}")
        print(f"   rerun from   : {'1_synth (FULL REBUILD)' if plan.full_rebuild else plan.rerun_from or 'finish'}")
        rm = plan.remove_paths + (plan.touch_paths if plan.full_rebuild else [])
        print(f"   --resume deletes automatically: "
              f"{', '.join(os.path.basename(x) for x in rm) or 'nothing'}")
        print(f"   reason       : {plan.reason}")
    print("\n(preview only — nothing touched)")
    return 0


# ---------------------------------------------------------------------------
# Parallel-variant fan-out (PARENT coordinator)
# ---------------------------------------------------------------------------
#  Multi-value flags are greedy (nargs='+'/'*'): consume every following token
#  until the next option (a token starting with '-').
#  NOTE: --blocks is deliberately NOT stripped — every child must inherit the
#  same block list.  Only parent-only greedy flags belong here.
_VARIANT_PARENT_GREEDY = {"--variants", "--extra-make"}
#  Single-value flags we rewrite per child (strip from the inherited argv so the
#  child gets the per-leg value we append instead).
_VARIANT_PARENT_ONEVAL = {
    "--variants-reserve-cores", "--num-cores", "--run-dir", "--variant",
}


def _strip_argv(argv: List[str], greedy: set, oneval: set) -> List[str]:
    """Return argv with the named options (and their values) removed.

    Handles both `--flag value` and `--flag=value` forms.  Greedy options
    consume every following non-option token; one-value options consume exactly
    one following token.
    """
    out: List[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        key = tok.split("=", 1)[0]
        if key in greedy or key in oneval:
            if "=" in tok:            # --flag=value : single token, just drop it
                i += 1
                continue
            i += 1                    # drop the flag itself
            if key in oneval:
                if i < n:
                    i += 1            # drop its single value
            else:                     # greedy: drop until the next option token
                while i < n and not argv[i].startswith("-"):
                    i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _parse_variant_spec(spec: str) -> (str, List[str]):
    """`TAG[:VAR=VAL,VAR=VAL,...]` -> (tag, ['VAR=VAL', ...])."""
    if ":" in spec:
        tag, rest = spec.split(":", 1)
        defines = [kv.strip() for kv in rest.split(",") if kv.strip()]
    else:
        tag, defines = spec, []
    tag = tag.strip()
    if not tag:
        raise ValueError(f"empty variant tag in spec {spec!r}")
    return tag, defines


def _run_variants_parent(args: argparse.Namespace, argv: Optional[List[str]]) -> int:
    """Fan out one parallel CHILD agent run per --variants spec on ONE box.

    Each child is a normal single-run of the SAME --blocks, but with its own
    ORFS FLOW_VARIANT leaf (=TAG) and lever defines (--extra-make), its own
    run-dir, and an even share of the --num-cores budget.  The whole per-run
    pipeline (stage-resume, QoR parse, golden check, notify) is reused verbatim;
    this coordinator only spawns + joins the children and aggregates exit codes.
    """
    import shlex
    import threading

    base_argv = list(argv) if argv is not None else list(sys.argv[1:])
    child_base = _strip_argv(base_argv, _VARIANT_PARENT_GREEDY, _VARIANT_PARENT_ONEVAL)

    # Parse + validate all specs up front.
    specs = []
    tags_seen = set()
    for raw in args.variants:
        tag, defines = _parse_variant_spec(raw)
        if tag in tags_seen:
            print(f"error: duplicate variant tag {tag!r}", file=sys.stderr)
            return 2
        tags_seen.add(tag)
        specs.append((tag, defines))

    # Resolve + split the core budget.
    total_cores = args.num_cores
    if total_cores is None:
        try:
            total_cores = int(subprocess.check_output(["nproc"], text=True).strip())
        except (subprocess.SubprocessError, ValueError, FileNotFoundError):
            total_cores = 8
    reserve = max(0, args.variants_reserve_cores)
    usable = max(1, total_cores - reserve)
    per_child = max(1, usable // len(specs))

    # Each child needs its own run-dir so logs/manifests never collide.
    parent_run_dir = args.run_dir or os.path.join(
        os.environ.get("JANUS_ASIC_HOME", os.path.expanduser("~/janus_asic")),
        "variants_" + datetime.now().strftime("%Y%m%d_%H%M%S"),
    )

    # Build the child command lines.
    children = []  # (tag, cmd:list[str], run_dir, results_hint)
    for tag, defines in specs:
        rd = os.path.join(parent_run_dir, tag)
        cmd = [sys.executable, os.path.abspath(__file__)] + child_base + [
            "--variant", tag,
            "--num-cores", str(per_child),
            "--run-dir", rd,
        ]
        if defines:
            cmd += ["--extra-make", *defines]
        results_hint = f"results/asap7/user_core_ara/{tag}/"
        children.append((tag, cmd, rd, results_hint))

    # Header / dry-run preview.
    print("=" * 72)
    print(f"PARALLEL VARIANT CAMPAIGN  ({len(specs)} variants, one box)")
    print(f"  total cores : {total_cores}   reserve: {reserve}   "
          f"per-child: {per_child}")
    print(f"  parent run-dir: {parent_run_dir}")
    for tag, cmd, rd, hint in children:
        print("-" * 72)
        print(f"  [{tag}]  ->  {hint}")
        print(f"      {shlex.join(cmd)}")
    print("=" * 72)

    if args.dry_run:
        print("(dry run — no child agents launched)")
        return 0

    os.makedirs(parent_run_dir, exist_ok=True)

    # Launch all children in parallel, each with a prefixed log tee.
    procs = []  # (tag, Popen, logfile handle)
    for tag, cmd, rd, _hint in children:
        os.makedirs(rd, exist_ok=True)
        logpath = os.path.join(rd, "variant_child.log")
        lf = open(logpath, "w")
        p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
        procs.append((tag, p, lf, logpath))
        print(f"launched [{tag}] pid={p.pid}  (log: {logpath})")

    # Optional: live-prefix tail of each child's log to the parent stdout so a
    # single attached terminal shows interleaved progress.
    stop = threading.Event()

    def _tail(tag, path):
        last = 0
        while not stop.is_set():
            try:
                with open(path) as fh:
                    fh.seek(last)
                    for line in fh:
                        sys.stdout.write(f"[{tag}] {line}")
                    last = fh.tell()
            except OSError:
                pass
            stop.wait(2.0)

    tails = [threading.Thread(target=_tail, args=(tag, path), daemon=True)
             for tag, _p, _lf, path in procs]
    for t in tails:
        t.start()

    results = {}
    for tag, p, lf, _path in procs:
        rc = p.wait()
        lf.close()
        results[tag] = rc
    stop.set()
    for t in tails:
        t.join(timeout=3.0)

    # Summary table.
    print("\n" + "=" * 72)
    print("VARIANT CAMPAIGN SUMMARY")
    worst = 0
    for tag, _cmd, _rd, hint in children:
        rc = results.get(tag, 1)
        worst = worst or rc
        status = "OK " if rc == 0 else f"FAIL({rc})"
        print(f"  {status:9s} [{tag}]  ->  {hint}")
    print("=" * 72)
    return 0 if worst == 0 else 1


def _relaunch_in_tmux(argv: Optional[List[str]]) -> int:
    """Re-exec this agent in a new DETACHED tmux session and exit.

    The child inherits every original arg EXCEPT --tmux (dropped to avoid an
    infinite re-launch loop) and the current environment (tmux passes the
    caller's env to the new session). The parent prints the attach command and
    returns immediately so the terminal is freed.
    """
    import shlex
    import shutil as _sh
    if _sh.which("tmux") is None:
        print("error: --tmux requested but 'tmux' is not installed / not on "
              "PATH. Install tmux or drop --tmux.", file=sys.stderr)
        return 2
    base_argv = list(argv) if argv is not None else list(sys.argv[1:])
    child_argv = [a for a in base_argv if a != "--tmux"]
    session = "janus-agent-" + datetime.now().strftime("%Y%m%d_%H%M%S")
    inner = [sys.executable, os.path.abspath(__file__)] + child_argv
    # §136: if a tmux server is ALREADY running, new-session takes the
    # server's global env, not ours, so `export SMTP_*` done after that server
    # started was silently lost (-> "no email"). Forward those vars with -e.
    fwd = []
    for k, v in os.environ.items():
        if k.startswith(("SMTP_", "NOTIFY_", "JANUS_")):
            fwd += ["-e", f"{k}={v}"]
    cmd = ["tmux", "new-session", "-d", "-s", session] + fwd + [shlex.join(inner)]
    try:
        try:
            subprocess.run(cmd, check=True, env=os.environ.copy(),
                           stderr=subprocess.DEVNULL if fwd else None)
        except subprocess.CalledProcessError:
            if not fwd:
                raise
            # tmux < 3.0 has no -e: launch anyway, but say what is at risk.
            print("warning: this tmux does not support -e; SMTP_*/NOTIFY_* may "
                  "not reach the session if a tmux server was already running. "
                  "Pass --smtp-host/--smtp-user/--smtp-pass instead.",
                  file=sys.stderr)
            subprocess.run(["tmux", "new-session", "-d", "-s", session,
                            shlex.join(inner)], check=True, env=os.environ.copy())
    except (subprocess.SubprocessError, OSError) as e:
        print(f"error: failed to launch tmux session {session!r}: {e}",
              file=sys.stderr)
        return 2
    print(f"Launched in tmux session: {session}")
    print(f"Attach with: tmux attach -t {session}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_skips(args)
    errs, warns = _validate_args(args)
    for w in warns:
        print(f"warning: {w}", file=sys.stderr)
    if errs:
        for e in errs:
            print(f"error: {e}", file=sys.stderr)
        print("(see --help)", file=sys.stderr)
        return 2
    if getattr(args, "resume_from", None):
        args.resume = True
    # --tmux: re-exec detached in a tmux session (inheriting all other args +
    # env) and exit immediately. Done BEFORE any heavy work so nothing runs
    # twice. The child (inside tmux) gets the live dashboard when it has a TTY.
    if getattr(args, "tmux", False):
        return _relaunch_in_tmux(argv)
    if args.blocks:
        bad = [b for b in args.blocks if b not in DEFAULT_GRAPH]
        if bad:
            print(f"error: unknown block(s): {', '.join(bad)}\n"
                  f"valid: {', '.join(DEFAULT_GRAPH)}", file=sys.stderr)
            return 2
    rc = _manifest_only_cli(args)
    if rc is not None:
        return rc
    # PARENT fan-out: --variants launches one parallel child agent per spec.
    if getattr(args, "variants", None):
        return _run_variants_parent(args, argv)
    agent = JANUSAutonomousAgent(args)
    return agent.run()


if __name__ == "__main__":
    raise SystemExit(main())
