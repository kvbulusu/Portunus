"""§156: pin-placement knobs must invalidate 3_place, not force a re-synthesis."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))

import flow_state  # noqa: E402


def test_pin_placement_keys_rerun_from_place():
    for k in ("PRE_IO_PLACEMENT_TCL", "PLACE_PINS_ARGS",
              "JANUS_IO_SHADOW_GAP_UM", "JANUS_IO_MACRO_SHADOW"):
        assert flow_state._earliest_stage_for_keys([k]) == "3_place", k


def test_unknown_key_still_resynths():
    assert flow_state._earliest_stage_for_keys(["SOMETHING_NEW"]) == "1_synth"


def test_user_core_mk_wires_hook():
    mk = os.path.join(HERE, "..", "..", "configs", "user_core.mk")
    txt = open(mk).read()
    assert "export PRE_IO_PLACEMENT_TCL = $(JANUS_RTL_ROOT)/asic_flow/scripts/hooks/janus_io_macro_shadow.tcl" in txt
    hook = os.path.join(HERE, "..", "..", "scripts", "hooks", "janus_io_macro_shadow.tcl")
    assert os.path.isfile(hook)
