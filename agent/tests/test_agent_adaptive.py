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
