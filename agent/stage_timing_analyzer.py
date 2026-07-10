#!/usr/bin/env python3
"""
stage_timing_analyzer.py — deterministic, report-first cross-stage investigator
for the JANUS ASIC agent (Tier 1 of the adaptive debug strategy).

WHY THIS EXISTS
---------------
`reasoning_engine.reason()` classifies a failure from the FAILING stage's log
excerpt alone. That is why the `security` block was mis-classified as a ROUTE
problem: detailed routing was the stage that *aborted*, so the single-log view
saw "route" — but the timing was already broken at FLOORPLAN (WNS -590 ps),
long before any wire was routed. No placement/density tweak can close a hole
that exists pre-placement.

A human physical-design lead does not look at one stage. They:
  1. read the WNS/TNS *trajectory* across every stage and find the FIRST stage
     where timing went violated (that localises the true owner: RTL/SDC vs
     place vs CTS vs route);
  2. read the synthesis area/instance breakdown to see if ONE module dominates
     (an area/flop explosion is an RTL problem, not a router problem);
  3. read the worst paths to see WHAT is failing — a huge flop array, a
     high-fanout reset, deep logic — and WHERE (start/endpoint module);
  4. cluster the DRC violations to tell global congestion from a local pin
     pileup inside one macro/cell.

This module does exactly that, deterministically, with pure stdlib and no LLM.
It turns a directory of ORFS stage reports into a `StageEvidence` pack, derives
a ROOT-CAUSE layer keyed on the first-violation stage, and — critically — can
REJECT a proposed fix whose layer cannot physically explain the evidence
(e.g. a PLACE/ROUTE density change for a pre-placement timing violation).

It is deliberately independent of reasoning_engine's log-signature path; the two
are fused by `reasoning_engine.reason_with_reports()` (see that module). Runs on
report text only — no openroad/yosys needed — so it works in CI and in the
sandbox. The optional active re-timing probe lives in `orfs_probe.py` (Tier 2).
"""

from __future__ import annotations

import os
import re
import glob
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------- #
# Stage ordering + report discovery
# ---------------------------------------------------------------------------- #
# Ordered implementation stages we can see timing for. "earlier" = smaller index.
STAGE_ORDER: Tuple[str, ...] = (
    "1_synth", "2_floorplan", "3_place", "4_cts", "5_route", "6_final",
)

# Glob patterns (relative to a report dir) that identify each stage's timing
# report. ORFS emits e.g. 2_floorplan_final.rpt, 3_place / 3_resizer, 4_cts_final,
# 5_route / 5_global_route, 6_finish/6_final. We take the LAST (most-final)
# matching file per stage so the trajectory reflects the end-of-stage state.
_STAGE_GLOBS: Dict[str, Tuple[str, ...]] = {
    "2_floorplan": ("2_floorplan*.rpt", "2_*floorplan*.rpt"),
    "3_place":     ("3_resizer*.rpt", "3_detailed_place*.rpt", "3_global_place*.rpt", "3_place*.rpt"),
    "4_cts":       ("4_cts*.rpt", "4_*cts*.rpt"),
    # prefer the timing report over the DRC report for slack extraction
    "5_route":     ("5_global_route*.rpt", "5_*route*.rpt", "5_route.rpt"),
    "6_final":     ("6_finish*.rpt", "6_final*.rpt"),
}

# regexes over report bodies
_WNS_RE = re.compile(r"^\s*wns\s+(?:max|min)?\s*(-?\d+(?:\.\d+)?)", re.I | re.M)
_TNS_RE = re.compile(r"^\s*tns\s+(?:max|min)?\s*(-?\d+(?:\.\d+)?)", re.I | re.M)
# yosys synth_stat: "Chip area for module '\name': 1234.56"
_AREA_RE = re.compile(r"Chip area for module '\\?([^']+)':\s*([\d.]+)")
# a flattened flop array endpoint, e.g. tag_q[8238]$_DFFE_PN0P_ or valid_q[12]
_FLOP_ARRAY_RE = re.compile(r"([A-Za-z_][\w.]*?_q)\[(\d+)\]")
# a reset-ish startpoint
_RESET_RE = re.compile(r"\b(rst_n|rst|reset|resetn|nreset|por_n?)\b", re.I)
# power group row: "Sequential 1.71e+01 ... 33.8%"
_PWR_ROW_RE = re.compile(
    r"^\s*(Sequential|Combinational|Clock|Macro|Pad)\s+.*?(\d+(?:\.\d+)?)%\s*$", re.M)


