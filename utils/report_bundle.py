#!/usr/bin/env python3
"""
report_bundle.py — per-block, stage-aware report bundle + QoR summary (§120/§121).

Used by notification.py when a block settles (DONE / FAILED / NEEDS_MANUAL).
Replaces the old "glob everything under the shared run dir" tarball, which
(under --parallel) shipped every block's agent log, lost results/ and the final
reports to a first-come-first-served 50 MB cap, and printed clamped / mislabeled
QoR in the email.

What it does, for exactly ONE block:
  1. detect the stage reached (and, for failures, the stage that failed) from
     the ORFS logs/reports trees of THAT block only;
  2. pick files by a documented stage table (STAGE_TABLE), in priority order so
     the critical files (6_finish.rpt, DRC, failing-stage log tail, ...) are
     always added before bulky ones; big text files are head/tail-truncated
     instead of dropping everything after them; heavy binaries are never added;
  3. build qor.json (timing per path group, violator counts, DRC, antenna, GRT
     overflow, area/util/power, clock period/fmax, skew/latency, stage info,
     failure triage) reusing the dashboard's parsers in agent/log_analyzer.py;
  4. write INDEX.md (included + why, skipped + why) and the tar.gz;
  5. render the plain-text email body.

Everything is stdlib. Units: ASAP7 Liberty time_unit = 1ps, so every slack /
TNS / skew value here is PICOSECONDS; area um^2; power W; frequency MHz.

CLI (box-side rerun of only the notifier/report step on an existing block):
    python3 asic_flow/utils/report_bundle.py --block noc \
        --flow-dir $ORFS_DIR/flow --variant base \
        --agent-log asic_flow/agent/runs/<ts>/noc.log --status DONE \
        --out /tmp/noc_bundle            # writes qor.json, INDEX.md, tar.gz
        [--dry-run]                      # print email + plan, write nothing
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import io
import json
import os
import re
import sys
import tarfile
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_AGENT = os.path.join(os.path.dirname(_HERE), "agent")
if _AGENT not in sys.path:
    sys.path.insert(0, _AGENT)

try:  # the dashboard's parsers — single source of truth for timing
    import log_analyzer as _la  # type: ignore
except Exception:  # pragma: no cover - degraded mode
    _la = None
try:
    from stage_timing_analyzer import parse_drc as _parse_drc  # type: ignore
except Exception:  # pragma: no cover
    _parse_drc = None

# ---------------------------------------------------------------------------
# Stage model
# ---------------------------------------------------------------------------
STAGES = ["synth", "floorplan", "place", "cts", "grt", "route", "finish"]
_STAGE_IDX = {s: i for i, s in enumerate(STAGES)}
_JANUS_NN_STAGE = {"01": "floorplan", "02": "floorplan", "03": "place",
                   "04": "place", "05": "place", "06": "cts", "07": "grt",
                   "08": "route", "09": "finish"}

HEAVY_EXTS = (".odb", ".def", ".gds", ".gds.gz", ".oas", ".spef", ".spef.gz",
              ".lef", ".lib", ".lib.gz", ".v.gz", ".cdl", ".rtlil", ".db",
              ".guide")
IMAGE_EXTS = (".png", ".webp", ".jpg")


def stage_of(name: str) -> Optional[str]:
    """Flow stage an ORFS / JANUS artifact belongs to (None = unknown)."""
    b = os.path.basename(name)
    m = re.match(r"janus_(\d\d)_", b)
    if m:
        return _JANUS_NN_STAGE.get(m.group(1))
    if b.startswith(("synth_", "1_")):
        return "synth"
    if b.startswith("2_"):
        return "floorplan"
    if b.startswith("3_"):
        return "place"
    if b.startswith("4_"):
        return "cts"
    if b.startswith(("5_1_", "grt_", "congestion", "5_global_route")):
        return "grt"
    if b.startswith(("5_", "drt_")):
        return "route"
    if b.startswith(("6_",)):
        return "finish"
    return None


def _stage_from_manifest(st: str) -> Optional[str]:
    """'5_2_route' / '4_cts' -> stage name."""
    return stage_of(st + "_x") if st else None


# STAGE_TABLE: what to bundle per stage. Each entry is
#   (root, glob, priority, mode, limit_bytes, reason)
# root: logs (ORFS logs/<plat>/<block>/<variant>), reports, results
# priority: lower = added first.  mode: "full" | "head" | "tail" (truncation
# side when over limit / remaining cap).  The table is also rendered into
# INDEX.md and documented in scripts/hooks/README.md.
STAGE_TABLE: Dict[str, List[Tuple[str, str, int, str, int, str]]] = {
    "synth": [
        ("logs", "1_*yosys*.log", 20, "tail", 1 << 20, "yosys log tail (errors/warnings at end)"),
        ("logs", "1_*.log", 25, "tail", 1 << 20, "synth step logs"),
        ("reports", "synth_stat.txt", 21, "full", 1 << 20, "cell stat / area"),
        ("reports", "synth_check.txt", 22, "full", 1 << 20, "yosys check report"),
        ("reports", "1_*.rpt", 23, "full", 1 << 20, "synth report"),
        ("logs", "1_*.json", 40, "full", 1 << 20, "synth metrics"),
        ("results", "1_*.sdc", 50, "full", 1 << 20, "post-synth SDC"),
        ("results", "clock_period.txt", 50, "full", 1 << 16, "synth clock period"),
        ("results", "mem*.json", 52, "full", 1 << 20, "memory inference summary"),
    ],
    "floorplan": [
        ("reports", "2_floorplan_final.rpt", 22, "head", 2 << 20, "floorplan timing/area report"),
        ("logs", "2_*.log", 25, "tail", 1 << 20, "floorplan/macro/tapcell/PDN logs"),
        ("logs", "2_*.json", 40, "full", 1 << 20, "floorplan metrics"),
        ("results", "2_*macro*.tcl", 50, "full", 1 << 20, "macro placement"),
        ("results", "2_*.sdc", 52, "full", 1 << 20, "floorplan SDC"),
    ],
    "place": [
        ("reports", "3_*.rpt", 22, "head", 2 << 20, "global place / resizer / detail place reports"),
        ("logs", "3_*gp*.log", 24, "tail", 1 << 20, "GPL log (overflow / divergence)"),
        ("logs", "3_*resiz*.log", 24, "tail", 1 << 20, "resizer log"),
        ("logs", "3_*.log", 25, "tail", 1 << 20, "placement step logs (IO placement, DPL)"),
        ("logs", "3_*.json", 40, "full", 1 << 20, "placement metrics"),
        ("results", "3_*iop*.tcl", 50, "full", 1 << 20, "IO pin placement"),
        ("results", "3_*.sdc", 52, "full", 1 << 20, "placement SDC"),
    ],
    "cts": [
        ("reports", "4_cts_final.rpt", 21, "head", 2 << 20, "CTS timing, skew/latency"),
        ("logs", "4_*.log", 24, "tail", 1 << 20, "CTS log"),
        ("logs", "4_*.json", 40, "full", 1 << 20, "CTS metrics"),
        ("results", "4_*.sdc", 52, "full", 1 << 20, "CTS SDC (propagated clocks)"),
    ],
    "grt": [
        ("reports", "5_global_route.rpt", 21, "head", 2 << 20, "GRT timing report"),
        ("reports", "congestion.rpt", 21, "head", 2 << 20, "GRT congestion report"),
        ("reports", "grt_antennas.log", 22, "full", 1 << 20, "post-GRT antenna check"),
        ("logs", "5_1_*.log", 23, "tail", 1 << 20, "GRT log (congestion table, overflow)"),
        ("logs", "5_1_*.json", 40, "full", 1 << 20, "GRT metrics"),
    ],
    "route": [
        ("reports", "5_route_drc.rpt", 20, "head", 2 << 20, "detail-route DRC"),
        ("reports", "drt_antennas.log", 21, "full", 1 << 20, "post-route antenna check"),
        ("logs", "5_2_*.log", 22, "tail", 1 << 20, "DRT log (iterations, violations, memory)"),
        ("logs", "5_3_*.log", 26, "tail", 1 << 19, "fill-cell log"),
        ("logs", "5_*.json", 40, "full", 1 << 20, "route metrics"),
        ("results", "5_*.sdc", 52, "full", 1 << 20, "route SDC"),
    ],
    "finish": [
        ("reports", "6_finish.rpt", 10, "head", 8 << 20, "FINAL signoff STA (path groups, slew/cap/fanout, power, skew)"),
        ("reports", "janus_09_final_debug.rpt", 12, "head", 4 << 20, "post-final debug report (top-N paths, skew, DRV, histogram)"),
        ("logs", "6_report.json", 13, "full", 2 << 20, "final metrics (area/util/power)"),
        ("logs", "6_*.log", 14, "tail", 1 << 20, "final report log (design area / util)"),
        ("reports", "V*.rpt", 30, "head", 1 << 20, "IR-drop reports"),
        ("results", "6_final.sdc", 15, "full", 1 << 20, "final SDC (clock period)"),
        ("results", "6_*.json", 52, "full", 1 << 20, "final results metadata"),
    ],
}

# Generic, stage-independent selections.
_JANUS_PATHS_LIMIT = 2 << 20           # head of janus_NN_*_paths.rpt
_AGENT_LOG_LIMIT = 2 << 20             # tail of the agent block log
_TRUNC_MIN = 64 << 10                  # don't bother adding < 64 KB slices


def _is_ara(block: str) -> bool:
    return "ara" in (block or "").lower()


def masku_wanted(block: str) -> bool:
    """masku per-op path-group reports are an Ara-only debug artifact."""
    v = os.environ.get("JANUS_MASKU_GROUPS", "")
    if v == "1":
        return True
    if v == "0":
        return False
    return _is_ara(block)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def _read(path: Optional[str], limit: Optional[int] = None) -> str:
    if not path or not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return ""


def _read_tail(path: str, nbytes: int) -> str:
    try:
        sz = os.path.getsize(path)
        with open(path, "rb") as fh:
            if sz > nbytes:
                fh.seek(sz - nbytes)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _fnum(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _ls(d: Optional[str]) -> List[str]:
    if not d or not os.path.isdir(d):
        return []
    try:
        return sorted(os.path.join(d, f) for f in os.listdir(d)
                      if os.path.isfile(os.path.join(d, f)))
    except OSError:
        return []


# ---------------------------------------------------------------------------
# Stage detection
# ---------------------------------------------------------------------------
def detect_stages(logs_dir: Optional[str], reports_dir: Optional[str],
                  status: str, manifest_stage: str = "") -> Tuple[str, str]:
    """Return (stage_reached, failed_stage).

    stage_reached: furthest stage with a log/report on disk for this block.
    failed_stage : for non-DONE runs, the stage whose ORFS log was written
                   last (the step that was running when the block died);
                   falls back to the manifest's last completed stage + 1.
    """
    best = -1
    newest: Tuple[float, Optional[str]] = (-1.0, None)
    for f in _ls(logs_dir) + _ls(reports_dir):
        st = stage_of(f)
        if st is None:
            continue
        best = max(best, _STAGE_IDX[st])
        if f.endswith(".log") and os.path.dirname(f) == (logs_dir or "#"):
            mt = os.path.getmtime(f)
            if mt > newest[0]:
                newest = (mt, st)
    reached = STAGES[best] if best >= 0 else ""
    failed = ""
    if str(status).upper() != "DONE":
        if newest[1]:
            failed = newest[1]
        else:
            ms = _stage_from_manifest(manifest_stage)
            if ms:
                failed = STAGES[min(_STAGE_IDX[ms] + 1, len(STAGES) - 1)]
            else:
                failed = reached or "synth"
    else:
        reached = reached or "finish"
    return reached, failed


# ---------------------------------------------------------------------------
# Parsers (6_finish extras, DRC, antenna, GRT overflow, area, clock)
# ---------------------------------------------------------------------------
_RE_SEC = re.compile(r"^finish\s+(.+?)\s*$")
_RE_SKEW_ROW = re.compile(r"^\s*(-?\d+\.?\d*)\s+(source latency|target latency|clock uncertainty|CRPR|setup skew|hold skew)\b(.*)$")
_RE_PMIN = re.compile(r"^\s*(\S+)\s+period_min\s*=\s*(-?\d+\.?\d*)\s+fmax\s*=\s*(-?\d+\.?\d*)")
_RE_PWR_TOTAL = re.compile(r"^\s*Total\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+")
_RE_CORNER = re.compile(r"^\s*Corner:\s*(\S+)")
_RE_DRV_ROW = re.compile(r"^\s*(\S+)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+\(VIOLATED\)")
_RE_SCALAR = re.compile(r"^\s*(-?\d+\.?\d*(?:[eE][-+]?\d+)?)\s*$")


def _hier_bucket(pin: str) -> str:
    """Top-level hierarchy of a pin/instance name (for violator bucketing)."""
    inst = pin.rsplit("/", 1)[0]
    for sep in (".", "/"):
        if sep in inst:
            return inst.split(sep, 1)[0]
    return "<top>"


def parse_finish_extras(text: str) -> Dict[str, object]:
    """Section-aware pass over 6_finish.rpt for what parse_final_sta_rpt does
    not cover: clock min period, skew/latency, critical path delay/slack,
    power per corner, DRV violator tables (worst slack, top pins, buckets)."""
    out: Dict[str, object] = {"clock": None, "period_min": None, "fmax_mhz": None,
                              "skew": {}, "critical_path": {}, "power": {},
                              "drv": {}}
    sec = ""
    drv_kind = None
    corner = None
    expect_scalar = None
    for line in text.splitlines():
        m = _RE_SEC.match(line)
        if m:
            sec = m.group(1)
            drv_kind = None
            corner = None
            expect_scalar = {"critical path delay": "delay",
                             "critical path slack": "slack",
                             "critical path target clock latency max path": "target_latency_max",
                             "critical path source clock latency min path": "source_latency_min",
                             }.get(sec)
            continue
        if expect_scalar:
            ms = _RE_SCALAR.match(line)
            if ms:
                out["critical_path"][expect_scalar] = float(ms.group(1))
                expect_scalar = None
            continue
        if sec.startswith("report_clock_min_period"):
            mp = _RE_PMIN.match(line)
            if mp and out["period_min"] is None:
                out["clock"], out["period_min"], out["fmax_mhz"] = (
                    mp.group(1), float(mp.group(2)), float(mp.group(3)))
        elif sec.startswith("report_clock_skew"):
            mk = _RE_SKEW_ROW.match(line)
            if mk:
                key = mk.group(2).replace(" ", "_").lower()
                out["skew"].setdefault(key, float(mk.group(1)))
                if key in ("source_latency", "target_latency"):
                    out["skew"].setdefault(key + "_pin", mk.group(3).strip())
            elif line.startswith("Clock ") and "clock" not in out["skew"]:
                out["skew"]["clock"] = line.split()[1]
        elif sec.startswith("report_power"):
            mc = _RE_CORNER.match(line)
            if mc:
                corner = mc.group(1)
                continue
            mt = _RE_PWR_TOTAL.match(line)
            if mt and corner and corner not in out["power"]:
                vals = [_fnum(x) for x in mt.groups()]
                out["power"][corner] = {"internal": vals[0], "switching": vals[1],
                                        "leakage": vals[2], "total": vals[3]}
        elif sec.startswith("report_check_types"):
            s = line.strip().lower()
            if s in ("max slew", "max capacitance", "max fanout"):
                drv_kind = {"max slew": "max_slew", "max capacitance": "max_cap",
                            "max fanout": "max_fanout"}[s]
                out["drv"].setdefault(drv_kind, {"rows": 0, "worst_slack": None,
                                                 "top": [], "by_module": {}})
                continue
            mr = _RE_DRV_ROW.match(line)
            if mr and drv_kind:
                d = out["drv"][drv_kind]
                slack = float(mr.group(4))
                d["rows"] += 1
                if d["worst_slack"] is None or slack < d["worst_slack"]:
                    d["worst_slack"] = slack
                if len(d["top"]) < 10:
                    d["top"].append({"pin": mr.group(1), "limit": float(mr.group(2)),
                                     "value": float(mr.group(3)), "slack": slack})
                b = _hier_bucket(mr.group(1))
                d["by_module"][b] = d["by_module"].get(b, 0) + 1
    for d in out["drv"].values():
        d["by_module"] = dict(sorted(d["by_module"].items(), key=lambda kv: -kv[1])[:10])
    return out


def parse_route_drc(path: Optional[str]) -> Optional[Dict[str, object]]:
    """Route DRC from 5_route_drc.rpt (the authoritative DRT output)."""
    if not path or not os.path.isfile(path):
        return None
    text = _read(path)
    if _parse_drc is not None:
        try:
            s = _parse_drc(text)
            return {"total": s.total, "by_type": s.by_type, "layers": s.layers,
                    "source": os.path.basename(path)}
        except Exception:
            pass
    types = re.findall(r"violation type:\s*(.+)", text)
    by: Dict[str, int] = {}
    for t in types:
        by[t.strip()] = by.get(t.strip(), 0) + 1
    return {"total": len(types), "by_type": by, "layers": {},
            "source": os.path.basename(path)}


_RE_DRT_VIOL = re.compile(r"\[INFO DRT-0199\]\s+Number of violations\s*=\s*(\d+)")
_RE_ANT = re.compile(r"\[INFO ANT-000([12])\]\s+Found\s+(\d+)\s+(pin|net)\s+violations")
_RE_GRT_TOTAL = re.compile(r"^Total\s+\d+\s+\d+\s+[\d.]+%\s+(\d+)\s*/\s*(\d+)\s*/\s*(\d+)")


def parse_antenna(text: str) -> Optional[Dict[str, int]]:
    net = pin = None
    for m in _RE_ANT.finditer(text):
        if m.group(3) == "net":
            net = int(m.group(2))
        else:
            pin = int(m.group(2))
    if net is None and pin is None:
        return None
    return {"nets": net or 0, "pins": pin or 0}


def parse_grt_overflow(text: str) -> Optional[Dict[str, int]]:
    """Last GRT-0096 'Final congestion report' Total row: overflow H/V/total."""
    res = None
    in_rep = False
    for line in text.splitlines():
        if "GRT-0096" in line:
            in_rep = True
            continue
        if in_rep:
            m = _RE_GRT_TOTAL.match(line.strip())
            if m:
                res = {"h": int(m.group(1)), "v": int(m.group(2)), "total": int(m.group(3))}
                in_rep = False
    return res


def _metrics_json(logs_dir: Optional[str]) -> Tuple[Dict, str]:
    """Latest ORFS metrics json for the block (6_report.json preferred)."""
    if not logs_dir:
        return {}, ""
    cands = [os.path.join(logs_dir, "6_report.json")]
    cands += sorted(glob.glob(os.path.join(logs_dir, "[0-9]_*.json")), reverse=True)
    for c in cands:
        if os.path.isfile(c):
            try:
                with open(c) as fh:
                    return json.load(fh), os.path.basename(c)
            except (OSError, ValueError):
                continue
    return {}, ""


def _mget(m: Dict, suffix: str):
    """Value of the metric whose key ends with suffix, latest stage first."""
    order = ["finish", "detailedroute", "globalroute", "cts", "detailedplace",
             "globalplace", "floorplan", "synth"]
    for st in order:
        k = f"{st}__{suffix}"
        if k in m:
            return m[k]
    for k, v in m.items():
        if k.endswith("__" + suffix):
            return v
    return None


def parse_sdc_period(results_dir: Optional[str]) -> Optional[Tuple[str, float]]:
    """(clock name, period ps) of the first port-sourced create_clock in the
    newest results SDC (write_sdc output is numeric)."""
    sdcs = sorted(glob.glob(os.path.join(results_dir or "#", "*.sdc")),
                  key=lambda p: (os.path.basename(p) != "6_final.sdc", -os.path.getmtime(p)))
    for s in sdcs:
        for line in _read(s).splitlines():
            if line.lstrip().startswith("create_clock") and "get_ports" in line:
                mp = re.search(r"-period\s+(\d+\.?\d*)", line)
                mn = re.search(r"-name\s+(\S+)", line)
                if mp:
                    return (mn.group(1) if mn else "?"), float(mp.group(1))
    return None


# ---------------------------------------------------------------------------
# Failure triage
# ---------------------------------------------------------------------------
# (regex, category, diagnosis, suggested knob). Order = priority.
TRIAGE_RULES: List[Tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"JANUS SDC:.*empty|object list is empty|matched no objects", re.I),
     "sdc-empty-list",
     "An SDC collection resolved to an empty list and the JANUS guard aborted the "
     "constraint load (otherwise set_input_delay/set_driving_cell would silently "
     "apply to nothing and leave paths unconstrained).",
     "Fix the get_ports/get_pins filter in the block SDC (e.g. 'direction == input', "
     "port names after synth renaming); do not delete the guard."),
    (re.compile(r"\b(Killed|Out of memory|Cannot allocate memory|bad_alloc|MemoryError)\b|Error 247|Error 137|signal 9", re.I),
     "oom-killed",
     "The tool process was killed by the OS (out of memory): no tool ERROR was "
     "printed, the step exited on a signal / make Error 247/137.",
     "Lower parallelism (--num-cores / ROUTING threads), reduce design size "
     "(mock memories), or rerun on a larger-RAM host; for DRT raise WSL2 memory."),
    (re.compile(r"(timed out|timeout|Terminated|SIGTERM|signal 15).*", re.I),
     "timing-repair-timeout",
     "The step was terminated by a wall-clock limit (agent/stage watchdog), most "
     "often inside repair_timing / repair_design on a large negative-slack design.",
     "Bound repair: SETUP_SLACK_MARGIN / TNS_END_PERCENT=5..20, SKIP_LAST_GASP=1, "
     "SKIP_INCREMENTAL_REPAIR=1, or relax the clock period for the first pass."),
    (re.compile(r"(Module|module)\s+\\?\S+.*(is not part of the design|not found|referenced.*not defined)|"
                r"Can't open include|unknown module|ERROR:\s+Module", re.I),
     "synth-missing-module",
     "Yosys could not resolve a module / include during elaboration (missing source "
     "file, wrong top, or a macro without a blackbox stub).",
     "Add the file to VERILOG_FILES / VERILOG_INCLUDE_DIRS, check DESIGN_NAME, or "
     "declare hardened macros via SYNTH_BLACKBOXES / ADDITIONAL_LEFS+ADDITIONAL_LIBS."),
    (re.compile(r"(liberty|\.lib).*(not found|No such file|can't open|cannot open)|ORD-2011|no liberty cell", re.I),
     "synth-missing-lib",
     "A Liberty view is missing: a macro master has no timing model (ORD-2011) or a "
     ".lib path does not exist, so timing through it is black-boxed or the load fails.",
     "Add the macro .lib to ADDITIONAL_LIBS (and LEF to ADDITIONAL_LEFS); verify the path."),
    (re.compile(r"\bPDN-\d{4}", re.I), "pdn",
     "Power-grid generation failed (PDN-*): straps/rings cannot be placed, usually "
     "because macros/halos leave no legal channel or the grid pitch does not fit the core.",
     "Check PDN_TCL for the block, increase MACRO_PLACE_HALO / channel width, or "
     "lower CORE_UTILIZATION to enlarge the core."),
    (re.compile(r"\bPPL-\d{4}", re.I), "io-placement",
     "IO pin placement failed (PPL-*): more pins than legal slots on the selected "
     "edges/layers, or conflicting pin constraints.",
     "Enlarge the die (CORE_UTILIZATION / DIE_AREA), relax PLACE_PINS_ARGS / pin "
     "constraints, or add IO_PLACER_H/V layers."),
    (re.compile(r"\bGPL-03\d\d|diverge", re.I), "gpl-divergence",
     "Global placement diverged / overflow failed to converge (GPL-03xx): density "
     "target too high for the available area, or routability/timing-driven mode thrashing.",
     "Lower PLACE_DENSITY (or PLACE_DENSITY_LB_ADDON) / CORE_UTILIZATION; try "
     "GPL_TIMING_DRIVEN=0 and/or GPL_ROUTABILITY_DRIVEN=0 for a first pass."),
    (re.compile(r"\bRSZ-\d{4}", re.I), "resizer",
     "The resizer (repair_design / repair_timing) errored (RSZ-*), e.g. no buffer "
     "cell available, dont_use too broad, or a max-wire-length it cannot meet.",
     "Review DONT_USE_CELLS / MIN_BUF_CELL_AND_PORTS, MAX_WIRE_LENGTH, and "
     "SETUP/HOLD_SLACK_MARGIN; SKIP_PIN_SWAP / SKIP_GATE_CLONING to narrow down."),
    (re.compile(r"\bCTS-\d{4}", re.I), "cts",
     "Clock-tree synthesis failed (CTS-*): clock not found in SDC, no root buffer, "
     "or sinks unreachable.",
     "Verify create_clock in the SDC names the real port; tune CTS_BUF_CELL, "
     "CTS_CLUSTER_SIZE / CTS_CLUSTER_DIAMETER, CTS_BUF_DISTANCE."),
    (re.compile(r"\bGRT-0(116|118|119|228)|congestion.*(too high|overflow)|Routing congestion", re.I),
     "grt-overflow",
     "Global routing could not resolve congestion (GRT overflow): routing demand "
     "exceeds track supply in hotspots.",
     "Lower CORE_UTILIZATION / PLACE_DENSITY, raise ROUTING_LAYER_ADJUSTMENT headroom "
     "carefully, enable GPL_ROUTABILITY_DRIVEN=1, or spread macros (halo)."),
    (re.compile(r"\bANT-\d{4}|antenna", re.I), "antenna",
     "Antenna repair failed / violations remain after repair_antennas.",
     "Enable diode insertion (DIODE_CELL / repair_antennas iterations), raise "
     "MAX_REPAIR_ANTENNAS_ITER_GRT / _DRT; check that ANTENNA rules exist in the LEF."),
    (re.compile(r"\bDRT-\d{4}|violations remain|detailed route.*(fail|not converge)", re.I),
     "drt-nonconvergence",
     "Detailed routing did not converge (DRT errors or residual violations), usually "
     "a consequence of GRT hotspots or pin-access problems on dense cells.",
     "Fix GRT congestion first (lower density), raise DETAILED_ROUTE_END_ITERATION, "
     "check pin access (DETAILED_ROUTE_ARGS -droute_end_iter / via-in-pin)."),
]

_RE_TAGGED_ERR = re.compile(r"\[(ERROR|FATAL)\s+([A-Z]{2,6}-\d+)\]")
_RE_UNTAGGED_ERR = re.compile(r"^(Error:|ERROR:|FATAL)|make.*\*\*\*.*Error\s+\d+|\bKilled\b|"
                              r"JANUS SDC:.*empty|Command terminated by signal", re.I)


def _ignored(line: str) -> bool:
    if _la is not None:
        return any(rx.search(line) for rx in _la._IGNORE_PATTERNS)
    return False


def find_first_error(text: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    """(first real error line, tool code or None, last error lines)."""
    first = None
    code = None
    errs: List[str] = []
    for line in text.splitlines():
        s = line.rstrip()
        if not s or _ignored(s):
            continue
        mt = _RE_TAGGED_ERR.search(s)
        if mt or _RE_UNTAGGED_ERR.search(s.strip()):
            errs.append(s.strip()[:300])
            if first is None:
                first = s.strip()[:300]
                code = mt.group(2) if mt else None
    return first, code, errs[-10:]


def triage_failure(texts: List[Tuple[str, str]], failed_stage: str) -> Dict[str, object]:
    """Classify the first real error across (name, text) logs, failing-stage
    log first. Returns {category, diagnosis, knob, first_error, code, source,
    last_errors}. Falls back to log_analyzer._TRIAGE_RULES and to an
    OOM-silent heuristic (log ends on a 'Peak memory' banner, no error)."""
    for name, text in texts:
        first, code, last = find_first_error(text)
        if not first:
            continue
        hay = first
        for rx, cat, diag, knob in TRIAGE_RULES:
            if rx.search(hay):
                return {"category": cat, "diagnosis": diag, "knob": knob,
                        "first_error": first, "code": code, "source": name,
                        "last_errors": last}
        if _la is not None:
            for rx, cat, msg in _la._TRIAGE_RULES:
                if rx.search(hay):
                    return {"category": cat, "diagnosis": msg, "knob": "",
                            "first_error": first, "code": code, "source": name,
                            "last_errors": last}
        return {"category": "unclassified",
                "diagnosis": f"First error in {name} ({failed_stage} stage) is not a "
                             f"known signature; read the quoted line and the log tail.",
                "knob": "", "first_error": first, "code": code, "source": name,
                "last_errors": last}
    for name, text in texts:
        tail = text[-4000:]
        if re.search(r"Peak memory", tail) and not re.search(r"\bExit\b|finished", tail[-400:]):
            return {"category": "oom-killed",
                    "diagnosis": f"{name} ends on OpenROAD's 'Peak memory' banner with no "
                                 "ERROR line: the process was most likely killed by the OS "
                                 "(OOM-silent; make reports Error 247).",
                    "knob": TRIAGE_RULES[1][3], "first_error": None, "code": None,
                    "source": name, "last_errors": tail.strip().splitlines()[-5:]}
    return {"category": "unknown", "diagnosis": "No error line found in the bundled logs.",
            "knob": "", "first_error": None, "code": None, "source": None,
            "last_errors": []}


# ---------------------------------------------------------------------------
# QoR
# ---------------------------------------------------------------------------
GROUPS = ("reg2reg", "reg2out", "in2reg", "in2out")


def build_qor(block: str, status: str, logs_dir: Optional[str],
              reports_dir: Optional[str], results_dir: Optional[str],
              agent_log: Optional[str] = None, manifest_stage: str = "",
              tries: Optional[int] = None, note: str = "") -> Dict[str, object]:
    reached, failed = detect_stages(logs_dir, reports_dir, status, manifest_stage)
    q: Dict[str, object] = {
        "schema": "janus.qor/1", "block": block, "status": status,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "units": {"time": "ps", "area": "um^2", "power": "W", "freq": "MHz"},
        "stage_reached": reached, "failed_stage": failed or None,
        "tries": tries, "note": note or None,
        "dirs": {"logs": logs_dir, "reports": reports_dir, "results": results_dir,
                 "agent_log": agent_log},
    }
    # ---- timing (dashboard parser) ----
    t = None
    if _la is not None:
        try:
            t = _la.read_block_timing(reports_dir)
        except Exception:
            t = None
    t = t or {}
    pg = t.get("path_groups") or {"setup": {}, "hold": {}}
    counts = t.get("counts") or {}
    setup_worst = t.get("setup_worst")
    if setup_worst is None:
        setup_worst = t.get("setup_ws")
    timing = {
        "source": t.get("source"), "source_file": os.path.basename(t["_source"]) if t.get("_source") else None,
        "final": bool(t.get("final")),
        "setup_wns": t.get("setup_ws"),          # ORFS 'wns max' (clamped to 0 when met)
        "setup_worst_slack": setup_worst,        # true worst setup slack
        "setup_tns": t.get("setup_tns"),
        "hold_worst_slack": t.get("hold_ws"),
        "groups": {"setup": {g: pg.get("setup", {}).get(g) for g in GROUPS},
                   "hold": {g: pg.get("hold", {}).get(g) for g in GROUPS}},
        "other_groups": {k: {g: v for g, v in pg.get(k, {}).items() if g not in GROUPS}
                         for k in ("setup", "hold")},
        "violators": {"setup": counts.get("setup"), "hold": counts.get("hold")},
        "verdict": (_la.timing_verdict(setup_worst, t.get("hold_ws")) if _la else None),
        "wns_note": "ORFS 'wns max' is clamped to 0 when met; setup_worst_slack "
                    "('worst slack max') is the true worst setup slack.",
    }
    q["timing"] = timing
    q["drv"] = {"max_slew": counts.get("max_slew"), "max_cap": counts.get("max_cap"),
                "max_fanout": counts.get("max_fanout")}
    # ---- 6_finish extras ----
    fin = os.path.join(reports_dir or "#", "6_finish.rpt")
    ex = parse_finish_extras(_read(fin)) if os.path.isfile(fin) and t.get("source") == "6_finish" else {}
    q["clock_tree"] = ex.get("skew") or {}
    q["critical_path"] = ex.get("critical_path") or {}
    q["drv_detail"] = ex.get("drv") or {}
    pw = ex.get("power") or {}
    # ---- clock / frequency ----
    sdc = parse_sdc_period(results_dir)
    clk = {"name": None, "period": None, "target_mhz": None, "achieved_mhz": None,
           "period_min": ex.get("period_min"), "source": None}
    if sdc:
        clk["name"], clk["period"], clk["source"] = sdc[0], sdc[1], "sdc"
    if ex.get("clock"):
        clk["name"] = ex["clock"]
    if clk["period"] is None and ex.get("period_min") is not None and setup_worst is not None:
        clk["period"] = round(ex["period_min"] + setup_worst, 2)
        clk["source"] = "period_min+worst_slack"
    if clk["period"]:
        clk["target_mhz"] = round(1e6 / clk["period"], 2)
    fmax = ex.get("fmax_mhz") or t.get("fmax_mhz")
    if fmax:
        clk["achieved_mhz"] = fmax
    elif clk["period"] and setup_worst is not None and clk["period"] - setup_worst > 0:
        clk["achieved_mhz"] = round(1e6 / (clk["period"] - min(setup_worst, 0.0)), 2)
    q["clock"] = clk
    # ---- DRC / antenna / GRT overflow ----
    drc = parse_route_drc(os.path.join(reports_dir or "#", "5_route_drc.rpt"))
    route_log = _read(os.path.join(logs_dir or "#", "5_2_route.log"))
    if drc is None:
        vs = _RE_DRT_VIOL.findall(route_log)
        if vs:
            drc = {"total": int(vs[-1]), "by_type": {}, "layers": {},
                   "source": "5_2_route.log (DRT-0199 last iteration)"}
    q["route_drc"] = drc
    ant = None
    for name, txt in (("drt_antennas.log", _read(os.path.join(reports_dir or "#", "drt_antennas.log"))),
                      ("5_2_route.log", route_log),
                      ("grt_antennas.log", _read(os.path.join(reports_dir or "#", "grt_antennas.log"))),
                      ("5_1_grt.log", _read(os.path.join(logs_dir or "#", "5_1_grt.log")))):
        a = parse_antenna(txt)
        if a is not None:
            ant = dict(a, source=name)
            break
    q["antenna"] = ant
    grt = parse_grt_overflow(_read(os.path.join(logs_dir or "#", "5_1_grt.log")))
    q["grt_overflow"] = grt
    # ---- area / util / power ----
    m, msrc = _metrics_json(logs_dir)
    util = _mget(m, "design__instance__utilization")
    area = {"instance": _mget(m, "design__instance__area"),
            "stdcell": _mget(m, "design__instance__area__stdcell"),
            "macros": _mget(m, "design__instance__area__macros"),
            "core": _mget(m, "design__core__area"), "die": _mget(m, "design__die__area"),
            "utilization_pct": round(util * 100, 2) if isinstance(util, (int, float)) and util <= 1.5 else util,
            "source": msrc or None}
    if area["instance"] is None:
        da = None
        for f in sorted(glob.glob(os.path.join(logs_dir or "#", "*.log")), reverse=True):
            hits = _la._RE_DESIGN_AREA_UTIL.findall(_read(f)) if _la else []
            if hits:
                da = hits[-1]
                area["source"] = os.path.basename(f)
                break
        if da:
            area["instance"], area["utilization_pct"] = float(da[0]), float(da[1])
    q["area"] = area
    q["power"] = {"corners": pw, "total": (pw.get("slow") or next(iter(pw.values()), {})).get("total") if pw else _mget(m, "power__total"),
                  "source": "6_finish.rpt" if pw else (msrc or None)}
    # ---- failure triage ----
    if str(status).upper() != "DONE":
        texts: List[Tuple[str, str]] = []
        for f in sorted(_ls(logs_dir), key=os.path.getmtime, reverse=True):
            if f.endswith(".log") and stage_of(f) == failed:
                texts.append((os.path.basename(f), _read_tail(f, 4 << 20)))
        if agent_log and os.path.isfile(agent_log):
            texts.append((os.path.basename(agent_log), _read_tail(agent_log, 4 << 20)))
        q["triage"] = triage_failure(texts, failed)
    else:
        q["triage"] = None
    return q


# ---------------------------------------------------------------------------
# Email body
# ---------------------------------------------------------------------------
def _f(v, fmt="{:.2f}", na="n/a"):
    if v is None:
        return na
    try:
        return fmt.format(v)
    except (ValueError, TypeError):
        return str(v)


def render_email_body(q: Dict[str, object]) -> str:
    t = q.get("timing") or {}
    c = q.get("clock") or {}
    L = [f"Block        : {q['block']}",
         f"Status       : {q['status']}",
         f"Stage reached: {q.get('stage_reached') or 'n/a'}"]
    if q.get("failed_stage"):
        L.append(f"Failed at    : {q['failed_stage']}")
    if q.get("tries"):
        L.append(f"Tries        : {q['tries']}")
    L.append(f"Generated    : {q.get('generated')}")
    L.append("")
    L.append(f"QoR (units: ps / um^2 / W / MHz; timing source: "
             f"{t.get('source_file') or 'none'}{' [final, RCX]' if t.get('final') else ''})")
    L.append(f"  Clock      : {c.get('name') or '?'} period {_f(c.get('period'))} ps "
             f"(target {_f(c.get('target_mhz'))} MHz) | achieved fmax {_f(c.get('achieved_mhz'))} MHz "
             f"(period_min {_f(c.get('period_min'))} ps)")
    v = t.get("violators") or {}
    L.append(f"  Setup      : worst slack {_f(t.get('setup_worst_slack'))}  WNS {_f(t.get('setup_wns'))}  "
             f"TNS {_f(t.get('setup_tns'))}  violators {_f(v.get('setup'), '{}')}")
    L.append(f"  Hold       : worst slack {_f(t.get('hold_worst_slack'))}  violators {_f(v.get('hold'), '{}')}")
    L.append(f"  Verdict    : {t.get('verdict') or 'UNKNOWN'}")
    g = t.get("groups") or {}
    L.append("  Path group     setup WS     hold WS")
    for name in GROUPS:
        L.append(f"    {name:<10} {_f(g.get('setup', {}).get(name)):>10}  {_f(g.get('hold', {}).get(name)):>10}")
    for k, extra in (t.get("other_groups") or {}).items():
        for name, val in extra.items():
            L.append(f"    {name[:24]:<24} ({k}) {_f(val)}")
    d = q.get("drv") or {}
    L.append(f"  DRV        : max slew {_f(d.get('max_slew'), '{}')} | max cap {_f(d.get('max_cap'), '{}')} "
             f"| max fanout {_f(d.get('max_fanout'), '{}')}")
    drc = q.get("route_drc")
    if drc:
        bt = ", ".join(f"{k} {v}" for k, v in sorted((drc.get("by_type") or {}).items(), key=lambda kv: -kv[1])[:5])
        L.append(f"  Route DRC  : {drc['total']}{' (' + bt + ')' if bt else ''} [{drc.get('source')}]")
    else:
        L.append("  Route DRC  : n/a (no 5_route_drc.rpt / DRT log)")
    a = q.get("antenna")
    L.append(f"  Antenna    : " + (f"{a['nets']} nets / {a['pins']} pins [{a['source']}]" if a else "n/a"))
    o = q.get("grt_overflow")
    L.append(f"  GRT ovfl   : " + (f"H {o['h']} / V {o['v']} / total {o['total']}" if o else "n/a"))
    ct = q.get("clock_tree") or {}
    L.append(f"  Clock tree : setup skew {_f(ct.get('setup_skew'))} | source latency "
             f"{_f(ct.get('source_latency'))} | target latency {_f(ct.get('target_latency'))}")
    ar = q.get("area") or {}
    L.append(f"  Area       : instance {_f(ar.get('instance'), '{:.0f}')} um^2 | util "
             f"{_f(ar.get('utilization_pct'), '{:.1f}')}% | core {_f(ar.get('core'), '{:.0f}')} | die "
             f"{_f(ar.get('die'), '{:.0f}')} [{ar.get('source') or 'n/a'}]")
    pw = q.get("power") or {}
    L.append(f"  Power      : {_f(pw.get('total'), '{:.3e}')} W [{pw.get('source') or 'n/a'}]")
    L.append(f"  Note       : {t.get('wns_note')}")
    tr = q.get("triage")
    if tr:
        L += ["", "FAILURE TRIAGE", f"  Category   : {tr['category']}"
              + (f" ({tr['code']})" if tr.get("code") else ""),
              f"  First error: {tr.get('first_error') or 'none found'}"
              + (f"  [{tr['source']}]" if tr.get("source") else ""),
              f"  Diagnosis  : {tr['diagnosis']}"]
        if tr.get("knob"):
            L.append(f"  Try        : {tr['knob']}")
        if tr.get("last_errors"):
            L.append("  Last error lines:")
            L += ["    " + x for x in tr["last_errors"][-8:]]
    if q.get("note"):
        L += ["", f"Agent note: {q['note']}"]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# File selection
# ---------------------------------------------------------------------------
class Pick:
    __slots__ = ("path", "arc", "prio", "mode", "limit", "reason")

    def __init__(self, path, arc, prio, mode, limit, reason):
        self.path, self.arc, self.prio, self.mode, self.limit, self.reason = (
            path, arc, prio, mode, limit, reason)


def select_files(block: str, status: str, stage_reached: str, failed_stage: str,
                 logs_dir: Optional[str], reports_dir: Optional[str],
                 results_dir: Optional[str], agent_log: Optional[str],
                 signoff_dir: Optional[str] = None
                 ) -> Tuple[List[Pick], List[Tuple[str, str]]]:
    """Candidates in priority order + skipped (path, reason). Only this block's
    dirs are ever read, so another block's logs cannot leak in."""
    ok = str(status).upper() == "DONE"
    limit_stage = failed_stage if (not ok and failed_stage) else (stage_reached or "finish")
    lim_idx = _STAGE_IDX.get(limit_stage, len(STAGES) - 1)
    roots = {"logs": logs_dir, "reports": reports_dir, "results": results_dir}
    picks: List[Pick] = []
    skipped: List[Tuple[str, str]] = []
    claimed = set()

    def claim(p: str) -> bool:
        if p in claimed:
            return False
        claimed.add(p)
        return True

    # stage-table entries, critical stage boosted to the front
    for st in STAGES:
        si = _STAGE_IDX[st]
        for root, pat, prio, mode, limit, reason in STAGE_TABLE[st]:
            for p in _ls(roots[root]):
                if not fnmatch.fnmatch(os.path.basename(p), pat) or p in claimed:
                    continue
                if si > lim_idx:
                    claim(p)
                    skipped.append((f"{root}/{os.path.basename(p)}",
                                    f"stale: {st} is after the {'failure' if not ok else 'last'} "
                                    f"stage ({limit_stage}); left over from an earlier run"))
                    continue
                claim(p)
                boost = 0 if si == lim_idx else (5 if si == lim_idx - 1 else 30)
                picks.append(Pick(p, f"{root}/{os.path.basename(p)}", prio + boost,
                                  mode, limit, f"[{st}] {reason}"))
    # agent log of THIS block only
    if agent_log and os.path.isfile(agent_log):
        picks.append(Pick(agent_log, "agent/" + os.path.basename(agent_log),
                          15 if not ok else 35, "tail", _AGENT_LOG_LIMIT,
                          "agent log for this block (tail)"))
    if signoff_dir and os.path.isdir(signoff_dir):
        for p in _ls(signoff_dir):
            if p.endswith((".json", ".txt", ".rpt", ".log")):
                picks.append(Pick(p, "signoff/" + os.path.basename(p), 36, "head",
                                  1 << 20, "agent signoff result for this block"))
    # remaining files in each root
    for root, d in roots.items():
        for p in _ls(d):
            if p in claimed:
                continue
            b = os.path.basename(p)
            low = b.lower()
            claim(p)
            st = stage_of(b)
            if any(low.endswith(e) for e in HEAVY_EXTS):
                skipped.append((f"{root}/{b}", "heavy binary/PD database (never bundled)"))
                continue
            if "masku_groups" in low and not masku_wanted(block):
                skipped.append((f"{root}/{b}", "masku debug report (Ara-only; JANUS_MASKU_GROUPS!=1)"))
                continue
            if st is not None and _STAGE_IDX[st] > lim_idx:
                skipped.append((f"{root}/{b}", f"stale: {st} after {limit_stage}"))
                continue
            if low.endswith(".v") or low.endswith(".sv"):
                skipped.append((f"{root}/{b}", "netlist (large; on the box)"))
                continue
            if low.startswith(".") or low.endswith((".tmp", ".mk")):
                skipped.append((f"{root}/{b}", "temp/flow-internal"))
                continue
            near = st is not None and _STAGE_IDX[st] >= lim_idx - 1
            if re.match(r"janus_\d\d_.*_paths\.rpt$", b):
                picks.append(Pick(p, f"{root}/{b}", 60 if near else 80, "head",
                                  _JANUS_PATHS_LIMIT, "per-stage top-path dump (head: worst paths first)"))
            elif re.match(r"janus_\d\d_.*_debug\.rpt$", b) or "masku_groups" in low:
                picks.append(Pick(p, f"{root}/{b}", 55 if near else 75, "head",
                                  2 << 20, "per-stage debug report"))
            elif low.endswith(IMAGE_EXTS):
                picks.append(Pick(p, f"{root}/{b}", 65 if near else 90, "full",
                                  4 << 20, "layout / clock-tree image"))
            elif low.endswith((".rpt", ".log", ".json", ".txt", ".sdc", ".tcl", ".csv", ".yaml", ".md")):
                picks.append(Pick(p, f"{root}/{b}", 70, "head" if root == "reports" else "tail",
                                  1 << 20, "other text artifact"))
            else:
                skipped.append((f"{root}/{b}", "unrecognised type"))
    picks.sort(key=lambda k: (k.prio, k.arc))
    return picks, skipped


