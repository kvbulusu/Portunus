"""
flow_state.py — Global, cross-run flow-state manifest for the JANUS ASIC agent.

WHY THIS EXISTS
---------------
The old per-run ``checkpoint.json`` lived *inside* each ``--run-dir``.  Because a
fresh timestamped run-dir is passed on every invocation, ``--resume`` loaded a
brand-new EMPTY checkpoint and never saw the previous run's progress — so a
block that had already completed synthesis..CTS..global-route was re-run from
scratch.  It also only tracked block-level status (DONE/FAILED), never *which
ORFS stages* finished, their QoR, or where the logs/reports lived.

``FlowState`` fixes all of that with ONE JSON file that lives OUTSIDE every
run-dir and OUTSIDE every block, so it is trivial to inspect, track, and use to
resume or clean up.  It records, per block:

  * overall status  (PENDING / RUNNING / <stage> / DONE / FAILED)
  * per-STAGE record: status, .odb path + fingerprint (mtime/size/sha), QoR,
    and timestamps  — the actual "stages done" ledger
  * a SEMANTIC snapshot of the config.mk / .sdc *variables* used when those
    stages were built (not a raw-byte hash) — so a ``scripts/03`` re-stamp that
    only churns comments / whitespace / mtime does NOT look like a real change
  * every run/log/report dir that ever touched the block, for cleanup

RESUME MODEL (correct-by-construction against ORFS incremental make)
-------------------------------------------------------------------
ORFS ``make`` is mtime-driven: a stage .odb is rebuilt if any prerequisite
(including config.mk) is *newer*.  A ``scripts/03`` re-stamp ``cp``s config.mk
and bumps its mtime, which makes ORFS redo EVERYTHING even when nothing
substantive changed.  ``resume_plan()`` compares the *parsed variables* of the
stored vs current config/sdc, maps any genuinely-changed key to the earliest
stage it affects, and returns concrete filesystem actions:

  * TOUCH every still-valid stage .odb to be newer than config.mk  -> ORFS make
    treats them as up-to-date and skips them (defeats the re-stamp mtime churn)
  * REMOVE the .odb of the earliest genuinely-invalidated stage and everything
    after it -> ORFS make redoes exactly those stages and no more

If nothing changed, only the touch actions run and the block resumes from the
first stage that was never reached.

This module is pure-Python and filesystem-only; it never imports ORFS or the
rest of the agent, so it is fully unit-testable off-box (see ``__main__``).
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1

# Canonical ORFS stage order.  Names match the leading token of the .odb files
# ORFS writes (1_synth.odb, 2_floorplan.odb, ...).  Some ORFS versions split
# routing into 5_1_grt.odb (global) + 5_2_route.odb (detail); those sort
# naturally under the "5" bucket via _stage_key() so we do NOT hard-code them.
STAGE_ORDER: List[str] = [
    "1_synth",
    "2_floorplan",
    "3_place",
    "4_cts",
    "5_route",
    "6_final",
]

# Map a config.mk / SDC variable to the EARLIEST stage its change invalidates.
# A key not listed here is treated conservatively as synthesis-affecting (the
# whole flow is redone) — safer to over-rebuild than to ship a stale netlist.
# The prefixes are checked with startswith(); exact keys take precedence.
_KEY_STAGE_EXACT: Dict[str, str] = {
    "CORE_UTILIZATION": "2_floorplan",
    "CORE_ASPECT_RATIO": "2_floorplan",
    "CORE_AREA": "2_floorplan",
    "DIE_AREA": "2_floorplan",
    "CORE_MARGIN": "2_floorplan",
    "PLACE_DENSITY": "3_place",
    "PLACE_DENSITY_LB_ADDON": "3_place",
    "GPL_ROUTABILITY_DRIVEN": "3_place",
    "GPL_TIMING_DRIVEN": "3_place",
    # §156 pin placement (3_2_place_iop) and its PRE hook
    "PLACE_PINS_ARGS": "3_place",
    "PRE_IO_PLACEMENT_TCL": "3_place",
    # §161 repair_design over-fix (first repair_design call is 3_4 resize) and
    # the 3_4 PRE hook (constant-net fold).
    "SLEW_MARGIN": "3_place",
    "CAP_MARGIN": "3_place",
    "PRE_RESIZE_TCL": "3_place",
    # §73: first stage whose repair_timing actually fixes hold. Verified in the
    # agent_domain 20261007_202416 log: floorplan passes -hold_margin but runs
    # `-setup` only; the 3_4 resize call is issued without -hold_margin; the
    # first hold repair (RSZ-0046, 165 endpoints) is in 4_1_cts.
    "HOLD_SLACK_MARGIN": "4_cts",
    # §107 GRT pre-hook (sourced at the start of global_route.tcl): changing it
    # only invalidates routing, not synthesis.
    "PRE_GLOBAL_ROUTE_TCL": "5_route",
    # §108 only consumed by global_route.tcl (-resistance_aware)
    "ENABLE_RESISTANCE_AWARE": "5_route",
    # §109 read only by the PRE_GLOBAL_ROUTE hook
    "JANUS_GRT_INCR_ALLOW_CONGESTION": "5_route",
    # §140: the kepler-formal LEC runs only inside 4_1_cts (repair_timing
    # before/after netlists 4_before_rsz_lec.v / 4_after_rsz_lec.v). Toggling it
    # must NOT force a re-synthesis (unknown keys default to 1_synth).
    "LEC_CHECK": "4_cts",
    # Antenna repair runs in global route / detail route only.
    "REPAIR_ANTENNAS": "5_route",
    # §167 only read by global_route.tcl (post-GRT repair_design/repair_timing)
    "SKIP_INCREMENTAL_REPAIR": "5_route",
}
_KEY_STAGE_PREFIX: List[Tuple[str, str]] = [
    ("SYNTH_", "1_synth"),
    ("ABC_", "1_synth"),
    ("VERILOG", "1_synth"),
    ("SDC_", "1_synth"),
    ("CLOCK_PERIOD", "1_synth"),
    ("MACRO_", "2_floorplan"),
    ("PDN_", "2_floorplan"),
    ("TAP_", "2_floorplan"),
    ("FInish", "6_final"),
    ("CELL_PAD", "3_place"),
    ("CTS_", "4_cts"),
    ("SKIP_CTS", "4_cts"),
    ("MIN_ROUTING_LAYER", "5_route"),
    ("MAX_ROUTING_LAYER", "5_route"),
    ("ROUTING_", "5_route"),
    ("GLOBAL_ROUT", "5_route"),
    ("DETAIL_ROUT", "5_route"),
    ("GRT_", "5_route"),
    ("DRT_", "5_route"),
    ("JANUS_MACRO_DETOUR", "5_route"),   # §107 detour hook knobs (read in GRT)
    ("JANUS_IO_SHADOW", "3_place"),      # §156 IO macro-shadow hook knobs
    ("JANUS_IO_MACRO_SHADOW", "3_place"),
    ("JANUS_UD_MACRO", "2_floorplan"),   # §164 user_domain_macros.tcl knobs
    ("JANUS_CTS_", "4_cts"),
    ("JANUS_RSZ_", "5_route"),           # §167 PRE_GLOBAL_ROUTE repair_design knobs
    ("JANUS_CONST_FOLD", "3_place"),     # §161 PRE_RESIZE constant-net fold knobs             # §114/§160 PRE_CTS hook knobs
]


def _stage_key(stage_name: str) -> Tuple[int, int]:
    """Sortable key from a stage / .odb base name.

    '1_synth' -> (1, 0); '5_1_grt' -> (5, 1); '5_2_route' -> (5, 2);
    '6_final' -> (6, 0).  Non-numeric / unknown -> (99, 99) so it sorts last.
    """
    toks = stage_name.split("_")
    if not toks or not toks[0].isdigit():
        return (99, 99)
    major = int(toks[0])
    minor = int(toks[1]) if len(toks) > 1 and toks[1].isdigit() else 0
    return (major, minor)


def _canonical_stage(stage_name: str) -> str:
    """Fold a fine-grained .odb name onto its canonical STAGE_ORDER bucket.

    '5_1_grt'/'5_2_route' -> '5_route';  '2_floorplan' -> '2_floorplan'.
    Falls back to the raw name if the major number is unknown.
    """
    major, _ = _stage_key(stage_name)
    for s in STAGE_ORDER:
        if _stage_key(s)[0] == major:
            return s
    return stage_name


def _stage_sibling_artifacts(results_dir: str, stage_base: str) -> List[str]:
    """Return every on-disk sibling artifact that belongs to the SAME ORFS stage
    as ``stage_base`` (matched by leading major number), not just its canonical
    ``.odb``.

    WHY THIS EXISTS
    ---------------
    ORFS synth is a make *chain*, and the ``.odb`` is the LAST link:

        RTL + config.mk -> 1_1_yosys_canonicalize.rtlil -> 1_2_yosys.v -> 1_synth.odb

    ``apply_resume_plan`` used to TOUCH only the canonical ``.odb`` (1_synth.odb)
    when a stage was reused.  It never touched the fine-grained yosys
    intermediates (``1_1_yosys_canonicalize.rtlil``, ``1_2_yosys.v``) that sit
    EARLIEST in the make dependency graph.  So after a ``scripts/03`` re-stamp
    bumped config.mk's mtime, make saw those intermediates as older than
    config.mk and rebuilt the ENTIRE synth chain — a spurious ~2h re-synth —
    even though 1_synth.odb itself had been freshly touched.

    Touching the whole family of ``{major}_*`` products (every stage-owned
    prerequisite that lives beside the .odb: .rtlil, .v, .sdc, .def, .json, ...)
    makes the reuse airtight against re-stamp mtime churn.  Only files that
    already exist are returned; the .odb itself is included so callers can pass
    a single list.
    """
    if not results_dir or not os.path.isdir(results_dir):
        return []
    major, _ = _stage_key(stage_base)
    if major == 99:  # unknown stage — do not sweep the whole dir
        return []
    prefix = f"{major}_"
    out: List[str] = []
    for name in os.listdir(results_dir):
        if not name.startswith(prefix):
            continue
        toks = name.split("_")
        if toks and toks[0].isdigit() and int(toks[0]) == major:
            out.append(os.path.abspath(os.path.join(results_dir, name)))
    return out


def _sha256_file(path: str, cap_bytes: int = 64 * 1024 * 1024) -> str:
    """SHA-256 of a file's first ``cap_bytes`` (odb files are large; a capped
    hash is enough to detect a rebuild and keeps this cheap)."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            h.update(fh.read(cap_bytes))
    except OSError:
        return ""
    return "sha256:" + h.hexdigest()[:16]