def _read(path: str) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def _find_stage_report(report_dir: str, stage: str) -> Optional[str]:
    """Return the most-final report file for a stage, or None."""
    for pat in _STAGE_GLOBS.get(stage, ()):
        hits = sorted(glob.glob(os.path.join(report_dir, pat)))
        # prefer non-empty files; among those pick the lexically last (…_final)
        nonempty = [h for h in hits if os.path.getsize(h) > 0]
        pool = nonempty or hits
        if pool:
            return pool[-1]
    return None


# ---------------------------------------------------------------------------- #
# Individual parsers (each returns plain data; all tolerant of missing input)
# ---------------------------------------------------------------------------- #
def parse_slack(text: str) -> Tuple[Optional[float], Optional[float]]:
    """Return (wns, tns) as floats (report units, ps for ASAP7). None if absent."""
    wns = tns = None
    m = _WNS_RE.search(text)
    if m:
        wns = float(m.group(1))
    m = _TNS_RE.search(text)
    if m:
        tns = float(m.group(1))
    return wns, tns


@dataclass
class WorstPath:
    startpoint: str = ""
    endpoint: str = ""
    slack: Optional[float] = None
    is_reset_launched: bool = False
    endpoint_flop_array: Optional[str] = None   # e.g. "tag_q"
    endpoint_flop_index: Optional[int] = None    # max flattened index seen
    logic_levels: int = 0


def parse_worst_paths(text: str, limit: int = 8) -> List[WorstPath]:
    """Parse report_checks-style path blocks: Startpoint/Endpoint + logic depth."""
    paths: List[WorstPath] = []
    # split into path blocks on "Startpoint:"
    blocks = re.split(r"(?=^Startpoint:)", text, flags=re.M)
    for blk in blocks:
        sm = re.search(r"^Startpoint:\s*(.+)$", blk, re.M)
        em = re.search(r"^Endpoint:\s*(.+)$", blk, re.M)
        if not sm or not em:
            continue
        wp = WorstPath(startpoint=sm.group(1).strip(), endpoint=em.group(1).strip())
        wp.is_reset_launched = bool(_RESET_RE.search(wp.startpoint))
        fa = _FLOP_ARRAY_RE.search(wp.endpoint)
        if fa:
            wp.endpoint_flop_array = fa.group(1)
            wp.endpoint_flop_index = int(fa.group(2))
        slk = re.search(r"(-?\d+(?:\.\d+)?)\s+slack", blk)
        if slk:
            wp.slack = float(slk.group(1))
        # logic levels ~= number of cell-output rows in the path (lines ending
        # with a lib cell and a rise/fall arrow). Count "/Y" or "/A"-style pins
        # is noisy; instead count numbered fanout stages "  <n>  ".
        levels = len(re.findall(r"^\s*\d+\s+[\d.]+\s+[\d.]+", blk, re.M))
        wp.logic_levels = levels
        paths.append(wp)
        if len(paths) >= limit:
            break
    return paths


@dataclass
class SynthStat:
    top_module: str = ""
    top_area: float = 0.0
    module_area: Dict[str, float] = field(default_factory=dict)
    dominant_module: str = ""
    dominant_area: float = 0.0
    dominant_pct: float = 0.0
    fakeram_instances: int = 0


def _leaf_name(mod: str) -> str:
    """'kv_cache_datapath$janus...u_kv_cache' -> 'kv_cache_datapath'."""
    return mod.split("$", 1)[0].lstrip("\\")


def parse_synth_stat(text: str) -> SynthStat:
    st = SynthStat()
    areas: List[Tuple[str, float]] = []
    for m in _AREA_RE.finditer(text):
        name, val = m.group(1), float(m.group(2))
        areas.append((name, val))
        st.module_area[name] = val
    if not areas:
        return st
    # top module = the single largest area (the subsystem itself)
    top_name, top_val = max(areas, key=lambda kv: kv[1])
    st.top_module = _leaf_name(top_name)
    st.top_area = top_val
    # dominant *sub*module = largest area that is NOT the top and NOT a hard macro
    subs = [(n, v) for n, v in areas
            if v < top_val and not _leaf_name(n).startswith(("asic_sram", "fakeram"))]
    if subs:
        dn, dv = max(subs, key=lambda kv: kv[1])
        st.dominant_module = _leaf_name(dn)
        st.dominant_area = dv
        st.dominant_pct = 100.0 * dv / top_val if top_val else 0.0
    st.fakeram_instances = len(re.findall(r"\bfakeram\w+\b", text))
    return st


