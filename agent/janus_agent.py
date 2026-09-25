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
import json
import os
import re
import shutil
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
from flow_state import FlowState                                        # noqa: E402
from reporter import Reporter                                           # noqa: E402
from notification import NotificationManager                            # noqa: E402
from timing_classifier import classify_routed_odb, render as tc_render   # noqa: E402
from incremental_opt import (run_eco_pass, decide_timing, decide_drc,     # noqa: E402
                             adopt_odb, discard_odb, ECOResult)
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
            logger=self.ui.debug,
        )
        if self.notifier.enabled and not self.notifier.email_configured \
                and getattr(args, "notify_email", None):
            self.ui.warn("--notify-email set but SMTP is not fully configured "
                         "(need host/user/pass via flags or env); emails will "
                         "be skipped. See asic_flow/utils/notification.py.")

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
            os.path.join(self.flow_dir, "results", "asap7", block, "base"),
            os.path.join(self.flow_dir, "results", "asap7", f"janus_{block}", "base"),
            os.path.join(self.flow_dir, "results", "asap7", "janus", block, "base"),
        ]
        cands += glob.glob(os.path.join(self.flow_dir, "results", "*",
                                        f"*{block}*", "base"))
        for d in cands:
            if os.path.isdir(d) and glob.glob(os.path.join(d, "*.odb")):
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
        if os.environ.get("JANUS_MULTICORNER", "1") != "0":
            cmd += ["CORNERS=slow fast",
                    "SLOW_LIB_FILES=$(WC_NLDM_LIB_FILES)",
                    "FAST_LIB_FILES=$(BC_NLDM_LIB_FILES)"]

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
                    cmd += ["-o", p]
                    n_old += 1
            if n_old:
                self.ui.step(f"{block}: resume — {n_old} reused artifact(s) "
                             f"passed to make as --old-file (no spurious rebuild)")

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

    # ----- stamp-freshness guard (prevents silent STOCK builds) --------------
    def _verify_stamp_fresh(self, block: str) -> Optional[str]:
        """Return an error string if the STAMPED ORFS config for `block` is out
        of sync with the repo SOURCE config, else None.

        WHY THIS EXISTS: `make DESIGN_CONFIG=<stamped>/config.mk` reads a COPY
        that scripts/03_setup_janus_configs.sh stamps from asic_flow/configs.
        agent.py does NOT auto-restamp, so if you `git pull` (or edit a source
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

        if not problems:
            return None
        return (
            "STAMP OUT OF SYNC — the ORFS stamped config does not match the repo "
            "source, so this run would build with the WRONG (likely STOCK) JANUS "
            "levers.\n" + "\n".join(problems) +
            "\n\nFIX: re-stamp before running:\n"
            "    bash asic_flow/scripts/03_setup_janus_configs.sh\n"
            "then re-launch. (agent.py does NOT auto-restamp. Override this guard "
            "with --skip-stamp-check only if you know the stamp is intentionally "
            "different.)")

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
            os.path.join(root, block, "base"),
            os.path.join(root, f"janus_{block}", "base"),
            os.path.join(root, "janus", block, "base"),
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
            os.path.join(self.flow_dir, "logs", "asap7", block, "base"),
            os.path.join(self.flow_dir, "logs", "asap7", f"janus_{block}", "base"),
        ]
        cands += glob.glob(os.path.join(self.flow_dir, "logs", "*",
                                        f"*{block}*", "base"))
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

    def _run_signoff(self, block: str) -> None:
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
        if proc.returncode == 0 and spefs:
            self.results[block].note += "  +signoff-rcx"
            self.ui.good(f"{block}: sign-off RCX OK — SPEF "
                         f"{os.path.basename(spefs[0])} "
                         "(PD-UNVALIDATED: extraction wired, not re-timed here)")
        elif any(m in low for m in skip_markers):
            # Asset(s) absent (LVS collateral) or the extractor tool not on PATH:
            # an honest, expected graceful skip — never a fabricated result.
            self.ui.info(f"    {block}: sign-off skipped — asset/tool absent "
                         f"(see {os.path.basename(log_path)}; e.g. LVS collateral "
                         "is a follow-up)")
        else:
            self.ui.warn(f"{block}: sign-off rc={proc.returncode} "
                         f"(block stays DONE; see {os.path.basename(log_path)})")

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
        """Pre-flight guard against the lane-abstract M6-OBS gotcha (ara only).

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
        the shared, deterministic, idempotent scripts/fix_abstract_obs.py. Only
        fires for user_core_ara; a no-op if the lane .lef is absent (flat build)
        or already stripped. Never raises — a guard must not break the flow.
        """
        if block != "user_core_ara":
            return
        try:
            lef = self._find_abstract("user_core_ara_lane", ".lef")
            if not lef or not os.path.isfile(lef):
                return  # flat ara build (no lane macro) or not yet generated
            scripts_dir = os.path.join(_ROOT, "scripts")
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            try:
                from fix_abstract_obs import fix_lef  # type: ignore
            except Exception as e:  # noqa
                self.ui.warn(f"lane-abstract OBS guard: cannot load "
                             f"fix_abstract_obs ({e}); skipping pre-flight check")
                return
            try:
                changed, msg = fix_lef(lef)
            except Exception as e:  # noqa
                self.ui.warn(f"lane-abstract OBS guard: {os.path.basename(lef)}: "
                             f"{e}")
                return
            if changed:
                self.ui.warn(
                    "lane-abstract OBS guard: power-pin-layer OBS auto-stripped "
                    "from %s before hardening user_core_ara — %s. The bloated "
                    "abstract OBS would otherwise blanket the lane's M6 power "
                    "pins and recur as PDN-0232/PDN-0233." % (
                        os.path.basename(lef), msg)
                )
        except Exception as e:  # noqa — a guard must never break the flow
            self.ui.warn(f"lane-abstract OBS guard: skipped ({e})")

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

        # Shared state updated by the reader loop; read by the heartbeat thread,
        # the optional in-place live console panel, and the web dashboard.
        t_start = time.time()
        state = {"stage": "starting", "lines": 0, "last_msg": "",
                 "stage_at": t_start, "prev_lines": 0, "stage_idx": 0}
        state_lock = threading.Lock()
        hb_stop = threading.Event()

        interval = max(5.0, float(getattr(self.args, "heartbeat", 30.0)))

        # In-place live status panel: only for a SERIAL run on a real TTY with
        # rich present and not --verbose (verbose streams raw ORFS output, which
        # would fight the panel). Parallel runs launch several _run_make threads
        # that cannot share one rich.Live, so they fall back to the throttled
        # one-line updates. The web dashboard (if any) is fed in every mode — it
        # is independent of the console.
        use_live = bool(
            _RICH and self.ui.console is not None and sys.stdout.isatty()
            and int(getattr(self.args, "parallel", 1)) <= 1
            and not self.args.verbose
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
                    if self.args.verbose:
                        self.ui.debug(f"{block}| {line}")
                proc.stdout.close()
            finally:
                proc.wait()
                hb_stop.set()
                hb.join(timeout=2.0)
                if _live is not None:
                    try:
                        _live.stop()
                    except Exception:
                        pass
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
                # Sign-off parasitic extraction (and, when its collateral lands,
                # LVS) on the routed .odb: runs the real ASAP7 OpenRCX model to
                # emit a signoff SPEF distinct from the flow's built-in estimate.
                # Runs on EVERY completed block by default (staff-engineer
                # discipline); disable with --no-signoff. Never flips the block
                # off DONE (fully wrapped in try/except inside). Runs BEFORE the
                # golden snapshot so its outputs are captured into golden/.
                self._run_signoff(block)
                # Sign-off snapshot: if this block converged (timing met within
                # the configured margins AND DRC within a handful), copy its
                # logs/reports/final outputs + a QoR manifest into the golden
                # area and commit surgically. Never flips the block off DONE
                # (fully wrapped in try/except inside).
                self._archive_golden(block, res)
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
                "wns_ns": r.wns_ns, "hold_ps": r.hold_ps, "tns_ns": r.tns_ns,
                "drc": r.drc_violations, "area": r.area, "power": r.power,
                "util": r.utilization, "status": r.status, "note": r.note,
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
                                             config_path=cfg, sdc_path=sdc)
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
                if done:
                    self.dep.mark(b, Status.DONE)
                    self.results[b].status = "DONE"
                    self.results[b].note = "resumed (already DONE)"
                    self.ui.info(f"{b}: skipped (resume — already DONE)")
                    continue
                self._plan_stage_resume(b)

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
            # Compact QoR summary for the email body.
            bits = []
            if res.wns_ns is not None:
                bits.append(f"WNS(setup)={res.wns_ns} ps")
            if res.hold_ps is not None:
                bits.append(f"WNS(hold)={res.hold_ps} ps")
            if res.tns_ns is not None:
                bits.append(f"TNS={res.tns_ns} ps")
            if res.drc_violations is not None:
                bits.append(f"DRC={res.drc_violations}")
            if res.area is not None:
                bits.append(f"area={res.area}")
            if res.utilization is not None:
                bits.append(f"util={res.utilization}")
            if res.tries:
                bits.append(f"tries={res.tries}")
            summary = "QoR: " + ", ".join(bits) if bits else ""
            if res.note:
                summary = (summary + "\n" if summary else "") + f"Note: {res.note}"
            self.notifier.notify(
                block=block,
                status=res.status,
                stage=stage,
                run_dir=self.run_dir,
                results_dir=self._results_dir(block),
                reports_dir=self._reports_dir(block),
                log_path=res.log_path or self._block_log(block),
                summary=summary,
            )
        except Exception as e:  # never let notification affect the run
            self.ui.debug(f"{block}: notification hook error (ignored): {e}")

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
# Consolidated --skip token -> the args.<attr> boolean it forces True.
# Each token is the ergonomic name; the attr is the existing --no-* switch
# so all downstream code stays unchanged.
_SKIP_MAP = {
    "sram":           "no_sram_regen",
    "abstract":       "no_abstract",
    "physical":       "no_physical",
    "post-route-opt": "no_post_route_opt",
    "golden":         "no_golden",
    "signoff":        "no_signoff",
    "llm":            "no_llm",
    "git-commit":     "no_git_commit",
}


def _apply_skips(args: argparse.Namespace) -> None:
    """Fold the consolidated --skip tokens into the individual --no-* booleans
    (OR-ing, so an explicit --no-* still wins). Idempotent."""
    for tok in (getattr(args, "skip", None) or []):
        attr = _SKIP_MAP.get(tok)
        if attr is not None:
            setattr(args, attr, True)


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
    p.add_argument("--skip-stamp-check", action="store_true",
                   help="bypass the pre-flight stamp-freshness guard that aborts "
                        "a run when the ORFS stamped config.mk/<blk>_local.mk "
                        "differ from the repo source (i.e. scripts/03 was not "
                        "re-run after a config edit/pull). Only use when the "
                        "stamp is intentionally different from source.")
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
    p.add_argument("--no-debug-dump", action="store_true",
                   help="disable the per-stage debug dumps. By default the "
                        "agent wires POST_<STEP>_TCL hooks (asic_flow/scripts/"
                        "hooks/) that, at the end of floorplan / macro-place / "
                        "place / resize / CTS / route, emit per-stage timing "
                        "reports (top-100 reg2reg, reg2out, in2reg for both "
                        "setup(max) and hold(min)), stage netlists (.v/.def) "
                        "and layout / clock-tree .webp images. Every hook is "
                        "catch-guarded so it can never abort the flow. This "
                        "flag suppresses the hook wiring entirely.")
    p.add_argument("--no-debug-images", action="store_true",
                   help="keep the per-stage timing reports and netlists but "
                        "skip the .webp layout / clock-tree images (sets "
                        "JANUS_DUMP_IMAGES=0). Images need a GUI-compiled "
                        "openroad; on a non-GUI build they are skipped anyway.")
    p.add_argument("--drc-handful", type=int, default=20,
                   help="max residual DRC count the post-route DRC-only ECO will "
                        "attempt to clean (default 20). Above this the failure is "
                        "treated as global congestion needing a floorplan/route "
                        "strategy change, not an ECO.")
    # ----- golden sign-off archiving --------------------------------------
    p.add_argument("--no-golden", action="store_true",
                   help="disable golden archiving. By DEFAULT, when a block "
                        "hardens AND converges (timing met within the golden "
                        "margins below AND DRC within --drc-handful), the agent "
                        "snapshots its logs/reports/final outputs + a QoR "
                        "manifest into the in-repo golden area and commits it "
                        "surgically (push only if --git-push/--git-sync-branches "
                        "are on).")
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
    p.add_argument("--no-signoff", action="store_true",
                   help="disable sign-off on completed blocks. By DEFAULT, every "
                        "block that reaches DONE gets a sign-off pass on its "
                        "routed .odb (staff-engineer discipline). Its outputs are "
                        "captured into the golden snapshot.")
    p.add_argument("--signoff-stage", choices=["rcx", "lvs", "both"],
                   default="rcx",
                   help="sign-off stage(s) to run (default rcx). 'rcx' is REAL "
                        "and available now: it runs the staged ASAP7 OpenRCX "
                        "model to write a sign-off SPEF distinct from — and "
                        "higher fidelity than — the flow's built-in 6_final.spef "
                        "estimate. 'lvs'/'both' also attempt device-LVS, which "
                        "GRACEFULLY SKIPS until its collateral (golden CDL, "
                        "BSIM-CMG models, KLayout FinFET runset) + engine are "
                        "staged — it never fabricates a result.")
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
                       help="also upload the log/report tarball to Google "
                            "Drive (folder JANUS_runs/<block>_<timestamp>). "
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
    #   One repeatable flag that folds the individual --no-* switches below
    #   into a single ergonomic interface. The --no-* flags remain fully
    #   supported (and documented above) as explicit aliases; --skip is just
    #   the consolidated form, e.g.  --skip sram abstract signoff
    p.add_argument("--skip", nargs="+", default=None, metavar="STAGE",
                   choices=sorted(_SKIP_MAP.keys()),
                   help="consolidated stage-disable switch (repeatable). "
                        f"choices: {', '.join(sorted(_SKIP_MAP.keys()))}. "
                        "Each maps to the matching --no-* flag, e.g. "
                        "`--skip sram abstract signoff` == "
                        "`--no-sram-regen --no-abstract --no-signoff`. "
                        "Both forms may be combined; effects are OR-ed.")
    return p


def _manifest_only_cli(args: argparse.Namespace) -> Optional[int]:
    """Handle the manifest-only sub-commands (--status / --gc / --gc-dry-run)
    WITHOUT constructing the full agent (which needs ORFS_DIR etc). Returns an
    exit code if it handled the invocation, else None."""
    if not (args.status or args.gc or args.gc_dry_run):
        return None
    mpath = args.manifest or FlowState.default_path(_ROOT)
    fs = FlowState(mpath).load()
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


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _apply_skips(args)
    if args.blocks:
        bad = [b for b in args.blocks if b not in DEFAULT_GRAPH]
        if bad:
            print(f"error: unknown block(s): {', '.join(bad)}\n"
                  f"valid: {', '.join(DEFAULT_GRAPH)}", file=sys.stderr)
            return 2
    rc = _manifest_only_cli(args)
    if rc is not None:
        return rc
    agent = JANUSAutonomousAgent(args)
    return agent.run()


if __name__ == "__main__":
    raise SystemExit(main())