def parse_make_vars(path: str) -> Dict[str, str]:
    """Parse ``export NAME = value`` / ``NAME := value`` assignments from an
    ORFS .mk file into a dict, ignoring comments and blank lines.  Returns {}
    if the file is missing.  This is the SEMANTIC fingerprint of a config — two
    files that assign the same variables are equivalent even if comments or
    whitespace differ (exactly what a scripts/03 re-stamp churns)."""
    out: Dict[str, str] = {}
    if not path or not os.path.isfile(path):
        return out
    rx = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*[:?+]?=\s*(.*?)\s*$")
    try:
        with open(path) as fh:
            for raw in fh:
                line = raw.split("#", 1)[0].rstrip()
                if not line.strip():
                    continue
                m = rx.match(line)
                if m:
                    out[m.group(1)] = m.group(2).strip()
    except OSError:
        return {}
    return out


CONFIG_FMT = "2"   # §74: include-following + $(VAR)-expanded snapshot
_REF_RX = re.compile(r"\$[({]([A-Za-z_][A-Za-z0-9_]*)[)}]")
_INC_RX = re.compile(r"^\s*-?include\s+(.+?)\s*$")
_ASSIGN_RX = re.compile(
    r"^\s*(?:export\s+|override\s+)*([A-Za-z_][A-Za-z0-9_]*)\s*(:{1,2}=|\?=|\+=|=)\s*(.*?)\s*$")