def _slice(path: str, mode: str, nbytes: int) -> bytes:
    marker = (f"\n[JANUS-BUNDLE] truncated: kept {mode} {nbytes} B of "
              f"{os.path.getsize(path)} B ({os.path.basename(path)})\n").encode()
    with open(path, "rb") as fh:
        if mode == "tail":
            fh.seek(max(0, os.path.getsize(path) - nbytes))
            return marker + fh.read(nbytes)
        return fh.read(nbytes) + marker


def stage_table_markdown() -> str:
    L = ["| Stage | root | file pattern | priority | truncation | why |",
         "|---|---|---|---|---|---|"]
    for st in STAGES:
        for root, pat, prio, mode, limit, reason in STAGE_TABLE[st]:
            L.append(f"| {st} | {root} | `{pat}` | {prio} | {mode} ≤{limit >> 10} KB | {reason} |")
    return "\n".join(L)


def build_bundle(block: str, status: str, out_dir: str, *,
                 logs_dir: Optional[str], reports_dir: Optional[str],
                 results_dir: Optional[str], agent_log: Optional[str] = None,
                 signoff_dir: Optional[str] = None, manifest_stage: str = "",
                 cap_bytes: int = 40 << 20, tries: Optional[int] = None,
                 note: str = "", qor: Optional[Dict] = None,
                 dry_run: bool = False) -> Dict[str, object]:
    """Build qor + INDEX + tar.gz. Returns {tar, qor, qor_path, index, included,
    skipped, body}. Never touches other blocks' files."""
    q = qor or build_qor(block, status, logs_dir, reports_dir, results_dir,
                         agent_log, manifest_stage, tries, note)
    picks, skipped = select_files(block, status, q.get("stage_reached") or "",
                                  q.get("failed_stage") or "", logs_dir,
                                  reports_dir, results_dir, agent_log, signoff_dir)
    qor_bytes = json.dumps(q, indent=2, default=str).encode()
    budget = cap_bytes - len(qor_bytes) - (64 << 10)  # reserve for INDEX.md
    included: List[Tuple[str, int, int, str, str]] = []  # arc, size, kept, how, reason
    plan: List[Tuple[Pick, int, bool]] = []
    for pk in picks:
        try:
            sz = os.path.getsize(pk.path)
        except OSError:
            continue
        want = min(sz, pk.limit)
        if want > budget:
            if budget >= _TRUNC_MIN and pk.mode in ("head", "tail"):
                want = budget
            else:
                skipped.append((pk.arc, f"size cap reached ({sz} B, {max(budget, 0)} B left)"))
                continue
        trunc = want < sz
        plan.append((pk, want, trunc))
        included.append((pk.arc, sz, want, (pk.mode + " truncated") if trunc else "full", pk.reason))
        budget -= want
    stage_s = "" if str(status).upper() == "DONE" else f"_{q.get('failed_stage') or 'fail'}"
    root = f"{block}{stage_s}"
    idx = render_index(q, included, skipped, cap_bytes)
    res = {"qor": q, "included": included, "skipped": skipped, "index": idx,
           "body": render_email_body(q), "tar": None, "qor_path": None}
    if dry_run:
        return res
    os.makedirs(out_dir, exist_ok=True)
    qp = os.path.join(out_dir, "qor.json")
    with open(qp, "wb") as fh:
        fh.write(qor_bytes)
    with open(os.path.join(out_dir, "INDEX.md"), "w") as fh:
        fh.write(idx)
    res["qor_path"] = qp
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tar_path = os.path.join(out_dir, f"{root}_{ts}.tar.gz")

    def _addbytes(tar, arc, data):
        ti = tarfile.TarInfo(f"{root}/{arc}")
        ti.size = len(data)
        ti.mtime = int(time.time())
        tar.addfile(ti, io.BytesIO(data))

    with tarfile.open(tar_path, "w:gz") as tar:
        _addbytes(tar, "INDEX.md", idx.encode())
        _addbytes(tar, "qor.json", qor_bytes)
        for pk, want, trunc in plan:
            try:
                if trunc:
                    _addbytes(tar, pk.arc, _slice(pk.path, pk.mode, want))
                else:
                    tar.add(pk.path, arcname=f"{root}/{pk.arc}")
            except OSError:
                continue
    res["tar"] = tar_path
    return res


