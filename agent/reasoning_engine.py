#!/usr/bin/env python3
"""
reasoning_engine.py — a tool-agnostic "staff/principal engineer" reasoning layer
for the JANUS ASIC agent.

WHY THIS EXISTS
---------------
`physical_analyzer.py` proved that ONE class of failure (MPL-0006 macro-fit) can
be reasoned about deterministically — no LLM, no guessing — because the physics
of the problem is known. This module GENERALISES that idea to the WHOLE flow.

The insight (see docs/AGENT_REASONING_ARCHITECTURE.md): the agent and the human
assistant call the *same* LLM. The difference in reasoning quality is NOT the
model — it is the SCAFFOLDING: the human loop retrieves evidence, forms and
ranks hypotheses, reasons about cross-stage blast radius, and only THEN decides
whether the true fix belongs in RTL / SDC / synthesis / floorplan / CTS / route,
or is merely a tool bug needing a workaround. This module gives the agent that
scaffolding, so the LLM (when it is finally consulted) is asked a *precisely
framed* engineering question rather than "here is a log, fix it".

DESIGN PRINCIPLES
-----------------
1. DETERMINISM FIRST. A catalogue of stage-aware failure signatures classifies
   the failure and names the most likely *remedy layer* WITHOUT an LLM call.
   Only genuinely novel/ambiguous failures escalate to the LLM.
2. TOOL-AGNOSTIC CORE. The taxonomy (which layer owns the fix, what it costs
   downstream) is independent of ORFS/Yosys/OpenROAD. Only the concrete
   *workaround* is tool-specific, and that is applied elsewhere (fix_applier).
3. BLAST-RADIUS AWARE. Every candidate fix is scored by which flow stages (and
   which dependent BLOCKS) it invalidates — an RTL edit re-runs everything;
   a route-layer tweak re-runs only routing. The agent is nudged toward the
   *smallest reversible* change that addresses the true root cause — never a
   band-aid that masks an upstream bug (e.g. lowering utilization to dodge a
   real RTL area explosion).
4. TOOL-BUG TRIAGE. Before proposing an RTL/SDC change, decide whether the tool
   itself crashed (segfault, missing .so, internal assertion). If so, the remedy
   is a workaround/skip/version-pin, NOT a design change.

Pure stdlib. No network. Safe to import anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------- #
# Taxonomy — tool-agnostic
# ---------------------------------------------------------------------------- #
# The ordered ASIC implementation stages. Blast radius = "this stage and every
# stage to its right must be re-run".
STAGES: Tuple[str, ...] = (
    "rtl", "1_synth", "2_floorplan", "3_place", "4_cts", "5_route", "6_final",
)


class Layer:
    """Where a fix *belongs* (who owns the root cause). Tool-agnostic."""
    ENV = "ENV"                    # environment/infra (missing .so, OOM, PATH)
    TOOL_WORKAROUND = "TOOL_WA"    # the EDA tool itself is buggy → workaround/skip/pin
    RTL = "RTL"                    # the source hardware is wrong/too big
    SDC = "SDC"                    # timing constraints (clock, IO delay, exceptions)
    SYNTH_OPT = "SYNTH_OPT"        # synthesis options (effort, ABC, flatten, retime)
    FLOORPLAN = "FLOORPLAN"        # die/core area, utilization, macro place, halo, PDN
    PLACE = "PLACE"                # placement density/padding/legalization
    CTS = "CTS"                    # clock tree options (buffers, skew, wire RC)
    ROUTE = "ROUTE"               # routing layers, congestion, antenna, DRC
    UNKNOWN = "UNKNOWN"

    # The earliest flow stage a change in this layer invalidates. Used to
    # compute blast radius deterministically.
    _ENTRY_STAGE: Dict[str, str] = {
        RTL: "1_synth",           # re-synth and everything after
        SYNTH_OPT: "1_synth",
        SDC: "1_synth",           # SDC feeds synth timing → safest to re-synth
        FLOORPLAN: "2_floorplan",
        PLACE: "3_place",
        CTS: "4_cts",
        ROUTE: "5_route",
        TOOL_WORKAROUND: "1_synth",  # unknown; assume the failing stage onward
        ENV: "1_synth",
        UNKNOWN: "1_synth",
    }


def downstream_stages(layer: str, failing_stage: Optional[str] = None) -> List[str]:
    """Stages that must be re-run if we change `layer`.

    For TOOL_WORKAROUND/ENV the change usually does not touch design intent, so
    we only re-run from the *failing* stage onward (cheaper). For design layers
    we re-run from the layer's entry stage onward (the honest cost).
    """
    if layer in (Layer.TOOL_WORKAROUND, Layer.ENV) and failing_stage in STAGES:
        entry = failing_stage
    else:
        entry = Layer._ENTRY_STAGE.get(layer, "1_synth")
    i = STAGES.index(entry) if entry in STAGES else 0
    return list(STAGES[i:])


def affected_blocks(block: str, layer: str, dep_graph: Dict[str, Set[str]]) -> List[str]:
    """Other blocks invalidated by a change to `block` at `layer`.

    Only design-layer changes that alter a block's *abstract* (.lef/.lib) —
    i.e. RTL, and area-moving FLOORPLAN/SYNTH changes — ripple to the blocks
    that consume it as a macro. SDC/CTS/ROUTE/tool changes that preserve the
    abstract footprint do not force dependents to re-run.
    """
    if layer not in (Layer.RTL, Layer.FLOORPLAN, Layer.SYNTH_OPT):
        return []
    dependents = [b for b, deps in dep_graph.items() if block in deps]
    # transitively close (a dependent's dependents also ripple)
    out: Set[str] = set(dependents)
    changed = True
    while changed:
        changed = False
        for b, deps in dep_graph.items():
            if b not in out and (deps & out):
                out.add(b)
                changed = True
    return sorted(out)


# ---------------------------------------------------------------------------- #
# Failure-signature catalogue (deterministic, stage-aware)
# ---------------------------------------------------------------------------- #
@dataclass
class Signature:
    name: str
    pattern: re.Pattern
    stage: str                     # flow stage this typically appears in
    layer: str                     # most likely remedy layer
    root_cause: str                # one-line human explanation
    remedy_hint: str               # what the fix typically is (tool-agnostic)
    confidence: float              # 0..1 how sure the *classification* is
    tool_bug: bool = False         # is this usually a tool defect, not design?
    reversible: bool = True        # can the fix be trivially rolled back?
    deterministic: bool = False    # True => a dedicated analyzer can auto-fix


# Ordered most-specific first. Every entry was chosen from real ORFS / Yosys /
# OpenROAD failure modes relevant to JANUS. `confidence` reflects how reliably
# the *pattern* implies the *layer* — not that the fix is guaranteed.
_SIGNATURES: List[Signature] = [
    # ---- ENV / TOOL packaging (never a design bug) -----------------------
    Signature("missing_shared_lib",
              re.compile(r"cannot open shared object file|while loading shared libraries|libnaja|libortools", re.I),
              "any", Layer.ENV,
              "an EDA binary can't load its own .so — install/packaging issue",
              "add the lib dir to LD_LIBRARY_PATH or skip the optional step (e.g. EQUIVALENCE_CHECK=0)",
              0.97, tool_bug=True),
    Signature("oom_killed",
              re.compile(r"\bKilled\b|Out of memory|std::bad_alloc|Cannot allocate memory", re.I),
              "any", Layer.ENV,
              "the process was OOM-killed — host RAM, not the design",
              "raise host memory / reduce parallelism / split the block",
              0.9, tool_bug=False),
    # ---- tool crashes (defect, not design) -------------------------------
    Signature("tool_segfault",
              re.compile(r"Segmentation fault|core dumped|Fatal: caught signal|terminate called after throwing", re.I),
              "any", Layer.TOOL_WORKAROUND,
              "the EDA tool crashed internally — most likely a tool defect",
              "isolate the triggering construct, try an alternate tool option/order, or pin a known-good version",
              0.75, tool_bug=True),
    Signature("openroad_assert",
              re.compile(r"\bAssertion .*failed|ORD-\d+ .*internal|\bstd::__throw", re.I),
              "any", Layer.TOOL_WORKAROUND,
              "OpenROAD internal assertion — usually a tool corner case",
              "try to sidestep with an option change; report upstream if reproducible",
              0.6, tool_bug=True),
    # ---- 1_synth : yosys / slang -----------------------------------------
    Signature("slang_parse_hang",
              re.compile(r"Executing SLANG frontend|read_slang", re.I),
              "1_synth", Layer.RTL,
              "front-end is spending huge time elaborating RTL (often an unrolled loop / massive memory init)",
              "reduce elaboration cost in RTL (multi-cycle reset, smaller unroll, $readmemh init) — not a tool knob",
              0.5),
    Signature("abc_hang",
              re.compile(r"Executing ABC|abc: |mapping.*aborted", re.I),
              "1_synth", Layer.SYNTH_OPT,
              "ABC tech-map is stuck (often a wide comb cone from a packed vector)",
              "adjust ABC script / enable flatten or map-splitting; sometimes an RTL restructure of the cone",
              0.45),
    Signature("synth_area_blowup",
              re.compile(r"design has \d{7,}|too many cells|netlist too large", re.I),
              "1_synth", Layer.RTL,
              "synthesised cell count is pathologically large — an RTL structure exploded",
              "fix the RTL that unrolled/duplicated logic; do NOT paper over with utilization",
              0.6),
    # ---- 2_floorplan ------------------------------------------------------
    Signature("macro_no_fit",
              re.compile(r"\bMPL-0006\b|macro.*(does not fit|cannot fit|larger than).*core", re.I),
              "2_floorplan", Layer.FLOORPLAN,
              "a macro (usually a pathological SRAM aspect ratio) can't be placed in the core",
              "reshape the macro (banks/column_mux) to ~square — deterministic; see physical_analyzer",
              0.9, deterministic=True),
    Signature("util_too_high",
              re.compile(r"utilization.*(exceed|too high|>|100%)|no space for cells|cannot place all", re.I),
              "2_floorplan", Layer.FLOORPLAN,
              "requested core utilization leaves no room for standard cells",
              "lower CORE_UTILIZATION *only if* the gate count is legitimate; otherwise fix the RTL that inflated area",
              0.55),
    Signature("pdn_ir_drop",
              re.compile(r"IR drop|PDN.*(error|fail)|power grid", re.I),
              "2_floorplan", Layer.FLOORPLAN,
              "power delivery network / IR-drop problem",
              "adjust PDN straps/pitch in the floorplan; not an RTL issue",
              0.6),
    # ---- 3_place ----------------------------------------------------------
    Signature("place_no_legal",
              re.compile(r"cannot legalize|no legal location|overlapping instances|detailed placement failed", re.I),
              "3_place", Layer.PLACE,
              "placer can't legalize — density or padding too tight, or macro halos block rows",
              "raise placement density target / cell padding, or open macro halos in floorplan",
              0.55),
    Signature("place_congestion",
              re.compile(r"congestion.*(high|estimate)|GRT-.*congest|global route.*overflow", re.I),
              "3_place", Layer.PLACE,
              "routing congestion predicted from placement",
              "lower placement density / spread macros; if persistent, add routing layers (ROUTE)",
              0.5),
    # ---- 4_cts ------------------------------------------------------------
    Signature("cts_skew",
              re.compile(r"CTS-.*(fail|error)|clock tree.*(fail|unbalanced)|skew.*exceed", re.I),
              "4_cts", Layer.CTS,
              "clock tree synthesis couldn't meet skew/latency",
              "adjust CTS buffer list / max skew / clustering; check the clock SDC",
              0.55),
    # ---- 5_route ----------------------------------------------------------
    Signature("route_congestion",
              re.compile(r"Routing congestion|DRT-.*(congest|fail)|cannot route|unrouted", re.I),
              "5_route", Layer.ROUTE,
              "detailed routing failed on congestion",
              "add/upgrade routing layers, or go back and lower placement density",
              0.55),
    Signature("antenna_drc",
              re.compile(r"antenna violation|DRC.*violation|max slew|min area", re.I),
              "5_route", Layer.ROUTE,
              "post-route DRC / antenna violations",
              "insert diodes / adjust layer rules; usually a route-stage fix",
              0.5),
    # ---- timing (can surface at several stages) --------------------------
    Signature("timing_not_met",
              re.compile(r"WNS\s*[:=]?\s*-|worst negative slack.*-|timing.*not met|setup violation", re.I),
              "6_final", Layer.SDC,
              "negative slack — setup timing not met",
              "decide: unrealistic SDC (relax/clean constraints) vs. needs SYNTH_OPT (retime/effort) vs. CTS; NOT an RTL bug unless the path is architecturally too long",
              0.4),
]


@dataclass
class Hypothesis:
    signature: str
    layer: str
    root_cause: str
    remedy_hint: str
    confidence: float
    tool_bug: bool
    reversible: bool
    deterministic: bool
    stage: str
    evidence: List[str] = field(default_factory=list)
    downstream: List[str] = field(default_factory=list)
    ripple_blocks: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = dict(self.__dict__)
        return d


@dataclass
class ReasoningResult:
    block: str
    failing_stage: Optional[str]
    hypotheses: List[Hypothesis]          # ranked, best first
    tool_bug_suspected: bool
    can_auto: bool                        # a deterministic analyzer can handle it
    needs_llm: bool
    briefing: str                         # the "staff engineer" prompt block for the LLM

    @property
    def top(self) -> Optional[Hypothesis]:
        return self.hypotheses[0] if self.hypotheses else None


# ---------------------------------------------------------------------------- #
# Core reasoning
# ---------------------------------------------------------------------------- #
def _infer_failing_stage(text: str, stage_hint: Optional[str]) -> Optional[str]:
    if stage_hint and stage_hint in STAGES:
        return stage_hint
    # crude stage sniff from the log
    order = [
        ("5_route", r"detailed_route|global_route|5_route|DRT-|GRT-"),
        ("4_cts", r"clock_tree|4_cts|CTS-"),
        ("3_place", r"detailed_place|global_place|3_place|GPL-|DPL-"),
        ("2_floorplan", r"floorplan|2_floorplan|MPL-|PDN|macro"),
        ("1_synth", r"yosys|SLANG|synth|ABC|1_synth"),
    ]
    for stg, pat in order:
        if re.search(pat, text, re.I):
            return stg
    return None


def reason(error_lines: List[str],
           tail: str,
           block: str,
           dep_graph: Dict[str, Set[str]],
           stage_hint: Optional[str] = None) -> ReasoningResult:
    """Classify a failure and produce ranked, blast-radius-annotated hypotheses.

    This is the deterministic core. It never calls an LLM; it decides WHETHER an
    LLM is needed and, if so, hands back a precisely framed `briefing`.
    """
    hay = list(error_lines) + (tail.splitlines() if tail else [])
    blob = "\n".join(hay)
    failing_stage = _infer_failing_stage(blob, stage_hint)

    matches: List[Hypothesis] = []
    for sig in _SIGNATURES:
        ev = [ln.strip()[:200] for ln in hay if sig.pattern.search(ln)]
        if not ev:
            continue
        h = Hypothesis(
            signature=sig.name, layer=sig.layer, root_cause=sig.root_cause,
            remedy_hint=sig.remedy_hint, confidence=sig.confidence,
            tool_bug=sig.tool_bug, reversible=sig.reversible,
            deterministic=sig.deterministic,
            stage=sig.stage if sig.stage != "any" else (failing_stage or "any"),
            evidence=ev[:3],
        )
        h.downstream = downstream_stages(h.layer, failing_stage)
        h.ripple_blocks = affected_blocks(block, h.layer, dep_graph)
        matches.append(h)

    # rank: deterministic first, then confidence, then smaller blast radius
    matches.sort(key=lambda h: (not h.deterministic, -h.confidence,
                                len(h.downstream) + len(h.ripple_blocks)))

    tool_bug = bool(matches and matches[0].tool_bug) or any(
        h.tool_bug and h.confidence >= 0.6 for h in matches)
    can_auto = bool(matches and matches[0].deterministic and matches[0].confidence >= 0.8)
    # We still want the LLM when nothing matched, or the top match is low
    # confidence, or it's a genuine tool bug needing a judgement call.
    needs_llm = (not can_auto) and (not matches or matches[0].confidence < 0.85)

    briefing = build_briefing(block, failing_stage, matches, tool_bug)
    return ReasoningResult(
        block=block, failing_stage=failing_stage, hypotheses=matches,
        tool_bug_suspected=tool_bug, can_auto=can_auto, needs_llm=needs_llm,
        briefing=briefing)


def build_briefing(block: str,
                   failing_stage: Optional[str],
                   hyps: List[Hypothesis],
                   tool_bug: bool) -> str:
    """Frame the failure for the LLM as a staff-engineer decision, not "fix this".

    The whole point: make the LLM (a) decide the remedy LAYER, (b) justify it
    against the cross-stage blast radius, and (c) prefer the smallest reversible
    change that fixes the ROOT cause rather than masking it.
    """
    lines = [
        "PRINCIPAL-ENGINEER FAILURE REVIEW (reason before you edit):",
        f"  block: {block}    failing stage: {failing_stage or 'unknown'}",
        f"  tool-bug suspected: {'YES — prefer a workaround/skip/version-pin over a design change' if tool_bug else 'no'}",
        "",
        "  Decide WHICH LAYER owns the root cause, then choose the SMALLEST",
        "  reversible change at that layer. Layers and what they cost downstream:",
    ]
    for layer in (Layer.RTL, Layer.SDC, Layer.SYNTH_OPT, Layer.FLOORPLAN,
                  Layer.PLACE, Layer.CTS, Layer.ROUTE, Layer.TOOL_WORKAROUND):
        ds = downstream_stages(layer, failing_stage)
        lines.append(f"    - {layer:<10} → re-runs {', '.join(ds)}")
    lines += [
        "",
        "  Deterministic classification (ranked; trust high-confidence ones):",
    ]
    if hyps:
        for h in hyps[:4]:
            rip = f"; ripples to blocks: {', '.join(h.ripple_blocks)}" if h.ripple_blocks else ""
            lines.append(
                f"    * [{h.confidence:.2f}] {h.signature}: likely {h.layer} — "
                f"{h.root_cause}. Remedy: {h.remedy_hint}. "
                f"Re-runs: {', '.join(h.downstream)}{rip}")
            if h.evidence:
                lines.append(f"        evidence: {h.evidence[0]}")
    else:
        lines.append("    * (no signature matched — this is a novel failure; reason from first principles)")
    lines += [
        "",
        "  RULES:",
        "   1. Do NOT lower utilization / relax SDC to hide a real RTL area or",
        "      timing explosion — fix the cause, not the symptom.",
        "   2. Prefer changes with the smallest blast radius that still fix root cause.",
        "   3. If a tool bug is suspected, propose a workaround/skip/pin, not a design edit.",
        "   4. State your chosen layer, the root cause, and the downstream re-run cost.",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------- #
# Tier-1 fusion: combine the single-log signature reasoning with the
# cross-stage report evidence, and apply the deterministic guardrail.
# ---------------------------------------------------------------------------- #
@dataclass
class FusedReview:
    """The full adaptive verdict handed to the agent's fail loop."""
    base: ReasoningResult                 # signature-based (log-only) reasoning
    evidence: object = None               # stage_timing_analyzer.StageEvidence
    root_cause: object = None             # stage_timing_analyzer.RootCause
    root_layer: str = Layer.UNKNOWN       # final agreed layer (evidence wins)
    can_auto: bool = False
    needs_llm: bool = True
    briefing: str = ""

    def fix_allowed(self, fix_layer: str) -> Tuple[bool, str]:
        """Guardrail entry point used by the fail loop before applying any fix."""
        if self.evidence is None:
            return True, "no cross-stage evidence available; allowing (log-only mode)."
        import stage_timing_analyzer as _sta
        return _sta.verdict_on_fix(self.evidence, fix_layer)