def parse_config_vars(path: str) -> Dict[str, str]:
    """Like parse_make_vars, but (a) follows ``-include``/``include`` lines (so
    vars set in e.g. user_core_ara_local.mk are VISIBLE to the resume diff),
    honouring make's ``?=`` / ``+=`` / ``:=`` order semantics, and (b) expands
    ``$(VAR)`` references against the final values, so
    ``HOLD_SLACK_MARGIN = $(JANUS_ARA_HOLD_MARGIN_PS)`` diffs as its real value.
    Conditionals (ifeq/ifdef) are not evaluated — every branch is read, which
    can only make the diff MORE conservative. Unknown refs stay literal.
    Adds ``__fmt`` and ``__refd`` (vars referenced by other vars) metadata."""
    vals: Dict[str, str] = {}
    refd: set = set()
    seen: set = set()

    def _read(fp: str, depth: int = 0) -> None:
        fp = os.path.abspath(fp)
        if depth > 4 or fp in seen or not os.path.isfile(fp):
            return
        seen.add(fp)
        here = os.path.dirname(fp) + "/"
        try:
            lines = open(fp).read().splitlines()
        except OSError:
            return
        for raw in lines:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            mi = _INC_RX.match(line)
            if mi:
                tgt = mi.group(1).replace("$(dir $(lastword $(MAKEFILE_LIST)))", here)
                if "$" not in tgt:
                    _read(tgt if os.path.isabs(tgt) else os.path.join(here, tgt), depth + 1)
                continue
            m = _ASSIGN_RX.match(line)
            if not m:
                continue
            k, op, v = m.group(1), m.group(2), m.group(3).strip()
            refd.update(_REF_RX.findall(v))
            if op == "?=":
                vals.setdefault(k, v)
            elif op == "+=":
                vals[k] = (vals[k] + " " + v).strip() if k in vals else v
            else:
                vals[k] = v

    if not path or not os.path.isfile(path):
        return {}
    _read(path)

    def _expand(v: str, depth: int = 0) -> str:
        if depth > 8 or "$" not in v:
            return v
        return _REF_RX.sub(
            lambda mm: _expand(vals[mm.group(1)], depth + 1)
            if mm.group(1) in vals else mm.group(0), v)

    out = {k: _expand(v) for k, v in vals.items()}
    out["__fmt"] = CONFIG_FMT
    out["__refd"] = " ".join(sorted(refd & set(vals)))
    return out


def _is_mapped_key(k: str) -> bool:
    return k in _KEY_STAGE_EXACT or any(k.upper().startswith(p.upper())
                                        for p, _ in _KEY_STAGE_PREFIX)


# §148: check-only switches. In ORFS (cts.tcl / final_outputs.tcl / synth_*.tcl)
# LEC_CHECK only writes *_lec.v side files and runs kepler-formal; it never
# alters the netlist/odb. So turning it OFF cannot invalidate a stage that was
# built with it ON (avoids re-running CTS->final on DONE blocks). Turning it ON
# still maps to 4_cts (the check has not been run yet).
_CHECK_ONLY_KEYS = frozenset({"LEC_CHECK"})

# §163: knobs read only by the agent's post-route ECO (after 6_final, on a copy
# of 6_final.odb). ORFS make never reads them, so they invalidate no stage.
_POST_FLOW_PREFIXES = ("JANUS_ECO_",)


def _is_off(v: Optional[str]) -> bool:
    return (v or "").strip() in ("", "0")


def _changed_keys(old: Dict[str, str], new: Dict[str, str]) -> List[str]:
    """Semantically changed keys. ``__*`` metadata is ignored. In fmt-2
    snapshots a var that is only an INPUT to other vars (in ``__refd``) and
    has no stage mapping of its own is skipped: its effect already shows up in
    the expanded value of the var that references it, which maps precisely.
    A check-only key (``_CHECK_ONLY_KEYS``) switched OFF is not a change, and
    post-flow ECO knobs (``_POST_FLOW_PREFIXES``) never are."""
    refd = set((new.get("__refd") or "").split()) | set((old.get("__refd") or "").split())
    keys = (set(old) | set(new))
    return sorted(k for k in keys
                  if not k.startswith("__") and old.get(k) != new.get(k)
                  and not (k in refd and not _is_mapped_key(k))
                  and not (k in _CHECK_ONLY_KEYS and _is_off(new.get(k)))
                  and not k.startswith(_POST_FLOW_PREFIXES))