def render_index(q: Dict, included, skipped, cap: int) -> str:
    L = [f"# JANUS report bundle — {q['block']} ({q['status']})", "",
         f"- stage reached: **{q.get('stage_reached') or 'n/a'}**"
         + (f", failed at: **{q['failed_stage']}**" if q.get("failed_stage") else ""),
         f"- generated: {q.get('generated')}",
         f"- uncompressed cap: {cap >> 20} MB (critical files first; large text "
         f"files head/tail-truncated, never silently dropped)",
         "- units: ps / um^2 / W / MHz; see `qor.json` for the machine-readable QoR", "",
         "## Included", "", "| file | size (B) | kept (B) | how | why |", "|---|---|---|---|---|",
         "| INDEX.md | | | full | this file |", "| qor.json | | | full | QoR summary (email numbers) |"]
    for arc, sz, kept, how, why in included:
        L.append(f"| {arc} | {sz} | {kept} | {how} | {why} |")
    L += ["", "## Skipped", "", "| file | reason |", "|---|---|"]
    for arc, why in skipped:
        L.append(f"| {arc} | {why} |")
    L += ["", "## Stage → artifact table", "", stage_table_markdown(), ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _find_dir(flow_dir: str, kind: str, block: str, variant: str) -> Optional[str]:
    for c in (os.path.join(flow_dir, kind, "asap7", block, variant),
              os.path.join(flow_dir, kind, "asap7", f"janus_{block}", variant)):
        if os.path.isdir(c):
            return c
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--block", required=True)
    ap.add_argument("--status", default="DONE")
    ap.add_argument("--flow-dir", default=os.environ.get("FLOW_HOME", ""))
    ap.add_argument("--variant", default="base")
    ap.add_argument("--logs-dir")
    ap.add_argument("--reports-dir")
    ap.add_argument("--results-dir")
    ap.add_argument("--agent-log")
    ap.add_argument("--signoff-dir")
    ap.add_argument("--out", default=".")
    ap.add_argument("--cap-mb", type=int, default=40)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    fd = a.flow_dir
    logs = a.logs_dir or (_find_dir(fd, "logs", a.block, a.variant) if fd else None)
    reps = a.reports_dir or (_find_dir(fd, "reports", a.block, a.variant) if fd else None)
    ress = a.results_dir or (_find_dir(fd, "results", a.block, a.variant) if fd else None)
    r = build_bundle(a.block, a.status, a.out, logs_dir=logs, reports_dir=reps,
                     results_dir=ress, agent_log=a.agent_log, signoff_dir=a.signoff_dir,
                     cap_bytes=a.cap_mb << 20, dry_run=a.dry_run)
    print(r["body"])
    print(f"\n[bundle] included {len(r['included'])} file(s), skipped {len(r['skipped'])}")
    for arc, sz, kept, how, why in r["included"]:
        print(f"  + {arc:<45} {kept:>10} B  {how:<15} {why}")
    for arc, why in r["skipped"]:
        print(f"  - {arc:<45} {why}")
    if r["tar"]:
        print(f"[bundle] wrote {r['tar']} and {r['qor_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
