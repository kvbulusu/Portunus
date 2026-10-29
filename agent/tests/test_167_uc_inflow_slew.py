"""§167 user_core in-flow long-wire slew fix: post-GRT repair_design gets
-max_wire_length and a GRT-only slew margin via hooks/janus_rsz_long_wire.tcl."""
import glob
import os
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "utils"))
import flow_state as fs  # noqa: E402

ASIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CFG = os.path.join(ASIC, "configs")
UC_MK = os.path.join(CFG, "user_core.mk")
HOOKS = os.path.join(ASIC, "scripts", "hooks")
HOOK = os.path.join(HOOKS, "janus_rsz_long_wire.tcl")
KNOBS = ("JANUS_RSZ_MAX_WIRE_UM", "JANUS_RSZ_GRT_SLEW_MARGIN",
         "JANUS_RSZ_GRT_CAP_MARGIN")


def _val(txt, key):
    m = re.search(rf"^export\s+{key}\s*\??=\s*(.+)$", txt, re.M)
    return m.group(1).strip() if m else None


def test_user_core_sets_167_knobs():
    uc = open(UC_MK).read()
    assert _val(uc, "JANUS_RSZ_MAX_WIRE_UM") == "160"
    assert _val(uc, "JANUS_RSZ_GRT_SLEW_MARGIN") == "30"
    assert _val(uc, "SKIP_INCREMENTAL_REPAIR") == "0"
    assert re.search(r"^export\s+JANUS_RSZ_MAX_WIRE_UM\s*\?=", uc, re.M)
    # hook path that sources the wrapper is the user_core GRT pre-hook
    assert _val(uc, "PRE_GLOBAL_ROUTE_TCL").endswith("hooks/pre_global_route.tcl")
    # §161/§165/§166 settings untouched (3_4 resize keeps its 20% margin)
    assert _val(uc, "SLEW_MARGIN") == "20"
    assert _val(uc, "CAP_MARGIN") == "20"
    assert _val(uc, "JANUS_ECO_SETUP_DRV") == "0"


def test_other_blocks_do_not_set_knobs():
    for mk in glob.glob(os.path.join(CFG, "*.mk")):
        if os.path.basename(mk) == "user_core.mk":
            continue
        txt = open(mk).read()
        for k in KNOBS:
            assert _val(txt, k) is None, f"{k} set in {mk}"


def test_pre_global_route_sources_hook():
    pre = open(os.path.join(HOOKS, "pre_global_route.tcl")).read()
    assert "janus_rsz_long_wire.tcl" in pre
    assert os.path.isfile(HOOK)


def test_resume_maps_to_5_route():
    """Resume must rerun only from routing, not from synthesis."""
    assert fs._earliest_stage_for_keys(list(KNOBS)) == "5_route"
    assert fs._earliest_stage_for_keys(["SKIP_INCREMENTAL_REPAIR"]) == "5_route"
    old = fs.parse_config_vars(UC_MK)
    prev = dict(old)
    for k in ("JANUS_RSZ_MAX_WIRE_UM", "JANUS_RSZ_GRT_SLEW_MARGIN",
              "SKIP_INCREMENTAL_REPAIR"):
        prev.pop(k, None)
    changed = fs._changed_keys(prev, old)
    assert set(changed) == {"JANUS_RSZ_MAX_WIRE_UM", "JANUS_RSZ_GRT_SLEW_MARGIN",
                            "SKIP_INCREMENTAL_REPAIR"}
    assert fs._earliest_stage_for_keys(changed) == "5_route"


def _tcl(script, env):
    return subprocess.run(["tclsh"], input=script, capture_output=True,
                          text=True, env=env, timeout=30)


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_hook_rewrites_args_and_noop_when_unset():
    probe = (f"source {{{HOOK}}}\n"
             "puts [::janus_rsz_rewrite_args {-verbose -cap_margin 20 -slew_margin 20}]\n"
             "puts [::janus_rsz_rewrite_args {-pre_placement -verbose}]\n"
             "puts [::janus_rsz_rewrite_args {-verbose -max_wire_length 50}]\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("JANUS_RSZ_")}
    out = _tcl(probe, env).stdout.splitlines()
    assert out == ["-verbose -cap_margin 20 -slew_margin 20",
                   "-pre_placement -verbose", "-verbose -max_wire_length 50"]
    env.update(JANUS_RSZ_MAX_WIRE_UM="160", JANUS_RSZ_GRT_SLEW_MARGIN="30")
    out = _tcl(probe, env).stdout.splitlines()
    assert out[0] == "-verbose -cap_margin 20 -slew_margin 30 -max_wire_length 160"
    assert out[1] == "-pre_placement -verbose"
    assert out[2] == "-verbose -max_wire_length 50 -slew_margin 30"
