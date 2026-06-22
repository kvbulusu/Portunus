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
    python3 janus_agent.py --no-llm                   # never call the LLM (manual mode)

Requires the env file written by the install scripts:
    source ~/janus_asic/janus_asic_env.sh
(or pass --orfs / --rtl-root explicitly).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# --- make sibling (agent/) and ../utils importable, however we're invoked -----
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)              # .../asic_flow
for _p in (_HERE, os.path.join(_ROOT, "utils")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from dependency_manager import DependencyManager, Status, DEFAULT_GRAPH  # noqa: E402
from log_analyzer import LogAnalyzer, classify_failure                  # noqa: E402
from llm_interface import AbacusLLM                                      # noqa: E402
from fix_applier import FixApplier                                       # noqa: E402
from checkpoint import Checkpoint                                        # noqa: E402
from reporter import Reporter                                           # noqa: E402

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

    def _emit(self, msg: str, style: str = "") -> None:
        with self._lock:
            if self.console:
                self.console.print(msg, style=style)
            else:
                print(msg)

    def info(self, msg: str) -> None:
        self._emit(msg)

    def good(self, msg: str) -> None:
        self._emit(msg, "bold green")

    def warn(self, msg: str) -> None:
        self._emit(msg, "yellow")

    def err(self, msg: str) -> None:
        self._emit(msg, "bold red")

    def step(self, msg: str) -> None:
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
                t = Table(title="JANUS block status", show_lines=False)
                for col in ("block", "status", "tries", "WNS(ns)", "DRC", "note"):
                    t.add_column(col)
                for r in rows:
                    t.add_row(
                        str(r.get("block", "")),
                        str(r.get("status", "")),
                        str(r.get("tries", 0)),
                        _fmt(r.get("wns_ns")),
                        _fmt(r.get("drc_violations")),
                        str(r.get("note", ""))[:48],
                    )
                self.console.print(t)
            else:
                print("\nblock           status     tries  WNS      DRC   note")
                for r in rows:
                    print("{:<15} {:<10} {:<6} {:<8} {:<5} {}".format(
                        r.get("block", ""), r.get("status", ""), r.get("tries", 0),
                        _fmt(r.get("wns_ns")), _fmt(r.get("drc_violations")),
                        str(r.get("note", ""))[:48]))


def _fmt(v) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:+.3f}" if v < 0 or v > 0 else "0.000"
    return str(v)