def reason_with_reports(error_lines: List[str],
                        tail: str,
                        block: str,
                        dep_graph: Dict[str, Set[str]],
                        report_dir: Optional[str] = None,
                        synth_stat_path: Optional[str] = None,
                        stage_hint: Optional[str] = None) -> FusedReview:
    """Adaptive, evidence-first review.

    1. Run the log-signature `reason()` (fast, always).
    2. If a report directory is available, build the cross-stage evidence pack
       and its deterministic root cause. The EVIDENCE-derived layer OVERRIDES the
       log-signature layer when they disagree, because the trajectory is a
       stronger signal than a single failing-stage excerpt (this is precisely
       what stops the "route" mis-classification the security run hit).
    3. Emit a combined briefing so, if the LLM is consulted, it sees the same
       evidence a human lead would — and the guardrail (fix_allowed) can veto a
       symptom fix.
    """
    base = reason(error_lines, tail, block, dep_graph, stage_hint)
    fused = FusedReview(base=base, root_layer=base.top.layer if base.top else Layer.UNKNOWN,
                        can_auto=base.can_auto, needs_llm=base.needs_llm,
                        briefing=base.briefing)
    if not report_dir:
        return fused

    try:
        import stage_timing_analyzer as sta
    except Exception:
        return fused

    ev = sta.analyze(report_dir, synth_stat_path=synth_stat_path)
    if not ev.trajectory and not ev.synth.top_module:
        return fused  # no usable reports; keep log-only verdict

    rc = sta.classify_root_cause(ev)
    fused.evidence, fused.root_cause = ev, rc

    # Evidence wins on the LAYER decision when it is confident.
    if rc.confidence >= 0.7 and rc.layer not in ("UNKNOWN",):
        disagree = (base.top is not None and base.top.layer != rc.layer)
        fused.root_layer = rc.layer
        # A confident, structural root cause (pre-place timing hole, area
        # explosion) is auto-actionable at the RTL/SDC layer and does NOT need
        # the LLM to re-derive it — but it is NOT auto-*applied* (RTL edits are
        # high blast-radius); the agent surfaces it for review.
        fused.can_auto = False
        fused.needs_llm = disagree or rc.layer in (Layer.RTL, Layer.SDC)
        note = ""
        if disagree:
            note = (f"\n  NOTE: log-only signature suggested "
                    f"{base.top.layer}, but the cross-stage EVIDENCE overrides it "
                    f"to {rc.layer} (trajectory beats a single failing-stage excerpt).")
        fused.briefing = (sta.evidence_briefing(ev, rc)
                          + "\n\n" + base.briefing + note)
    else:
        fused.briefing = sta.evidence_briefing(ev, rc) + "\n\n" + base.briefing
    return fused


