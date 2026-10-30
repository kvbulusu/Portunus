"""§172 user_domain: probe runs the PD corner set + real hook; JANUS_CTS_IO_REFPIN corner-true IO reference."""
import os
import re
import shutil
import subprocess

import pytest

ASIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(ASIC, "scripts")
CTS_HOOK = os.path.join(SCRIPTS, "hooks", "janus_cts_balance.tcl")
AGENT = os.path.join(ASIC, "agent", "janus_agent.py")


def _read(p):
    with open(p) as f:
        return f.read()


def test_probe_make_args_mirror_agent():
    sh = _read(os.path.join(SCRIPTS, "ud_cts_probe.sh"))
    agent = re.sub(r'"\s*\n\s*"', "", _read(AGENT))  # join split string literals
    for frag in ("CORNER=WC", "CORNERS=slow fast",
                 "SLOW_LIB_FILES=$(WC_NLDM_LIB_FILES) $(filter-out %_fast.lib,$(ADDITIONAL_LIBS))",
                 "FAST_LIB_FILES=$(BC_NLDM_LIB_FILES) $(filter-out %_slow.lib,$(ADDITIONAL_LIBS))",
                 "DONT_USE_CELLS=*x1p*_ASAP7* *xp*_ASAP7* ICG*"):
        assert frag in sh, frag
        assert frag in agent, frag
    assert '"${MK[@]}"' in sh
    assert "JANUS_MULTICORNER:-1" in sh


def test_probe_uses_real_hook_all_modes():
    tcl = _read(os.path.join(SCRIPTS, "ud_cts_probe.tcl"))
    assert "set ::env(JANUS_CTS_IO_LATENCY_TRACK) 1" in tcl
    assert 'set ::env(JANUS_CTS_IO_REFPIN) [expr {$mode eq "refpin" ? 1 : 0}]' in tcl
    # the hand-rolled IO latency is gone (hook is the single source of truth)
    assert "set_clock_latency -source $med" not in tcl
    assert "hold demand" in tcl and "PD 201223 ref" in tcl
    assert "write_db" not in tcl


def test_hook_refpin_structure():
    t = _read(CTS_HOOK)
    assert "proc janus_cts_io_refpin" in t
    assert "-reference_pin $best" in t
    assert "unset_input_delay -clock" in t and "unset_output_delay -clock" in t
    w = t[t.index("proc clock_tree_synthesis"):]
    # order: offset trim -> IO_LAT -> REFPIN -> DIAG (DIAG must see final IO model)
    assert w.index("janus_cts_macro_offset") < w.index("janus_cts_io_latency_track") \
        < w.index("janus_cts_io_refpin") < w.index("janus_cts_diag")


def test_refpin_not_armed_in_any_config_yet():
    # §172 is probe-only until the probe proves it; no .mk arms it (Restamp: no)
    cfg = os.path.join(ASIC, "configs")
    for name in os.listdir(cfg):
        if name.endswith(".mk"):
            assert not re.search(r"JANUS_CTS_IO_REFPIN\s*[:?]?=\s*1", _read(os.path.join(cfg, name))), name


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_hook_refpin_arms():
    script = (
        "proc clock_tree_synthesis { args } { return orig }\n"
        f"source {{{CTS_HOOK}}}\n"
        "puts [info procs janus_cts_io_refpin]\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("JANUS_CTS_")}
    out = subprocess.run(["tclsh"], input=script, capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "janus_cts_io_refpin" in out.stdout and "REFPIN: armed" not in out.stdout
    env["JANUS_CTS_IO_REFPIN"] = "1"
    out = subprocess.run(["tclsh"], input=script, capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "JANUS_CTS_IO_REFPIN: armed" in out.stdout
