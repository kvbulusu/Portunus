"""§140: LEC toggle must not force re-synthesis; OOM-killed LEC child is triaged."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))

import flow_state  # noqa: E402
from log_analyzer import LogReport, classify_failure  # noqa: E402


def test_lec_check_invalidates_from_cts_only():
    assert flow_state._earliest_stage_for_keys(["LEC_CHECK"]) == "4_cts"
    assert flow_state._earliest_stage_for_keys(["REPAIR_ANTENNAS"]) == "5_route"
    assert flow_state._earliest_stage_for_keys(["LEC_CHECK", "HOLD_SLACK_MARGIN"]) == "4_cts"


def test_conditional_assign_parsed(tmp_path):
    mk = tmp_path / "c.mk"
    mk.write_text("export LEC_CHECK ?= 0\nexport HOLD_SLACK_MARGIN ?= 25\n")
    v = flow_state.parse_make_vars(str(mk))
    assert v["LEC_CHECK"] == "0" and v["HOLD_SLACK_MARGIN"] == "25"


def test_child_killed_triaged_as_lec_oom():
    rep = LogReport(block="user_core_ara", stage="cts", failed=True)
    rep.tail = ("[info] [KeplerFormal.cpp:1017] Found top design: user_core_ara\n"
                "child killed: kill signal\n"
                "make[1]: *** [Makefile:511: do-4_1_cts] Error 1\n")
    t = classify_failure(rep)
    assert t is not None and t.category == "env-oom-lec"
    assert "LEC_CHECK" in t.message


def test_repo_configs():
    cfg = os.path.join(HERE, "..", "..", "configs")
    ara = flow_state.parse_make_vars(os.path.join(cfg, "user_core_ara.mk"))
    cva6 = flow_state.parse_make_vars(os.path.join(cfg, "user_core_cva6.mk"))
    assert ara["LEC_CHECK"] == "0"
    assert cva6["HOLD_SLACK_MARGIN"] == "25"
    # §148: LEC off for every block config
    import glob
    for mk in glob.glob(os.path.join(cfg, "*.mk")):
        v = flow_state.parse_make_vars(mk)
        assert v.get("LEC_CHECK", "0") == "0", mk


def test_lec_off_does_not_invalidate_done_block():
    """§148: 1->0 is check-only (no netlist change) -> no rerun; 0->1 still 4_cts."""
    on = {"LEC_CHECK": "1", "HOLD_SLACK_MARGIN": "25"}
    off = {"LEC_CHECK": "0", "HOLD_SLACK_MARGIN": "25"}
    assert flow_state._changed_keys(on, off) == []
    assert flow_state._changed_keys(on, {"HOLD_SLACK_MARGIN": "25"}) == []
    assert flow_state._changed_keys(off, on) == ["LEC_CHECK"]
    both = dict(off, HOLD_SLACK_MARGIN="30")
    assert flow_state._changed_keys(on, both) == ["HOLD_SLACK_MARGIN"]


def test_dup_child_lib_triaged_not_oom():
    """§147: user_core 20261016_202941 — Kepler got <child>_fast.lib + _slow.lib."""
    rep = LogReport(block="user_core", stage="cts", failed=True)
    rep.error_lines = ["Error: cts.tcl, 85 [info] [KeplerFormal.cpp:658] KEPLER FORMAL: Run."]
    rep.tail = ("[info] [KeplerFormal.cpp:747] Loading library file: "
                "results/asap7/user_core_cva6/base/user_core_cva6_slow.lib\n"
                "[critical] [KeplerFormal.cpp:1047] Netlist loading failed: NLLibrary "
                "user_core_cva6 contains already a SNLDesign named: user_core_cva6\n"
                "child process exited abnormally\n")
    t = classify_failure(rep)
    assert t is not None and t.category == "env-lec-dup-lib"
    assert "REMOVE_LIBS_FOR_LEC" in t.message


def test_make_cmd_dedups_lec_libs():
    src = open(os.path.join(HERE, "..", "janus_agent.py")).read()
    assert '"REMOVE_LIBS_FOR_LEC=$(filter %_fast.lib,$(ADDITIONAL_LIBS))"' in src
    # command-line only: must not appear in a block config (would force re-synth)
    assert flow_state._earliest_stage_for_keys(["REMOVE_LIBS_FOR_LEC"]) == "1_synth"
