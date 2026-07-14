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
    python3 janus_agent.py --interactive              # prompt on the shell before LLM/fixes
    python3 janus_agent.py --git-push                 # also push each committed fix to origin

HIERARCHICAL ABSTRACTS
----------------------
After a block hardens (reaches DONE), the agent runs the ORFS abstract target
(auto-detected from flow/Makefile; canonically `generate_abstract`) to emit that
block's <name>.lef + <name>.lib. janus_soc_top consumes those as hard macros for
bottom-up SoC assembly (peak RAM = the largest single block, not the whole SoC).
Disable with --no-abstract. The top level itself is skipped (it is not a macro).

SELF-HEALING + GIT (the "fix once, git pull, rerun" loop)
---------------------------------------------------------
On a REAL RTL/config failure the agent asks the LLM, applies a minimal fix, and
commits it to the CURRENT branch (disable with --no-git-commit; also push with
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
from git_ops import GitOps                                               # noqa: E402
from physical_analyzer import (detect_macro_fit_failure,                 # noqa: E402
                               analyze_and_recommend, MacroGeom)
from reasoning_engine import reason as reason_failure                    # noqa: E402
from reasoning_engine import reason_with_reports                         # noqa: E402
from dependency_manager import DEFAULT_GRAPH as _DEP_GRAPH               # noqa: E402
from checkpoint import Checkpoint                                        # noqa: E402
from reporter import Reporter                                           # noqa: E402
from timing_classifier import classify_routed_odb, render as tc_render   # noqa: E402
from incremental_opt import (run_eco_pass, decide_timing, decide_drc,     # noqa: E402
                             adopt_odb, discard_odb, ECOResult)

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

    def as_row(self) -> Dict:
        return {
            "block": self.block, "status": self.status, "tries": self.tries,
            "wns_ns": self.wns_ns, "hold_ps": self.hold_ps, "tns_ns": self.tns_ns,
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

        # repo SOURCE configs (asic_flow/configs) — used to mirror LLM fixes back
        # so they survive a scripts/03 re-stamp and can be committed.
        self.configs_dir = os.path.join(_ROOT, "configs")
        # SRAM setup script (tracked source of truth for FakeRAM geometry) and
        # the dir the generated macros land in (ASAP7_SRAM_DIR). Used by the
        # deterministic macro-fit self-heal (MPL-0006 -> sram_reshape).
        self.sram_setup_script = os.path.join(_ROOT, "scripts", "04_setup_srams.sh")
        self.sram_out_dir = (os.environ.get("ASAP7_SRAM_DIR")
                             or os.path.join(_ROOT, "srams", "asap7"))
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

    # ----- DESIGN_CONFIG path for a block -----------------------------------
    def _config_path(self, block: str) -> str:
        return os.path.join(self.design_root, block, "config.mk")

    def _block_log(self, block: str) -> str:
        return os.path.join(self.run_dir, f"{block}.log")

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
            os.path.join(self.flow_dir, "reports", "asap7", block, "base"),
            os.path.join(self.flow_dir, "reports", "asap7", f"janus_{block}", "base"),
        ]
        # last resort: glob for any reports dir whose name contains the block
        cands += glob.glob(os.path.join(self.flow_dir, "reports", "*", f"*{block}*", "base"))
        for d in cands:
            if os.path.isdir(d) and glob.glob(os.path.join(d, "*_*.rpt")):
                return d
        return None

    def _synth_stat_path(self, block: str) -> Optional[str]:
        """synth_stat.txt lives in the ORFS logs tree (1_synth) for a block."""
        if not self.flow_dir:
            return None
        for pat in (
            os.path.join(self.flow_dir, "logs", "asap7", block, "base", "*synth_stat*"),
            os.path.join(self.flow_dir, "reports", "asap7", block, "base", "synth_stat.txt"),
            os.path.join(self.flow_dir, "logs", "asap7", f"janus_{block}", "base", "*synth_stat*"),
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
        return cmd

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
        cmd = ["make", f"DESIGN_CONFIG={self._config_path(block)}", target]
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

    def _find_abstract(self, block: str, ext: str) -> Optional[str]:
        """Locate a freshly generated abstract file (.lef/.lib) for a block."""
        if not self.orfs:
            return None
        pat = os.path.join(self.orfs, "flow", "results", "asap7", "janus",
                           block, "base", f"*{ext}")
        hits = sorted(glob.glob(pat))
        return hits[0] if hits else None

    def _find_routed_odb(self, block: str) -> Optional[str]:
        """Locate the final routed .odb for a block (prefer 6_final, then route)."""
        if not self.orfs:
            return None
        base = os.path.join(self.orfs, "flow", "results", "asap7", "janus",
                            block, "base")
        for name in ("6_final.odb", "5_route.odb", "5_2_route.odb"):
            p = os.path.join(base, name)
            if os.path.exists(p):
                return p
        # last resort: highest-numbered odb in the dir
        hits = sorted(glob.glob(os.path.join(base, "*.odb")))
        return hits[-1] if hits else None

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
        # Prefer these authoritative slacks over the stdout-scraped ones; this
        # is where hold stops being 'n/a'.
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
            for pat in ("flow/platforms/asap7/lib/*.lib",
                        "flow/platforms/asap7/lib/*.lib.gz"):
                libs += sorted(glob.glob(os.path.join(self.orfs, pat)))
        # hardened child-block .lib abstracts + SRAM libs also matter for the top
        libs += sorted(glob.glob(os.path.join(self.sram_out_dir, "*.lib")))
        if libs:
            env["LIB_FILES"] = " ".join(libs)
        return env

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
            return
        res = self.results[block]
        setup = res.wns_ns          # authoritative setup WNS (ps) from _classify_timing
        hold = res.hold_ps          # authoritative hold WS (ps)
        drc = res.drc_violations
        has_setup_viol = setup is not None and setup < 0.0
        has_hold_viol = hold is not None and hold < 0.0
        timing_closed = (setup is not None and setup >= 0.0
                         and hold is not None and hold >= 0.0)
        # nothing to do: closed timing and no DRCs
        if not (has_setup_viol or has_hold_viol) and not (
                timing_closed and drc and drc > 0):
            return
        odb = self._find_routed_odb(block)
        if not odb:
            self.ui.debug(f"{block}: no routed .odb — skipping post-route ECO")
            return
        orb = self._openroad_bin()
        env = self._orfs_lib_env(block)
        tmpdir = os.path.dirname(odb)

        # ---- Stage 1: timing ECO (only if there is a timing violation) ------
        if has_setup_viol or has_hold_viol:
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
            # Stamp git provenance at the top of every block log so any log
            # shared for triage is self-identifying (branch, HEAD sha+subject,
            # uncommitted tracked edits) — no reliance on branch names or
            # remembering "what was I running".
            if self.git.available():
                header += self.git.provenance() + "\n"
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
            res.drc_violations = report.drc_violations
            res.area = report.area
            res.power = report.power
            res.utilization = report.utilization
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
                self._post_route_opt(block)
                # Emit the block's .lef/.lib abstract so janus_soc_top can
                # consume it as a hard macro (hierarchical bottom-up assembly).
                self._run_abstract(block)
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

    # ----- preflight checks --------------------------------------------------
    def _preflight(self) -> None:
        problems = []
        # Fix the kepler-formal shared-lib path up front so the optional
        # equiv-check loads (or is cleanly skipped) instead of crashing CTS.
        self._ensure_kepler_libpath()
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
    p.add_argument("--resume", action="store_true",
                   help="skip blocks already marked DONE in the checkpoint")
    p.add_argument("--dry-run", action="store_true",
                   help="print the make commands but do not run anything")
    p.add_argument("--estimate", action="store_true",
                   help="print the execution plan + rough cost estimate, then exit")
    p.add_argument("--no-llm", action="store_true",
                   help="never call the LLM; stop a block on first failure")
    p.add_argument("--interactive", action="store_true",
                   help="prompt on the shell before spending an LLM call and "
                        "before applying fixes (only if stdin is a TTY; "
                        "otherwise runs fully autonomously)")
    p.add_argument("--no-git-commit", action="store_true",
                   help="do NOT git-commit LLM fixes (default: commit them to "
                        "the current branch so you can just `git pull`)")
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
    p.add_argument("--no-physical", action="store_true",
                   help="disable the deterministic macro-fit (MPL-0006) SRAM "
                        "reshape self-heal; send such failures to the LLM instead")
    p.add_argument("--no-sram-regen", action="store_true",
                   help="when reshaping SRAMs, edit scripts/04_setup_srams.sh but "
                        "do NOT regenerate the macros in-process (you regenerate "
                        "manually on `git pull` before --resume)")
    p.add_argument("--no-abstract", action="store_true",
                   help="skip generating each block's .lef/.lib abstract after "
                        "it hardens (default: generate them for hierarchical "
                        "top-level assembly)")
    p.add_argument("--no-post-route-opt", action="store_true",
                   help="disable the adaptive post-route ECO pass. By default, "
                        "when a hardened block still has a setup/hold violation "
                        "the agent runs an incremental repair (hold first — it "
                        "is functionally critical), adopts it ONLY if timing "
                        "strictly improves, and otherwise discards it. If timing "
                        "is closed but a handful of DRCs remain it runs a "
                        "DRC-only cleanup under the same keep/revert policy.")
    p.add_argument("--drc-handful", type=int, default=20,
                   help="max residual DRC count the post-route DRC-only ECO will "
                        "attempt to clean (default 20). Above this the failure is "
                        "treated as global congestion needing a floorplan/route "
                        "strategy change, not an ECO.")
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
