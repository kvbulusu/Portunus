"""§126 agent-side adaptive closure: variant seeding never touches the base
leaf, make line re-pointing + overlay/restore hook chaining, advise path.
Run: python3 -m pytest asic_flow/agent/tests/test_agent_adaptive.py -q"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "utils"))
import janus_agent as ja  # noqa: E402
import adaptive_closure as ac  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


class _UI:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _agent(tmp_path, *argv, base_cmd=None):
    a = ja.JANUSAutonomousAgent.__new__(ja.JANUSAutonomousAgent)
    a.args = ja.build_parser().parse_args(list(argv))
    ja._apply_skips(a.args)
    a.flow_dir = str(tmp_path / "flow")
    a.ui = _UI()
    cmd = base_cmd or ["make", "-C", a.flow_dir, "DESIGN_CONFIG=x/config.mk",
                       "FLOW_VARIANT=base", "NUM_CORES=8",
                       "POST_DETAIL_PLACE_TCL=/h/post_detail_place.tcl",
                       "-o", "results/asap7/b/base/1_synth.v"]
    a._make_cmd = types.MethodType(lambda self, blk: list(cmd), a)
    return a


def _tree(flow):
    files = {"results": ["1_synth.v", "1_synth.sdc", "2_floorplan.odb", "2_floorplan.sdc",
                         "clock_period.txt", "3_place.odb", "6_final.odb"],
             "logs": ["1_2_yosys.log", "2_1_floorplan.log", "3_3_place_gp.log"],
             "reports": ["synth_stat.txt", "2_floorplan_final.rpt", "6_finish.rpt", "qor.json"],
             "objects": ["lib/merged.lib", "klayout.lyt"]}
    for sub, names in files.items():
        for n in names:
            p = flow / sub / "asap7" / "b" / "base" / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(n)
    return str(flow / "results" / "asap7" / "b" / "base")


def _snapshot(root):
    out = {}
    for r, _d, fs in os.walk(root):
        for f in fs:
            p = os.path.join(r, f)
            out[p] = (os.path.getmtime(p), open(p).read())
    return out


def test_seed_copies_le2_only_and_base_untouched(tmp_path):
    a = _agent(tmp_path)
    base = _tree(tmp_path / "flow")
    before = {s: _snapshot(str(tmp_path / "flow" / s / "asap7" / "b" / "base"))
              for s in ("results", "logs", "reports", "objects")}
    seeded = a._adaptive_seed(base, "adapt1_clock_skew_limited")
    v = tmp_path / "flow" / "results" / "asap7" / "b" / "adapt1_clock_skew_limited"
    assert sorted(os.listdir(v)) == ["1_synth.sdc", "1_synth.v", "2_floorplan.odb",
                                    "2_floorplan.sdc", "clock_period.txt"]
    vr = tmp_path / "flow" / "reports" / "asap7" / "b" / "adapt1_clock_skew_limited"
    assert sorted(os.listdir(vr)) == ["2_floorplan_final.rpt", "synth_stat.txt"]
    assert "objects/asap7/b/adapt1_clock_skew_limited/lib/merged.lib" in seeded
    assert not any(s.startswith("reports/") for s in seeded)
    assert all(not os.path.isabs(s) for s in seeded)
    for s, snap in before.items():   # base leaf byte- and mtime-identical
        assert _snapshot(str(tmp_path / "flow" / s / "asap7" / "b" / "base")) == snap
    # re-seed cleans ONLY the variant leaf
    (v / "3_place.odb").write_text("stale")
    a._adaptive_seed(base, "adapt1_clock_skew_limited")
    assert not (v / "3_place.odb").exists()


@pytest.mark.parametrize("bad", ["base", "../base", "adapt1", "x/adapt1_moderate", ""])
def test_seed_rejects_non_adaptive_leaf(tmp_path, bad):
    a = _agent(tmp_path)
    base = _tree(tmp_path / "flow")
    with pytest.raises(ValueError):
        a._adaptive_seed(base, bad)


def test_variant_cmd_repoints_and_chains_hooks(tmp_path):
    a = _agent(tmp_path)
    knobs = {"vars": ["SETUP_SLACK_MARGIN=50"]}
    cmd = a._adaptive_variant_cmd("b", "adapt1_moderate", knobs,
                                  ["results/asap7/b/adapt1_moderate/2_floorplan.odb"], None)
    assert "FLOW_VARIANT=base" not in cmd and "FLOW_VARIANT=adapt1_moderate" in cmd
    assert "results/asap7/b/base/1_synth.v" not in cmd           # base -o dropped
    assert cmd[-2:] == ["-o", "results/asap7/b/adapt1_moderate/2_floorplan.odb"]
    assert "SETUP_SLACK_MARGIN=50" in cmd and "NUM_CORES=8" in cmd
    assert "POST_DETAIL_PLACE_TCL=/h/post_detail_place.tcl" in cmd  # untouched w/o overlay
    assert not any(t.startswith("PRE_") for t in cmd)

    cmd = a._adaptive_variant_cmd("b", "adapt1_clock_skew_limited", {"vars": []}, [],
                                  "/v/ov.tcl", "/v/rs.tcl")
    pre = [t for t in cmd if t.startswith("PRE_")]
    assert len(pre) == 3 and all(t.endswith("janus_adaptive_pre.tcl") for t in pre)
    posts = [t for t in cmd if t.startswith("POST_DETAIL_PLACE_TCL=")]
    assert len(posts) == 1 and posts[0].endswith("janus_adaptive_post.tcl")
    assert "JANUS_ADAPT_CHAIN_POST=/h/post_detail_place.tcl" in cmd
    assert "JANUS_CLOCK_RESTORE_TCL=/v/rs.tcl" in cmd and "JANUS_CLOCK_LATENCY_TCL=/v/ov.tcl" in cmd


def test_advise_writes_adaptive_json(tmp_path):
    import json
    import shutil
    a = _agent(tmp_path)
    rep = tmp_path / "rep"
    shutil.copytree(os.path.join(FIX, "ara_finish_run"), rep)
    a._reports_dir = lambda b: str(rep)
    a._logs_dir = lambda b: None
    a._results_dir = lambda b: None
    a._leaf = lambda: "base"
    res = types.SimpleNamespace(status="DONE", note="")
    rec = a._adaptive_advise("user_core_ara", res)
    assert rec["class"] == "CLOCK_SKEW_LIMITED" and rec["rerun"] is False
    body = json.load(open(rep / "adaptive.json"))
    assert body["mode"] == "advise" and "_reports_dir" not in body
    assert "adaptive[advise] CLOCK_SKEW_LIMITED" in res.note
    # off / dry-run / non-DONE -> nothing
    assert _agent(tmp_path, "--adaptive", "off")._adaptive_advise("b", res) is None
    assert a._adaptive_advise("b", types.SimpleNamespace(status="FAILED", note="")) is None


def test_auto_protected_block_never_reruns(tmp_path):
    a = _agent(tmp_path, "--adaptive", "auto")
    called = []
    a._adaptive_run_variant = lambda *x: called.append(x)
    rec = ac.plan("user_core_ara_lane", {"class": "MODERATE"}, "auto")
    a._adaptive_auto("user_core_ara_lane", types.SimpleNamespace(note=""), rec)
    assert not called and rec.get("blocked")


# ----- §145: promotion of a better extra pass in place -----------------------
def _variant_tree(flow, v="adapt1_clock_skew_limited"):
    for sub, names in {"results": ["3_place.odb", "6_final.odb", "b.lef"],
                       "logs": ["3_3_place_gp.log"],
                       "reports": ["6_finish.rpt"],
                       "objects": ["klayout.lyt"]}.items():
        for n in names:
            p = flow / sub / "asap7" / "b" / v / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("V:" + n)
    return v


def _promo_agent(tmp_path):
    a = _agent(tmp_path, "--adaptive", "auto")
    base = _tree(tmp_path / "flow")
    a._results_dir = lambda b: base
    a._leaf = lambda: "base"
    a._authoritative_timing_from_rpt = lambda b: None
    a.abstracts = []
    a._run_abstract = lambda b: a.abstracts.append(b)
    return a, base


def test_promote_swaps_leaf_keeps_superseded(tmp_path):
    a, base = _promo_agent(tmp_path)
    v = _variant_tree(tmp_path / "flow")
    res = types.SimpleNamespace(note="", wns_ns=-20.0, hold_ps=5.0, tns_ns=-100.0,
                                drc_violations=3, max_slew_violations=9)
    rec = {"promotion": {"pass": v}, "passes": []}
    best = {"name": v, "wns_ps": 4.0, "hold_wns_ps": 6.0, "tns_ps": 0.0, "drc": 0,
            "slew_viol": 2}
    assert a._adaptive_promote("b", res, rec, best, "improves: wns")
    r = tmp_path / "flow" / "results" / "asap7" / "b"
    assert open(r / "base" / "6_final.odb").read() == "V:6_final.odb"     # promoted
    assert open(r / "base" / "1_synth.v").read() == "1_synth.v"           # backfilled
    assert not (r / "base" / "b.lef").exists()                            # stale abstract
    assert not (r / v).exists()
    keep = rec["promotion"]["superseded_leaf"]
    assert keep.startswith("base_superseded_") and rec["promotion"]["promoted"]
    for sub in ("results", "logs", "reports", "objects"):
        assert (tmp_path / "flow" / sub / "asap7" / "b" / keep).is_dir()
    assert open(r / keep / "6_final.odb").read() == "6_final.odb"         # old kept
    rep = tmp_path / "flow" / "reports" / "asap7" / "b" / "base"
    assert (rep / "synth_stat.txt").exists() and (rep / "adaptive_promoted.json").exists()
    assert res.wns_ns == 4.0 and res.drc_violations == 0 and res.max_slew_violations == 2
    assert "adaptive PROMOTED" in res.note and a.abstracts == ["b"]


def test_promote_rollback_on_rename_failure(tmp_path, monkeypatch):
    a, base = _promo_agent(tmp_path)
    v = _variant_tree(tmp_path / "flow")
    real = os.rename
    n = {"c": 0}

    def flaky(s, d):
        n["c"] += 1
        if n["c"] == 4:          # fail mid-way (logs subdir)
            raise OSError("boom")
        return real(s, d)
    monkeypatch.setattr(ja.os, "rename", flaky)
    res = types.SimpleNamespace(note="", wns_ns=-20.0)
    assert not a._adaptive_promote("b", res, {"promotion": {}}, {"name": v}, "x")
    monkeypatch.setattr(ja.os, "rename", real)
    for sub in ("results", "logs", "reports", "objects"):
        names = sorted(os.listdir(tmp_path / "flow" / sub / "asap7" / "b"))
        assert names == ["adapt1_clock_skew_limited", "base"], (sub, names)
    assert open(tmp_path / "flow" / "results" / "asap7" / "b" / "base" / "6_final.odb").read() == "6_final.odb"
    assert "FAILED" in res.note and not a.abstracts


def test_promote_rejects_non_adaptive_name(tmp_path):
    a, _ = _promo_agent(tmp_path)
    assert not a._adaptive_promote("b", types.SimpleNamespace(note=""), {"promotion": {}},
                                   {"name": "../base"}, "x")


@pytest.mark.parametrize("val,exp", [(None, 259200), ("0", None), ("3600", 3600),
                                     ("junk", 259200)])
def test_adaptive_timeout_env(monkeypatch, val, exp):
    if val is None:
        monkeypatch.delenv("JANUS_ADAPTIVE_TIMEOUT_S", raising=False)
    else:
        monkeypatch.setenv("JANUS_ADAPTIVE_TIMEOUT_S", val)
    assert ja.adaptive_timeout_s() == exp


def test_adaptive_runs_before_done_mark():
    import inspect
    src = inspect.getsource(ja.JANUSAutonomousAgent._process_and_mark)
    i_auto = src.index("_adaptive_auto(")
    assert i_auto < src.index("_notify_settled(") and i_auto < src.index("_dash_update(")


def test_run_table_pending_and_done(tmp_path):
    a = _agent(tmp_path)
    a.dep = types.SimpleNamespace(graph={"noc": [], "ara": [], "soc": ["noc"]},
                                  summary=lambda: {"ara": ja.Status.RUNNING.value})
    a.results = {"noc": types.SimpleNamespace(status="DONE", wns_ns=12.0, hold_ps=3.0,
                                              drc_violations=0, max_slew_violations=0,
                                              note="adaptive PROMOTED adapt1_x"),
                 "ara": types.SimpleNamespace(status="PENDING", wns_ns=None, hold_ps=None,
                                              drc_violations=None, max_slew_violations=None,
                                              note="")}
    a._adapt_live = {}
    rows = {r["block"]: r for r in a._run_table()}
    assert rows["noc"]["status"] == "DONE" and rows["noc"]["setup_ps"] == 12.0
    assert rows["noc"]["note"] == "adaptive PROMOTED"
    assert rows["ara"]["status"] == "RUNNING" and rows["ara"]["setup_ps"] is None
    assert rows["soc"]["status"] == "PENDING"
    a._adapt_live = {"ara": {"stage": "5_route"}}
    assert {r["block"]: r for r in a._run_table()}["ara"]["status"] == "ADAPT (5_route)"


def test_drive_stamp_from_run_dir(tmp_path):
    a = _agent(tmp_path)
    a.run_dir = "/x/runs/20261011_124432"
    assert a._drive_run_stamp() == "2026-10-11_12-44-32"