def _earliest_stage_for_keys(keys: List[str]) -> Optional[str]:
    """Earliest STAGE_ORDER stage invalidated by the given changed keys.
    Unknown key -> conservatively synthesis (whole flow)."""
    if not keys:
        return None
    best_idx = len(STAGE_ORDER)  # sentinel = nothing
    for k in keys:
        stage = _KEY_STAGE_EXACT.get(k)
        if stage is None:
            ku = k.upper()
            for pref, st in _KEY_STAGE_PREFIX:
                if ku.startswith(pref.upper()):
                    stage = st
                    break
        if stage is None:
            stage = "1_synth"  # unknown => rebuild everything (safe)
        idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else 0
        best_idx = min(best_idx, idx)
    return STAGE_ORDER[best_idx] if best_idx < len(STAGE_ORDER) else None


@dataclass
class ResumePlan:
    block: str
    reuse: List[str] = field(default_factory=list)       # canonical stages kept
    rerun_from: Optional[str] = None                      # canonical stage or None
    config_changed: bool = False
    changed_keys: List[str] = field(default_factory=list)
    touch_paths: List[str] = field(default_factory=list)  # .odb to touch newer
    remove_paths: List[str] = field(default_factory=list)  # .odb to delete
    reason: str = ""
    full_rebuild: bool = False
    notes: List[str] = field(default_factory=list)       # per-stage provenance

    def to_dict(self) -> Dict[str, Any]:
        return {
            "block": self.block, "reuse": self.reuse, "rerun_from": self.rerun_from,
            "config_changed": self.config_changed, "changed_keys": self.changed_keys,
            "touch": self.touch_paths, "remove": self.remove_paths,
            "reason": self.reason, "full_rebuild": self.full_rebuild,
            "notes": self.notes,
        }


