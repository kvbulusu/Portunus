"""§160: ICG-latch unpad knob is wired in user_core and resumes from 4_cts."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))

import flow_state  # noqa: E402


def test_cts_hook_keys_rerun_from_cts():
    for k in ("JANUS_CTS_ICG_UNPAD", "JANUS_CTS_ICG_UNPAD_SETUP_TARGET",
              "JANUS_CTS_ICG_UNPAD_HOLD_FLOOR", "JANUS_CTS_DELAY_BUFFER_DERATE"):
        assert flow_state._earliest_stage_for_keys([k]) == "4_cts", k


def test_user_core_mk_enables_unpad_hard():
    txt = open(os.path.join(HERE, "..", "..", "configs", "user_core.mk")).read()
    assert re.search(r"^export JANUS_CTS_ICG_UNPAD = 1$", txt, re.M)
    assert "export PRE_CTS_TCL = $(JANUS_RTL_ROOT)/asic_flow/scripts/hooks/janus_cts_balance.tcl" in txt


def test_hook_wraps_cts_and_logs():
    hook = open(os.path.join(HERE, "..", "..", "scripts", "hooks",
                             "janus_cts_balance.tcl")).read()
    assert "rename clock_tree_synthesis janus_orig_clock_tree_synthesis" in hook
    assert "JANUS_CTS_ICG_UNPAD: removed" in hook
    assert "catch {janus_cts_icg_unpad}" in hook