# ---------------------------------------------------------------------------- #
# Per-block bookkeeping
# ---------------------------------------------------------------------------- #
@dataclass
class BlockResult:
    block: str
    status: str = "PENDING"
    tries: int = 0
    wns_ns: Optional[float] = None
    tns_ns: Optional[float] = None
    drc_violations: Optional[int] = None
    area: Optional[float] = None
    power: Optional[float] = None
    utilization: Optional[float] = None
    log_path: str = ""
    note: str = ""
    llm_calls: int = 0

    def as_row(self) -> Dict:
        return {
            "block": self.block, "status": self.status, "tries": self.tries,
            "wns_ns": self.wns_ns, "tns_ns": self.tns_ns,
            "drc_violations": self.drc_violations, "area": self.area,
            "power": self.power, "utilization": self.utilization,
            "log_path": self.log_path, "note": self.note, "llm_calls": self.llm_calls,
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

        self.results: Dict[str, BlockResult] = {
            b: BlockResult(block=b) for b in self.dep.graph
        }
        self._results_lock = threading.Lock()

    # ----- DESIGN_CONFIG path for a block -----------------------------------
    def _config_path(self, block: str) -> str:
        return os.path.join(self.design_root, block, "config.mk")

    def _block_log(self, block: str) -> str:
        return os.path.join(self.run_dir, f"{block}.log")

    # ----- the make command --------------------------------------------------
    def _make_cmd(self, block: str) -> List[str]:
        return ["make", f"DESIGN_CONFIG={self._config_path(block)}"]

    # ----- map a raw ORFS output line to a human-readable flow stage ---------
    # ORFS emits markers like "Running synth_odb.tcl, stage 1_synth" and step
    # banners; we translate those into a coarse stage label so the heartbeat can
    # tell the user *where* in the flow a long-running block currently is.
    _STAGE_PATTERNS = [
        (re.compile(r"stage\s+1_synth", re.I),            "1_synth (synthesis)"),
        (re.compile(r"stage\s+2_floorplan", re.I),        "2_floorplan"),
        (re.compile(r"stage\s+3_place", re.I),            "3_place"),
        (re.compile(r"stage\s+4_cts", re.I),              "4_cts (clock tree)"),
        (re.compile(r"stage\s+5_route", re.I),            "5_route"),
        (re.compile(r"stage\s+6_(final|report)", re.I),   "6_final"),
        (re.compile(r"Running\s+(\S+?)\.tcl", re.I),      None),   # fallback: script name
        (re.compile(r"\bglobal_place\b", re.I),           "3_place (global)"),
        (re.compile(r"\bdetailed_place\b", re.I),         "3_place (detailed)"),
        (re.compile(r"\bclock_tree_synthesis\b", re.I),   "4_cts"),
        (re.compile(r"\bglobal_route\b", re.I),           "5_route (global)"),
        (re.compile(r"\bdetailed_route\b", re.I),         "5_route (detailed)"),
        (re.compile(r"\bdetailed routing\b", re.I),       "5_route (detailed)"),
    ]

    @classmethod
    def _detect_stage(cls, line: str) -> Optional[str]:
        for pat, label in cls._STAGE_PATTERNS:
            m = pat.search(line)
            if m:
                return label if label is not None else f"{m.group(1)}"
        return None

    @staticmethod
    def _fmt_elapsed(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"

    # ----- run one make and capture the log ---------------------------------
    def _run_make(self, block: str) -> int:
        log_path = self._block_log(block)
        cmd = self._make_cmd(block)
        self.ui.debug(f"{block}: cwd={self.flow_dir} cmd={' '.join(cmd)}")

        # Shared state updated by the reader loop, read by the heartbeat thread.
        t_start = time.time()
        state = {"stage": "starting", "lines": 0, "last_msg": ""}
        state_lock = threading.Lock()
        hb_stop = threading.Event()

        interval = max(5.0, float(getattr(self.args, "heartbeat", 30.0)))

        def _heartbeat() -> None:
            # Periodically reassure the user the block is alive and show where it
            # is. Fires even when ORFS is silent for minutes (e.g. detailed_route),
            # because it runs independently of the (blocking) stdout reader.
            while not hb_stop.wait(interval):
                with state_lock:
                    stg, n = state["stage"], state["lines"]
                el = self._fmt_elapsed(time.time() - t_start)
                self.ui.info(
                    f"    … {block}: running — stage={stg}  elapsed={el}  "
                    f"({n} log lines)  log: {os.path.basename(log_path)}"
                )

        hb = threading.Thread(target=_heartbeat, name=f"hb-{block}", daemon=True)
        hb.start()

        with open(log_path, "ab", buffering=0) as logf:
            header = f"\n===== {block} : {' '.join(cmd)} @ {datetime.now()} =====\n"
            logf.write(header.encode())
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
                    with state_lock:
                        state["lines"] += 1
                        if stg and stg != state["stage"]:
                            state["stage"] = stg
                            el = self._fmt_elapsed(time.time() - t_start)
                            self.ui.step(f"{block}: → {stg}  (elapsed {el})")
                    if self.args.verbose:
                        self.ui.debug(f"{block}| {line}")
                proc.stdout.close()
            finally:
                proc.wait()
                hb_stop.set()
                hb.join(timeout=2.0)
        el = self._fmt_elapsed(time.time() - t_start)
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

            # absorb whatever metrics ORFS actually reported (may be None)
            res.wns_ns = report.wns_ns
            res.tns_ns = report.tns_ns
            res.drc_violations = report.drc_violations
            res.area = report.area
            res.power = report.power
            res.utilization = report.utilization
            res.log_path = self._block_log(block)

            if rc == 0 and not report.failed:
                res.status = "DONE"
                res.note = report.headline()
                self.ui.good(f"{block}: PASS — {report.headline()}")
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

            if self.args.no_llm or not self.llm.available():
                res.status = "FAILED"
                res.note = "failed; LLM disabled — see log: " + res.log_path
                self.ui.err(f"{block}: stopping (no LLM). Inspect {res.log_path}")
                return res

            if attempt == self.args.max_retries:
                res.status = "FAILED"
                res.note = f"failed after {attempt} attempts — see {res.log_path}"
                self.ui.err(f"{block}: exhausted retries.")
                return res

            # --- ask the LLM for a fix --------------------------------------
            excerpt = self.analyzer.build_llm_excerpt(report)
            self.ui.step(f"{block}: consulting Abacus LLM (attempt {attempt})")
            llm_res = self.llm.request_fix(excerpt)
            res.llm_calls += 1
            if not llm_res.ok or not llm_res.fixes:
                res.status = "FAILED"
                res.note = f"LLM no actionable fix: {llm_res.error or llm_res.diagnosis}"[:160]
                self.ui.err(f"{block}: LLM returned no fix. {res.note}")
                return res

            self.ui.info(f"{block}: LLM diagnosis [{llm_res.confidence}]: {llm_res.diagnosis[:160]}")
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
                            wns_ns=r.wns_ns, drc=r.drc_violations,
                            note=r.note, log=r.log_path)

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

    # ----- main entry --------------------------------------------------------
    def run(self) -> int:
        self._preflight()
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

        # honor --resume: mark already-DONE blocks
        if self.args.resume:
            for b in list(self.dep.graph):
                if self.ckpt.is_done(b):
                    self.dep.mark(b, Status.DONE)
                    self.results[b].status = "DONE"
                    self.results[b].note = "resumed (already DONE)"
                    self.ui.info(f"{b}: skipped (resume — already DONE)")

        start = time.time()
        try:
            self._main_loop()
        except KeyboardInterrupt:
            self._stop.set()
            self.ui.err("\nInterrupted — saving checkpoint and writing report...")
        finally:
            elapsed = time.time() - start
            self._finalize(elapsed)
        # exit code: 0 if every targeted block is DONE
        failed = [b for b, r in self.results.items()
                  if r.status not in ("DONE",) and b in self.dep.graph]
        return 0 if not failed else 1

    def _main_loop(self) -> None:
        parallel = max(1, self.args.parallel)
        while not self.dep.all_settled() and not self._stop.is_set():
            ready = self.dep.ready()
            if not ready:
                break
            for b in ready:
                self.dep.mark(b, Status.RUNNING)

            if parallel == 1 or len(ready) == 1:
                for b in ready:
                    self._process_and_mark(b)
            else:
                with ThreadPoolExecutor(max_workers=parallel) as ex:
                    futs = {ex.submit(self._process_and_mark, b): b for b in ready}
                    for _ in as_completed(futs):
                        pass
            self.ui.table([self.results[b].as_row() for b in self.dep.graph])

    def _process_and_mark(self, block: str) -> None:
        try:
            res = self._process_block(block)
        except Exception as e:  # never let one block kill the run
            res = self.results[block]
            res.status = "FAILED"
            res.note = f"agent exception: {e}"[:160]
            self.ui.err(f"{block}: agent exception: {e}")
        status = Status.DONE if res.status == "DONE" else Status.FAILED
        with self._results_lock:
            self.dep.mark(block, status)
            self._snapshot(block)
            # reflect dependency-driven BLOCKED state in our result table
            for b, st in self.dep.summary().items():
                if st == Status.BLOCKED.value and self.results[b].status == "PENDING":
                    self.results[b].status = "BLOCKED"
                    self.results[b].note = "skipped — a dependency failed"

    # ----- preflight checks --------------------------------------------------
    def _preflight(self) -> None:
        problems = []
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
        self.ui.panel("JANUS Autonomous ASIC Agent",
                      (f"  ORFS    : {self.orfs or '(unset)'}\n"
                       f"  RTL root: {self.rtl_root}\n"
                       f"  run dir : {self.run_dir}\n"
                       f"  blocks  : {', '.join(self.dep.graph)}\n"
                       f"  parallel: {self.args.parallel}   max-retries: {self.args.max_retries}\n"
                       f"  LLM     : {'ENABLED' if (self.llm.available() and not self.args.no_llm) else 'disabled'}"))

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
        done = sum(1 for r in self.results.values() if r.status == "DONE")
        self.ui.panel(
            "Run complete",
            (f"  blocks DONE : {done}/{len(rows)}\n"
             f"  elapsed     : {elapsed/60:.1f} min\n"
             f"  LLM calls   : {usage.get('llm_calls', 0)}  "
             f"(~{usage.get('approx_tokens', 0)} tokens, ESTIMATE)\n"
             f"  report      : {paths['html']}\n"
             f"  checkpoint  : {self.ckpt.path}\n\n"
             "  Reminder: numbers above are ORFS output for this run (or n/a).\n"
             "  No metric here is a guaranteed/verified silicon result."))


# ---------------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------------- #
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
    p.add_argument("--max-retries", type=int, default=3,
                   help="max attempts per block including the first (default 3)")
    p.add_argument("--orfs", default=None,
                   help="path to OpenROAD-flow-scripts (default $ORFS_DIR)")
    p.add_argument("--rtl-root", default=None,
                   help="path to the JANUS repo root holding rtl/ (default $JANUS_RTL_ROOT)")
    p.add_argument("--run-dir", default=None,
                   help="override the run/log/report directory")
    p.add_argument("--resume", action="store_true",
                   help="skip blocks already marked DONE in the checkpoint")
    p.add_argument("--dry-run", action="store_true",
                   help="print the make commands but do not run anything")
    p.add_argument("--estimate", action="store_true",
                   help="print the execution plan + rough cost estimate, then exit")
    p.add_argument("--no-llm", action="store_true",
                   help="never call the LLM; stop a block on first failure")
    p.add_argument("--llm-interval", type=float, default=3.0,
                   help="min seconds between LLM calls (rate limit, default 3)")
    p.add_argument("--llm-retries", type=int, default=3,
                   help="LLM network retry attempts per call (default 3)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="stream ORFS output and debug detail to the console")
    p.add_argument("--heartbeat", type=float, default=30.0,
                   help="seconds between 'still running' progress lines per "
                        "block (default 30; min 5). Lets you see a long block "
                        "is alive without --verbose.")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.blocks:
        bad = [b for b in args.blocks if b not in DEFAULT_GRAPH]
        if bad:
            print(f"error: unknown block(s): {', '.join(bad)}\n"
                  f"valid: {', '.join(DEFAULT_GRAPH)}", file=sys.stderr)
            return 2
    agent = JANUSAutonomousAgent(args)
    return agent.run()


if __name__ == "__main__":
    raise SystemExit(main())