if __name__ == "__main__":
    dep = {
        "security": set(), "noc": set(), "ampo": {"security"},
        "agent_domain": {"security", "noc"}, "user_domain": {"security", "noc"},
        "janus_soc_top": {"security", "noc", "ampo", "agent_domain", "user_domain"},
    }

    # 1) macro-fit → FLOORPLAN, deterministic, can_auto, no LLM needed
    r = reason(["[ERROR MPL-0006] Found macro u_ram that does not fit in the core"],
               "", "security", dep)
    assert r.top.layer == Layer.FLOORPLAN and r.can_auto and not r.needs_llm, r.top
    assert r.top.downstream[0] == "2_floorplan", r.top.downstream
    print("macro-fit:", r.top.signature, "auto=", r.can_auto)

    # 2) missing .so → ENV/tool bug
    r = reason(["kepler-formal: error while loading shared libraries: libnaja_snl_pyloader.so"],
               "", "noc", dep)
    assert r.tool_bug_suspected and r.top.layer == Layer.ENV, r.top
    print("env:", r.top.signature, "tool_bug=", r.tool_bug_suspected)

    # 3) RTL change to 'security' ripples to dependents
    r = reason(["design has 12345678 cells; netlist too large"], "", "security", dep)
    assert r.top.layer == Layer.RTL, r.top
    assert "ampo" in r.top.ripple_blocks and "janus_soc_top" in r.top.ripple_blocks, r.top.ripple_blocks
    print("rtl ripple:", r.top.ripple_blocks)

    # 4) timing → SDC, low confidence → needs LLM, briefing generated
    r = reason(["WNS = -0.145  worst negative slack -0.145"], "", "user_domain", dep)
    assert r.needs_llm and "PRINCIPAL-ENGINEER" in r.briefing, r.needs_llm
    print("timing:", r.top.layer, "needs_llm=", r.needs_llm)

    # 5) novel failure → no match, needs LLM, still briefs from first principles
    r = reason(["some totally unknown weirdness 0xdeadbeef"], "", "noc", dep)
    assert r.needs_llm and not r.hypotheses
    print("novel: needs_llm=", r.needs_llm)

    # 6) blast radius sanity
    assert downstream_stages(Layer.ROUTE) == ["5_route", "6_final"]
    assert downstream_stages(Layer.RTL)[0] == "1_synth"
    print("blast radius OK")
    print("reasoning_engine self-test OK")
