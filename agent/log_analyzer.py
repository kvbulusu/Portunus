"""
log_analyzer.py — Parse OpenROAD / Yosys (ORFS) logs into structured reports.

The orchestrator feeds raw stage logs here to:
  * decide whether a stage FAILED or PASSED,
  * extract the most relevant error lines (kept small for the LLM),
  * pull out timing (WNS/TNS), DRC counts, and area/power if present.

Pure stdlib. No network. Designed so the structured report it returns can be
serialized to JSON and/or trimmed to a byte budget before being sent to the LLM.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple


# Phrases that indicate a hard failure in ORFS / OpenROAD / Yosys output.
_ERROR_PATTERNS = [
    re.compile(r"\bERROR\b", re.I),
    re.compile(r"\bFATAL\b", re.I),
    re.compile(r"\bfailed\b", re.I),
    re.compile(r"make.*\*\*\*.*Error"),         # GNU make error
    re.compile(r"^Error:", re.I),               # OpenROAD Tcl error
    re.compile(r"\bsyntax error\b", re.I),
    re.compile(r"\bcannot find\b", re.I),
    re.compile(r"\bunresolved\b", re.I),
    re.compile(r"\bno such file\b", re.I),
    re.compile(r"\bundefined\b.*\bmodule\b", re.I),
    re.compile(r"\bnot declared\b", re.I),
]

_WARNING_PATTERNS = [
    re.compile(r"\bWARNING\b", re.I),
    re.compile(r"^Warning:", re.I),
]

# OpenROAD / Yosys tool messages are severity-tagged "[SEVERITY TOOL-NNNN]"
# (e.g. "[WARNING RSZ-2005]", "[INFO GPL-0106]", "[ERROR ODB-0012]"). When a
# line carries such a tag, the tag is AUTHORITATIVE and must win over the generic
# text heuristics in _ERROR_PATTERNS: a "[WARNING RSZ-2005] cannot find a viable
# buffering solution ..." is a warning, NOT a hard failure, even though its text
# contains "cannot find". Only ERROR/FATAL tags (that survive _IGNORE_PATTERNS)
# are real failures. Untagged lines (make/Tcl errors) still use the heuristics.
_RE_MSG_TAG = re.compile(r"\[(ERROR|FATAL|WARNING|INFO|DEBUG)\s+[A-Z]{2,6}-\d+\]", re.I)

# Lines that look like an error but are benign noise we should not flag.
#
# The last four entries below are COSMETIC errors emitted by the artifact/image
# dump hooks (janus_stage_dump.tcl / janus_stage_images.tcl), NOT flow failures:
#   * STA-2204 "get_property <corner> is not an object" — the post-CTS image hook
#     probes a corner object for coloring; harmless, the route still completes.
#   * STA-0562 "... is not a known keyword or flag" — the summary dump used the
#     invalid `report_wns -hold`/`report_tns -hold` form (fixed in the hook, but
#     tolerated here for logs produced before that fix). Hold is still captured
#     via the `report_worst_slack -min` line.
#   * GUI-0056 "Invalid syntax for -filter" — the image hook's GUI filter string;
#     affects a PNG dump only.
# These appear with a zero return code and a written 6_final.gds, so treating
# them as hard failures is a false FAIL. Signoff timing comes from the per-stage
# detail-route .rpt, not from these lines.
_IGNORE_PATTERNS = [
    re.compile(r"0 errors", re.I),
    re.compile(r"error_recovery", re.I),
    re.compile(r"errors?:\s*0\b", re.I),
    re.compile(r"\[ERROR STA-2204\]", re.I),
    re.compile(r"\[ERROR STA-0562\]", re.I),
    re.compile(r"\[ERROR GUI-0056\]", re.I),
    re.compile(r"is not an object", re.I),
    re.compile(r"is not a known keyword or flag", re.I),
    re.compile(r"Invalid syntax for -filter", re.I),
]

# §180: lines the AGENT itself writes into the block log (NOT tool output) must
# never be scanned for failure text. The make header echoes the full command,
# including every `-o <file>` resume old-file — run 20261030_185523 was falsely
# marked FAILED because `-o .../5_1_grt-failed.odb` matched r"\bfailed\b", which
# skipped the whole DONE path (post-route opt, abstract, RCX signoff, golden).
# The GIT PROVENANCE block carries a free-text commit subject (same risk).
_RE_AGENT_HEADER = re.compile(r"^===== \S+ : .*=====\s*$")
_RE_PROV_START = re.compile(r"^-{3,} GIT PROVENANCE -*\s*$")
_RE_PROV_END = re.compile(r"^-{20,}\s*$")
_RE_PROV_NOGIT = re.compile(r"^GIT PROVENANCE: ")

# §223: diagnostics printed by OUR OWN flow hooks ("JANUS_CTS_TAP_SPLIT: ...",
# "[JANUS-DUMP] ...", "JANUS_PRNC: FAIL ...") are advisory by design — every
# hook catches its own errors and never errors the stage; a real hook failure
# surfaces as a Tcl "Error:" / make "*** Error" / nonzero rc instead. Run
# 20261108_121357 (rc=0, 4_cts setup +0.8 hold 0.0) was falsely marked FAILED
# because "JANUS_CTS_TAP_SPLIT: ... error min -24.3 max -24.3 ps" (latency-target
# error) matched r"\bERROR\b". These lines are skipped by the text heuristics.
_RE_JANUS_HOOK = re.compile(r"^\s*(?:\[JANUS-[A-Z0-9_-]+\]|JANUS_[A-Z0-9_]+:)")

# Metric extractors (best-effort; ORFS report formats vary by version).
#
# UNITS: for the ASAP7 PDK the standard-cell Liberty declares time_unit = 1ps,
# so EVERY slack/WNS/TNS value OpenROAD prints for this flow is in PICOSECONDS,
# not nanoseconds. The `wns_ns` field name below is retained only for
# checkpoint.json backward-compatibility; the value it holds is ps. Displays
# label it "ps" accordingly.
_RE_WNS = re.compile(r"\bwns\b\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
_RE_TNS = re.compile(r"\btns\b\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
_RE_SLACK = re.compile(r"worst slack\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
# Setup vs hold split. OpenROAD's report_worst_slack prints "worst slack max ..."
# (setup) and "worst slack min ..." (hold); ORFS metrics use the
# "<stage>__timing__setup__ws" / "__hold__ws" keys. Prefer these specific forms
# so setup and hold are never conflated.
_RE_WS_MAX   = re.compile(r"worst\s+slack\s+max\s+(-?\d+\.?\d*)", re.I)
_RE_WS_MIN   = re.compile(r"worst\s+slack\s+min\s+(-?\d+\.?\d*)", re.I)
_RE_SETUP_WS = re.compile(r"timing__setup__ws\"?\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
_RE_HOLD_WS  = re.compile(r"timing__hold__ws\"?\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
_RE_SETUP_TNS = re.compile(r"timing__setup__tns\"?\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
# Lines carrying a "worst slack" number that is NOT a signoff result and must
# never be mistaken for one by the generic fallbacks below. GPL-0106
# "Timing-driven: worst slack ..." is the global-PLACEMENT internal estimate (in
# seconds, e-09), printed long before routing; GRT-* are global-route estimates.
# When the real post-route STA print is absent from the log (e.g. killed by a
# cosmetic STA error in a dump hook), these estimates are the only "worst slack"
# matches left — and grabbing the LAST one yields a bogus WNS like -1.35 (ps?)
# that is actually -1.35e-09 s of placement slack. Skip them.
_RE_NONSIGNOFF = re.compile(r"GPL-0106|Timing-driven|\bGRT-\d|global\s+route", re.I)
_RE_NONSIGNOFF_OR_HOLD = re.compile(_RE_NONSIGNOFF.pattern + r"|\bhold\b", re.I)

# Per-stage detail-route .rpt header emitted by janus_stage_dump.tcl. This is the
# AUTHORITATIVE signoff-comparable timing (design fully loaded, multi-corner).
# Format:  "wns max -466.73" / "tns max -1100706.75" / "worst slack min -87.31".
_RE_RPT_WNS_MAX = re.compile(r"\bwns\s+max\s+(-?\d+\.?\d*(?:[eE][-+]?\d+)?)", re.I)
_RE_RPT_TNS_MAX = re.compile(r"\btns\s+max\s+(-?\d+\.?\d*(?:[eE][-+]?\d+)?)", re.I)
_RE_RPT_WS_MIN  = re.compile(r"worst\s+slack\s+min\s+(-?\d+\.?\d*(?:[eE][-+]?\d+)?)", re.I)

# Per-PATH-GROUP path-body markers inside the same detail-route .rpt. Each
# individual path block carries a "Path Group: reg2reg|reg2out|in2reg|in2out",
# a "Path Type: max" (SETUP) / "min" (HOLD), and terminates in a
# "<slack>   slack (VIOLATED|MET)" line. Bucketing the worst slack per group
# lets the dashboard show reg2reg vs reg2out for setup AND hold, so a block
# whose reg2reg closes but reg2out violates (a boundary-budget signature) is
# never read as a clean pass off the single overall WNS.
# Group names may contain spaces ("gated clock"), so capture to end of line.
_RE_PG_GROUP = re.compile(r"Path\s+Group:\s*(\S.*?)\s*$", re.I)
_RE_PG_TYPE  = re.compile(r"Path\s+Type:\s*(max|min)", re.I)
_RE_PG_SLACK = re.compile(
    r"(-?\d+\.?\d*(?:[eE][-+]?\d+)?)\s+slack\s+\((?:VIOLATED|MET)\)", re.I)

_RE_DRC = re.compile(r"(?:total\s+)?(?:drc|violations?)\s*[:=]?\s*(\d+)", re.I)
_RE_AREA = re.compile(r"(?:design|core|total)\s+area\s*[:=]?\s*(\d+\.?\d*)", re.I)
_RE_POWER = re.compile(r"\btotal\s+power\s*[:=]?\s*(\d+\.?\d*([eE]-?\d+)?)", re.I)
_RE_UTIL = re.compile(r"utiliz\w*\s*[:=]?\s*(\d+\.?\d*)\s*%?", re.I)
# OpenROAD report_design_area: "Design area 198507 um^2 55% utilization."
# The LAST such line is the final (6_report) value; the generic _RE_UTIL can
# latch onto unrelated lines (e.g. an earlier "100% utilization"), so prefer this.
_RE_DESIGN_AREA_UTIL = re.compile(
    r"Design area\s+(\d+\.?\d*)\s+um\^2\s+(\d+\.?\d*)%\s+utilization", re.I)
# report_cell_usage "Cell type report:" table rows: "  <label>   <count>  <area>"
_RE_CELL_TYPE_HDR = re.compile(r"^\s*Cell type report:", re.I)
_RE_CELL_TYPE_ROW = re.compile(r"^\s+([A-Za-z][A-Za-z \-/]*?)\s+(\d+)\s+(\d+\.?\d*)\s*$")
# Rows that are NOT standard logic cells (excluded from std_cells).
_NON_STD_CELL_TYPES = {"macro", "fill cell", "tap cell", "endcap cell", "total"}


# =============================================================================
# Deterministic failure triage — runs BEFORE any LLM call.
# These are environment / infrastructure / tooling errors that an LLM cannot
# fix by editing RTL or config. Detecting them here saves LLM credits and gives
# the user a precise, actionable manual instruction instead of a vague guess.
# Each entry: (compiled regex, category, human-readable manual fix message).
# =============================================================================
_TRIAGE_RULES = [
    # §140: OpenROAD's exec of the kepler-formal LEC child, when the OS kills it
    # (OOM), prints exactly "child killed: kill signal" and 4_1_cts fails.
    # Seen on user_core_ara (flat Ara) 2026-10-15: KeplerFormal reached
    # "Found top design" then was killed on the ~248 GB box.
    (re.compile(r"child killed: kill signal"), "env-oom-lec",
     "A child process of OpenROAD was killed by the OS (almost always out of "
     "memory). In 4_1_cts this child is the kepler-formal logic-equivalence "
     "check (LEC_CHECK=1), which does not fit in RAM for very large flat blocks "
     "such as user_core_ara. The LEC is a verification extra; the layout is "
     "unaffected. Fix: set  export LEC_CHECK = 0  (plain '=', not '?=', which "
     "yields to an inherited env/_local.mk value; user_core_ara since §144), re-run "
     "scripts/03_setup_janus_configs.sh <block>, then --resume (continues at "
     "4_cts, no re-synthesis). Not an RTL/config bug."),
    # §147: kepler-formal given two liberty files defining the same cell (the
    # per-corner <child>_fast.lib + <child>_slow.lib of a hardened macro).
    # Seen on user_core run 20261016_202941 (peak mem 2.4 GB, so not OOM).
    (re.compile(r"contains already a SNLDesign named"), "env-lec-dup-lib",
     "kepler-formal (LEC_CHECK=1) aborted while loading liberty: two .lib files "
     "define the same cell — almost always a hardened child's per-corner "
     "<child>_fast.lib AND <child>_slow.lib, both in ORFS $LIB_FILES. Not OOM, "
     "not an RTL bug. The agent passes REMOVE_LIBS_FOR_LEC=<the _fast twins> on "
     "the make command line (§147); if you still see this, your ORFS "
     "formal_check.tcl predates REMOVE_LIBS_FOR_LEC (preflight warns) — update "
     "ORFS or set LEC_CHECK=0 for this block. --resume restarts at 4_cts."),
    # --- missing shared libraries (OpenROAD / OR-Tools runtime) -------------
    (re.compile(r"libortools\S*\.so", re.I), "env-openroad-ortools",
     "OpenROAD binary cannot load OR-Tools (libortools.so.*). The OpenROAD "
     "install is incomplete or its lib dir is not on the loader path. Fix: "
     "run  ldd $ORFS_DIR/tools/install/OpenROAD/bin/openroad  to see the "
     "missing lib, then either rebuild OpenROAD (./build_openroad.sh inside "
     "OpenROAD-flow-scripts) or add the dir containing libortools.so.9 to "
     "LD_LIBRARY_PATH in your janus_asic_env.sh. This is NOT an RTL/config bug."),
    # kepler-formal (ORFS logic-equivalence checker, run after synth/CTS) is a
    # frequent offender: its libnaja_snl_pyloader.so isn't on the loader path.
    # The equivalence check is OPTIONAL — the layout is already correct without
    # it — so the fastest unblock is to disable it; or put the lib on the path.
    (re.compile(r"(kepler-formal|libnaja\S*\.so)", re.I), "env-kepler-formal",
     "ORFS's optional logic-equivalence check (kepler-formal) failed to start "
     "because libnaja_snl_pyloader.so is not on the loader path. The place/route "
     "result itself is fine — this check is a verification extra. The agent now "
     "AUTO-FIXES this at preflight (_ensure_kepler_libpath: it finds libnaja*.so "
     "under $ORFS_DIR/tools/install and prepends its dir to LD_LIBRARY_PATH), so "
     "a fresh run should not hit this. If you still see it, the .so is genuinely "
     "missing (incomplete install) — then DISABLE the check: add  export "
     "EQUIVALENCE_CHECK = 0  to the block .mk and re-run scripts/03 (note: some "
     "ORFS versions gate it via a lec_check_enabled proc, not this var). "
     "Not an RTL/config bug. Re-run with --resume to continue from the completed "
     "CTS stage (no re-synthesis)."),
    (re.compile(r"error while loading shared libraries", re.I), "env-shared-lib",
     "A tool binary failed to start because a shared library is missing. Run "
     "ldd on the failing binary, install the matching runtime package, or add "
     "its directory to LD_LIBRARY_PATH. Not an RTL/config bug."),
    (re.compile(r"cannot open shared object file", re.I), "env-shared-lib",
     "A required shared object is missing on this machine. Install the runtime "
     "package or fix LD_LIBRARY_PATH. Not an RTL/config bug."),
    # --- missing tool binaries ----------------------------------------------
    (re.compile(r"(openroad|yosys|klayout|sta):?\s*(command not found|No such file)", re.I),
     "env-missing-tool",
     "A required EDA tool binary was not found. Verify the OpenROAD-flow-scripts "
     "build completed and that ORFS_DIR/env.sh is sourced. Not an RTL/config bug."),
    # --- out-of-memory / OS killed the process ------------------------------
    (re.compile(r"\b(Killed|Out of memory|Cannot allocate memory|std::bad_alloc|"
                r"bad_alloc|MemoryError)\b"), "env-oom",
     "The process was killed, most likely out of memory. This 20GB-RAM WSL2 "
     "setup can OOM on large blocks (CVA6+Ara). For SYNTH OOM, lower parallelism "
     "or mock large behavioral memories. For DETAIL-ROUTE OOM on user_core "
     "(~1M instances, ~19GB routing DB), lowering parallelism barely helps (the "
     "routing DB dominates and grows through DRT iterations) -- raise the WSL2 "
     "memory limit to >=32GB in .wslconfig (host RAM permitting) or route the "
     "block on a larger machine. Not fixable by editing RTL syntax."),
    # OpenROAD/TritonRoute do not always print a text 'Killed' banner when the
    # box OOMs -- OpenROAD prints its normal end-of-run 'Peak memory' banner and
    # then the process exits non-zero, which make reports as a bare 'Error 247'.
    # (Seen in user_domain synth @19.6GB and user_core detail-route @19.53GB, both
    # against the ~20GB WSL2 ceiling.) Match that exit code so the same env-oom
    # guidance fires instead of the run failing silently / going to the LLM.
    (re.compile(r"\bError 247\b"), "env-oom",
     "make exited with code 247, which in this flow has consistently meant the "
     "OS killed a tool for running out of memory on the ~20GB WSL2 box (OpenROAD "
     "prints its 'Peak memory' banner near ~19-20GB and then exits, so there is "
     "no 'Killed' text to match). For user_core detail routing (~1M instances) "
     "the ~19GB routing DB cannot fit -- raise the WSL2 memory limit to >=32GB "
     "in .wslconfig (host RAM permitting) or route the block on a larger machine. "
     "Reducing --num-cores saves only the small per-worker overhead and will "
     "very likely OOM again in a later DRT iteration. Not an RTL/config bug."),
    # --- yosys-slang plugin not loaded --------------------------------------
    (re.compile(r"(slang\.so|yosys-slang).*(not found|cannot|No such|failed to load)", re.I),
     "env-slang-plugin",
     "The yosys-slang plugin could not be loaded. Re-run "
     "scripts/03_setup_janus_configs.sh so SLANG_PLUGIN_PATH points at a valid "
     "slang.so, and confirm SYNTH_HDL_FRONTEND=slang in your env file."),
    # --- disk full -----------------------------------------------------------
    (re.compile(r"No space left on device", re.I), "env-disk-full",
     "The disk is full. Free space (old runs/ directories are bulky) and retry. "
     "Not an RTL/config bug."),
]


@dataclass
class Triage:
    """Result of deterministic (pre-LLM) failure classification."""
    category: str
    message: str
    matched_line: str

    def to_dict(self) -> Dict:
        return asdict(self)


def classify_failure(rep: "LogReport") -> Optional[Triage]:
    """Return a Triage if the failure is a known environment/infra error that
    the LLM cannot fix (so the caller should skip the LLM and surface a precise
    manual instruction). Returns None if the failure looks like a real RTL/
    config problem worth sending to the LLM.

    Scans the captured error lines first (most specific), then the log tail.
    """
    haystack: List[str] = list(rep.error_lines)
    if rep.tail:
        haystack.extend(rep.tail.splitlines())
    for line in haystack:
        for rx, category, message in _TRIAGE_RULES:
            if rx.search(line):
                return Triage(category=category, message=message,
                              matched_line=line.strip()[:200])
    return None


@dataclass
class LogReport:
    block: str
    stage: str
    failed: bool = False
    error_lines: List[str] = field(default_factory=list)
    warning_count: int = 0
    # metrics (None if not found in this log).
    # NOTE: ASAP7 reports time in PICOSECONDS. `wns_ns` is the SETUP worst slack
    # (name kept for checkpoint back-compat; value is ps). `hold_ps` is the HOLD
    # worst slack in ps. Both are worst-slack, so negative = violation.
    wns_ns: Optional[float] = None      # setup worst slack, ps
    hold_ps: Optional[float] = None     # hold worst slack, ps
    tns_ns: Optional[float] = None
    drc_violations: Optional[int] = None
    area: Optional[float] = None
    power: Optional[float] = None
    utilization: Optional[float] = None
    # Standard logic cells from the LAST "Cell type report" (excludes macros,
    # fill, tap/endcap). None when the log has no such table.
    std_cells: Optional[int] = None
    # Worst-case IR drop in volts (max over VDD/VSS reports in 6_report).
    # None when the log has no IR report (ORFS prints it in 6_report only).
    ir_drop_worst_v: Optional[float] = None
    tail: str = ""   # last N lines, useful context

    def to_dict(self) -> Dict:
        return asdict(self)

    def has_timing_violation(self) -> bool:
        # A violation is a NEGATIVE worst slack on EITHER setup or hold.
        setup_bad = self.wns_ns is not None and self.wns_ns < 0
        hold_bad = self.hold_ps is not None and self.hold_ps < 0
        return setup_bad or hold_bad

    def headline(self) -> str:
        if self.failed:
            first = self.error_lines[0] if self.error_lines else "unknown error"
            return f"{self.block}/{self.stage}: FAILED — {first[:120]}"
        bits = []
        if self.wns_ns is not None:
            bits.append(f"setup WNS={self.wns_ns:+.3f}ps")
        if self.hold_ps is not None:
            bits.append(f"hold WS={self.hold_ps:+.3f}ps")
        if self.drc_violations is not None:
            bits.append(f"DRC={self.drc_violations}")
        if self.ir_drop_worst_v is not None:
            bits.append(f"IRdrop={self.ir_drop_worst_v*1000:.2f}mV")
        if self.area is not None:
            bits.append(f"area={self.area}")
        return f"{self.block}/{self.stage}: OK" + (f" ({', '.join(bits)})" if bits else "")


def parse_std_cell_count(lines: List[str]) -> Optional[int]:
    """Standard-cell count from the LAST OpenROAD "Cell type report" table.

    Sums every row except Macro / Fill cell / Tap cell / Endcap cell / Total
    (i.e. sequential + combinational + buffers + inverters + tie + clock cells).
    Returns None when no table is present."""
    last: Optional[int] = None
    i, n = 0, len(lines)
    while i < n:
        if _RE_CELL_TYPE_HDR.search(lines[i]):
            total, seen = 0, False
            j = i + 1
            while j < n:
                m = _RE_CELL_TYPE_ROW.match(lines[j])
                if not m:
                    break
                label = m.group(1).strip().lower()
                if label == "total":
                    j += 1
                    break
                if label not in _NON_STD_CELL_TYPES:
                    total += int(m.group(2))
                seen = True
                j += 1
            if seen:
                last = total
            i = j
        else:
            i += 1
    return last


class LogAnalyzer:
    def __init__(self, max_error_lines: int = 40, tail_lines: int = 60):
        self.max_error_lines = max_error_lines
        self.tail_lines = tail_lines

    # ---- public API ---------------------------------------------------------
    def analyze_text(self, text: str, block: str, stage: str) -> LogReport:
        lines = text.splitlines()
        rep = LogReport(block=block, stage=stage)

        in_prov = False
        for i, line in enumerate(lines):
            # §180: skip agent-written headers / provenance (not tool output).
            if in_prov:
                if _RE_PROV_END.match(line):
                    in_prov = False
                continue
            if _RE_PROV_START.match(line):
                in_prov = True
                continue
            if _RE_AGENT_HEADER.match(line) or _RE_PROV_NOGIT.match(line):
                continue
            if _RE_JANUS_HOOK.match(line):
                continue
            if any(p.search(line) for p in _IGNORE_PATTERNS):
                continue

            # Explicit tool severity tag wins over the text heuristics so a
            # "[WARNING ...] cannot find ..." never masquerades as a failure.
            tag = _RE_MSG_TAG.search(line)
            if tag:
                sev = tag.group(1).upper()
                if sev in ("ERROR", "FATAL"):
                    rep.failed = True
                    ctx = lines[max(0, i - 1): i + 2]
                    for c in ctx:
                        if c not in rep.error_lines:
                            rep.error_lines.append(c.rstrip())
                elif sev == "WARNING":
                    rep.warning_count += 1
                # INFO/DEBUG: neither error nor warning
                continue

            if any(p.search(line) for p in _ERROR_PATTERNS):
                rep.failed = True
                # capture a little context around the error
                ctx = lines[max(0, i - 1): i + 2]
                for c in ctx:
                    if c not in rep.error_lines:
                        rep.error_lines.append(c.rstrip())
            elif any(p.search(line) for p in _WARNING_PATTERNS):
                rep.warning_count += 1

        # cap error lines for the LLM budget
        if len(rep.error_lines) > self.max_error_lines:
            rep.error_lines = rep.error_lines[: self.max_error_lines] + ["... (truncated)"]

        rep.tail = "\n".join(lines[-self.tail_lines:])
        self._extract_metrics(text, rep)
        return rep

    def analyze_file(self, path: str, block: str, stage: str) -> LogReport:
        try:
            with open(path, "r", errors="replace") as fh:
                text = fh.read()
        except FileNotFoundError:
            rep = LogReport(block=block, stage=stage, failed=True)
            rep.error_lines = [f"log file not found: {path}"]
            return rep
        return self.analyze_text(text, block, stage)

    # ---- metric extraction --------------------------------------------------
    def _extract_metrics(self, text: str, rep: LogReport) -> None:
        lines = text.splitlines()

        def grab(rx: re.Pattern, cast=float, skip: Optional[re.Pattern] = None):
            # Scan line by line so we can skip non-signoff lines (e.g. the
            # GPL-0106 global-placement "worst slack" estimate) that would
            # otherwise poison the generic fallbacks. Keep the LAST match, which
            # for a well-formed log is the final/report value.
            m = None
            for line in lines:
                if skip is not None and skip.search(line):
                    continue
                mm = rx.search(line)
                if mm:
                    m = mm
            if m:
                try:
                    return cast(m.group(1))
                except (ValueError, IndexError):
                    return None
            return None

        # ---- SETUP worst slack (ps) --------------------------------------
        # Prefer the unambiguous ORFS metrics key, then OpenROAD's
        # "worst slack max", then the generic "wns"/"worst slack" fallbacks.
        # The generic fallbacks SKIP non-signoff lines (GPL/GRT estimates) so a
        # log whose real STA print was lost never reports a placement estimate
        # as the WNS.
        setup = grab(_RE_SETUP_WS)
        if setup is None:
            setup = grab(_RE_WS_MAX, skip=_RE_NONSIGNOFF)
        if setup is None:
            # §223: a line that names "hold" ("JANUS_CTS_HOLD_ABORT: pre-repair
            # hold WNS -414.1 ps") is never the setup WNS.
            setup = grab(_RE_WNS, skip=_RE_NONSIGNOFF_OR_HOLD)
        if setup is None:
            setup = grab(_RE_SLACK, skip=_RE_NONSIGNOFF)
        rep.wns_ns = setup   # field name kept for checkpoint back-compat; value is ps

        # ---- HOLD worst slack (ps) ---------------------------------------
        hold = grab(_RE_HOLD_WS)
        if hold is None:
            hold = grab(_RE_WS_MIN)
        rep.hold_ps = hold

        # ---- TNS (ps): prefer setup TNS key, else generic --------------------
        rep.tns_ns = grab(_RE_SETUP_TNS)
        if rep.tns_ns is None:
            rep.tns_ns = grab(_RE_TNS)
        drc = grab(_RE_DRC, int)
        rep.drc_violations = drc
        rep.area = grab(_RE_AREA)
        rep.power = grab(_RE_POWER)
        da = None
        for line in lines:
            mm = _RE_DESIGN_AREA_UTIL.search(line)
            if mm:
                da = mm
        if da is not None:
            rep.utilization = float(da.group(2))
        else:
            rep.utilization = grab(_RE_UTIL)
        rep.std_cells = parse_std_cell_count(lines)

        # ---- IR drop (volts) ------------------------------------------------
        # ORFS 6_report prints e.g. "Worstcase IR drop: 4.54e-03 V" once per
        # rail (VDD, VSS). Take the max; the pass/fail threshold is applied by
        # the agent (IR_DROP_MAX_PCT of nominal VDD).
        worst_ir = None
        for line in lines:
            mm = re.search(r"Worstcase IR drop:\s*([0-9.eE+-]+)\s*V", line)
            if mm:
                try:
                    v = float(mm.group(1))
                    if worst_ir is None or v > worst_ir:
                        worst_ir = v
                except ValueError:
                    pass
        rep.ir_drop_worst_v = worst_ir

    # ---- LLM payload --------------------------------------------------------
    def build_llm_excerpt(self, rep: LogReport, byte_budget: int = 5000) -> str:
        """Compact text describing the failure, capped to byte_budget (API limit).

        Prioritizes the actual error lines, then the log tail.
        """
        head = [
            f"Block: {rep.block}",
            f"Stage: {rep.stage}",
            f"Result: {'FAILED' if rep.failed else 'completed with warnings'}",
            f"Warnings: {rep.warning_count}",
            "",
            "=== Error lines ===",
        ]
        head += rep.error_lines or ["(no explicit ERROR lines captured)"]
        head += ["", "=== Log tail ==="]
        body = "\n".join(head)
        remaining = max(0, byte_budget - len(body.encode("utf-8")) - 16)
        tail = rep.tail.encode("utf-8")[-remaining:].decode("utf-8", errors="replace")
        return (body + "\n" + tail)[:byte_budget]


# =============================================================================
# Authoritative per-stage timing — parsed from the detail-route .rpt header.
# =============================================================================
def parse_stage_timing_rpt(text: str) -> Dict[str, Optional[float]]:
    """Parse the signoff-comparable timing from a janus_<NN>_<stage>_paths.rpt.

    These reports are written by janus_stage_dump.tcl with the design fully
    loaded (multi-corner if enabled), so their header is the authoritative,
    6_finish-comparable timing for the stage — unlike the run log, whose final
    STA print can be lost to a cosmetic dump-hook error.

    Only the small header (first few hundred bytes) is needed; callers should
    pass just the head of the file (these reports can be tens of MB of path
    bodies). Returns picoseconds for the ASAP7 PDK.

        {"setup_ws": <wns max>, "setup_tns": <tns max>, "hold_ws": <worst slack min>}

    Any field not found is None. STA-0562 lines (from the old invalid
    `report_wns -hold` form) carry no number and are simply not matched.
    """
    def _last(rx: re.Pattern) -> Optional[float]:
        val = None
        for m in rx.finditer(text):
            try:
                val = float(m.group(1))
            except (ValueError, IndexError):
                pass
        return val

    return {
        "setup_ws": _last(_RE_RPT_WNS_MAX),
        "setup_tns": _last(_RE_RPT_TNS_MAX),
        "hold_ws": _last(_RE_RPT_WS_MIN),
    }


def parse_path_group_slacks(lines) -> Dict[str, Dict[str, float]]:
    """Worst (minimum) slack per PATH GROUP, split into setup and hold.

    Unlike the summary header (which collapses everything into one setup WNS and
    one hold WNS), this walks the per-path bodies of a detail-route paths .rpt
    and buckets the worst slack by path group (reg2reg, reg2out, in2reg,
    in2out, ...) separately for SETUP (Path Type: max) and HOLD (min). That is
    what lets the dashboard say, at a glance, that e.g. reg2reg closes but
    reg2out does not — the classic standalone-build boundary-budget signature.

    `lines` is any iterable of text lines, so the caller can STREAM the file
    (these reports are multi-MB of path bodies) instead of loading it whole.
    Returns picoseconds for the ASAP7 PDK:

        {"setup": {"reg2reg": .., "reg2out": ..},
         "hold":  {"reg2reg": .., "reg2out": ..}}

    Groups with no matched path are simply absent; both sub-dicts are empty when
    the file carries no per-path bodies (e.g. only a header was passed).
    """
    out: Dict[str, Dict[str, float]] = {"setup": {}, "hold": {}}
    cur_group: Optional[str] = None
    cur_type: Optional[str] = None  # 'max' (setup) | 'min' (hold)
    for line in lines:
        mg = _RE_PG_GROUP.search(line)
        if mg:
            cur_group = mg.group(1)
            continue
        mt = _RE_PG_TYPE.search(line)
        if mt:
            cur_type = mt.group(1).lower()
            continue
        ms = _RE_PG_SLACK.search(line)
        if ms and cur_group is not None and cur_type is not None:
            try:
                slk = float(ms.group(1))
            except (ValueError, IndexError):
                continue
            bucket = out["setup"] if cur_type == "max" else out["hold"]
            if cur_group not in bucket or slk < bucket[cur_group]:
                bucket[cur_group] = slk
            # Each path block re-declares its own Path Group / Path Type, so
            # reset to avoid attributing a stray slack line to the wrong block.
            cur_group = None
            cur_type = None
    return out


# =============================================================================
# FINAL signoff timing — ORFS 6_finish.rpt (post-route, OpenRCX-extracted).
# =============================================================================
# Why this exists: janus_08_detail_route_paths.rpt is dumped by the post-detail-
# route hook BEFORE 6_final runs OpenRCX; its parasitics are the flow's
# GRT/placement estimate, not the extracted routed RC. On agent_domain run
# 20261007_104052 it reported hold reg2reg -122 ps while 6_finish.rpt (same
# netlist, RCX-extracted, multi-corner) reported +9.95 ps and
# "hold violation count 0". The dashboard must read the latter when it exists.
_RE_FIN_COUNT = re.compile(
    r"^\s*(setup|hold|max\s+slew|max\s+cap|max\s+fanout)\s+violation\s+count\s+(\d+)",
    re.I)
_RE_FIN_WS_MAX = re.compile(r"^\s*worst\s+slack\s+max\s+(-?\d+\.?\d*(?:[eE][-+]?\d+)?)", re.I)
_RE_FIN_FMAX = re.compile(r"period_min\s*=\s*(-?\d+\.?\d*)\s+fmax\s*=\s*(-?\d+\.?\d*)", re.I)

FINAL_RPT_NAME = "6_finish.rpt"
GRT_RPT_NAME = "janus_08_detail_route_paths.rpt"


def parse_final_sta_rpt(lines) -> Dict[str, object]:
    """One streamed pass over an ORFS 6_finish.rpt.

    Returns (ASAP7 ps; None when absent):
        setup_ws   'wns max'  (ORFS clamps WNS at 0 when met)
        setup_tns  'tns max'
        setup_worst 'worst slack max' (true worst setup slack, may be > 0)
        hold_ws    'worst slack min' if printed, else min over the per-group
                   hold path bodies (6_finish prints no 'worst slack min')
        fmax_mhz, counts {setup, hold, max_slew, max_cap, max_fanout},
        path_groups {"setup": {...}, "hold": {...}}
    """
    head: List[str] = []
    counts: Dict[str, int] = {}
    setup_worst: Optional[float] = None
    fmax: Optional[float] = None

    def _tee():
        nonlocal setup_worst, fmax
        for i, line in enumerate(lines):
            if i < 400:
                head.append(line)
            mc = _RE_FIN_COUNT.search(line)
            if mc:
                counts[re.sub(r"\s+", "_", mc.group(1).lower())] = int(mc.group(2))
            mw = _RE_FIN_WS_MAX.search(line)
            if mw and setup_worst is None:
                setup_worst = float(mw.group(1))
            mf = _RE_FIN_FMAX.search(line)
            if mf and fmax is None:
                fmax = float(mf.group(2))
            yield line

    groups = parse_path_group_slacks(_tee())
    hdr = parse_stage_timing_rpt("".join(head))
    hold = hdr.get("hold_ws")
    if hold is None and groups.get("hold"):
        hold = min(groups["hold"].values())
    return {
        "setup_ws": hdr.get("setup_ws"),
        "setup_tns": hdr.get("setup_tns"),
        "setup_worst": setup_worst,
        "hold_ws": hold,
        "fmax_mhz": fmax,
        "counts": counts,
        "path_groups": groups,
    }


def select_timing_report(rdir: Optional[str]) -> Optional[Tuple[str, str]]:
    """Pick the most authoritative timing report in an ORFS reports dir.

    Returns (path, source) with source "6_finish" (post-route RCX signoff) or
    "janus_08" (detail-route dump, GRT/estimated RC) / "janus_NN" (older stage
    dump), or None. 6_finish wins only if it is at least as new as the janus_08
    dump, so a stale 6_finish left over from a previous run is never paired with
    a newer, failed-before-finish route.
    """
    if not rdir or not os.path.isdir(rdir):
        return None
    fin = os.path.join(rdir, FINAL_RPT_NAME)
    grt = os.path.join(rdir, GRT_RPT_NAME)
    if not os.path.isfile(grt):
        others = sorted(glob.glob(os.path.join(rdir, "janus_*_paths.rpt")))
        grt = others[-1] if others else ""
    has_fin, has_grt = os.path.isfile(fin), bool(grt) and os.path.isfile(grt)
    if has_fin and (not has_grt or os.path.getmtime(fin) >= os.path.getmtime(grt)):
        return fin, "6_finish"
    if has_grt:
        tag = os.path.basename(grt).split("_")
        return grt, ("janus_" + tag[1]) if len(tag) > 1 else "janus"
    return None


def read_block_timing(rdir: Optional[str]) -> Optional[Dict[str, object]]:
    """Authoritative block timing for the dashboard/result, source-tagged.

    Prefers 6_finish.rpt (final extraction); falls back to the janus_08 GRT-RC
    dump. Result keys: setup_ws, setup_tns, hold_ws, path_groups, counts,
    setup_worst, fmax_mhz, _source (path), source ("6_finish"|"janus_08"|…),
    final (bool). None when nothing parseable is found.
    """
    sel = select_timing_report(rdir)
    if not sel:
        return None
    path, source = sel
    try:
        with open(path, "r", errors="replace") as fh:
            if source == "6_finish":
                t = parse_final_sta_rpt(fh)
            else:
                t = {"counts": {}, "setup_worst": None, "fmax_mhz": None}
                head = fh.read(16384)
                t.update(parse_stage_timing_rpt(head))
                fh.seek(0)
                t["path_groups"] = parse_path_group_slacks(fh)
    except OSError:
        return None
    if t.get("setup_ws") is None and t.get("hold_ws") is None \
            and not (t.get("path_groups") or {}).get("hold"):
        return None
    t["_source"] = path
    t["source"] = source
    t["final"] = source == "6_finish"
    return t


def timing_verdict(setup_ws: Optional[float], hold_ws: Optional[float]) -> str:
    """'CLOSED' (setup AND hold worst slack >= 0), 'VIOLATED', or 'UNKNOWN'."""
    if setup_ws is None and hold_ws is None:
        return "UNKNOWN"
    if (setup_ws is not None and setup_ws < 0) or (hold_ws is not None and hold_ws < 0):
        return "VIOLATED"
    return "CLOSED"


# ---- self-test --------------------------------------------------------------
if __name__ == "__main__":
    sample = """
    [INFO] Reading verilog ...
    ERROR: syntax error in file rtl/ampo/ampo_top.sv near line 42
    make[1]: *** [synth] Error 1
    wns -0.123
    tns -4.5
    total power 1.23e-03
    """
    a = LogAnalyzer()
    r = a.analyze_text(sample, "ampo", "synth")
    assert r.failed
    assert r.wns_ns == -0.123, r.wns_ns
    assert r.has_timing_violation()
    print("log_analyzer self-test OK:", r.headline())
    print(a.build_llm_excerpt(r, 400)[:200])

    # ---- setup + hold split (ASAP7 ps values) -------------------------------
    metrics = """
    finish__timing__setup__ws -9.305
    finish__timing__setup__tns -152.0
    finish__timing__hold__ws 12.4
    """
    r2 = a.analyze_text(metrics, "security", "finish")
    assert r2.wns_ns == -9.305, r2.wns_ns          # setup worst slack (ps)
    assert r2.hold_ps == 12.4, r2.hold_ps          # hold worst slack (ps)
    assert r2.tns_ns == -152.0, r2.tns_ns
    assert r2.has_timing_violation()               # setup negative
    print("setup/hold split OK:", r2.headline())

    # hold-only violation must also flag
    r3 = a.analyze_text("worst slack max 5.0\nworst slack min -2.0\n", "noc", "finish")
    assert r3.wns_ns == 5.0 and r3.hold_ps == -2.0
    assert r3.has_timing_violation()
    print("hold-violation detection OK:", r3.headline())

    # ---- cosmetic dump-hook errors must NOT be a hard failure ----------------
    # STA-2204 / STA-0562 / GUI-0056 come from the image/report dump hooks with a
    # zero return code and a written GDS. They used to trip \bERROR\b -> FAILED.
    cosmetic = """
    [INFO] detailed route completed, 0 violations
    [ERROR GUI-0056] Invalid syntax for -filter. Use -filter attribute=value.
    [ERROR STA-2204] get_property slow is not an object.
    [ERROR STA-0562] report_wns -hold is not a known keyword or flag.
    cp results/asap7/user_core_ara/stock/6_1_merged.gds .../6_final.gds
    """
    rc = a.analyze_text(cosmetic, "user_core_ara", "flow")
    assert not rc.failed, f"cosmetic dump errors wrongly flagged FAILED: {rc.error_lines}"
    print("cosmetic-error tolerance OK:", rc.headline())

    # a REAL (untagged-ignore) tool ERROR still fails
    rc2 = a.analyze_text("[ERROR ODB-0001] cannot open def file\n", "noc", "flow")
    assert rc2.failed
    print("real-error still fails OK:", rc2.headline())

    # ---- [WARNING ...] with scary text must NOT fail -------------------------
    # RSZ-2005 "cannot find a viable buffering solution" is emitted thousands of
    # times as a WARNING; its text matches the \bcannot find\b heuristic but the
    # explicit [WARNING] tag must win.
    rsz = (
        "[WARNING RSZ-2005] cannot find a viable buffering solution on pin X\n"
        "[WARNING RSZ-2005] cannot find a viable buffering solution on pin Y\n"
        "[INFO] detailed route completed, 0 violations\n"
    )
    rw = a.analyze_text(rsz, "user_core_ara", "flow")
    assert not rw.failed, f"tagged WARNING wrongly flagged FAILED: {rw.error_lines}"
    assert rw.warning_count == 2, rw.warning_count
    print("tagged-warning tolerance OK:", rw.headline())

    # untagged make/Tcl errors still fail via the text heuristics
    rc3 = a.analyze_text("make[1]: *** [synth] Error 1\n", "noc", "flow")
    assert rc3.failed
    print("untagged make-error still fails OK:", rc3.headline())

    # ---- GPL-0106 placement estimate must NOT become the WNS -----------------
    # When the real STA print is absent, the only "worst slack" lines left are
    # the global-placement estimates. The generic fallback must skip them.
    gpl = """
    [INFO GPL-0106] Timing-driven: worst slack -1.8682291e-09
    [INFO GPL-0106] Timing-driven: worst slack -1.3477159e-09
    [INFO] detailed route completed, 0 violations
    """
    rg = a.analyze_text(gpl, "user_core_ara", "flow")
    assert rg.wns_ns is None, f"GPL estimate leaked into WNS: {rg.wns_ns}"
    print("GPL-estimate guard OK (wns_ns stays None)")

    # ---- authoritative .rpt header parse ------------------------------------
    rpt_header = """JANUS per-stage timing dump -- stage=detail_route
    ==============================================================================
    SETUP report_wns
    ------------------------------------------------------------------------------
    wns max -466.73
    SETUP report_tns
    tns max -1100706.75
    HOLD  report_wns -hold
    [ERROR STA-0562] report_wns -hold is not a known keyword or flag.
    HOLD  report_worst_slack -min
    worst slack min -87.31
    """
    t = parse_stage_timing_rpt(rpt_header)
    assert t["setup_ws"] == -466.73, t
    assert t["setup_tns"] == -1100706.75, t
    assert t["hold_ws"] == -87.31, t
    print("stage-rpt parse OK:", t)

    # ---- per-path-group slack parse -----------------------------------------
    # Two path blocks per section; worst (min) per group must win, and setup
    # (Path Type: max) must never leak into hold (min) or vice-versa.
    pg_body = """TOP-100 ANY-CLASS SETUP (max)
    Startpoint: a
    Path Group: reg2out
    Path Type: max
                      -126.61   slack (VIOLATED)
    Startpoint: b
    Path Group: reg2reg
    Path Type: max
                      274.00   slack (MET)
    Startpoint: c
    Path Group: reg2reg
    Path Type: max
                      300.00   slack (MET)
    TOP-100 ANY-CLASS HOLD (min)
    Startpoint: d
    Path Group: reg2reg
    Path Type: min
                      23.15   slack (MET)
    Startpoint: e
    Path Group: reg2out
    Path Type: min
                      602.80   slack (MET)
    """
    g = parse_path_group_slacks(pg_body.splitlines())
    assert g["setup"]["reg2out"] == -126.61, g
    assert g["setup"]["reg2reg"] == 274.00, g   # worst of 274/300
    assert g["hold"]["reg2reg"] == 23.15, g
    assert g["hold"]["reg2out"] == 602.80, g
    # header-only input yields no per-group data
    assert parse_path_group_slacks(rpt_header.splitlines()) == {"setup": {}, "hold": {}}
    print("path-group parse OK:", g)

    print("\nALL log_analyzer self-tests passed.")
