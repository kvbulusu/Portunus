#!/usr/bin/env python3
"""
orfs_probe.py — Tier-2 active investigator for the JANUS ASIC agent.

WHEN THIS RUNS
--------------
Tier 1 (stage_timing_analyzer) reasons from report TEXT alone and is enough for
most failures (it localised the `security` regression to RTL with no tool call).
But sometimes the reports don't disambiguate *where* a regression entered — e.g.
WNS is fine after placement but bad after CTS: is that a clock-tree problem, or
did the SDC clock get mis-propagated, or did a specific path explode? A human
then does exactly one thing: re-opens the saved database from the SUSPECT stage
in a fresh, read-only OpenROAD session and re-times it / compares stage-over-
stage to pin the transition.

This module does that, deterministically and safely:
  * spawns `openroad -exit` in a SUBPROCESS with a short generated TCL,
  * loads a stage .odb READ-ONLY (never writes it back, never touches the live
    run's working directory),
  * re-runs report_checks / report_design_area / report_check_types,
  * parses the output back into the same primitives Tier 1 uses,
  * is TIMEOUT-guarded and degrades gracefully when openroad is absent
    (e.g. in CI / the assistant sandbox) — callers get ok=False, not a crash.

It NEVER mutates the main flow. It is a probe, not a fix. The fix decision still
belongs to the deterministic reconciler in reasoning_engine / janus_agent.

Because the assistant sandbox has no OpenROAD, the live path is exercised on the
user's machine; here it is covered by unit tests that inject a fake runner.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


# A runner takes (argv, timeout) and returns (returncode, stdout, stderr).
Runner = Callable[[List[str], int], Tuple[int, str, str]]


def _default_runner(argv: List[str], timeout: int) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, check=False)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + "\n[timeout]"
    except FileNotFoundError:
        return 127, "", "openroad not found on PATH"


# TCL executed inside openroad. Read-only: reads .odb, reports, exits. We guard
# every command so a missing SDC / liberty doesn't abort before we get output.
_PROBE_TCL = r"""
# --- janus orfs_probe (read-only) -------------------------------------------
if {[catch {read_db {ODB}} err]} {
    puts "PROBE_ERROR read_db: $err"
    exit 1
}
catch {puts "PROBE_AREA [expr {[rsz::design_area]}]"}
if {[catch {report_worst_slack -max} err]} { puts "PROBE_WARN worst_slack: $err" }
if {[catch {report_tns} err]} { puts "PROBE_WARN tns: $err" }
puts "PROBE_BEGIN_CHECKS"
catch {report_checks -path_delay max -fields {slew cap input_pins nets fanout} -format full_clock_expanded -group_count {N}}
puts "PROBE_END_CHECKS"
exit 0
"""


@dataclass
class ProbeResult:
    ok: bool = False
    stage_odb: str = ""
    wns: Optional[float] = None
    tns: Optional[float] = None
    design_area: Optional[float] = None
    worst_paths_text: str = ""
    returncode: int = -1
    error: str = ""
    raw: str = ""


# NOTE: use [^\d-]* (not \D*) so the optional leading minus is NOT consumed.
_WNS_RE = re.compile(r"(?:wns|worst slack)[^\d-]*(-?\d+(?:\.\d+)?)", re.I)
_TNS_RE = re.compile(r"tns[^\d-]*(-?\d+(?:\.\d+)?)", re.I)
_AREA_RE = re.compile(r"PROBE_AREA\s+(-?\d+(?:\.\d+)?)")


def probe_stage_odb(odb_path: str,
                    openroad_bin: str = "openroad",
                    group_count: int = 5,
                    timeout: int = 300,
                    runner: Optional[Runner] = None) -> ProbeResult:
    """Re-open a stage .odb read-only and re-time it. Never mutates the run.

    Returns a ProbeResult; ok=False (never raises) if openroad is missing, the
    odb can't be read, or the run times out — so the caller can fall back to the
    report-only Tier-1 verdict.
    """
    res = ProbeResult(stage_odb=odb_path)
    if not odb_path or not os.path.exists(odb_path):
        res.error = f"odb not found: {odb_path}"
        return res
    run = runner or _default_runner
    tcl = _PROBE_TCL.replace("{ODB}", odb_path).replace("{N}", str(group_count))
    tf = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".tcl", delete=False) as fh:
            fh.write(tcl)
            tf = fh.name
        rc, out, err = run([openroad_bin, "-no_init", "-exit", tf], timeout)
        res.returncode, res.raw = rc, out
        if rc == 127:
            res.error = "openroad not available (Tier-2 probe skipped; using Tier-1)"
            return res
        if rc == 124:
            res.error = "probe timed out"
            return res
        if "PROBE_ERROR" in out:
            res.error = next((l for l in out.splitlines() if "PROBE_ERROR" in l), "read error")
            return res
        m = _WNS_RE.search(out)
        if m:
            res.wns = float(m.group(1))
        m = _TNS_RE.search(out)
        if m:
            res.tns = float(m.group(1))
        m = _AREA_RE.search(out)
        if m:
            res.design_area = float(m.group(1))
        seg = re.search(r"PROBE_BEGIN_CHECKS(.*?)PROBE_END_CHECKS", out, re.S)
        res.worst_paths_text = seg.group(1).strip() if seg else ""
        res.ok = res.wns is not None or bool(res.worst_paths_text)
        if not res.ok and err:
            res.error = err.strip()[:300]
        return res
    finally:
        if tf and os.path.exists(tf):
            os.unlink(tf)


@dataclass
class RegressionLocus:
    from_stage: str = ""
    to_stage: str = ""
    delta_wns: Optional[float] = None
    verdict: str = ""


def compare_stages(probe_a: ProbeResult, stage_a: str,
                   probe_b: ProbeResult, stage_b: str,
                   noise_ps: float = 20.0) -> RegressionLocus:
    """Given two stage probes (a earlier, b later), attribute any WNS drop.

    Deterministic: if WNS degrades by more than `noise_ps` between the two,
    the transition `stage_a -> stage_b` OWNS the regression.
    """
    loc = RegressionLocus(from_stage=stage_a, to_stage=stage_b)
    if probe_a.wns is None or probe_b.wns is None:
        loc.verdict = "inconclusive: missing WNS in one probe"
        return loc
    loc.delta_wns = probe_b.wns - probe_a.wns
    if loc.delta_wns < -noise_ps:
        loc.verdict = (f"regression ENTERS at {stage_a}->{stage_b}: "
                       f"WNS {probe_a.wns:.1f} -> {probe_b.wns:.1f} ps "
                       f"(dropped {abs(loc.delta_wns):.1f} ps). Owner = {stage_b} layer.")
    elif loc.delta_wns > noise_ps:
        loc.verdict = (f"{stage_a}->{stage_b} IMPROVED WNS by {loc.delta_wns:.1f} ps "
                       f"(optimisation recovered timing here).")
    else:
        loc.verdict = (f"{stage_a}->{stage_b} is timing-neutral "
                       f"(dWNS={loc.delta_wns:+.1f} ps) — look elsewhere.")
    return loc


def find_stage_odb(run_dir: str, stage: str) -> Optional[str]:
    """Locate a saved .odb for a stage under an ORFS results dir.

    ORFS writes e.g. results/<platform>/<design>/base/3_place.odb. We just glob
    for <stage-prefix>*.odb so callers pass '3_place', '4_cts', etc.
    """
    import glob
    prefix = stage.split("_")[0] if "_" in stage else stage
    for pat in (f"{stage}.odb", f"{stage}*.odb", f"{prefix}_*.odb", f"{prefix}*.odb"):
        hits = sorted(glob.glob(os.path.join(run_dir, pat)))
        if hits:
            return hits[-1]
    return None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        r = probe_stage_odb(sys.argv[1])
        print(f"ok={r.ok} wns={r.wns} tns={r.tns} area={r.design_area} err={r.error}")
    else:
        print("usage: orfs_probe.py <stage.odb>   (requires openroad on PATH)")
