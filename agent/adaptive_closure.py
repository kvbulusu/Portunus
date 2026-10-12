"""§125 adaptive closure engine — difficulty classifier + bounded action policy.

Pure Python, deterministic, no ORFS calls. It reads a qor-like dict and returns
a class, a confidence and an evidence list, then maps the class to a recommended
action. NOT yet wired into janus_agent.py (see PR body): default behaviour of
the agent is unchanged. Mode "auto" only produces a plan; nothing here runs make.
"""
from __future__ import annotations
import json, re

DEFAULT_THRESHOLDS = {
    "moderate_wns_frac": 0.05,      # |WNS| <= 5% of period -> PD-repairable
    "skew_frac_period": 0.10,       # skew >= 10% of period
    "skew_frac_wns": 0.50,          # or skew >= 50% of |WNS|
    "io_share": 0.70,               # >=70% of violators in in2reg/reg2out/in2out
    "grt_overflow": 0,              # any GRT overflow
    "util_hot": 0.75,               # utilisation >= 75%
    "logic_depth_wns_frac": 0.10,   # reg2reg |WNS| > 10% of period ...
    "logic_wire_share_max": 0.30,   # ... with wire delay share <= 30%
    "drv_viol": 10,                 # slew+cap violations
    "max_extra_passes": 1,
}
CLASSES = ("EASY", "MODERATE", "CLOCK_SKEW_LIMITED", "IO_BUDGET_LIMITED",
           "CONGESTION_LIMITED", "LOGIC_DEPTH_LIMITED", "DRV_LIMITED")
IO_GROUPS = ("in2reg", "reg2out", "in2out")
PROTECTED_BLOCKS = {"user_core_ara_lane"}

ACTIONS = {
    "EASY": ("none", "positive margin; no extra pass"),
    "MODERATE": ("repair_pass", "rerun from 3_place in new FLOW_VARIANT with SETUP_SLACK_MARGIN raised"),
    "CLOCK_SKEW_LIMITED": ("cts_latency_feedback", "pass-1 CTS latency -> set_clock_latency overlay sourced by a PRE hook; rerun from 3_place in new FLOW_VARIANT"),
    "IO_BUDGET_LIMITED": ("report_sdc_budget", "IO budget issue: fix SDC input/output delays; no PD rerun"),
    "CONGESTION_LIMITED": ("congestion_pass", "lower PLACE_DENSITY / raise CELL_PAD_IN_SITES_* / GRT layer adjust; rerun from 3_place in new FLOW_VARIANT"),
    "LOGIC_DEPTH_LIMITED": ("needs_rtl", "stop PD iterations; report top cones; NEEDS_RTL"),
    "DRV_LIMITED": ("drv_pass", "repair_design with tightened max slew/cap or --post-triage-slew"),
}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def classify(q: dict, th: dict | None = None) -> dict:
    """q keys (all optional): period_ps, groups{name:{wns_ps,tns_ps,violators}},
    wire_share, grt_overflow, util, slew_viol, cap_viol, skew_ps, hold_wns_ps."""
    t = dict(DEFAULT_THRESHOLDS, **(th or {}))
    ev, per = [], _f(q.get("period_ps"))
    groups = q.get("groups") or {}
    wns_all = [_f(g.get("wns_ps")) for g in groups.values() if _f(g.get("wns_ps")) is not None]
    wns = min(wns_all) if wns_all else _f(q.get("wns_ps"))
    drv = int(q.get("slew_viol") or 0) + int(q.get("cap_viol") or 0)
    ovf = _f(q.get("grt_overflow")) or 0
    util = _f(q.get("util"))
    skew = _f(q.get("skew_ps"))
    if wns is None:
        return {"class": "UNKNOWN", "confidence": 0.0, "evidence": ["no setup WNS in input"]}
    ev.append(f"worst setup WNS {wns:+.1f} ps" + (f" (period {per:.0f} ps)" if per else ""))
    if ovf > t["grt_overflow"]:
        return {"class": "CONGESTION_LIMITED", "confidence": 0.9, "evidence": ev + [f"GRT overflow {ovf:g}"]}
    if drv >= t["drv_viol"]:
        return {"class": "DRV_LIMITED", "confidence": 0.8, "evidence": ev + [f"slew+cap violations {drv}"]}
    if wns >= 0:
        return {"class": "EASY", "confidence": 0.95, "evidence": ev}
    viol = {k: int(g.get("violators") or 0) for k, g in groups.items()}
    nv = sum(viol.values())
    io = sum(v for k, v in viol.items() if k in IO_GROUPS)
    if nv and io / nv >= t["io_share"]:
        return {"class": "IO_BUDGET_LIMITED", "confidence": 0.85,
                "evidence": ev + [f"{io}/{nv} violators in IO groups"]}
    if nv == 0 and groups:  # no counts: fall back to which group owns the worst WNS
        worst = min(groups, key=lambda k: _f(groups[k].get("wns_ps")) or 0)
        if worst in IO_GROUPS:
            return {"class": "IO_BUDGET_LIMITED", "confidence": 0.6,
                    "evidence": ev + [f"worst group is {worst}; reg2reg not worst"]}
    if skew is not None and ((per and skew >= t["skew_frac_period"] * per) or skew >= t["skew_frac_wns"] * abs(wns)):
        return {"class": "CLOCK_SKEW_LIMITED", "confidence": 0.75, "evidence": ev + [f"skew {skew:.1f} ps"]}
    if util is not None and util >= t["util_hot"]:
        return {"class": "CONGESTION_LIMITED", "confidence": 0.6, "evidence": ev + [f"utilisation {util:.2f}"]}
    r2r = _f((groups.get("reg2reg") or {}).get("wns_ps"))
    ws = _f(q.get("wire_share"))
    if per and r2r is not None and r2r < -t["logic_depth_wns_frac"] * per and (ws is None or ws <= t["logic_wire_share_max"]):
        return {"class": "LOGIC_DEPTH_LIMITED", "confidence": 0.8 if ws is not None else 0.6,
                "evidence": ev + [f"reg2reg {r2r:+.1f} ps", f"wire share {ws}"]}
    if per is None or abs(wns) <= t["moderate_wns_frac"] * per:
        return {"class": "MODERATE", "confidence": 0.7, "evidence": ev}
    return {"class": "LOGIC_DEPTH_LIMITED", "confidence": 0.5, "evidence": ev + ["large negative, no wire/skew/IO explanation"]}


