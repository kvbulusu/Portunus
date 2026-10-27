"""§153 force-clean must wipe ONLY the target block, never sibling blocks
whose names share a prefix (user_core vs user_core_cva6/user_core_ara/...).
Run: python3 -m pytest asic_flow/agent/tests/test_force_clean.py -q"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "utils"))
import janus_agent as ja  # noqa: E402

KINDS = ("results", "reports", "logs", "objects")
BLOCKS = ("user_core", "user_core_cva6", "user_core_ara", "user_core_ara_lane",
          "user_domain", "janus_soc_top", "janus_user_core")


class _UI:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _agent(tmp_path, targets):
    a = ja.JANUSAutonomousAgent.__new__(ja.JANUSAutonomousAgent)
    a.flow_dir = str(tmp_path / "flow")
    a.ui = _UI()
    a.manifest = None
    a.results = {}
    a._force_targets = types.MethodType(lambda self: list(targets), a)
    return a


def _tree(tmp_path):
    flow = tmp_path / "flow"
    for kind in KINDS:
        for b in BLOCKS:
            for leaf in ("base", "base_adapt1_demoted_20261020_125944"):
                d = flow / kind / "asap7" / b / leaf
                d.mkdir(parents=True)
                (d / "x.lib").write_text("x")
    return flow


def _names(dirs):
    return {os.path.basename(d) for d in dirs}


def test_state_dirs_exact_names_only(tmp_path):
    _tree(tmp_path)
    a = _agent(tmp_path, [])
    names = _names(a._orfs_state_dirs("user_core"))
    assert "user_core" in names and "janus_user_core" in names
    for sib in ("user_core_cva6", "user_core_ara", "user_core_ara_lane", "user_domain"):
        assert sib not in names
    ara = _names(a._orfs_state_dirs("user_core_ara"))
    assert "user_core_ara_lane" not in ara and "user_core" not in ara


def test_force_clean_user_core_keeps_sibling_abstracts(tmp_path):
    flow = _tree(tmp_path)
    _agent(tmp_path, ["user_core"])._force_clean_blocks()
    for kind in KINDS:
        base = flow / kind / "asap7"
        assert not (base / "user_core").exists()
        for sib in ("user_core_cva6", "user_core_ara", "user_core_ara_lane", "user_domain"):
            assert (base / sib / "base" / "x.lib").exists(), (kind, sib)
            # variant leaves of siblings survive too
            assert (base / sib / "base_adapt1_demoted_20261020_125944").is_dir()


def test_force_clean_removes_own_variant_leaves(tmp_path):
    flow = _tree(tmp_path)
    _agent(tmp_path, ["user_core_ara"])._force_clean_blocks()
    base = flow / "results" / "asap7"
    assert not (base / "user_core_ara").exists()   # base + adapt leaves gone
    assert (base / "user_core_ara_lane" / "base" / "x.lib").exists()


def test_no_flow_dir(tmp_path):
    a = _agent(tmp_path, [])
    a.flow_dir = None
    assert a._orfs_state_dirs("user_core") == []


# §154: report/result/log dir lookup must not fall back to a sibling block.
def test_dir_lookup_never_picks_sibling(tmp_path):
    flow = tmp_path / "flow"
    for kind, f in (("reports", "6_finish.rpt"), ("results", "6_final.odb"),
                    ("logs", "1_2_yosys.log")):
        d = flow / kind / "asap7" / "user_core_ara" / "base"
        d.mkdir(parents=True)
        (d / f).write_text("x")
    a = _agent(tmp_path, [])
    a.args = types.SimpleNamespace(reports_dir=None, variant=None)
    a._leaf = types.MethodType(lambda self: "base", a)
    assert a._reports_dir("user_core") is None
    assert a._results_dir("user_core") is None
    assert a._logs_dir("user_core") is None
    assert a._reports_dir("user_core_ara").endswith("user_core_ara/base")
    # own dir still found once it exists
    own = flow / "reports" / "asap7" / "user_core" / "base"
    own.mkdir(parents=True)
    (own / "1_synth.rpt").write_text("x")
    assert a._reports_dir("user_core") == str(own)


def test_mcp_dir_lookup_never_picks_sibling(tmp_path):
    import mcp_server as ms
    d = tmp_path / "reports" / "asap7" / "user_core_ara" / "base"
    d.mkdir(parents=True)
    (d / "6_finish.rpt").write_text("x")
    r = tmp_path / "results" / "asap7" / "user_core_ara" / "base"
    r.mkdir(parents=True)
    (r / "6_final.odb").write_text("x")
    assert ms._reports_dir_for(str(tmp_path), "user_core") is None
    assert ms._results_dir_for(str(tmp_path), "user_core") is None