@dataclass
class PowerBreakdown:
    sequential_pct: float = 0.0
    combinational_pct: float = 0.0
    clock_pct: float = 0.0
    macro_pct: float = 0.0

    @property
    def flop_clock_pct(self) -> float:
        return self.sequential_pct + self.clock_pct


def parse_power(text: str) -> PowerBreakdown:
    pb = PowerBreakdown()
    for m in _PWR_ROW_RE.finditer(text):
        grp, pct = m.group(1).lower(), float(m.group(2))
        setattr(pb, {"sequential": "sequential_pct", "combinational": "combinational_pct",
                     "clock": "clock_pct", "macro": "macro_pct"}.get(grp, "macro_pct"), pct)
    return pb


@dataclass
class DrcSummary:
    total: int = 0
    by_type: Dict[str, int] = field(default_factory=dict)
    by_prefix: Dict[str, int] = field(default_factory=dict)
    layers: Dict[str, int] = field(default_factory=dict)
    dominant_prefix: str = ""
    dominant_prefix_pct: float = 0.0
    localized: bool = False   # True => violations cluster in ONE instance subtree


def parse_drc(text: str) -> DrcSummary:
    ds = DrcSummary()
    if not text.strip():
        return ds
    # violation type lines like "Lef58SpacingEndOfLine" / "Metal Spacing"
    for m in re.finditer(r"violation type:\s*(.+)", text, re.I):
        t = m.group(1).strip()
        ds.by_type[t] = ds.by_type.get(t, 0) + 1
        ds.total += 1
    # layers
    for m in re.finditer(r"\b(Metal\d+|M\d+|via\d+|V\d+)\b", text, re.I):
        lay = m.group(1)
        ds.layers[lay] = ds.layers.get(lay, 0) + 1
    # instance clustering: bucket by the TOP-level hierarchy component of any
    # instance path (e.g. "u_kv_cache/_063232_" -> "u_kv_cache"). This tells a
    # design-wide DRC storm from a pileup localised inside one block/macro.
    for m in re.finditer(r"\b([A-Za-z_]\w*)/[A-Za-z_]\w*", text):
        pref = m.group(1)
        ds.by_prefix[pref] = ds.by_prefix.get(pref, 0) + 1
    if ds.total == 0:
        # fall back: count "bbox" or numbered violation entries
        ds.total = len(re.findall(r"\bbox\b|\bviolation\b", text, re.I))
    if ds.by_prefix:
        top_pref, top_cnt = max(ds.by_prefix.items(), key=lambda kv: kv[1])
        total_pref = sum(ds.by_prefix.values()) or 1
        ds.dominant_prefix = top_pref
        ds.dominant_prefix_pct = 100.0 * top_cnt / total_pref
        ds.localized = ds.dominant_prefix_pct >= 60.0
    return ds


# ---------------------------------------------------------------------------- #
# The evidence pack
# ---------------------------------------------------------------------------- #
@dataclass
class StageEvidence:
    report_dir: str = ""
    # ordered [(stage, wns, tns)] for stages that had a timing report
    trajectory: List[Tuple[str, Optional[float], Optional[float]]] = field(default_factory=list)
    first_violation_stage: Optional[str] = None
    worst_wns: Optional[float] = None
    worst_wns_stage: Optional[str] = None
    synth: SynthStat = field(default_factory=SynthStat)
    power: PowerBreakdown = field(default_factory=PowerBreakdown)
    worst_paths: List[WorstPath] = field(default_factory=list)
    drc: DrcSummary = field(default_factory=DrcSummary)

    # derived structural flags
    timing_violated_pre_place: bool = False
    area_dominated_by_one_module: bool = False
    flop_array_in_worst_path: bool = False
    reset_high_fanout_suspect: bool = False
    flop_or_clock_power_dominated: bool = False

    def wns_at(self, stage: str) -> Optional[float]:
        for s, w, _ in self.trajectory:
            if s == stage:
                return w
        return None


