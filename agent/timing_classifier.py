#!/usr/bin/env python3
"""
timing_classifier.py — turn OpenROAD's classified-timing dump into a per-block
breakdown the console can show.

WHY THIS EXISTS
---------------
The agent's status table scraped setup WNS from ORFS stdout and showed hold as
"n/a" (ORFS's make output has no hold worst-slack line the scraper caught), and
it never said WHAT is failing. A physical-design lead wants, per block:

    setup WNS = -3.9 ps  |  hold WS = +12.0 ps
    setup violating endpoints: 7      hold violating endpoints: 0
      reg2reg : 5     (register -> register)
      in2reg  : 1     (input port -> register)
      reg2mem : 1     (register -> SRAM/macro)

This module parses the dump emitted by scripts/report_timing_classified.tcl
(run READ-ONLY on the final routed .odb) and produces exactly that.

Design notes
  * The RISKY logic (endpoint classification) is here in Python and is fully
    unit-tested against synthetic OpenROAD text — see the __main__ self-test.
  * The Tcl side only emits robust primitives: the port list, the macro-instance
    list, worst slack / TNS, and the raw report_checks bodies. Classification is
    a pure name lookup: a pin whose instance is a macro -> "mem"; a pin that is
    a top-level port -> "io"; anything else -> "reg".
  * Everything degrades gracefully: a missing openroad, a missing .odb, or a
    parse miss yields an object with ok=False and None slacks so the caller
    falls back to whatever it already had. Never raises on bad input.

Pure stdlib. No network.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

# Path-class labels. Kept explicit so the console wording is stable and the
# categories the user asked for (reg2reg, reg2mem, reg2io, io2io) are first.
_CLASS_ORDER = [
    "reg2reg", "in2reg", "reg2out", "in2out",
    "reg2mem", "mem2reg", "mem2mem", "other",
]
_CLASS_HELP = {
    "reg2reg": "register -> register",
    "in2reg":  "input port -> register",
    "reg2out": "register -> output port",
    "in2out":  "input port -> output port (feed-through)",
    "reg2mem": "register -> SRAM/macro",
    "mem2reg": "SRAM/macro -> register",
    "mem2mem": "SRAM/macro -> SRAM/macro",
    "other":   "unclassified",
}


def _kind(token: str, ports: set, macros: set) -> str:
    """Classify one path terminal as 'io' | 'mem' | 'reg'.

    `token` is a Startpoint/Endpoint name as printed by report_checks, e.g.
    'core/u_rf/mem_q[3]/CLK', 'data_in[7]', or 'u_sram_0/A[4]'. We test the
    whole name and its instance prefix (name up to the last '/') against the
    port and macro sets emitted by the Tcl.
    """
    inst = token.rsplit("/", 1)[0] if "/" in token else token
    # strip a trailing bus subscript on the instance, e.g. u_sram[0] stays as-is
    if token in ports or inst in ports:
        return "io"
    if inst in macros or token in macros:
        return "mem"
    # a bare port often prints with no '/'; if it's not a known port/macro and
    # has no pin path it is still most likely a register bit or constant — treat
    # a slash-less token that is not a port as 'reg' (registers print .../CLK).
    return "reg"


def _classify(start: str, end: str, ports: set, macros: set) -> str:
    s = _kind(start, ports, macros)
    e = _kind(end, ports, macros)
    # collapse to the user's vocabulary
    smap = {"io": "in", "reg": "reg", "mem": "mem"}
    emap = {"io": "out", "reg": "reg", "mem": "mem"}
    key = f"{smap[s]}2{emap[e]}"
    return key if key in _CLASS_HELP else "other"


@dataclass
class ClassifiedTiming:
    ok: bool = False
    odb: str = ""
    setup_wns: Optional[float] = None      # ps, -ve = violation
    setup_tns: Optional[float] = None
    hold_wns: Optional[float] = None       # ps, -ve = violation  (this is hold WS)
    hold_tns: Optional[float] = None
    setup_viol_endpoints: int = 0
    hold_viol_endpoints: int = 0
    setup_classes: Dict[str, int] = field(default_factory=dict)
    hold_classes: Dict[str, int] = field(default_factory=dict)
    error: str = ""
    raw: str = ""


# --- parsing -----------------------------------------------------------------
_RE_PORT = re.compile(r"^JANUS_PORT\s+(\S+)", re.M)
_RE_MACRO = re.compile(r"^JANUS_MACRO\s+(\S+)", re.M)
_RE_TC = re.compile(r"^JANUS_TC\s+(setup|hold)\s+(wns|tns)\s+(-?\d+(?:\.\d+)?)", re.M)
_RE_START = re.compile(r"Startpoint:\s*(\S+)")
_RE_END = re.compile(r"Endpoint:\s*(\S+)")


def _segment(out: str, which: str) -> str:
    m = re.search(rf"JANUS_TC_BEGIN\s+{which}\b(.*?)JANUS_TC_END\s+{which}\b",
                  out, re.S)
    return m.group(1) if m else ""


def _paths(segment: str) -> List[Tuple[str, str]]:
    """Extract (startpoint, endpoint) pairs in report order.

    report_checks prints, per path, a 'Startpoint:' line then an 'Endpoint:'
    line. We pair them positionally, which is robust to the fields in between.
    """
    starts = _RE_START.findall(segment)
    ends = _RE_END.findall(segment)
    return list(zip(starts, ends))


def parse_classified(out: str, odb: str = "") -> ClassifiedTiming:
    """Parse the report_timing_classified.tcl dump. Never raises."""
    ct = ClassifiedTiming(odb=odb, raw=out)
    if "JANUS_TC_ERROR" in out:
        ct.error = next((l for l in out.splitlines() if "JANUS_TC_ERROR" in l), "error")
        return ct
    ports = set(_RE_PORT.findall(out))
    macros = set(_RE_MACRO.findall(out))
    for kind, metric, val in _RE_TC.findall(out):
        v = float(val)
        setattr(ct, f"{kind}_{'wns' if metric == 'wns' else 'tns'}", v)

    for which in ("setup", "hold"):
        seg = _segment(out, which)
        classes: Dict[str, int] = {}
        seen_endpoints = set()
        for start, end in _paths(seg):
            if end in seen_endpoints:      # dedup: count each endpoint once
                continue
            seen_endpoints.add(end)
            cls = _classify(start, end, ports, macros)
            classes[cls] = classes.get(cls, 0) + 1
        if which == "setup":
            ct.setup_viol_endpoints = len(seen_endpoints)
            ct.setup_classes = classes
        else:
            ct.hold_viol_endpoints = len(seen_endpoints)
            ct.hold_classes = classes

    ct.ok = (ct.setup_wns is not None) or (ct.hold_wns is not None) \
        or bool(ct.setup_classes) or bool(ct.hold_classes)
    return ct


# --- rendering ---------------------------------------------------------------
def _fmt_slack(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{v:+.3f} ps"


def render(ct: ClassifiedTiming) -> str:
    """Human-readable console block (plain text, safe everywhere)."""
    if not ct.ok:
        return f"  timing detail: unavailable ({ct.error or 'no data'})"
    lines: List[str] = []
    lines.append(f"  setup WNS = {_fmt_slack(ct.setup_wns)}   "
                 f"hold WS = {_fmt_slack(ct.hold_wns)}")
    lines.append(f"  setup TNS = {_fmt_slack(ct.setup_tns)}   "
                 f"hold TNS = {_fmt_slack(ct.hold_tns)}")
    lines.append(f"  violating endpoints:  setup={ct.setup_viol_endpoints}   "
                 f"hold={ct.hold_viol_endpoints}")
    for label, classes in (("setup", ct.setup_classes), ("hold", ct.hold_classes)):
        if not classes:
            continue
        lines.append(f"  {label} violations by path class:")
        for cls in _CLASS_ORDER:
            if classes.get(cls):
                lines.append(f"      {cls:<8} {classes[cls]:>5}   "
                             f"({_CLASS_HELP[cls]})")
    return "\n".join(lines)


# --- runner ------------------------------------------------------------------
Runner = Callable[[List[str], int, Dict[str, str]], Tuple[int, str, str]]

_TCL_REL = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "report_timing_classified.tcl")


def _default_runner(argv: List[str], timeout: int,
                    env: Dict[str, str]) -> Tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, check=False, env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", (e.stderr or "") + "\n[timeout]"
    except FileNotFoundError:
        return 127, "", "openroad not found on PATH"


def classify_routed_odb(odb_path: str,
                        openroad_bin: str = "openroad",
                        tcl_path: Optional[str] = None,
                        timeout: int = 600,
                        runner: Optional[Runner] = None) -> ClassifiedTiming:
    """Run report_timing_classified.tcl on a routed .odb and parse it.

    ok=False (never raises) if openroad is missing, the odb can't be read, or
    the run times out — the caller keeps whatever metrics it already had.
    """
    ct = ClassifiedTiming(odb=odb_path)
    if not odb_path or not os.path.exists(odb_path):
        ct.error = f"odb not found: {odb_path}"
        return ct
    tcl = tcl_path or os.path.abspath(_TCL_REL)
    if not os.path.exists(tcl):
        ct.error = f"tcl not found: {tcl}"
        return ct
    run = runner or _default_runner
    env = dict(os.environ)
    env["JANUS_ODB"] = odb_path
    rc, out, err = run([openroad_bin, "-no_init", "-exit", "-threads", "1", tcl],
                       timeout, env)
    if rc == 127:
        ct.error = "openroad not available (classified timing skipped)"
        return ct
    if rc == 124:
        ct.error = "classified-timing probe timed out"
        return ct
    parsed = parse_classified(out, odb=odb_path)
    if not parsed.ok and err and not parsed.error:
        parsed.error = err.strip()[:300]
    return parsed


# --- self-test ---------------------------------------------------------------
if __name__ == "__main__":
    # Synthetic OpenROAD dump: 3 ports, 1 SRAM macro, mixed violating paths.
    sample = r"""