def variant_name(block: str, cls: str, n: int) -> str:
    return f"adapt{n}_{cls.lower()}"


def plan(block: str, cls_result: dict, mode: str = "advise", passes_done: int = 0,
         th: dict | None = None) -> dict:
    """Return the action record written to qor.json['adaptive']. Never cleans."""
    if mode not in ("off", "advise", "auto"):
        raise ValueError(mode)
    t = dict(DEFAULT_THRESHOLDS, **(th or {}))
    cls = cls_result.get("class", "UNKNOWN")
    act, why = ACTIONS.get(cls, ("needs_manual", "unclassified; manual diagnosis"))
    rec = {"mode": mode, "class": cls, "confidence": cls_result.get("confidence"),
           "evidence": cls_result.get("evidence", []), "action": act, "reason": why,
           "rerun": False, "flow_variant": None, "clean": False}
    if mode == "off":
        rec["action"] = "off"
        return rec
    reruns = act in ("repair_pass", "cts_latency_feedback", "congestion_pass", "drv_pass")
    if mode == "auto" and reruns:
        if block in PROTECTED_BLOCKS:
            rec["blocked"] = f"{block} is protected; no rerun"
        elif passes_done >= t["max_extra_passes"]:
            rec["blocked"] = f"pass budget {t['max_extra_passes']} exhausted"
        else:
            rec["rerun"] = True
            rec["flow_variant"] = variant_name(block, cls, passes_done + 1)
            rec["resume_from"] = "3_place"
    return rec


def qor_key(p: dict):
    """Best-pass ordering: DRC-clean, then setup WNS, hold WNS, setup TNS (higher is better)."""
    return (int(p.get("drc", 0) or 0) == 0, _f(p.get("wns_ps")) or -1e9,
            _f(p.get("hold_wns_ps")) or -1e9, _f(p.get("tns_ps")) or -1e9)


def pick_best(passes: list[dict]) -> dict:
    return max(passes, key=qor_key)


def latency_overlay(lat: dict[str, float], unc: dict[str, float] | None = None) -> str:
    """Tcl overlay for 2-pass CTS feedback: ideal latency = measured pass-1 insertion."""
    out = ["# generated by adaptive_closure.latency_overlay (§125) - source from a PRE hook"]
    for clk, v in sorted(lat.items()):
        if not re.fullmatch(r"[A-Za-z_][\w\[\]/.]*", clk):
            raise ValueError(f"bad clock name {clk!r}")
        out.append(f"if {{[llength [get_clocks -quiet {{{clk}}}]]}} {{ set_clock_latency {float(v):.3f} [get_clocks {{{clk}}}] }}")
    for clk, v in sorted((unc or {}).items()):
        out.append(f"if {{[llength [get_clocks -quiet {{{clk}}}]]}} {{ set_clock_uncertainty -setup {float(v):.3f} [get_clocks {{{clk}}}] }}")
    return "\n".join(out) + "\n"


def parse_finish_rpt(text: str) -> dict:
    """Minimal extraction from an ORFS 6_finish.rpt: worst setup/hold slack per group."""
    q = {"groups": {}}
    for m in re.finditer(r"max_delay/setup group (\w+)[\s\S]*?(-?\d+\.\d+)\s+slack", text):
        g, s = m.group(1), float(m.group(2))
        cur = q["groups"].setdefault(g, {"wns_ps": s})
        cur["wns_ps"] = min(cur["wns_ps"], s)
    # ORFS report_worst_slack / report_tns summary lines ("worst slack max -1948.44")
    m = re.search(r"worst slack max\s+(-?\d+(?:\.\d+)?)", text)
    if m:
        q["wns_ps"] = float(m.group(1))
    m = re.search(r"worst slack min\s+(-?\d+(?:\.\d+)?)", text)
    if m:
        q["hold_wns_ps"] = float(m.group(1))
    m = re.search(r"tns max\s+(-?\d+(?:\.\d+)?)", text)
    if m:
        q["tns_ps"] = float(m.group(1))
    return q


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(description="classify a qor json (advise only)")
    ap.add_argument("qor"); ap.add_argument("--block", default="block")
    ap.add_argument("--adaptive", choices=["off", "advise", "auto"], default="advise")
    ap.add_argument("--thresholds", help="json file overriding DEFAULT_THRESHOLDS")
    a = ap.parse_args()
    th = json.load(open(a.thresholds)) if a.thresholds else None
    q = json.load(open(a.qor))
    json.dump(plan(a.block, classify(q, th), a.adaptive, th=th), sys.stdout, indent=2)