def _stage_index(stage: Optional[str]) -> int:
    try:
        return STAGE_ORDER.index(stage) if stage else 99
    except ValueError:
        return 99


def analyze(report_dir: str, synth_stat_path: Optional[str] = None,
            slack_threshold: float = 0.0) -> StageEvidence:
    """Build a StageEvidence pack from a directory of ORFS stage reports.

    `slack_threshold` is the WNS below which we call a stage "violated" (0 ps
    by default; pass a small negative number to tolerate rounding noise).
    """
    ev = StageEvidence(report_dir=report_dir)

    # 1) WNS/TNS trajectory across stages ----------------------------------
    for stage in STAGE_ORDER:
        if stage == "1_synth":
            continue  # synth has no post-place timing report; area handled below
        rpt = _find_stage_report(report_dir, stage)
        if not rpt:
            continue
        wns, tns = parse_slack(_read(rpt))
        if wns is None and tns is None:
            continue
        ev.trajectory.append((stage, wns, tns))
        if wns is not None and (ev.worst_wns is None or wns < ev.worst_wns):
            ev.worst_wns, ev.worst_wns_stage = wns, stage
        if ev.first_violation_stage is None and wns is not None and wns < slack_threshold:
            ev.first_violation_stage = stage

    ev.timing_violated_pre_place = (
        ev.first_violation_stage is not None
        and _stage_index(ev.first_violation_stage) <= _stage_index("3_place"))

    # 2) synthesis area / dominance ----------------------------------------
    ss_path = synth_stat_path or _first_existing(
        report_dir, ("synth_stat.txt", "1_synth.rpt", "1_1_yosys*.rpt"))
    if ss_path:
        ev.synth = parse_synth_stat(_read(ss_path))
        ev.area_dominated_by_one_module = ev.synth.dominant_pct >= 60.0

    # 3) worst paths — scan BOTH the earliest-violated stage (localises the
    #    origin) AND the final stage (shows what still fails after opt), then
    #    union the structural flags. A flop array or reset cone can dominate at
    #    one stage but not the other, so we must look at both.
    wp_stage = ev.first_violation_stage or ev.worst_wns_stage or "6_final"
    seen = set()
    for stg in (wp_stage, "6_final"):
        rpt = _find_stage_report(report_dir, stg)
        if not rpt or rpt in seen:
            continue
        seen.add(rpt)
        ev.worst_paths.extend(parse_worst_paths(_read(rpt)))
    ev.flop_array_in_worst_path = any(p.endpoint_flop_array for p in ev.worst_paths)
    ev.reset_high_fanout_suspect = any(
        p.is_reset_launched and p.endpoint_flop_array for p in ev.worst_paths)

    # 4) power breakdown (from the final report, if present) ----------------
    fin = _find_stage_report(report_dir, "6_final")
    if fin:
        ev.power = parse_power(_read(fin))
        ev.flop_or_clock_power_dominated = ev.power.flop_clock_pct >= 60.0

    # 5) DRC clustering -----------------------------------------------------
    drc_path = _first_existing(report_dir, ("5_route_drc*.rpt", "*drc*.rpt"))
    if drc_path:
        ev.drc = parse_drc(_read(drc_path))

    return ev


def _first_existing(report_dir: str, pats: Tuple[str, ...]) -> Optional[str]:
    for pat in pats:
        hits = sorted(glob.glob(os.path.join(report_dir, pat)))
        nonempty = [h for h in hits if os.path.getsize(h) > 0]
        if nonempty:
            return nonempty[-1]
        if hits:
            return hits[-1]
    return None


# ---------------------------------------------------------------------------- #
# Deterministic root-cause classification from the evidence pack
# ---------------------------------------------------------------------------- #
# Layer names mirror reasoning_engine.Layer (kept as bare strings so this module
# has no import dependency on it — the fusion happens in reasoning_engine).
_L_RTL, _L_SDC, _L_SYNTH = "RTL", "SDC", "SYNTH_OPT"
_L_FP, _L_PLACE, _L_CTS, _L_ROUTE = "FLOORPLAN", "PLACE", "CTS", "ROUTE"

# The earliest stage a fix at a given layer can *first* influence timing.
# A fix whose entry stage is AFTER the first-violation stage cannot explain a
# violation that already existed upstream — that is the core guardrail.
_LAYER_ENTRY_STAGE = {
    _L_RTL: "1_synth", _L_SDC: "1_synth", _L_SYNTH: "1_synth",
    _L_FP: "2_floorplan", _L_PLACE: "3_place", _L_CTS: "4_cts", _L_ROUTE: "5_route",
}


