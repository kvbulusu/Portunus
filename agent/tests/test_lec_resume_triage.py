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
    assert cva6["LEC_CHECK"] == "1" and cva6["HOLD_SLACK_MARGIN"] == "25"
