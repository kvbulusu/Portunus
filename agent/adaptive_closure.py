"""§125 adaptive closure engine — difficulty classifier + bounded action policy.

Pure Python, deterministic, no ORFS calls. It reads a qor-like dict and returns
a class, a confidence and an evidence list, then maps the class to a recommended
action. §126: wired into janus_agent.py via --adaptive {off,advise,auto}
(default advise = classify + write adaptive.json, never runs make). In auto the
AGENT runs the bounded variant rerun; this module stays pure (no ORFS calls):
from_qor() maps report_bundle's nested qor.json, class_knobs() returns the make
vars / latency overlay for a class, parse_spef_sta() reads the RCX-SPEF STA rpt.
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
    if _f(q.get("wns_ps")) is not None:  # true worst slack may sit in a non-class group
        wns_all.append(_f(q.get("wns_ps")))
    wns = min(wns_all) if wns_all else None
    drv = int(q.get("slew_viol") or 0) + int(q.get("cap_viol") or 0)
    ovf = _f(q.get("grt_overflow")) or 0
    util = _f(q.get("util"))
    skew = _f(q.get("skew_ps"))
    if wns is None:
        return {"class": "UNKNOWN", "confidence": 0.0, "evidence": ["no setup WNS in input"]}
    ev.append(f"worst setup WNS {wns:+.1f} ps" + (f" (period {per:.0f} ps)" if per else ""))
    if ovf > t["grt_overflow"]:
        return {"class": "CONGESTION_LIMITED", "confidence": 0.9, "evidence": ev + [f"GRT overflow {ovf:g}"]}
    # §126: DRV_LIMITED only when setup is met or within the PD-repairable band;
    # a DRV pass cannot recover a large WNS (ara fixture: -1948 ps with 479 DRV
    # was mis-binned DRV_LIMITED), so there DRV is kept as secondary evidence.
    near_met = wns >= 0 or (per is not None and abs(wns) <= t["moderate_wns_frac"] * per)
    if drv >= t["drv_viol"]:
        if near_met:
            return {"class": "DRV_LIMITED", "confidence": 0.8, "evidence": ev + [f"slew+cap violations {drv}"]}
        ev.append(f"secondary: slew+cap violations {drv}")
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


def _num(v, default=None):
    x = _f(v)
    return default if x is None else x


def promotion_verdict(base: dict, cand: dict) -> tuple[bool, str]:
    """§145: should pass *cand* REPLACE *base* as the block's result?

    Promote only if nothing signoff-relevant regresses AND at least one metric
    strictly improves. Regressions: more route DRC, more max-slew violations,
    setup or hold slack getting worse while (or by becoming) negative. Setup
    margin that shrinks but stays >= 0 is NOT a regression. Missing metrics on
    the candidate are a regression when the base had them (never guess)."""
    reg, imp = [], []
    for key in ("wns_ps", "hold_wns_ps"):
        b, c = _f(base.get(key)), _f(cand.get(key))
        if c is None:
            if b is not None:
                reg.append(f"{key} missing")
            continue
        if b is None:
            continue
        if c < b and c < 0:
            reg.append(f"{key} {b:+.2f}->{c:+.2f}")
        elif c > b and b < 0:
            imp.append(f"{key} {b:+.2f}->{c:+.2f}")
        elif c > b + 1.0:
            imp.append(f"{key} {b:+.2f}->{c:+.2f}")
    for key in ("drc", "slew_viol"):
        b, c = int(_num(base.get(key), 0)), int(_num(cand.get(key), 0))
        if c > b:
            reg.append(f"{key} {b}->{c}")
        elif c < b:
            imp.append(f"{key} {b}->{c}")
    b, c = _f(base.get("tns_ps")), _f(cand.get("tns_ps"))
    if b is not None and c is not None and b < 0 and c > b:
        imp.append(f"tns_ps {b:+.1f}->{c:+.1f}")
    if reg:
        return False, "regresses: " + ", ".join(reg)
    if not imp:
        return False, "no improvement over base"
    return True, "improves: " + ", ".join(imp)


def pick_promotion(passes: list[dict]) -> tuple[dict | None, str]:
    """passes[0] is the base. Return (best promotable pass, reason) or
    (None, why-not). Among promotable passes the qor_key order decides,
    fewer slew violations break ties."""
    if len(passes) < 2:
        return None, "no extra pass"
    base, ok, why = passes[0], [], []
    for p in passes[1:]:
        good, r = promotion_verdict(base, p)
        (ok if good else why).append((p, r))
    if not ok:
        return None, "; ".join(f"{p['name']}: {r}" for p, r in why)
    best = max(ok, key=lambda pr: (qor_key(pr[0]), -int(_num(pr[0].get("slew_viol"), 0))))
    return best[0], best[1]


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


def latency_restore(unc: dict[str, float], unset_latency: list[str] | None = None) -> str:
    """Undo latency_overlay BEFORE detail_place.tcl writes 3_place.sdc (which
    CTS loads), so the overlay is placement-only and the variant's CTS/route/
    final QoR is timed under the ORIGINAL constraints: setup uncertainty back
    to its pre-overlay value, overlay-added ideal latency unset."""
    out = ["# generated by adaptive_closure.latency_restore (§126) - source from POST_DETAIL_PLACE"]
    for clk in sorted(set(unc) | set(unset_latency or [])):
        if not re.fullmatch(r"[A-Za-z_][\w\[\]/.]*", clk):
            raise ValueError(f"bad clock name {clk!r}")
    for clk, v in sorted(unc.items()):
        out.append(f"if {{[llength [get_clocks -quiet {{{clk}}}]]}} {{ set_clock_uncertainty -setup {float(v):.3f} [get_clocks {{{clk}}}] }}")
    for clk in sorted(unset_latency or []):
        out.append(f"if {{[llength [get_clocks -quiet {{{clk}}}]]}} {{ unset_clock_latency [get_clocks {{{clk}}}] }}")
    return "\n".join(out) + "\n"


def sdc_sets_latency(sdc_text: str, clk: str) -> bool:
    """True if the SDC already has a (network) set_clock_latency on clk."""
    for line in sdc_text.splitlines():
        line = line.split("#", 1)[0]
        if "set_clock_latency" in line and "-source" not in line and re.search(
                r"(^|[\s{\[])" + re.escape(clk) + r"($|[\s}\]])", line):
            return True
    return False


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


def from_qor(qor: dict) -> dict:
    """Map report_bundle.build_qor()'s nested qor.json (janus.qor/1) to the flat
    classify() input. Missing data stays missing (never invented)."""
    t = qor.get("timing") or {}
    clk = qor.get("clock") or {}
    ct = qor.get("clock_tree") or {}
    out: dict = {}
    if _f(clk.get("period")):
        out["period_ps"] = _f(clk["period"])
    split = t.get("class_split") or {}
    groups = {}
    setup = dict((t.get("other_groups") or {}).get("setup") or {})
    setup.update((t.get("groups") or {}).get("setup") or {})
    for g, v in setup.items():
        w = _f(v)
        if w is None:
            continue
        rec = {"wns_ps": w}
        if (split.get(g) or {}).get("violators") is not None:
            rec["violators"] = int(split[g]["violators"])
        groups[g] = rec
    if groups:
        out["groups"] = groups
    ws = _f(t.get("setup_worst_slack"))
    if ws is None:
        ws = _f(t.get("setup_wns"))
    if ws is not None:
        out["wns_ps"] = ws
    for k_out, v in (("hold_wns_ps", t.get("hold_worst_slack")), ("tns_ps", t.get("setup_tns"))):
        if _f(v) is not None:
            out[k_out] = _f(v)
    drv = qor.get("drv") or {}
    out["slew_viol"] = int(drv.get("max_slew") or 0)
    out["cap_viol"] = int(drv.get("max_cap") or 0)
    grt = qor.get("grt_overflow")
    if isinstance(grt, dict) and grt.get("total") is not None:
        out["grt_overflow"] = grt["total"]
    elif _f(grt) is not None:
        out["grt_overflow"] = _f(grt)
    up = _f((qor.get("area") or {}).get("utilization_pct"))
    if up is not None:
        out["util"] = up / 100.0
    if _f(ct.get("setup_skew")) is not None:
        out["skew_ps"] = abs(_f(ct["setup_skew"]))
    drc = qor.get("route_drc") or {}
    if drc.get("total") is not None:
        out["drc"] = int(drc["total"])
    name = clk.get("name") or ct.get("clock")
    if name:
        out["clock_name"] = name
    for k_out, k_in in (("latency_ps", "source_latency"), ("uncertainty_ps", "clock_uncertainty")):
        if _f(ct.get(k_in)) is not None:
            out[k_out] = _f(ct[k_in])
    return out


def pass_summary(name: str, fq: dict) -> dict:
    """Row for pick_best() from a from_qor() dict."""
    return {"name": name, "wns_ps": fq.get("wns_ps"), "hold_wns_ps": fq.get("hold_wns_ps"),
            "tns_ps": fq.get("tns_ps"), "drc": fq.get("drc", 0),
            "slew_viol": fq.get("slew_viol", 0)}


def class_knobs(cls: str, fq: dict, place_density: float | None = None,
                sdc_text: str | None = None) -> dict:
    """ORFS make vars (+ optional Tcl overlay text) for one adaptive pass.
    Only real ORFS variables (flow/scripts/variables.yaml) and the existing
    PRE_/POST_<STEP>_TCL hook channel are used. Returns {"vars": [...],
    "overlay": str|None, "restore": str|None, "notes": [...]}; no vars and no
    overlay => nothing safe to try."""
    per, wns = _f(fq.get("period_ps")), _f(fq.get("wns_ps"))
    out = {"vars": [], "overlay": None, "restore": None, "notes": []}
    if cls == "MODERATE":
        cap = 0.05 * per if per else 100.0
        m = int(round(min(max(abs(wns or 0.0), 10.0), cap)))
        out["vars"].append(f"SETUP_SLACK_MARGIN={m}")
        out["notes"].append(f"repair_timing over-fix margin {m} ps")
    elif cls == "CLOCK_SKEW_LIMITED":
        clk, skew = fq.get("clock_name"), _f(fq.get("skew_ps")) or 0.0
        if not clk:
            out["notes"].append("no clock name in qor; overlay skipped")
            return out
        lat = _f(fq.get("latency_ps"))
        lat_d = {clk: lat} if lat and lat > 0 else {}
        if lat_d and (sdc_text is None or sdc_sets_latency(sdc_text, clk)):
            # never override an SDC-authored latency (or one we cannot see)
            out["notes"].append("ideal latency not overlaid: "
                                + ("SDC unavailable" if sdc_text is None else "SDC already sets it"))
            lat_d = {}
        base_unc = _f(fq.get("uncertainty_ps"))
        unc_d, note = {}, []
        if base_unc is not None and skew > 0:
            unc = base_unc + 0.5 * skew
            if per:
                unc = min(unc, 0.15 * per)
            if unc > base_unc:
                unc_d = {clk: unc}
                note.append(f"setup uncertainty {base_unc:.1f}->{unc:.1f} ps (placement only, restored before 3_place.sdc)")
        elif skew > 0:
            out["notes"].append("original clock uncertainty unknown; uncertainty bump skipped (cannot restore)")
        if lat_d:
            note.append(f"ideal latency {lat:.1f} ps")
        if not (lat_d or unc_d):
            out["notes"].append("no latency/uncertainty evidence; overlay skipped")
            return out
        out["overlay"] = latency_overlay(lat_d, unc_d)
        out["restore"] = latency_restore({clk: base_unc} if unc_d else {}, list(lat_d))
        out["notes"].append("pre-CTS overlay: " + ", ".join(note))
    elif cls == "CONGESTION_LIMITED":
        if place_density:
            d = max(0.20, round(place_density - 0.05, 2))
            out["vars"].append(f"PLACE_DENSITY={d}")
            out["notes"].append(f"PLACE_DENSITY {place_density} -> {d}")
        out["vars"].append("GPL_ROUTABILITY_DRIVEN=1")
    elif cls == "DRV_LIMITED":
        out["vars"] += ["SLEW_MARGIN=20", "CAP_MARGIN=20"]
        out["notes"].append("repair_design over-fix 20% slew/cap")
    return out


def parse_spef_sta(text: str) -> dict:
    """Lines 'janus_spef_sta <metric> <value>' from hooks/janus_spef_sta.tcl."""
    out = {}
    for m in re.finditer(r"^janus_spef_sta\s+(\w+)\s+(\S+)", text, re.M):
        v = _f(m.group(2))
        out[m.group(1)] = v if v is not None else m.group(2)
    return out


if __name__ == "__main__":
    import argparse, sys
    ap = argparse.ArgumentParser(description="classify a qor json (advise only)")
    ap.add_argument("qor"); ap.add_argument("--block", default="block")
    ap.add_argument("--adaptive", choices=["off", "advise", "auto"], default="advise")
    ap.add_argument("--thresholds", help="json file overriding DEFAULT_THRESHOLDS")
    a = ap.parse_args()
    th = json.load(open(a.thresholds)) if a.thresholds else None
    q = json.load(open(a.qor))
    if "timing" in q:  # nested report_bundle qor.json
        q = from_qor(q)
    json.dump(plan(a.block, classify(q, th), a.adaptive, th=th), sys.stdout, indent=2)