@dataclass
class RootCause:
    layer: str
    confidence: float
    summary: str
    rationale: List[str] = field(default_factory=list)
    fix_hint: str = ""


def classify_root_cause(ev: StageEvidence) -> RootCause:
    """Name the layer that OWNS the failure, keyed on the FIRST-violation stage.

    Determinism: this reads only the parsed evidence, in a fixed priority order.
    It is intentionally conservative — when the trajectory is clean until a late
    stage, it blames that stage's layer; when timing is broken before placement,
    it refuses to blame place/route no matter what stage aborted.
    """
    rat: List[str] = []
    fv = ev.first_violation_stage

    # --- Case A: timing violated at/ before placement => upstream owner ------
    if ev.timing_violated_pre_place:
        rat.append(f"WNS is already violated at {fv} "
                   f"(wns={ev.wns_at(fv)} ps) — before detailed placement/routing, "
                   f"so no place/route/density change can be the root fix.")
        # distinguish RTL area-explosion vs SDC reset/constraint vs generic
        conf = 0.9
        if ev.area_dominated_by_one_module:
            rat.append(f"{ev.synth.dominant_module} occupies "
                       f"{ev.synth.dominant_pct:.0f}% of {ev.synth.top_module} area — "
                       f"a single-module area explosion is an RTL problem.")
        if ev.flop_array_in_worst_path:
            arrs = sorted({p.endpoint_flop_array for p in ev.worst_paths
                           if p.endpoint_flop_array})
            rat.append(f"worst-path endpoint(s) are large flop arrays "
                       f"({', '.join(arrs)}) — arrays this size belong in SRAM, not flops.")
        if ev.flop_or_clock_power_dominated:
            rat.append(f"sequential+clock power is {ev.power.flop_clock_pct:.0f}% "
                       f"of total — the design is flop/clock-tree bound.")
        if ev.reset_high_fanout_suspect:
            rat.append("a reset signal launches the worst path into a large flop "
                       "array — a high-fanout reset cone (SDC false-path / reset "
                       "pipelining / async reset may be needed).")
        # decide RTL vs SDC as the PRIMARY owner
        rtl_signal = ev.area_dominated_by_one_module or ev.flop_array_in_worst_path
        if rtl_signal:
            return RootCause(
                _L_RTL, conf,
                f"Pre-placement timing violation driven by an RTL area/flop "
                f"explosion in {ev.synth.dominant_module or 'a dominant module'}.",
                rat,
                "Re-architect the offending module (move large flop arrays into "
                "SRAM / reduce state), or scale the design-size parameter down for "
                "bring-up. Then re-synthesise. Do NOT touch placement density.")
        if ev.reset_high_fanout_suspect:
            return RootCause(
                _L_SDC, 0.75,
                "Pre-placement timing violation dominated by a high-fanout reset path.",
                rat,
                "Add a reset false-path / register the reset / synchronise + "
                "pipeline it, then re-synthesise. Density changes will not help.")
        return RootCause(
            _L_RTL, 0.7,
            "Pre-placement timing violation with no obvious congestion cause.",
            rat,
            "Investigate RTL/SDC (logic depth, constraints); placement/route "
            "tweaks cannot recover a pre-placement hole.")

    # --- Case B: timing clean until a specific late stage => that stage ------
    # find the stage where WNS first crossed into violation
    if fv is None:
        # never violated in what we saw; if the run still failed it is likely a
        # DRC / congestion problem localised late
        if ev.drc.total and ev.drc.localized:
            rat.append(f"{ev.drc.total} DRC violations cluster in "
                       f"{ev.drc.dominant_prefix} — a localised pin/congestion pileup.")
            return RootCause(_L_ROUTE, 0.7,
                             f"Localised routing DRCs inside {ev.drc.dominant_prefix}.",
                             rat,
                             "Address local congestion (cell padding / density near "
                             "that instance, pin access, or macro halo) — not a global "
                             "density change.")
        return RootCause("UNKNOWN", 0.3, "No timing violation found in reports.", rat,
                         "Inspect the failing stage log directly.")

    entry_layer = {"2_floorplan": _L_FP, "3_place": _L_PLACE,
                   "4_cts": _L_CTS, "5_route": _L_ROUTE}.get(fv, _L_ROUTE)
    rat.append(f"WNS stays clean until {fv}, then degrades — consistent with a "
               f"{entry_layer}-layer cause.")
    if fv == "5_route" and ev.drc.total:
        rat.append(f"{ev.drc.total} route DRCs, "
                   f"{'localised in ' + ev.drc.dominant_prefix if ev.drc.localized else 'spread across the design'}.")
    return RootCause(entry_layer, 0.75,
                     f"Timing/quality regression first appears at {fv}.", rat,
                     f"Apply a {entry_layer}-layer remedy; upstream stages were clean.")


