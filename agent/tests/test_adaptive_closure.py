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
