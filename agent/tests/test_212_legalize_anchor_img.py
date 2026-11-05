"""§212 (run 20261105_110236, 4_cts S -876.4 / H -309.6, 16713 hold buffers).
(a) JANUS_CTS_LEGALIZE: hooks measured S -701.8 / H -486.2 before cts.tcl's
    detailed_placement moved the hook clock buffers (repair started -1003 /
    -865) -> legalize after MACRO_ALIGN and TAP_SPLIT; HOLD_ABORT -700.
(b) JANUS_PIN_FENCE_ANCHOR knob (probe only, not enabled in the mk).
(c) janus_stage_images: get_name only on SWIG pointers (STA-2204 on "slow").
"""
import re
import subprocess
from pathlib import Path

from test_209_pin_fence_dpl import FLOW, MK, _run, _xy

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
CB = (HOOKS / "janus_cts_balance.tcl").read_text()
PF = (HOOKS / "janus_pin_fence.tcl").read_text()
IMG = HOOKS / "janus_stage_images.tcl"


def _hier():
    h = MK[MK.index("ifneq ($(JANUS_FLAT),1)"):]
    return h[:h.index("\nendif")]


def test_mk_legalize_on_abort_tight_anchor_off():
    h = _hier()
    assert re.search(r"^export JANUS_CTS_LEGALIZE\s*=\s*1\s*$", h, re.M)
    assert re.search(r"^export JANUS_CTS_HOLD_ABORT_PS\s*=\s*-700\s*$", h, re.M)
    assert not re.search(r"^export JANUS_PIN_FENCE_ANCHOR", MK, re.M)


def test_legalize_called_after_align_and_split_before_diag():
    w = CB[CB.index("proc clock_tree_synthesis"):]
    i_mal = w.index("janus_cts_legalize macro_align")
    i_ts = w.index("janus_cts_legalize tap_split")
    assert w.index("janus_cts_tap_split}") > i_mal
    assert i_ts > w.index("janus_cts_tap_split}")
    assert w.index("JANUS_CTS_DIAG 0") > i_ts
    p = CB[CB.index("proc janus_cts_legalize"):CB.index("proc janus_cts_hold_abort")]
    assert 'JANUS_CTS_LEGALIZE 0] eq "0"' in p and "catch { detailed_placement" in p


def test_legalize_proc_gating_tclsh():
    p = CB[CB.index("proc janus_cts_legalize"):CB.index("# §198b JANUS_CTS_HOLD_ABORT_PS")]
    stub = r"""
set ::calls {}
proc janus_cts_env {n d} { if {[info exists ::env($n)]} { return $::env($n) }; return $d }
proc estimate_parasitics {args} { lappend ::calls est }
proc detailed_placement {args} { lappend ::calls [list dpl {*}$args] }
proc janus_ofs_wns {} { return "S -1 H -2" }
""" + p + r"""
janus_cts_legalize t0
puts "OFF $::calls"
set ::env(JANUS_CTS_LEGALIZE) 1
janus_cts_legalize t1
puts "ON $::calls"
set ::calls {}
set ::env(DETAIL_PLACEMENT_ARGS) {-max_displacement {10 10}}
janus_cts_legalize t2
puts "ARGS $::calls"
"""
    r = subprocess.run(["tclsh"], input=stub, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0].strip() == "OFF"
    assert "dpl -max_displacement {500 500}" in r.stdout
    assert "JANUS_CTS_LEGALIZE: t1 before S -1 H -2 after S -1 H -2" in r.stdout
    assert "dpl -max_displacement {10 10}" in r.stdout


def test_img_scene_name_only_derefs_swig_pointers():
    src = IMG.read_text()
    p = src[src.index("proc janus_img_scene_name"):src.index("# clock nets + clock-tree")]
    t = r"""
set ::n 0
proc get_name {o} { incr ::n; return "slowcorner" }
""" + p + r"""
puts "A [janus_img_scene_name slow] $::n"
puts "B [janus_img_scene_name _0123abcd_p_sta__Scene] $::n"
"""
    r = subprocess.run(["tclsh"], input=t, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "A slow 0" in r.stdout and "B slowcorner 1" in r.stdout
    assert "-scene $scene_name" in src


def test_pin_fence_anchor_moves_member_to_anchor_pin(tmp_path):
    # rx's own pin is u_m/dout (W edge, y 242.1); anchor it at u_m/clk (y 232.1)
    rc, out = _run(tmp_path, "anc", FLOW, {"JANUS_PIN_FENCE": "fx:u_m:rx",
                                          "JANUS_PIN_FENCE_MODE": "prelock",
                                          "JANUS_PIN_FENCE_ANCHOR": "fx:rx:clk"})
    assert rc == 0, out[-3000:]
    assert re.search(r"fx anchor 'rx' -> u_m \{clk\} at \(230\.\d,232\.\d\) edge W members 1", out), out[-3000:]
    assert "fx anchored 1 member(s) at JANUS_PIN_FENCE_ANCHOR pins" in out
    x, y = _xy(out, "rx")
    assert 185.0 <= x <= 232.0 and 226.0 <= y <= 237.5, (x, y)
    rc0, out0 = _run(tmp_path, "ref", FLOW, {"JANUS_PIN_FENCE": "fx:u_m:rx",
                                            "JANUS_PIN_FENCE_MODE": "prelock"})
    assert rc0 == 0 and "anchor" not in out0.split("JANUS_PIN_FENCE")[0]
    assert _xy(out0, "rx")[1] > y + 4.0


def test_pin_fence_bad_anchor_spec_warns(tmp_path):
    rc, out = _run(tmp_path, "bad", FLOW, {"JANUS_PIN_FENCE": "fx:u_m:rx",
                                          "JANUS_PIN_FENCE_MODE": "prelock",
                                          "JANUS_PIN_FENCE_ANCHOR": "fx:rx fx:rx:nopin"})
    assert rc == 0, out[-3000:]
    assert "bad anchor spec 'fx:rx'" in out
    assert "anchor 'rx': no u_m pin in {nopin}; ignored" in out
    assert "prelocked 1 (mode prelock)" in out
