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


# ---- §164 addendum: user_core slew triage -> user_domain --------------------
HOOKS = os.path.join(ASIC, "scripts", "hooks")


def _read(p):
    with open(p) as f:
        return f.read()


def test_const_fold_tie_fallback_wired():
    """ORFS erases TIEHI/TIELO_CELL_AND_PORT before PRE_RESIZE (stages synth,
    floorplan), so the fold needs non-ORFS fallback names. Opt-in per block:
    user_domain and (since §165) user_core set them."""
    txt = _mk()
    assert _val(txt, "JANUS_CONST_FOLD_TIEHI") == "$(TIEHI_CELL_AND_PORT)"
    assert _val(txt, "JANUS_CONST_FOLD_TIELO") == "$(TIELO_CELL_AND_PORT)"
    assert _val(txt, "PRE_RESIZE_TCL") == "$(JANUS_CONST_FOLD_TCL)"
    hook = _read(os.path.join(HOOKS, "janus_const_fold.tcl"))
    assert "tie_master $db TIELO_CELL_AND_PORT JANUS_CONST_FOLD_TIELO" in hook
    assert "tie_master $db TIEHI_CELL_AND_PORT JANUS_CONST_FOLD_TIEHI" in hook
    # stock ORFS name is still tried first
    assert re.search(r"set spec \[env \$var \"\"\]\s*\n\s*if \{\[llength \$spec\] != 2 && \$fallback", hook)


def test_detour_const_guard_default_off_on_in_user_domain():
    txt = _mk()
    assert _val(txt, "JANUS_MACRO_DETOUR_SKIP_CONST") == "1"
    hook = _read(os.path.join(HOOKS, "janus_macro_detour.tcl"))
    # default 0 => blocks that do not opt in are unchanged
    assert "[env JANUS_MACRO_DETOUR_SKIP_CONST 0] eq \"0\"" in hook
    # guard applied in both the macro-pin pass and the straddle pass
    assert hook.count("[net_is_const $n]") == 2


def test_addendum_knobs_resume_stage():
    assert fs._earliest_stage_for_keys(["JANUS_CONST_FOLD_TIEHI"]) == "3_place"
    assert fs._earliest_stage_for_keys(["JANUS_MACRO_DETOUR_SKIP_CONST"]) == "5_route"
