import os, sys, subprocess, shutil, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import adaptive_closure as ac

P = 5000.0
CASES = {
    "EASY": {"period_ps": P, "groups": {"reg2reg": {"wns_ps": 20}}},
    "MODERATE": {"period_ps": P, "groups": {"reg2reg": {"wns_ps": -50, "violators": 5}}, "wire_share": 0.6},
    "CLOCK_SKEW_LIMITED": {"period_ps": P, "groups": {"reg2reg": {"wns_ps": -100, "violators": 9}}, "skew_ps": 600},
    "IO_BUDGET_LIMITED": {"period_ps": P, "groups": {"reg2reg": {"wns_ps": -5, "violators": 1}, "reg2out": {"wns_ps": -127, "violators": 9}}},
    "CONGESTION_LIMITED": {"period_ps": P, "groups": {"reg2reg": {"wns_ps": -30}}, "grt_overflow": 120},
    "LOGIC_DEPTH_LIMITED": {"period_ps": P, "groups": {"reg2reg": {"wns_ps": -900, "violators": 300}}, "wire_share": 0.15},
    "DRV_LIMITED": {"period_ps": P, "groups": {"reg2reg": {"wns_ps": 10}}, "slew_viol": 40},
}

@pytest.mark.parametrize("cls", list(CASES))
def test_every_class(cls):
    r = ac.classify(CASES[cls])
    assert r["class"] == cls and r["evidence"] and 0 < r["confidence"] <= 1

def test_noc_real_numbers():  # earlier noc: setup r2r +274, r2o -127
    q = {"period_ps": 1666.67, "groups": {"reg2reg": {"wns_ps": 274}, "reg2out": {"wns_ps": -127}}}
    assert ac.classify(q)["class"] == "IO_BUDGET_LIMITED"

def test_thresholds_override():
    assert ac.classify(CASES["MODERATE"], {"moderate_wns_frac": 0.001})["class"] == "LOGIC_DEPTH_LIMITED"

def test_unknown():
    assert ac.classify({})["class"] == "UNKNOWN"
    assert ac.plan("b", {"class": "UNKNOWN"}, "auto")["action"] == "needs_manual"

def test_advise_never_reruns():
    for c in CASES:
        r = ac.plan("noc", ac.classify(CASES[c]), "advise")
        assert r["rerun"] is False and r["clean"] is False

def test_auto_budget_variant_protected():
    cr = ac.classify(CASES["CLOCK_SKEW_LIMITED"])
    r = ac.plan("noc", cr, "auto")
    assert r["rerun"] and r["flow_variant"] == "adapt1_clock_skew_limited" and r["resume_from"] == "3_place" and not r["clean"]
    assert not ac.plan("noc", cr, "auto", passes_done=1)["rerun"]
    assert not ac.plan("user_core_ara_lane", cr, "auto")["rerun"]
    for c in ("IO_BUDGET_LIMITED", "LOGIC_DEPTH_LIMITED", "EASY"):
        assert not ac.plan("noc", ac.classify(CASES[c]), "auto")["rerun"]

def test_off():
    assert ac.plan("noc", ac.classify(CASES["MODERATE"]), "off")["action"] == "off"

def test_pick_best():
    ps = [{"name": "p1", "wns_ps": -5, "drc": 0}, {"name": "p2", "wns_ps": 3, "drc": 2}, {"name": "p3", "wns_ps": 1, "drc": 0}]
    assert ac.pick_best(ps)["name"] == "p3"

def test_overlay_tcl_syntax(tmp_path):
    s = ac.latency_overlay({"core_clock": 412.5, "user_clk": 300}, {"core_clock": 60})
    assert "set_clock_latency 412.500" in s
    with pytest.raises(ValueError):
        ac.latency_overlay({"x; exec": 1})
    if shutil.which("tclsh"):
        f = tmp_path / "o.tcl"; f.write_text(s)
        out = subprocess.run(["tclsh"], input=f"set f [open {f}]; puts [info complete [read $f]]", capture_output=True, text=True).stdout
        assert out.strip() == "1"

RPT = "/home/ubuntu/Uploads/6_finish (1).rpt"
@pytest.mark.skipif(not os.path.exists(RPT), reason="box report not present")
def test_real_finish_rpt():
    q = ac.parse_finish_rpt(open(RPT, errors="ignore").read())
    q["period_ps"] = 2600.0
    r = ac.classify(q)
    print(q, r)
    assert r["class"] in ac.CLASSES


# ---- §126: report_bundle qor.json -> classifier, knobs, SPEF STA parse ----
FIX = os.path.join(os.path.dirname(__file__), "fixtures")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "utils"))


def _fq(run):
    import report_bundle as rb
    return ac.from_qor(rb.build_qor("user_core_ara", "DONE", None, os.path.join(FIX, run), None))


def test_from_qor_real_ara():
    fq = _fq("ara_finish_run")
    assert fq["wns_ps"] == -1948.44 and fq["hold_wns_ps"] == 8.95 and fq["tns_ps"] == -85378.1
    assert fq["groups"]["reg2reg"]["wns_ps"] == -1948.44 and fq["groups"]["reg2out"]["wns_ps"] == 5.24
    assert (fq["slew_viol"], fq["cap_viol"]) == (471, 8)
    assert fq["skew_ps"] == 1074.05 and fq["latency_ps"] == 1487.02 and fq["clock_name"] == "core_clk"


