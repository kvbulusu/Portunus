#!/usr/bin/env python3
"""
incremental_opt.py — adaptive post-route ECO orchestrator for the JANUS agent.

PRINCIPAL-ENGINEER POLICY (what the user asked for)
---------------------------------------------------
When a block finishes routing with residual violations, a good PD lead does not
blindly accept whatever an ECO produces. They run an incremental pass and then
JUDGE it:

  * HOLD is functionally critical — a hold violation means the chip does not
    work at any clock period. So hold is repaired first and ANY hold regression
    is an automatic DISCARD (revert to the pre-ECO result).
  * A setup regression beyond noise is also a discard.
  * If the ECO gives no meaningful improvement, discard it too — needless
    post-route churn is not worth perturbing a signed-off database.
  * Only if timing IMPROVED (or closed) do we adopt the ECO result.
  * Separately: if timing is CLOSED but a handful of DRCs remain, attempt a
    DRC-only cleanup — but keep it only if it reduces DRCs without reopening
    timing. Otherwise revert.

SAFETY MODEL
------------
The ECO Tcl (scripts/post_route_opt.tcl) always writes to a SEPARATE temp .odb;
the canonical 6_final.odb is never touched until we DECIDE to adopt. So a
degraded or failed ECO simply leaves the original untouched — "discard" is the
zero-risk default, and it is also what we fall back to whenever the probe can't
produce trustworthy before/after numbers. We never discard a good routed result
because our optional probe failed.

This module is pure orchestration + a pure, fully unit-tested decision engine.
The OpenROAD calls live in the Tcl; here we shell out, parse, and decide.
No network. Pure stdlib.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Parsed result of one ECO pass
# --------------------------------------------------------------------------- #
@dataclass
class ECOResult:
    ok: bool = False
    mode: str = "timing"
    setup_wns: Optional[float] = None      # ps, -ve = violation
    setup_tns: Optional[float] = None
    hold_wns: Optional[float] = None       # ps, -ve = violation (== hold WS)
    hold_tns: Optional[float] = None
    drc: Optional[int] = None
    out_odb: str = ""
    wrote: bool = False
    error: str = ""
    raw: str = ""
    pre_setup: Optional[float] = None      # §149 JANUS_PR_PRE (same session, before)
    pre_hold: Optional[float] = None
    slew_viol: Optional[int] = None        # §183 drv mode: max-slew count after (RCX)
    pre_slew_viol: Optional[int] = None    # §183 drv mode: max-slew count before
    pre_setup_tns: Optional[float] = None  # §232 JANUS_PR_PRE setup/hold tns
    pre_hold_tns: Optional[float] = None
    hold_viol: Optional[int] = None        # §232 hold violating endpoints after
    pre_hold_viol: Optional[int] = None    # ... and before
    setup_viol: Optional[int] = None       # §247 setup violating endpoints after
    pre_setup_viol: Optional[int] = None   # ... and before (email rows only)


_RE_PRE = re.compile(r"^JANUS_PR_PRE\s+(setup|hold)\s+wns\s+(-?\d+(?:\.\d+)?)", re.M)


def parse_eco_pre(out: str) -> Tuple[Optional[float], Optional[float]]:
    """(setup_wns, hold_wns) the ECO Tcl measured BEFORE repairing (§149)."""
    d = {k: float(v) for k, v in _RE_PRE.findall(out)}
    return d.get("setup"), d.get("hold")


_RE_SLEW = re.compile(r"^JANUS_PR\s+drv\s+slew\s+(-?\d+)", re.M)
_RE_PRE_SLEW = re.compile(r"^JANUS_PR_PRE\s+drv\s+slew\s+(-?\d+)", re.M)


_RE_PRE_TNS = re.compile(r"^JANUS_PR_PRE\s+(setup|hold)\s+tns\s+(-?\d+(?:\.\d+)?)", re.M)
_RE_HVIOL = re.compile(r"^JANUS_PR\s+hold\s+viol\s+(-?\d+)", re.M)
_RE_PRE_HVIOL = re.compile(r"^JANUS_PR_PRE\s+hold\s+viol\s+(-?\d+)", re.M)
_RE_SVIOL = re.compile(r"^JANUS_PR\s+setup\s+viol\s+(-?\d+)", re.M)
_RE_PRE_SVIOL = re.compile(r"^JANUS_PR_PRE\s+setup\s+viol\s+(-?\d+)", re.M)


def _slew_count(rx: "re.Pattern[str]", out: str) -> Optional[int]:
    """Last reported slew count; -1 (Tcl counter unavailable) -> None."""
    vals = rx.findall(out)
    if not vals:
        return None
    v = int(vals[-1])
    return v if v >= 0 else None


_RE_PR = re.compile(r"^JANUS_PR\s+(setup|hold)\s+(wns|tns)\s+(-?\d+(?:\.\d+)?)", re.M)
_RE_DRC = re.compile(r"^JANUS_PR\s+drc\s+(\d+)", re.M)
_RE_WROTE = re.compile(r"^JANUS_PR\s+wrote\s+(\S+)", re.M)
_RE_ERR = re.compile(r"^JANUS_PR_ERROR\s+(.*)$", re.M)


def parse_eco(out: str, mode: str = "timing", out_odb: str = "") -> ECOResult:
    """Parse post_route_opt.tcl output. Never raises."""
    r = ECOResult(mode=mode, out_odb=out_odb, raw=out)
    r.pre_slew_viol = _slew_count(_RE_PRE_SLEW, out)
    for kind, val in _RE_PRE_TNS.findall(out):
        setattr(r, f"pre_{kind}_tns", float(val))
    r.pre_hold_viol = _slew_count(_RE_PRE_HVIOL, out)
    r.pre_setup_viol = _slew_count(_RE_PRE_SVIOL, out)
    m = _RE_ERR.search(out)
    if m:
        r.error = m.group(1).strip()
        return r
    for kind, metric, val in _RE_PR.findall(out):
        setattr(r, f"{kind}_{metric}", float(val))
    md = _RE_DRC.search(out)
    if md:
        r.drc = int(md.group(1))
    r.slew_viol = _slew_count(_RE_SLEW, out)
    r.hold_viol = _slew_count(_RE_HVIOL, out)
    r.setup_viol = _slew_count(_RE_SVIOL, out)
    mw = _RE_WROTE.search(out)
    r.wrote = bool(mw)
    if mw and not out_odb:
        r.out_odb = mw.group(1)
    # trustworthy only if it wrote a db AND reported at least one slack
    r.ok = r.wrote and (r.setup_wns is not None or r.hold_wns is not None)
    return r


# --------------------------------------------------------------------------- #
# PURE decision engine (unit-tested)
# --------------------------------------------------------------------------- #
def _closed(setup: Optional[float], hold: Optional[float]) -> bool:
    """Timing closed == both setup and hold worst-slack >= 0 (known)."""
    return (setup is not None and setup >= 0.0
            and hold is not None and hold >= 0.0)


def decide_timing(before_setup: Optional[float], before_hold: Optional[float],
                  after: ECOResult, noise_ps: float = 1.0
                  ) -> Tuple[str, str]:
    """Adopt or discard a timing ECO. Returns ("adopt"|"discard", reason).

    HOLD is critical: any hold regression -> discard, unconditionally.
    """
    if not after.ok:
        return ("discard",
                f"ECO produced no trustworthy timing data ({after.error or 'no write/slack'}); "
                "keeping pre-ECO result untouched")

    a_setup, a_hold = after.setup_wns, after.hold_wns

    # 1) HOLD guard (functional correctness) — reject any real hold regression.
    if before_hold is not None and a_hold is not None:
        if a_hold < before_hold - noise_ps:
            return ("discard",
                    f"HOLD WS regressed {before_hold:+.3f} -> {a_hold:+.3f} ps "
                    "— hold is functionally critical; reverting")

    # 2) SETUP guard — reject a setup regression beyond noise.
    if before_setup is not None and a_setup is not None:
        if a_setup < before_setup - noise_ps:
            return ("discard",
                    f"setup WNS regressed {before_setup:+.3f} -> {a_setup:+.3f} ps; reverting")

    # 3) Did it actually help? Improvement = meaningful gain on either metric,
    #    OR it closed timing that was previously open.
    improved = False
    gains = []
    if before_hold is not None and a_hold is not None and a_hold > before_hold + noise_ps:
        improved = True
        gains.append(f"hold {before_hold:+.3f}->{a_hold:+.3f} ps")
    if before_setup is not None and a_setup is not None and a_setup > before_setup + noise_ps:
        improved = True
        gains.append(f"setup {before_setup:+.3f}->{a_setup:+.3f} ps")
    now_closed = _closed(a_setup, a_hold)
    was_closed = _closed(before_setup, before_hold)
    if now_closed and not was_closed:
        improved = True
        gains.append("timing now CLOSED")

    if improved:
        return ("adopt", "ECO improved timing: " + "; ".join(gains))
    return ("discard",
            "ECO gave no meaningful timing improvement; reverting to avoid "
            "unnecessary post-route churn")


def decide_drv(before_setup: Optional[float], before_hold: Optional[float],
               before_drc: Optional[int], after: ECOResult,
               setup_tol_ps: float = 5.0, noise_ps: float = 1.0
               ) -> Tuple[str, str]:
    """§183 adopt/discard a post-route max-slew (drv) ECO.

    The goal is fewer max-slew violations on an already-closed block, so the
    timing guards are absolute: hold must stay >= 0 and not regress beyond
    noise; setup may move by <= setup_tol_ps (repeaters on long port wires
    usually help, but a small shift is acceptable) and must not go negative if
    it was >= 0; DRC must not increase; the slew count must drop.
    """
    if not after.ok:
        return ("discard", f"drv ECO produced no trustworthy data "
                f"({after.error or 'no write/slack'}); keeping pre-ECO result")
    a_s, a_h = after.setup_wns, after.hold_wns
    if a_h is None or a_s is None:
        return ("discard", "drv ECO: post-ECO setup/hold unknown; reverting")
    # §249: a block whose hold is already slightly negative (soc_top -7.31 ps,
    # 2 macro-pin endpoints) may still take a slew ECO provided hold does not
    # get worse: WS within noise AND violator count not up. A block with hold
    # >= 0 must stay >= 0 (§183 rule, unchanged).
    if a_h < 0.0 and (before_hold is None or before_hold >= 0.0):
        return ("discard", f"drv ECO: hold {a_h:+.3f} ps < 0; reverting")
    if before_hold is not None and a_h < before_hold - noise_ps:
        return ("discard", f"drv ECO: hold regressed {before_hold:+.3f} -> {a_h:+.3f} ps; reverting")
    if (after.pre_hold_viol is not None and after.hold_viol is not None
            and after.hold_viol > after.pre_hold_viol):
        return ("discard", f"drv ECO: hold violators {after.pre_hold_viol} -> "
                f"{after.hold_viol}; reverting")
    if before_setup is not None:
        if a_s < before_setup - setup_tol_ps:
            return ("discard", f"drv ECO: setup regressed {before_setup:+.3f} -> {a_s:+.3f} ps "
                    f"(> {setup_tol_ps:g} ps tol); reverting")
        if before_setup >= 0.0 and a_s < 0.0:
            return ("discard", f"drv ECO: setup reopened {before_setup:+.3f} -> {a_s:+.3f} ps; reverting")
    if before_drc is not None and (after.drc is None or after.drc > before_drc):
        return ("discard", f"drv ECO: DRC {before_drc} -> {after.drc}; reverting")
    pre, post = after.pre_slew_viol, after.slew_viol
    if pre is None or post is None:
        return ("discard", f"drv ECO: slew count unknown ({pre} -> {post}); reverting")
    if post >= pre:
        return ("discard", f"drv ECO: max-slew not reduced ({pre} -> {post}); reverting")
    return ("adopt", f"max-slew {pre} -> {post}; setup {before_setup} -> {a_s:+.3f}, "
            f"hold {before_hold} -> {a_h:+.3f} ps, DRC {before_drc} -> {after.drc}")


def decide_drc(timing_closed: bool,
               before_drc: Optional[int], after: ECOResult,
               handful: int = 20, noise_ps: float = 1.0,
               pre_setup: Optional[float] = None,
               pre_hold: Optional[float] = None) -> Tuple[str, str]:
    """Decide whether to attempt / keep a DRC-only cleanup.

    Only run when timing is CLOSED and a small number of DRCs remain. Keep the
    result only if DRCs dropped and timing stayed closed (no reopened setup/hold).
    Returns one of: "skip" (don't attempt), "adopt", "discard".
    """
    if not timing_closed:
        return ("skip", "timing not closed — fix timing before DRC")
    if before_drc is None:
        return ("skip", "DRC count unknown — not attempting blind DRC ECO")
    if before_drc == 0:
        return ("skip", "no DRCs to fix")
    if before_drc > handful:
        return ("skip",
                f"{before_drc} DRCs exceed the 'handful' threshold ({handful}) — "
                "likely global congestion; needs a floorplan/route strategy, not an ECO")
    # attempted: judge the after
    if not after.wrote:
        return ("discard", f"DRC ECO did not complete ({after.error or 'no write'}); reverting")
    # timing must still be closed
    if not _closed(after.setup_wns, after.hold_wns):
        return ("discard", "DRC ECO reopened timing; reverting")
    # hold must not have regressed vs pre
    if pre_hold is not None and after.hold_wns is not None and after.hold_wns < pre_hold - noise_ps:
        return ("discard", "DRC ECO regressed hold; reverting")
    if after.drc is None:
        return ("discard", "DRC count after ECO unknown — cannot confirm improvement; reverting")
    if after.drc < before_drc:
        return ("adopt", f"DRC reduced {before_drc} -> {after.drc}, timing still closed")
    return ("discard", f"DRC not reduced ({before_drc} -> {after.drc}); reverting")


# --------------------------------------------------------------------------- #
# Orchestration (shells out to OpenROAD via the Tcl)
# --------------------------------------------------------------------------- #
Runner = Callable[[List[str], int, Dict[str, str]], Tuple[int, str, str]]

_TCL_REL = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "post_route_opt.tcl")


def _default_runner(argv, timeout, env):
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, check=False, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + "\n[timeout]"
    except FileNotFoundError:
        return 127, "", "openroad not found on PATH"


def run_eco_pass(odb_path: str, mode: str,
                 openroad_bin: str = "openroad",
                 tcl_path: Optional[str] = None,
                 extra_env: Optional[Dict[str, str]] = None,
                 timeout: int = 3600,
                 runner: Optional[Runner] = None,
                 tmpdir: Optional[str] = None) -> ECOResult:
    """Run one ECO pass writing to a temp odb. ok=False (never raises) on any
    failure so the caller keeps the original."""
    r = ECOResult(mode=mode)
    if not odb_path or not os.path.exists(odb_path):
        r.error = f"odb not found: {odb_path}"
        return r
    tcl = tcl_path or os.path.abspath(_TCL_REL)
    if not os.path.exists(tcl):
        r.error = f"tcl not found: {tcl}"
        return r
    out_odb = os.path.join(tmpdir or tempfile.gettempdir(),
                           f"janus_eco_{mode}_{os.getpid()}.odb")
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env["JANUS_ODB"] = odb_path
    env["JANUS_OUT_ODB"] = out_odb
    env["JANUS_PR_MODE"] = mode
    run = runner or _default_runner
    rc, out, err = run([openroad_bin, "-no_init", "-exit", tcl], timeout, env)
    if rc == 127:
        r.error = "openroad not available (post-route ECO skipped)"
        return r
    if rc == 124:
        r.error = "post-route ECO timed out"
        return r
    parsed = parse_eco(out, mode=mode, out_odb=out_odb)
    if not parsed.ok and err and not parsed.error:
        parsed.error = err.strip()[:300]
    return parsed


def adopt_odb(new_odb: str, canonical_odb: str,
              backup_suffix: str = ".preeco") -> bool:
    """Replace the canonical routed odb with the ECO result, keeping a backup.
    Returns True on success."""
    if not (new_odb and os.path.exists(new_odb)):
        return False
    try:
        if os.path.exists(canonical_odb):
            shutil.copy2(canonical_odb, canonical_odb + backup_suffix)
        shutil.move(new_odb, canonical_odb)
        return True
    except OSError:
        return False


def discard_odb(new_odb: str) -> None:
    """Throw away an ECO result; canonical odb was never touched."""
    try:
        if new_odb and os.path.exists(new_odb):
            os.remove(new_odb)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# self-test — the decision engine is the risky logic, so exercise it hard
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # ---- parse ----
    good = ("JANUS_PR_BEGIN timing\n"
            "JANUS_PR setup wns -0.500\nJANUS_PR setup tns -3.0\n"
            "JANUS_PR hold wns 2.000\nJANUS_PR hold tns 0.0\n"
            "JANUS_PR drc 4\nJANUS_PR wrote /tmp/x.odb\nJANUS_PR_END timing\n")
    r = parse_eco(good, "timing", "/tmp/x.odb")
    assert r.ok and r.setup_wns == -0.5 and r.hold_wns == 2.0 and r.drc == 4 and r.wrote

    err = parse_eco("JANUS_PR_ERROR read_db: nope\n")
    assert not err.ok and "read_db" in err.error

    nowrite = parse_eco("JANUS_PR setup wns 1.0\nJANUS_PR hold wns 1.0\n")
    assert not nowrite.ok  # never wrote a db -> not trustworthy

    # ---- decide_timing ----
    # hold regression -> discard even if setup improved a lot
    a = parse_eco("JANUS_PR setup wns 5.0\nJANUS_PR hold wns -0.5\nJANUS_PR wrote /t\n")
    d, why = decide_timing(before_setup=-3.0, before_hold=2.0, after=a)
    assert d == "discard" and "HOLD" in why, (d, why)

    # setup regression -> discard
    a = parse_eco("JANUS_PR setup wns -5.0\nJANUS_PR hold wns 2.0\nJANUS_PR wrote /t\n")
    d, why = decide_timing(before_setup=-3.0, before_hold=2.0, after=a)
    assert d == "discard" and "setup WNS regressed" in why

    # genuine improvement (both up, closes) -> adopt
    a = parse_eco("JANUS_PR setup wns 0.5\nJANUS_PR hold wns 3.0\nJANUS_PR wrote /t\n")
    d, why = decide_timing(before_setup=-3.876, before_hold=2.0, after=a)
    assert d == "adopt" and "CLOSED" in why, (d, why)

    # neutral (no meaningful change) -> discard (avoid churn)
    a = parse_eco("JANUS_PR setup wns -3.6\nJANUS_PR hold wns 2.0\nJANUS_PR wrote /t\n")
    d, why = decide_timing(before_setup=-3.876, before_hold=2.0, after=a)
    assert d == "discard" and "no meaningful" in why, (d, why)

    # probe failed -> discard, keep original (safe)
    a = parse_eco("JANUS_PR_ERROR openroad boom\n")
    d, why = decide_timing(-3.0, 2.0, a)
    assert d == "discard" and "untouched" in why

    # hold improvement alone (setup neutral) -> adopt
    a = parse_eco("JANUS_PR setup wns -3.8\nJANUS_PR hold wns 1.5\nJANUS_PR wrote /t\n")
    d, why = decide_timing(before_setup=-3.876, before_hold=-0.5, after=a)
    assert d == "adopt" and "hold" in why, (d, why)

    # ---- decide_drc ----
    # timing open -> skip
    assert decide_drc(False, 5, ECOResult())[0] == "skip"
    # too many -> skip (congestion)
    s, why = decide_drc(True, 500, ECOResult(), handful=20)
    assert s == "skip" and "congestion" in why
    # unknown before -> skip
    assert decide_drc(True, None, ECOResult())[0] == "skip"
    # attempted, reduced, timing still closed -> adopt
    a = parse_eco("JANUS_PR setup wns 0.5\nJANUS_PR hold wns 3.0\nJANUS_PR drc 1\nJANUS_PR wrote /t\n")
    s, why = decide_drc(True, 4, a, pre_setup=0.5, pre_hold=3.0)
    assert s == "adopt" and "4 -> 1" in why, (s, why)
    # attempted, reopened timing -> discard
    a = parse_eco("JANUS_PR setup wns -0.2\nJANUS_PR hold wns 3.0\nJANUS_PR drc 0\nJANUS_PR wrote /t\n")
    s, why = decide_drc(True, 4, a, pre_setup=0.5, pre_hold=3.0)
    assert s == "discard" and "reopened" in why
    # attempted, DRC not reduced -> discard
    a = parse_eco("JANUS_PR setup wns 0.5\nJANUS_PR hold wns 3.0\nJANUS_PR drc 4\nJANUS_PR wrote /t\n")
    s, why = decide_drc(True, 4, a, pre_setup=0.5, pre_hold=3.0)
    assert s == "discard" and "not reduced" in why

    print("incremental_opt self-test OK")
