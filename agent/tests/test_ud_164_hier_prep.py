"""§164 user_domain hierarchical prep: config wiring, agent OBS guard, stages."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "utils"))
import janus_agent as ja  # noqa: E402
import flow_state as fs  # noqa: E402

ASIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.dirname(ASIC)
MK = os.path.join(ASIC, "configs", "user_domain.mk")


def _mk():
    with open(MK) as f:
        return f.read()


def _val(txt, key):
    m = re.search(rf"^export\s+{key}\s*\??=\s*(.+)$", txt, re.M)
    return m.group(1).strip() if m else None


def test_hooks_wired_and_present():
    txt = _mk()
    for key, rel in [
        ("MACRO_PLACEMENT_TCL", "scripts/user_domain_macros.tcl"),
        ("PRE_IO_PLACEMENT_TCL", "scripts/hooks/janus_io_macro_shadow.tcl"),
        ("PDN_TCL", "scripts/pdn/user_core_grid_strategy.tcl"),
        ("POST_PDN_TCL", "scripts/hooks/restore_macro_route_obs.tcl"),
        ("PRE_GLOBAL_ROUTE_TCL", "scripts/hooks/pre_global_route.tcl"),
        ("POST_GLOBAL_ROUTE_TCL", "scripts/hooks/post_global_route.tcl"),
    ]:
        v = _val(txt, key)
        assert v and v.endswith("asic_flow/" + rel), (key, v)
        assert os.path.isfile(os.path.join(ASIC, rel)), rel
    assert _val(txt, "ENABLE_RESISTANCE_AWARE") == "0"
    assert _val(txt, "HOLD_SLACK_MARGIN") == "20"


def test_boundary_cut_rtl_in_filelist():
    txt = _mk()
    assert "-D JANUS_UD_BOUNDARY_CUT" in txt
    assert "noc/axi4_register_slice.sv" in txt
    with open(os.path.join(REPO, "rtl", "user", "user_cluster_top.sv")) as f:
        rtl = f.read()
    assert "`ifdef JANUS_UD_BOUNDARY_CUT" in rtl
    assert rtl.count("axi4_register_slice") >= 5


def test_no_die_or_layer_change():
    """Owner rule: no die-size / layer changes -- utilisation-driven die only."""
    txt = _mk()
    assert _val(txt, "CORE_UTILIZATION") is not None
    assert not re.search(r"^export\s+(DIE_AREA|CORE_AREA)\b", txt, re.M)
    assert not re.search(r"^export\s+(MIN|MAX)_ROUTING_LAYER\b", txt, re.M)


def test_ud_macro_knobs_floorplan_stage():
    assert fs._earliest_stage_for_keys(["JANUS_UD_MACRO_FRAC_MID"]) == "2_floorplan"
    assert fs._earliest_stage_for_keys(["JANUS_UD_BOUNDARY_CUT"]) == "1_synth"


class _UI:
    def __getattr__(self, _):
        return lambda *a, **k: None


def test_agent_strips_user_core_abstract_for_user_domain(monkeypatch):
    a = ja.JANUSAutonomousAgent.__new__(ja.JANUSAutonomousAgent)
    a.ui = _UI()
    calls = []
    monkeypatch.setattr(a, "_strip_child_abstract_obs",
                        lambda blk, child, above, keep_pin_layer=False:
                        calls.append((blk, child, above, keep_pin_layer)),
                        raising=False)
    monkeypatch.delenv("JANUS_OBS_STRIP_PIN_LAYER", raising=False)
    a._ensure_lane_abstract_obs_fixed("user_domain")
    assert calls == [("user_domain", "user_core", True, True)]