def test_ara_not_drv_limited():
    # -1948 ps WNS with 479 DRV used to classify DRV_LIMITED; DRV is secondary.
    r = ac.classify(_fq("ara_finish_run"))
    assert r["class"] == "CLOCK_SKEW_LIMITED"
    assert any("secondary" in e for e in r["evidence"])


def test_drv_limited_only_near_met():
    q = {"period_ps": P, "groups": {"reg2reg": {"wns_ps": -3000}}, "slew_viol": 400}
    assert ac.classify(q)["class"] != "DRV_LIMITED"


def test_from_qor_missing_never_invented():
    fq = ac.from_qor({})
    assert "wns_ps" not in fq and "period_ps" not in fq and "skew_ps" not in fq
    assert ac.classify(fq)["class"] == "UNKNOWN"


def test_class_knobs():
    fq = _fq("ara_finish_run")
    sdc = "create_clock -name core_clk -period 2857 [get_ports clk]\n"
    k = ac.class_knobs("CLOCK_SKEW_LIMITED", fq, sdc_text=sdc)
    assert k["vars"] == [] and "set_clock_latency 1487.020" in k["overlay"]
    assert "set_clock_uncertainty -setup 428.571" in k["overlay"]  # capped at 15% period
    assert "set_clock_uncertainty -setup 80.000" in k["restore"]    # original value
    assert "unset_clock_latency [get_clocks {core_clk}]" in k["restore"]
    # SDC already sets latency (ORFS gcd does) or SDC unknown -> latency untouched
    for txt in (sdc + "set_clock_latency 36.2250 [get_clocks {core_clk}]\n", None):
        k1 = ac.class_knobs("CLOCK_SKEW_LIMITED", fq, sdc_text=txt)
        assert "set_clock_latency" not in k1["overlay"] and "unset_clock_latency" not in k1["restore"]
        assert "set_clock_uncertainty -setup 428.571" in k1["overlay"]
    assert ac.class_knobs("CLOCK_SKEW_LIMITED", {"skew_ps": 500})["overlay"] is None
    # unknown original uncertainty -> never bump what we cannot restore
    k2 = ac.class_knobs("CLOCK_SKEW_LIMITED", {"clock_name": "c", "skew_ps": 500, "latency_ps": 300}, sdc_text="")
    assert "set_clock_uncertainty" not in k2["overlay"] and "unset_clock_latency" in k2["restore"]
    assert ac.class_knobs("CLOCK_SKEW_LIMITED", {"clock_name": "c", "skew_ps": 500})["overlay"] is None
    assert ac.class_knobs("MODERATE", {"period_ps": 1000, "wns_ps": -500})["vars"] == ["SETUP_SLACK_MARGIN=50"]
    c = ac.class_knobs("CONGESTION_LIMITED", {}, 0.6)["vars"]
    assert "PLACE_DENSITY=0.55" in c and "GPL_ROUTABILITY_DRIVEN=1" in c
    assert ac.class_knobs("DRV_LIMITED", {})["vars"] == ["SLEW_MARGIN=20", "CAP_MARGIN=20"]
    for cls in ("EASY", "IO_BUDGET_LIMITED", "LOGIC_DEPTH_LIMITED", "UNKNOWN"):
        k = ac.class_knobs(cls, fq)
        assert k["vars"] == [] and k["overlay"] is None


def test_sdc_sets_latency():
    assert ac.sdc_sets_latency("set_clock_latency 36.2 [get_clocks {core_clock}]", "core_clock")
    assert not ac.sdc_sets_latency("set_clock_latency 36.2 [get_clocks {vclk_core_clock}]", "core_clock")
    assert not ac.sdc_sets_latency("set_clock_latency -source 5 [get_clocks core_clock]", "core_clock")
    assert not ac.sdc_sets_latency("# set_clock_latency 1 core_clock", "core_clock")


def test_pass_summary_pick_best():
    base = ac.pass_summary("base", _fq("ara_finish_run"))
    better = dict(base, name="adapt1", wns_ps=-900.0)
    assert ac.pick_best([base, better])["name"] == "adapt1"


def test_parse_spef_sta():
    t = "noise\njanus_spef_sta status ok\njanus_spef_sta worst_slack_max -12.5\njanus_spef_sta max_slew_viol 3\n"
    assert ac.parse_spef_sta(t) == {"status": "ok", "worst_slack_max": -12.5, "max_slew_viol": 3.0}
    assert ac.parse_spef_sta("") == {}


def test_hook_tcl_complete():
    hooks = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "hooks")
    if not shutil.which("tclsh"):
        pytest.skip("no tclsh")
    for h in ("janus_adaptive_pre.tcl", "janus_adaptive_post.tcl", "janus_spef_sta.tcl"):
        f = os.path.join(hooks, h)
        out = subprocess.run(["tclsh"], input=f"set f [open {{{f}}}]; puts [info complete [read $f]]",
                             capture_output=True, text=True).stdout
        assert out.strip() == "1", h
