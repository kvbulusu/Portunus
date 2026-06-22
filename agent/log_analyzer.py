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

import re
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


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

# Lines that look like an error but are benign noise we should not flag.
_IGNORE_PATTERNS = [
    re.compile(r"0 errors", re.I),
    re.compile(r"error_recovery", re.I),
    re.compile(r"errors?:\s*0\b", re.I),
]

# Metric extractors (best-effort; ORFS report formats vary by version).
_RE_WNS = re.compile(r"\bwns\b\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
_RE_TNS = re.compile(r"\btns\b\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
_RE_SLACK = re.compile(r"worst slack\s*[:=]?\s*(-?\d+\.?\d*)", re.I)
_RE_DRC = re.compile(r"(?:total\s+)?(?:drc|violations?)\s*[:=]?\s*(\d+)", re.I)
_RE_AREA = re.compile(r"(?:design|core|total)\s+area\s*[:=]?\s*(\d+\.?\d*)", re.I)
_RE_POWER = re.compile(r"\btotal\s+power\s*[:=]?\s*(\d+\.?\d*([eE]-?\d+)?)", re.I)
_RE_UTIL = re.compile(r"utiliz\w*\s*[:=]?\s*(\d+\.?\d*)\s*%?", re.I)


# =============================================================================
# Deterministic failure triage — runs BEFORE any LLM call.
# These are environment / infrastructure / tooling errors that an LLM cannot
# fix by editing RTL or config. Detecting them here saves LLM credits and gives
# the user a precise, actionable manual instruction instead of a vague guess.
# Each entry: (compiled regex, category, human-readable manual fix message).
# =============================================================================
_TRIAGE_RULES = [
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
     "result itself is fine — this check is a verification extra. TWO fixes: "
     "(A) DISABLE it: add  export EQUIVALENCE_CHECK = 0  to the block .mk (and "
     "re-run scripts/03), OR set it in the flow; the flow then skips kepler. "
     "(B) Or PUT THE LIB ON THE PATH: run  find $ORFS_DIR -name 'libnaja*.so*'  "
     "then add that directory to LD_LIBRARY_PATH in janus_asic_env.sh and "
     "re-source it. Not an RTL/config bug. Re-run with --resume to continue from "
     "the completed CTS stage (no re-synthesis)."),
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
     "setup can OOM on large blocks (user_domain CVA6+Ara). Lower parallelism, "
     "increase the WSL2 memory limit in .wslconfig, or reduce design params "
     "(e.g. SCRATCH_KB). Not fixable by editing RTL syntax."),
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
    # metrics (None if not found in this log)
    wns_ns: Optional[float] = None
    tns_ns: Optional[float] = None
    drc_violations: Optional[int] = None
    area: Optional[float] = None
    power: Optional[float] = None
    utilization: Optional[float] = None
    tail: str = ""   # last N lines, useful context

    def to_dict(self) -> Dict:
        return asdict(self)

    def has_timing_violation(self) -> bool:
        return self.wns_ns is not None and self.wns_ns < 0

    def headline(self) -> str:
        if self.failed:
            first = self.error_lines[0] if self.error_lines else "unknown error"
            return f"{self.block}/{self.stage}: FAILED — {first[:120]}"
        bits = []
        if self.wns_ns is not None:
            bits.append(f"WNS={self.wns_ns:+.3f}ns")
        if self.drc_violations is not None:
            bits.append(f"DRC={self.drc_violations}")
        if self.area is not None:
            bits.append(f"area={self.area}")
        return f"{self.block}/{self.stage}: OK" + (f" ({', '.join(bits)})" if bits else "")


class LogAnalyzer:
    def __init__(self, max_error_lines: int = 40, tail_lines: int = 60):
        self.max_error_lines = max_error_lines
        self.tail_lines = tail_lines

    # ---- public API ---------------------------------------------------------
    def analyze_text(self, text: str, block: str, stage: str) -> LogReport:
        lines = text.splitlines()
        rep = LogReport(block=block, stage=stage)

        for i, line in enumerate(lines):
            if any(p.search(line) for p in _IGNORE_PATTERNS):
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
        def grab(rx: re.Pattern, cast=float):
            m = None
            for m in rx.finditer(text):  # take the LAST occurrence (final report)
                pass
            if m:
                try:
                    return cast(m.group(1))
                except (ValueError, IndexError):
                    return None
            return None

        rep.wns_ns = grab(_RE_WNS)
        if rep.wns_ns is None:
            rep.wns_ns = grab(_RE_SLACK)
        rep.tns_ns = grab(_RE_TNS)
        drc = grab(_RE_DRC, int)
        rep.drc_violations = drc
        rep.area = grab(_RE_AREA)
        rep.power = grab(_RE_POWER)
        rep.utilization = grab(_RE_UTIL)

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
