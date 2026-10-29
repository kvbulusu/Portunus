"""§165 user_core slew fix (triage 20261028_194857): const-fold tie fallback,
detour constant-net guard, post-route DRV repair in the setup ECO."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "utils"))
import flow_state as fs  # noqa: E402

ASIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
UC_MK = os.path.join(ASIC, "configs", "user_core.mk")
TCL = os.path.join(ASIC, "scripts", "post_route_opt.tcl")


def _val(txt, key):
    m = re.search(rf"^export\s+{key}\s*\??=\s*(.+)$", txt, re.M)
    return m.group(1).strip() if m else None


def test_user_core_opts_in_to_slew_fixes():
    uc = open(UC_MK).read()
    assert _val(uc, "JANUS_CONST_FOLD_TIEHI") == "$(TIEHI_CELL_AND_PORT)"
    assert _val(uc, "JANUS_CONST_FOLD_TIELO") == "$(TIELO_CELL_AND_PORT)"
    assert _val(uc, "PRE_RESIZE_TCL") == "$(JANUS_CONST_FOLD_TCL)"
    assert _val(uc, "JANUS_MACRO_DETOUR_SKIP_CONST") == "1"
    assert _val(uc, "JANUS_ECO_SETUP_DRV") == "1"
    # §163 settings untouched
    assert _val(uc, "JANUS_ECO_HOLD_MARGIN_PS") == "20"
    assert _val(uc, "SLEW_MARGIN") == "20"


def test_resume_stages():
    """--resume reruns user_core from 3_place (fold at 3_4); the DRV knob is a
    post-flow ECO knob and invalidates no stage."""
    assert fs._earliest_stage_for_keys(["JANUS_CONST_FOLD_TIEHI",
                                        "JANUS_MACRO_DETOUR_SKIP_CONST"]) == "3_place"
    assert fs._changed_keys({"JANUS_ECO_SETUP_DRV": "0"}, {"JANUS_ECO_SETUP_DRV": "1"}) == []


def test_tcl_drv_repair_gated_and_ordered():
    s = open(TCL).read()
    gate = s.index('[_env JANUS_ECO_SETUP_DRV 0] ne "0"')
    drv = s.index("repair_design -slew_margin $sm -cap_margin $cm")
    setup = s.index("if {[catch {repair_timing -setup")
    grt = s.index("    _grt_start\n")
    reroute = s.index("    _reroute\n")
    # inside the GRT incremental window, before setup sizing
    assert grt < gate < drv < setup < reroute
    assert '$mode eq "setup"' in s[gate - 80:gate]