def verdict_on_fix(ev: StageEvidence, fix_layer: str) -> Tuple[bool, str]:
    """Guardrail: is a fix at `fix_layer` consistent with the evidence?

    Rejects the classic mistake the security run made: a PLACE/ROUTE/density
    change proposed for a violation that exists BEFORE placement.
    Returns (accept, human_reason).
    """
    fix_layer = (fix_layer or "").upper()
    entry = _LAYER_ENTRY_STAGE.get(fix_layer)
    fv = ev.first_violation_stage
    if fv and entry and ev.timing_violated_pre_place and \
            _stage_index(entry) > _stage_index(fv):
        return (False,
                f"REJECTED: a {fix_layer} change (first affects {entry}) cannot fix a "
                f"timing violation that already exists at {fv} "
                f"(wns={ev.wns_at(fv)} ps). Fix the upstream (RTL/SDC/synth) cause.")
    if fix_layer in (_L_PLACE, _L_ROUTE) and ev.timing_violated_pre_place:
        return (False,
                f"REJECTED: {fix_layer}/density tweaks address congestion, but timing "
                f"is broken pre-placement at {fv}. This would mask, not fix, the cause.")
    return (True, f"allowed: a {fix_layer or 'unspecified'} change is consistent with the evidence.")


def evidence_briefing(ev: StageEvidence, rc: Optional[RootCause] = None) -> str:
    """Render the evidence pack + root cause as a human/LLM-readable block."""
    rc = rc or classify_root_cause(ev)
    lines = ["CROSS-STAGE EVIDENCE (deterministic, report-derived):",
             "  WNS/TNS trajectory:"]
    for s, w, t in ev.trajectory:
        flag = "  <-- FIRST VIOLATION" if s == ev.first_violation_stage else ""
        lines.append(f"    {s:<12} wns={w} ps  tns={t} ps{flag}")
    if ev.synth.top_module:
        lines.append(f"  area: {ev.synth.dominant_module} = "
                     f"{ev.synth.dominant_pct:.0f}% of {ev.synth.top_module}")
    if ev.power.flop_clock_pct:
        lines.append(f"  power: sequential+clock = {ev.power.flop_clock_pct:.0f}% of total")
    if ev.worst_paths:
        wp = ev.worst_paths[0]
        lines.append(f"  worst path: {wp.startpoint}  ->  {wp.endpoint}"
                     + (f"  [flop array {wp.endpoint_flop_array}]" if wp.endpoint_flop_array else ""))
    if ev.drc.total:
        lines.append(f"  DRC: {ev.drc.total} violations"
                     + (f", localised in {ev.drc.dominant_prefix}" if ev.drc.localized else ", spread"))
    lines += ["", f"  ROOT CAUSE -> {rc.layer} (confidence {rc.confidence:.2f})",
              f"    {rc.summary}"]
    for r in rc.rationale:
        lines.append(f"      - {r}")
    if rc.fix_hint:
        lines.append(f"    suggested remedy: {rc.fix_hint}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    e = analyze(d)
    print("trajectory:")
    for s, w, t in e.trajectory:
        print(f"  {s:<12} wns={w}  tns={t}")
    print("first violation stage:", e.first_violation_stage)
    print("pre-place violation   :", e.timing_violated_pre_place)
    print("dominant module       :", e.synth.dominant_module,
          f"{e.synth.dominant_pct:.1f}% of {e.synth.top_module}")
    print("flop array in path    :", e.flop_array_in_worst_path)
    print("reset high-fanout      :", e.reset_high_fanout_suspect)
    print("flop/clock power %    :", e.power.flop_clock_pct)
    print("drc total / localized :", e.drc.total, e.drc.localized, e.drc.dominant_prefix)