@dataclass
class FlowState:
    """Single global manifest, persisted atomically to one JSON file."""
    path: str
    state: Dict[str, Any] = field(default_factory=dict)

    # ---- location helper ----------------------------------------------------
    @staticmethod
    def default_path(asic_flow_root: str) -> str:
        """Stable location OUTSIDE any run-dir: <asic_flow>/.janus_state/…json.
        Overridable via $JANUS_MANIFEST."""
        env = os.environ.get("JANUS_MANIFEST")
        if env:
            return env
        return os.path.join(asic_flow_root, ".janus_state", "janus_flow_state.json")

    # ---- load / save (atomic) ----------------------------------------------
    def load(self) -> "FlowState":
        if os.path.isfile(self.path):
            try:
                with open(self.path) as fh:
                    self.state = json.load(fh)
            except (json.JSONDecodeError, OSError):
                self.state = {}
        self.state.setdefault("_schema", SCHEMA_VERSION)
        self.state.setdefault("blocks", {})
        self.state.setdefault("runs", [])
        return self

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.state["_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(self.state, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    # ---- block accessors ----------------------------------------------------
    def _block(self, block: str) -> Dict[str, Any]:
        return self.state.setdefault("blocks", {}).setdefault(
            block, {"status": "PENDING", "stages": {}, "runs": []})

    def block_status(self, block: str) -> str:
        return self.state.get("blocks", {}).get(block, {}).get("status", "PENDING")

    def is_done(self, block: str) -> bool:
        return self.block_status(block) == "DONE"

    def forget_block(self, block: str, save: bool = True) -> bool:
        """Drop ALL recorded state for ``block`` (status, per-stage ledger, QoR,
        config/sdc snapshot, run list).  Used by the agent's --force-blocks /
        --clean path so a forced re-run cannot reuse a stale stage record or the
        old config snapshot.  Returns True if an entry was actually removed.
        Best-effort / idempotent — forgetting an unknown block is a no-op."""
        blocks = self.state.setdefault("blocks", {})
        existed = block in blocks
        if existed:
            del blocks[block]
            if save:
                self.save()
        return existed

    # ---- stage scan / update ------------------------------------------------
    @staticmethod
    def scan_stage_odbs(results_dir: str) -> List[Tuple[str, str]]:
        """Return [(stage_base, abspath), ...] for every N[_M]_*.odb in a
        results dir, ordered by stage.  stage_base is the filename without
        '.odb' (e.g. '5_2_route')."""
        if not results_dir or not os.path.isdir(results_dir):
            return []
        hits = glob.glob(os.path.join(results_dir, "*.odb"))
        out: List[Tuple[str, str]] = []
        for p in hits:
            base = os.path.basename(p)[:-4]  # strip .odb
            if base and base[0].isdigit():
                out.append((base, os.path.abspath(p)))
        out.sort(key=lambda t: _stage_key(t[0]))
        return out

    def update_block_from_scan(self, block: str, results_dir: str,
                               config_path: Optional[str] = None,
                               sdc_path: Optional[str] = None,
                               top_qor: Optional[Dict[str, Any]] = None,
                               final_done: bool = False) -> Dict[str, Any]:
        """Scan the ORFS results dir and record which stages are present, their
        fingerprints, and the SEMANTIC config/sdc snapshot used.  Sets the
        block's overall status to the highest canonical stage present (or DONE
        when final_done)."""
        rec = self._block(block)
        stages = rec.setdefault("stages", {})
        found = self.scan_stage_odbs(results_dir)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        highest_canon: Optional[str] = None
        for base, p in found:
            canon = _canonical_stage(base)
            try:
                st = os.stat(p)
                mtime, size = st.st_mtime, st.st_size
            except OSError:
                continue
            srec = stages.get(canon, {})
            prev_sha = srec.get("sha")
            # only re-hash if size/mtime moved (hashing large odbs is costly)
            if srec.get("size") != size or srec.get("mtime") != mtime or not prev_sha:
                sha = _sha256_file(p)
            else:
                sha = prev_sha
            srec.update({
                "status": "DONE", "odb": p, "odb_name": base,
                "mtime": mtime, "size": size, "sha": sha,
                "updated": now,
            })
            srec.setdefault("first_seen", now)
            stages[canon] = srec
            highest_canon = canon
        if top_qor:
            rec["final_qor"] = top_qor
        if config_path is not None:
            rec["config_vars"] = parse_config_vars(config_path)
            rec["config_path"] = os.path.abspath(config_path)
        if sdc_path is not None:
            rec["sdc_vars"] = parse_make_vars(sdc_path)
            rec["sdc_path"] = os.path.abspath(sdc_path)
        if final_done:
            rec["status"] = "DONE"
        elif highest_canon:
            rec["status"] = highest_canon
        rec["last_results_dir"] = os.path.abspath(results_dir) if results_dir else None
        self.save()
        return rec

    def set_stage_qor(self, block: str, stage: str, qor: Dict[str, Any]) -> None:
        rec = self._block(block)
        srec = rec.setdefault("stages", {}).setdefault(_canonical_stage(stage), {})
        srec["qor"] = qor
        self.save()

    def set_block_status(self, block: str, status: str,
                         note: Optional[str] = None) -> None:
        rec = self._block(block)
        rec["status"] = status
        if note is not None:
            rec["note"] = note[:240]
        self.save()

    def mark_inflight(self, block: str, config_path: Optional[str] = None,
                      sdc_path: Optional[str] = None) -> None:
        """§74: record the config a make is ABOUT to build with. If the run is
        killed before the end-of-block snapshot, resume_plan can still tell
        which on-disk .odb were produced by it (mtime >= ts) and with what."""
        rec = self._block(block)
        rec["inflight"] = {
            "ts": time.time(),
            "config_vars": parse_config_vars(config_path) if config_path else {},
            "sdc_vars": parse_make_vars(sdc_path) if sdc_path else {},
        }
        self.save()

    def last_completed_stage(self, block: str) -> Optional[str]:
        stages = self.state.get("blocks", {}).get(block, {}).get("stages", {})
        done = [s for s, r in stages.items() if r.get("status") == "DONE"]
        if not done:
            return None
        return sorted(done, key=_stage_key)[-1]

    # ---- run ledger (for cleanup) ------------------------------------------
    def record_run(self, block: str, run_dir: Optional[str], log: Optional[str],
                   reports_dir: Optional[str], result: str,
                   reached_stage: Optional[str] = None) -> None:
        rec = self._block(block)
        runs = rec.setdefault("runs", [])
        entry = {
            "run_dir": os.path.abspath(run_dir) if run_dir else None,
            "log": os.path.abspath(log) if log and os.path.exists(log) else log,
            "reports_dir": os.path.abspath(reports_dir) if reports_dir else None,
            "result": result, "reached_stage": reached_stage,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        # dedup by run_dir: update in place if the same run-dir reappears
        for i, e in enumerate(runs):
            if e.get("run_dir") == entry["run_dir"]:
                runs[i] = entry
                break
        else:
            runs.append(entry)
        # global run index
        gidx = self.state.setdefault("runs", [])
        for e in gidx:
            if e.get("run_dir") == entry["run_dir"]:
                if block not in e.setdefault("blocks", []):
                    e["blocks"].append(block)
                break
        else:
            gidx.append({"run_dir": entry["run_dir"], "ts": entry["ts"],
                         "blocks": [block]})
        self.save()

    def gc_candidates(self) -> List[Dict[str, Any]]:
        """Run-dirs safe to delete: every block they produced has since been
        superseded by a newer run for that block.  Returns a list of
        {run_dir, reason, blocks}; callers decide whether to delete (never
        auto-deletes)."""
        blocks = self.state.get("blocks", {})
        # newest run-dir per block
        newest: Dict[str, str] = {}
        for b, rec in blocks.items():
            runs = rec.get("runs", [])
            if runs:
                newest[b] = sorted(runs, key=lambda e: e.get("ts", ""))[-1].get("run_dir")
        out: List[Dict[str, Any]] = []
        for e in self.state.get("runs", []):
            rd = e.get("run_dir")
            if not rd:
                continue
            bl = e.get("blocks", [])
            superseded = all(newest.get(b) and newest[b] != rd for b in bl)
            if bl and superseded:
                out.append({"run_dir": rd, "blocks": bl,
                            "reason": "superseded by newer run(s) for all its blocks"})
        return out

    # ---- resume planning ----------------------------------------------------
    def resume_plan(self, block: str, results_dir: str,
                    config_path: Optional[str] = None,
                    sdc_path: Optional[str] = None,
                    force_from: Optional[str] = None) -> ResumePlan:
        """Compute how to resume ``block`` given what is on disk now vs what the
        manifest recorded.  See module docstring for the model.

        §74 provenance: every present stage .odb is also fingerprinted against
        the manifest record. One that changed since it was recorded came from a
        run that never snapshotted (killed): if an ``inflight`` record covers
        it, that run's config is diffed instead; otherwise its config is
        UNKNOWN and it is rebuilt. ``force_from`` (``--resume-from``) overrides
        all detection: trust every stage before it, rebuild it and after."""
        plan = ResumePlan(block=block)
        if force_from:
            force_from = _canonical_stage(force_from)
        present = self.scan_stage_odbs(results_dir)
        if not present:
            plan.full_rebuild = True
            plan.reason = "no stage .odb found on disk — full run from synthesis"
            return plan

        present_canon = sorted({_canonical_stage(b) for b, _ in present}, key=_stage_key)
        path_by_canon: Dict[str, str] = {}
        for b, p in present:
            path_by_canon[_canonical_stage(b)] = p  # highest wins (sorted asc)

        rec = self.state.get("blocks", {}).get(block, {})
        old_cfg = rec.get("config_vars")
        old_sdc = rec.get("sdc_vars")
        cur_sdc = parse_make_vars(sdc_path) if sdc_path else {}

        def _cur_cfg_for(snapshot: Optional[Dict[str, str]]) -> Dict[str, str]:
            # legacy (pre-§74) snapshots were single-file + unexpanded: compare
            # like with like, else every $(..) value would look "changed".
            if not config_path:
                return {}
            if snapshot is not None and snapshot.get("__fmt") == CONFIG_FMT:
                return parse_config_vars(config_path)
            return parse_make_vars(config_path)

        def _diff(cfg_old, sdc_old) -> Tuple[List[str], Optional[str]]:
            ck = _changed_keys(cfg_old, _cur_cfg_for(cfg_old))
            sk = _changed_keys(sdc_old or {}, cur_sdc) if sdc_old is not None else []
            cands = [x for x in (_earliest_stage_for_keys(ck), "1_synth" if sk else None) if x]
            first = sorted(cands, key=_stage_key)[0] if cands else None
            return ck + [f"SDC:{k}" for k in sk], first

        def _earlier(a: Optional[str], b: Optional[str]) -> Optional[str]:
            if not a or not b:
                return a or b
            return a if _stage_key(a) <= _stage_key(b) else b

        earliest_invalid: Optional[str] = None
        if old_cfg is not None:
            keys, earliest_invalid = _diff(old_cfg, old_sdc)
            plan.changed_keys = keys
            plan.config_changed = bool(keys)
        else:
            # no prior semantic snapshot — trust what's on disk but say so
            plan.reason = "no stored config snapshot; reusing on-disk stages as-is. "

        # §74 per-stage provenance (only meaningful once a snapshot exists)
        if old_cfg is not None:
            stages_rec = rec.get("stages", {})
            infl = rec.get("inflight") or {}
            for s in present_canon:
                p = path_by_canon.get(s)
                want = (stages_rec.get(s) or {}).get("sha")
                if not p or (want and _sha256_file(p) == want):
                    continue
                try:
                    mt = os.stat(p).st_mtime
                except OSError:
                    continue
                if infl and mt >= infl.get("ts", 1e18) - 1:
                    keys, first = _diff(infl.get("config_vars", {}), infl.get("sdc_vars"))
                    if first and _stage_key(first) <= _stage_key(s):
                        plan.notes.append(f"{s}: built by an interrupted run whose config "
                                          f"differs ({', '.join(keys)}) -> rebuild")
                        earliest_invalid = _earlier(earliest_invalid, s)
                        plan.config_changed = True
                    else:
                        plan.notes.append(f"{s}: built by an interrupted run with a "
                                          "compatible config -> reuse")
                else:
                    plan.notes.append(f"{s}: .odb changed since the manifest recorded it "
                                      "and no in-flight record covers it (config UNKNOWN) "
                                      "-> rebuild (override with --resume-from)")
                    earliest_invalid = _earlier(earliest_invalid, s)
                    plan.config_changed = True
                # later stages are judged on their own, but anything after an
                # invalid stage is invalid anyway; stop hashing past it.
                if earliest_invalid and _stage_key(earliest_invalid) <= _stage_key(s):
                    break

        if force_from:
            if earliest_invalid and _stage_key(earliest_invalid) < _stage_key(force_from):
                plan.notes.append(f"--resume-from {force_from} OVERRIDES detection "
                                  f"(which said rebuild from {earliest_invalid})")
            earliest_invalid = force_from

        # §113 stale-SDC guard (overrides --resume-from). ORFS freezes the SDC at
        # synthesis: `cp $(SDC_FILE) 1_2_yosys.sdc` -> 1_synth.sdc -> 2_floorplan.sdc,
        # and every later stage reads THAT copy, never constraint.sdc. So resuming
        # past 1_synth after an SDC edit silently runs the OLD constraints (run
        # 20261011: §111 set_max_transition never reached 3_place..6_final —
        # 1_2_yosys.sdc sha == pre-§111 SDC). The semantic sdc_vars diff above only
        # sees `set var value` lines, not commands, so compare bytes instead.
        # Escape hatch: JANUS_ALLOW_STALE_SDC=1.
        frozen_sdc = os.path.join(results_dir, "1_2_yosys.sdc")
        if (sdc_path and os.path.isfile(sdc_path) and os.path.isfile(frozen_sdc)
                and _sha256_file(sdc_path) != _sha256_file(frozen_sdc)
                and not (earliest_invalid and earliest_invalid == "1_synth")):
            if os.environ.get("JANUS_ALLOW_STALE_SDC") == "1":
                plan.notes.append("constraint.sdc differs from the SDC frozen at synth "
                                  "(1_2_yosys.sdc) but JANUS_ALLOW_STALE_SDC=1 — reusing "
                                  "stages WITH THE OLD CONSTRAINTS")
            else:
                plan.notes.append("constraint.sdc differs from the SDC frozen at synth "
                                  f"(1_2_yosys.sdc); was rebuild from "
                                  f"{earliest_invalid or 'nothing'} -> FORCED rebuild from "
                                  "1_synth (later stages never re-read constraint.sdc). "
                                  "Override: JANUS_ALLOW_STALE_SDC=1")
                plan.changed_keys.append("SDC:<bytes>")
                plan.config_changed = True
                earliest_invalid = "1_synth"

        # reuse = present canonical stages strictly before earliest_invalid
        for s in present_canon:
            if earliest_invalid and _stage_key(s) >= _stage_key(earliest_invalid):
                if path_by_canon.get(s):
                    plan.remove_paths.append(path_by_canon[s])
            else:
                plan.reuse.append(s)
                if path_by_canon.get(s):
                    # Touch the WHOLE stage artifact family (.odb + fine-grained
                    # yosys/floorplan/route intermediates), not just the canonical
                    # .odb — otherwise a config.mk re-stamp makes ORFS rebuild the
                    # earliest intermediate (e.g. 1_1_yosys_canonicalize.rtlil) and
                    # cascade a spurious full re-synth of a reused stage.
                    sibs = _stage_sibling_artifacts(results_dir, s)
                    if sibs:
                        for sp in sibs:
                            if sp not in plan.touch_paths:
                                plan.touch_paths.append(sp)
                    else:
                        plan.touch_paths.append(path_by_canon[s])

        # also remove any fine-grained sibling .odb at/after earliest_invalid
        if earliest_invalid:
            inv_key = _stage_key(earliest_invalid)
            for b, p in present:
                if _stage_key(b) >= inv_key and p not in plan.remove_paths:
                    plan.remove_paths.append(p)

        # rerun_from
        if earliest_invalid:
            plan.rerun_from = earliest_invalid
            plan.reason += (f"config/sdc change invalidates {earliest_invalid} onward "
                            f"(changed: {', '.join(plan.changed_keys) or 'n/a'}); "
                            f"reusing {', '.join(plan.reuse) or 'nothing'}.")
            if _stage_key(earliest_invalid) <= _stage_key("1_synth"):
                plan.full_rebuild = True
        else:
            # nothing invalidated: resume from the first stage never reached
            highest = present_canon[-1] if present_canon else None
            nxt = None
            if highest and highest in STAGE_ORDER:
                i = STAGE_ORDER.index(highest)
                nxt = STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None
            plan.rerun_from = nxt
            plan.reason += (f"no substantive config/sdc change; reusing "
                            f"{', '.join(plan.reuse) or 'nothing'}, resuming at "
                            f"{nxt or 'finish (all stages present)'}.")
        return plan

    def apply_resume_plan(self, plan: ResumePlan, config_path: Optional[str] = None,
                          dry_run: bool = False) -> Dict[str, Any]:
        """Execute a plan's filesystem actions: TOUCH reused .odb newer than the
        config (defeat re-stamp mtime churn) and REMOVE invalidated .odb.
        Returns a summary dict.  Safe / idempotent."""
        summary = {"touched": [], "removed": [], "errors": []}
        if plan.full_rebuild:
            # redo everything: remove all present odbs so make can't shortcut
            for p in list(plan.touch_paths) + list(plan.remove_paths):
                if dry_run:
                    summary["removed"].append(p)
                    continue
                try:
                    if os.path.exists(p):
                        os.remove(p)
                        summary["removed"].append(p)
                except OSError as e:
                    summary["errors"].append(f"rm {p}: {e}")
            return summary
        # determine a target mtime a bit AFTER the config, so make sees reused
        # stages as up-to-date even though the re-stamp bumped config mtime.
        base_t = time.time()
        if config_path and os.path.isfile(config_path):
            base_t = max(base_t, os.stat(config_path).st_mtime + 1.0)
        for p in plan.remove_paths:
            if dry_run:
                summary["removed"].append(p)
                continue
            try:
                if os.path.exists(p):
                    os.remove(p)
                    summary["removed"].append(p)
            except OSError as e:
                summary["errors"].append(f"rm {p}: {e}")
        for p in plan.touch_paths:
            if dry_run:
                summary["touched"].append(p)
                continue
            try:
                if os.path.exists(p):
                    os.utime(p, (base_t, base_t))
                    summary["touched"].append(p)
            except OSError as e:
                summary["errors"].append(f"touch {p}: {e}")
        return summary

    # ---- human-readable status ---------------------------------------------
    def render_status(self, blocks: Optional[List[str]] = None) -> str:
        lines: List[str] = []
        lines.append(f"JANUS flow-state manifest: {self.path}")
        lines.append(f"updated: {self.state.get('_updated', 'n/a')}")
        bl = self.state.get("blocks", {})
        names = blocks or sorted(bl)
        if not names:
            lines.append("(no blocks recorded yet)")
            return "\n".join(lines)
        for b in names:
            rec = bl.get(b, {})
            status = rec.get("status", "PENDING")
            stages = rec.get("stages", {})
            done = sorted((s for s, r in stages.items() if r.get("status") == "DONE"),
                          key=_stage_key)
            lines.append(f"\n[{b}]  status={status}")
            marks = []
            for s in STAGE_ORDER:
                marks.append(f"{'✓' if s in done else '·'} {s}")
            lines.append("   stages: " + "  ".join(marks))
            fq = rec.get("final_qor")
            if fq:
                lines.append("   QoR: " + ", ".join(f"{k}={v}" for k, v in fq.items()))
            runs = rec.get("runs", [])
            if runs:
                lines.append(f"   runs: {len(runs)} "
                             f"(latest: {runs[-1].get('run_dir')} -> {runs[-1].get('result')})")
        gc = self.gc_candidates()
        if gc:
            lines.append(f"\ncleanup: {len(gc)} run-dir(s) superseded & safe to delete:")
            for e in gc:
                lines.append(f"   {e['run_dir']}  ({', '.join(e['blocks'])})")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# self-test — exercises stage ordering, scan, semantic resume planning, the
# re-stamp no-op case, config-change invalidation, and cleanup.  Runs entirely
# on synthetic temp files (no ORFS needed).
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import shutil

    tmp = tempfile.mkdtemp(prefix="flowstate_")
    try:
        # ----- stage key / canonical folding -----
        assert _stage_key("1_synth") == (1, 0)
        assert _stage_key("5_2_route") == (5, 2)
        assert _canonical_stage("5_1_grt") == "5_route"
        assert _canonical_stage("5_2_route") == "5_route"
        assert _canonical_stage("2_floorplan") == "2_floorplan"
        print("stage-key / canonical folding OK")

        # ----- synthetic ORFS results dir: synth..global-route done, detail not
        results = os.path.join(tmp, "results", "asap7", "user_core", "base")
        os.makedirs(results)
        for name in ("1_synth.odb", "2_floorplan.odb", "3_place.odb",
                     "4_cts.odb", "5_1_grt.odb"):
            with open(os.path.join(results, name), "wb") as fh:
                fh.write(os.urandom(1024))

        # synthetic config.mk
        cfg = os.path.join(tmp, "user_core.mk")
        with open(cfg, "w") as fh:
            fh.write("# comment\nexport SYNTH_MEMORY_MAX_BITS = 16384\n"
                     "export PLACE_DENSITY = 0.65\nexport CORE_UTILIZATION = 55\n")

        fs = FlowState(os.path.join(tmp, ".janus_state", "state.json")).load()
        rec = fs.update_block_from_scan("user_core", results, config_path=cfg)
        assert fs.block_status("user_core") == "5_route", fs.block_status("user_core")
        assert fs.last_completed_stage("user_core") == "5_route"
        print("scan + status OK:", fs.block_status("user_core"),
              "last=", fs.last_completed_stage("user_core"))

        # ----- resume with NO config change (only mtime churn, like scripts/03) -
        # simulate a re-stamp: rewrite config with reordered comments/whitespace
        # but IDENTICAL variables, and bump its mtime far into the future.
        with open(cfg, "w") as fh:
            fh.write("export CORE_UTILIZATION = 55   # re-stamped\n"
                     "\n\nexport PLACE_DENSITY = 0.65\n"
                     "# new banner\nexport SYNTH_MEMORY_MAX_BITS = 16384\n")
        os.utime(cfg, (time.time() + 1000, time.time() + 1000))
        plan = fs.resume_plan("user_core", results, config_path=cfg)
        assert not plan.config_changed, f"restamp must be a no-op, got {plan.changed_keys}"
        assert set(plan.reuse) == {"1_synth", "2_floorplan", "3_place", "4_cts", "5_route"}, plan.reuse
        assert plan.rerun_from == "6_final", plan.rerun_from
        summ = fs.apply_resume_plan(plan, config_path=cfg)
        # every reused odb must now be NEWER than the (future-dated) config
        cfg_m = os.stat(cfg).st_mtime
        for p in plan.touch_paths:
            assert os.stat(p).st_mtime >= cfg_m, "touch did not defeat mtime churn"
        assert len(summ["touched"]) == 5 and not summ["removed"], summ
        print("re-stamp no-op resume OK: reuse all, touch 5, remove 0")

        # ----- resume WITH a place-level config change -----
        with open(cfg, "w") as fh:
            fh.write("export SYNTH_MEMORY_MAX_BITS = 16384\n"
                     "export PLACE_DENSITY = 0.72\nexport CORE_UTILIZATION = 55\n")
        plan2 = fs.resume_plan("user_core", results, config_path=cfg)
        assert plan2.config_changed and plan2.rerun_from == "3_place", plan2.to_dict()
        assert set(plan2.reuse) == {"1_synth", "2_floorplan"}, plan2.reuse
        summ2 = fs.apply_resume_plan(plan2, config_path=cfg, dry_run=True)
        # 3_place, 4_cts, 5_1_grt must be scheduled for removal
        removed_names = {os.path.basename(p) for p in summ2["removed"]}
        assert {"3_place.odb", "4_cts.odb", "5_1_grt.odb"} <= removed_names, removed_names
        print("place-change resume OK: reuse synth+fp, rerun from 3_place, remove 3")

        # ----- resume WITH a synth-level change -> full rebuild -----
        with open(cfg, "w") as fh:
            fh.write("export SYNTH_MEMORY_MAX_BITS = 32768\n"
                     "export PLACE_DENSITY = 0.72\nexport CORE_UTILIZATION = 55\n")
        plan3 = fs.resume_plan("user_core", results, config_path=cfg)
        assert plan3.full_rebuild and plan3.rerun_from == "1_synth", plan3.to_dict()
        print("synth-change resume OK: full rebuild from 1_synth")

        # ----- run ledger + gc -----
        fs.record_run("user_core", os.path.join(tmp, "runs", "r1"),
                      None, None, "FAILED", reached_stage="5_route")
        fs.record_run("user_core", os.path.join(tmp, "runs", "r2"),
                      None, None, "FAILED", reached_stage="5_route")
        gc = fs.gc_candidates()
        assert any(e["run_dir"].endswith("r1") for e in gc), gc
        assert not any(e["run_dir"].endswith("r2") for e in gc), "newest run must be kept"
        print("run ledger + gc OK: r1 superseded, r2 kept")

        # ----- persistence round-trip -----
        fs.save()
        fs2 = FlowState(fs.path).load()
        assert fs2.block_status("user_core") == "5_route"
        print("persistence round-trip OK")

        print("\nrender_status():\n" + fs.render_status())
        print("\nALL flow_state self-tests PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
