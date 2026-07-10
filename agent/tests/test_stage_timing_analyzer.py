#!/usr/bin/env python3
"""
Validation for the Tier-1 cross-stage investigator + guardrail, on THREE
scenarios a human lead would debug differently:

  (a) security_run     — REAL uploaded reports. Timing broken pre-placement
                         (WNS -590 ps @ floorplan), one module = 92% area,
                         flop-array + reset worst paths. Expected: root cause
                         RTL; a PLACE/ROUTE density fix is REJECTED (this is the
                         exact mistake the two reverted commits made).
  (b) route_congestion — timing clean through CTS, degrades only at 5_route with
                         spread DRCs. Expected: root cause ROUTE; density fix
                         ALLOWED.
  (c) cts_skew         — timing clean through place, degrades at 4_cts.
                         Expected: root cause CTS.

Plus the Tier-2 probe (orfs_probe) exercised with an injected fake runner (no
openroad in CI/sandbox) and the reasoning_engine fusion override.

Run: python3 tests/test_stage_timing_analyzer.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
sys.path.insert(0, AGENT)

import stage_timing_analyzer as sta          # noqa: E402
import orfs_probe as probe                    # noqa: E402
import reasoning_engine as re_eng             # noqa: E402

FIX = os.path.join(HERE, "fixtures")
checks = 0


def ok(cond, msg):
    global checks
    assert cond, "FAIL: " + msg
    checks += 1
    print("  ok:", msg)


# ---------------------------------------------------------------------------- #
def test_scenario_a_security_is_rtl_not_route():
    print("[a] security — pre-place RTL explosion, reject density fix")
    ev = sta.analyze(os.path.join(FIX, "security_run"))
    ok(ev.first_violation_stage == "2_floorplan",
       f"first violation at floorplan (got {ev.first_violation_stage})")
    ok(ev.timing_violated_pre_place, "flagged pre-placement violation")
    ok(ev.synth.dominant_module == "kv_cache_datapath" and ev.synth.dominant_pct >= 90,
       f"kv_cache_datapath dominates area ({ev.synth.dominant_pct:.0f}%)")
    ok(ev.flop_array_in_worst_path, "flop array detected in worst path")
    ok(ev.reset_high_fanout_suspect, "reset high-fanout cone detected")
    ok(ev.flop_or_clock_power_dominated, "flop/clock power dominated")
    rc = sta.classify_root_cause(ev)
    ok(rc.layer == "RTL" and rc.confidence >= 0.85,
       f"root cause RTL ({rc.confidence:.2f})")
    # the guardrail must reject EXACTLY what the two reverted commits did
    for bad in ("PLACE", "ROUTE"):
        accept, why = sta.verdict_on_fix(ev, bad)
        ok(not accept, f"{bad}/density fix rejected — {why.split(':')[0]}")
    for good in ("RTL", "SDC", "FLOORPLAN"):
        accept, _ = sta.verdict_on_fix(ev, good)
        ok(accept, f"{good} fix allowed")


def test_scenario_b_route_congestion_allows_density():
    print("[b] route congestion — clean until route, density fix allowed")
    ev = sta.analyze(os.path.join(FIX, "route_congestion_run"))
    ok(ev.first_violation_stage == "5_route", "first violation at route")
    ok(not ev.timing_violated_pre_place, "NOT a pre-placement violation")
    rc = sta.classify_root_cause(ev)
    ok(rc.layer == "ROUTE", f"root cause ROUTE (got {rc.layer})")
    ok(sta.verdict_on_fix(ev, "ROUTE")[0], "ROUTE fix allowed")
    ok(sta.verdict_on_fix(ev, "PLACE")[0], "PLACE fix allowed (legitimate here)")


def test_scenario_c_cts():
    print("[c] cts skew — clean until CTS")
    ev = sta.analyze(os.path.join(FIX, "cts_skew_run"))
    ok(ev.first_violation_stage == "4_cts", "first violation at CTS")
    rc = sta.classify_root_cause(ev)
    ok(rc.layer == "CTS", f"root cause CTS (got {rc.layer})")
    ok(sta.verdict_on_fix(ev, "CTS")[0], "CTS fix allowed")


def test_fusion_override():
    print("[d] reasoning_engine fusion overrides log-only ROUTE -> RTL")
    fr = re_eng.reason_with_reports(
        ["[ERROR DRT-0247] detailed routing failed"], "iteration 0 aborted",
        "security", {"security": set()},
        report_dir=os.path.join(FIX, "security_run"),
        synth_stat_path=os.path.join(FIX, "security_run", "synth_stat.txt"),
        stage_hint="5_route")
    ok(fr.base.top.layer == "ROUTE", "log-only signature said ROUTE")
    ok(fr.root_layer == "RTL", "fused evidence overrode to RTL")
    ok(not fr.fix_allowed("PLACE")[0], "guardrail vetoes PLACE via fused review")
    ok("FIRST VIOLATION" in fr.briefing, "briefing carries the trajectory")


def test_tier2_probe_mocked():
    print("[e] orfs_probe with injected fake runner (no openroad needed)")
    # fabricate a fake openroad that prints the sentinels the parser expects
    fake_odb = os.path.join(FIX, "security_run", "synth_stat.txt")  # any real file

    def fake_runner_place(argv, timeout):
        return 0, ("PROBE_AREA 119377.3\n"
                   "wns -1166.54\n tns -49438544.0\n"
                   "PROBE_BEGIN_CHECKS\n Startpoint: rst_n\n PROBE_END_CHECKS\n"), ""

    def fake_runner_fp(argv, timeout):
        return 0, "PROBE_AREA 118000.0\nwns -590.42\ntns -27352296.0\n", ""

    r_fp = probe.probe_stage_odb(fake_odb, runner=fake_runner_fp)
    r_pl = probe.probe_stage_odb(fake_odb, runner=fake_runner_place)
    ok(r_fp.ok and r_fp.wns == -590.42, "probe parsed floorplan WNS")
    ok(r_pl.ok and r_pl.wns == -1166.54, "probe parsed place WNS")
    loc = probe.compare_stages(r_fp, "2_floorplan", r_pl, "3_place")
    ok(loc.delta_wns < 0 and "regression ENTERS" in loc.verdict,
       "stage compare attributes the drop to floorplan->place")

    # graceful degradation when openroad is missing (rc 127)
    r_missing = probe.probe_stage_odb(fake_odb, runner=lambda a, t: (127, "", "not found"))
    ok(not r_missing.ok and "openroad not available" in r_missing.error,
       "missing openroad degrades to ok=False (no crash)")

    # timeout guard
    r_to = probe.probe_stage_odb(fake_odb, runner=lambda a, t: (124, "", "timeout"))
    ok(not r_to.ok and "timed out" in r_to.error, "timeout degrades to ok=False")


if __name__ == "__main__":
    test_scenario_a_security_is_rtl_not_route()
    test_scenario_b_route_congestion_allows_density()
    test_scenario_c_cts()
    test_fusion_override()
    test_tier2_probe_mocked()
    print(f"\nALL {checks} CHECKS PASSED")
