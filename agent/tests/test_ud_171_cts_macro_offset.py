"""§171 user_domain CTS: closed-loop macro clock offset trim (JANUS_CTS_MACRO_OFFSET) + probe fix171 mode."""
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
SCRIPTS = os.path.join(ASIC, "scripts")
CTS_HOOK = os.path.join(SCRIPTS, "hooks", "janus_cts_balance.tcl")


def _read(p):
    with open(p) as f:
        return f.read()


def test_mk_knob():
    mk = _read(os.path.join(CFG, "user_domain.mk"))
    assert re.search(r"^export\s+JANUS_CTS_MACRO_OFFSET\s+=\s*1166\s*$", mk, re.M)
    # §170/§168 knobs unchanged
    assert re.search(r"^export\s+JANUS_CTS_NO_INSERTION_DELAY\s+=\s*0\s*$", mk, re.M)
    assert re.search(r"^export\s+JANUS_CTS_IO_LATENCY_TRACK\s+=\s*1", mk, re.M)


def test_only_user_domain_sets_offset():
    for name in os.listdir(CFG):
        if name.endswith(".mk") and name != "user_domain.mk":
            assert "JANUS_CTS_MACRO_OFFSET" not in _read(os.path.join(CFG, name)), name


def test_resume_stage_mapping():
    assert fs._earliest_stage_for_keys(["JANUS_CTS_MACRO_OFFSET"]) == "4_cts"


def test_hook_structure():
    t = _read(CTS_HOOK)
    assert "proc janus_cts_macro_offset" in t
    assert "insert_buffer -load_pins" in t and "clkbuf_regs_0*" in t
    assert "janus_ofsbuf* delaybuf_*" in t
    # gated: off when unset/0, armed flag in the wrapper condition
    assert 'set janus_cts_mofs [expr {[janus_cts_env JANUS_CTS_MACRO_OFFSET 0] ni {0 ""}}]' in t
    assert "$janus_cts_diag || $janus_cts_mofs" in t
    # trim runs BEFORE the IO latency retarget (IO reference must follow the moved regs)
    w = t[t.index("proc clock_tree_synthesis"):]
    assert w.index("janus_cts_macro_offset") < w.index("janus_cts_io_latency_track")


def test_probe_fix171_mode():
    tcl = _read(os.path.join(SCRIPTS, "ud_cts_probe.tcl"))
    sh = _read(os.path.join(SCRIPTS, "ud_cts_probe.sh"))
    assert 'if { $mode in {fix171 refpin} }' in tcl
    assert "hooks janus_cts_balance.tcl" in tcl and "PROBE_FIX171_TARGET" in tcl
    # probe stays read-only: hook DIAG disabled inside the probe (§172: IO_LAT
    # now runs through the real hook for PD parity)
    assert "foreach k {JANUS_CTS_DIAG JANUS_CTS_NO_INSERTION_DELAY JANUS_CTS_ICG_UNPAD}" in tcl
    assert "write_db" not in tcl
    assert "fix171" in sh and "JANUS_CTS_MACRO_OFS: (before|after)" in sh
    # default modes unchanged
    assert 'MODES=${2:-"ideal balanced noins"}' in sh


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_hook_parses_and_disarmed_by_default():
    script = (
        "proc clock_tree_synthesis { args } { return orig }\n"
        f"source {{{CTS_HOOK}}}\n"
        "puts [info commands janus_orig_clock_tree_synthesis]\n"
        "puts [info procs janus_cts_macro_offset]\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("JANUS_CTS_")}
    out = subprocess.run(["tclsh"], input=script, capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.split()
    assert "janus_orig_clock_tree_synthesis" not in lines
    assert "janus_cts_macro_offset" in lines
    env["JANUS_CTS_MACRO_OFFSET"] = "1166"
    out = subprocess.run(["tclsh"], input=script, capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "janus_orig_clock_tree_synthesis" in out.stdout
    assert "JANUS_CTS_MACRO_OFS: armed (target D = 1166 ps)" in out.stdout