JANUS_PORT clk
JANUS_PORT data_in[0]
JANUS_PORT data_out[0]
JANUS_MACRO u_l2_sram_0
JANUS_TC setup wns -3.876
JANUS_TC setup tns -41.200
JANUS_TC hold wns 12.004
JANUS_TC hold tns 0.000
JANUS_TC_BEGIN setup
Startpoint: core/a_reg/CLK (rising edge-triggered ...)
Endpoint: core/b_reg/D (rising edge-triggered ...)
  ... slack -1.100
Startpoint: core/c_reg/CLK
Endpoint: core/b_reg/D
  ... slack -0.900
Startpoint: data_in[0] (input port ...)
Endpoint: core/d_reg/D
  ... slack -0.500
Startpoint: core/e_reg/CLK
Endpoint: u_l2_sram_0/A[3]
  ... slack -3.876
JANUS_TC_END setup
JANUS_TC_BEGIN hold
JANUS_TC_END hold
"""
    ct = parse_classified(sample, odb="/x/6_final.odb")
    assert ct.ok
    assert ct.setup_wns == -3.876, ct.setup_wns
    assert ct.setup_tns == -41.2, ct.setup_tns
    assert ct.hold_wns == 12.004, ct.hold_wns
    # dedup: b_reg/D appears twice -> counts once. Unique endpoints: b_reg,
    # d_reg, sram -> 3 violating endpoints.
    assert ct.setup_viol_endpoints == 3, ct.setup_viol_endpoints
    assert ct.setup_classes.get("reg2reg") == 1, ct.setup_classes   # a->b (first wins)
    assert ct.setup_classes.get("in2reg") == 1, ct.setup_classes    # data_in->d
    assert ct.setup_classes.get("reg2mem") == 1, ct.setup_classes   # e->sram
    assert ct.hold_viol_endpoints == 0
    # renderer smoke
    txt = render(ct)
    assert "setup WNS = -3.876 ps" in txt
    assert "hold WS = +12.004 ps" in txt
    assert "reg2mem" in txt and "reg2reg" in txt

    # clean design: no violations
    clean = ("JANUS_TC setup wns 5.000\nJANUS_TC hold wns 3.000\n"
             "JANUS_TC_BEGIN setup\nJANUS_TC_END setup\n"
             "JANUS_TC_BEGIN hold\nJANUS_TC_END hold\n")
    c2 = parse_classified(clean)
    assert c2.ok and c2.setup_viol_endpoints == 0 and c2.hold_viol_endpoints == 0
    assert "violating endpoints:  setup=0   hold=0" in render(c2)

    # error dump
    e = parse_classified("JANUS_TC_ERROR read_db: no such file")
    assert not e.ok and "read_db" in e.error

    # missing odb
    r = classify_routed_odb("/no/such.odb")
    assert not r.ok and "odb not found" in r.error

    print("timing_classifier self-test OK")
