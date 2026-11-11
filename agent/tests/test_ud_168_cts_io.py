"""§168 user_domain CTS RSZ-0060: virtual IO clock tracking the real tree + glue-side IO pins."""
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
SHADOW = os.path.join(ASIC, "scripts", "hooks", "janus_io_macro_shadow.tcl")
CTS_HOOK = os.path.join(ASIC, "scripts", "hooks", "janus_cts_balance.tcl")


def _read(*p):
    with open(os.path.join(*p)) as f:
        return f.read()


def _val(txt, key):
    m = re.search(rf"^export\s+{key}\s*\??=\s*(.+)$", txt, re.M)
    return m.group(1).strip() if m else None


def _code(txt):
    return "\n".join(l.split(";#")[0] for l in txt.splitlines() if not l.lstrip().startswith("#"))


def test_sdc_io_on_virtual_clock_with_latency():
    sdc = _code(_read(CFG, "constraints", "user_domain.sdc"))
    assert "set io_clk_name        ${clk_name}_io" in sdc
    assert re.search(r"create_clock -name \$io_clk_name -period \$clk_period\s*$", sdc, re.M)
    assert "set_clock_latency -source $io_clk_latency_ps [get_clocks $io_clk_name]" in sdc
    assert "set_clock_latency         $io_clk_latency_ps [get_clocks $clk_name]" in sdc
    assert "set_input_delay  -clock $io_clk_name" in sdc
    assert "set_output_delay -clock $io_clk_name" in sdc
    assert "-clock $clk_name $in_dly" not in sdc
    # virtual clocks reject set_clock_transition
    assert "set_clock_transition        [expr {0.050 * $PS}] [get_clocks $clk_name]" in sdc
    # §168 itself adds no exceptions. §174 adds exactly three narrow control-signal
    # exceptions (1 false path, 2 multicycle; see test_ud_174_ctrl_exceptions); nothing else.
    for kw in ("set_max_delay", "set_disable_timing"):
        assert kw not in sdc
    assert sdc.count("set_false_path") == 1 and sdc.count("set_multicycle_path") == 2
    assert sdc.index("set_false_path") > sdc.index("set_output_delay -clock $io_clk_name")


def test_mk_knobs():
    mk = _read(CFG, "user_domain.mk")
    assert _val(mk, "JANUS_IO_SHADOW_EXCLUDE_EDGES") == "left"
    assert _val(mk, "JANUS_IO_SHADOW_SPAN_EDGES") == "bottom top"
    assert _val(mk, "PRE_CTS_TCL").endswith("asic_flow/scripts/hooks/janus_cts_balance.tcl")
    assert _val(mk, "JANUS_CTS_IO_LATENCY_TRACK") == "1"
    assert _val(mk, "JANUS_CTS_IO_CLOCK") == "core_clk_io"
    # must-hold knobs use plain '=' (§141 lesson)
    for k in ("JANUS_IO_SHADOW_EXCLUDE_EDGES", "JANUS_IO_SHADOW_SPAN_EDGES", "JANUS_CTS_IO_LATENCY_TRACK"):
        assert re.search(rf"^export\s+{k}\s+=", mk, re.M), k


def test_other_blocks_unaffected():
    for name in os.listdir(CFG):
        if not name.endswith(".mk") or name in ("user_domain.mk", "janus_soc_top.mk"):  # §187 uses the shadow knobs
            continue
        txt = _read(CFG, name)
        for k in ("JANUS_IO_SHADOW_EXCLUDE_EDGES", "JANUS_IO_SHADOW_SPAN_EDGES", "JANUS_CTS_IO_LATENCY_TRACK"):
            assert _val(txt, k) is None, (name, k)


def test_flow_state_stages():
    assert fs._earliest_stage_for_keys(["JANUS_IO_SHADOW_EXCLUDE_EDGES"]) == "3_place"
    assert fs._earliest_stage_for_keys(["JANUS_IO_SHADOW_SPAN_EDGES"]) == "3_place"
    assert fs._earliest_stage_for_keys(["JANUS_CTS_IO_LATENCY_TRACK"]) == "4_cts"


MOCK = r"""
proc mkobj {name body} { proc $name {method args} $body }
mkobj DIE {switch $method {xMin {return 0} yMin {return 0} xMax {return 1280285} yMax {return 1280285}}}
mkobj BB0 {switch $method {xMin {return 14070} yMin {return 91832} xMax {return 804070} yMax {return 551832}}}
mkobj BB1 {switch $method {xMin {return 14070} yMin {return 727680} xMax {return 804070} yMax {return 1187680}}}
mkobj M  {switch $method {isBlock {return 1}}}
mkobj I0 {switch $method {getMaster {return M} isPlaced {return 1} getBBox {return BB0} getName {return g_core0}}}
mkobj I1 {switch $method {getMaster {return M} isPlaced {return 1} getBBox {return BB1} getName {return g_core1}}}
mkobj TECH {return 1000}
mkobj DB {return TECH}
mkobj BLK {switch $method {getDieArea {return DIE} getInsts {return {I0 I1}} getBTerms {return [lrepeat 1571 x]}}}
namespace eval ord { proc get_db_block {} {return BLK}; proc get_db {} {return DB} }
source [lindex $::argv 0]
puts "ARGS=[expr {[info exists ::env(PLACE_PINS_ARGS)] ? $::env(PLACE_PINS_ARGS) : {}}]"
"""


def _shadow(tmp_path, **env):
    if not shutil.which("tclsh"):
        pytest.skip("tclsh not available")
    m = tmp_path / "mock.tcl"
    m.write_text(MOCK)
    e = {k: v for k, v in os.environ.items() if not k.startswith(("JANUS_IO", "PLACE_PINS"))}
    e.update(env)
    out = subprocess.run(["tclsh", str(m), SHADOW], env=e, capture_output=True, text=True, timeout=60).stdout
    return out.split("ARGS=")[-1].strip(), out


def test_shadow_baseline_unchanged(tmp_path):
    # matches the run 20261029_131116 3_2_place_iop log exactly
    args, _ = _shadow(tmp_path)
    assert args == "-exclude left:91.832-551.832 -exclude left:727.680-1187.680"


def test_shadow_168_glue_side(tmp_path):
    args, _ = _shadow(tmp_path, JANUS_IO_SHADOW_EXCLUDE_EDGES="left",
                      JANUS_IO_SHADOW_SPAN_EDGES="bottom top")
    assert args == "-exclude left:* -exclude bottom:14.070-804.070 -exclude top:14.070-804.070"


def test_shadow_capacity_guard(tmp_path):
    args, out = _shadow(tmp_path, JANUS_IO_SHADOW_EXCLUDE_EDGES="left",
                        JANUS_IO_SHADOW_SPAN_EDGES="bottom top", JANUS_IO_SHADOW_PIN_PITCH_UM="2")
    assert args == "" and "NOT applying excludes" in out


def test_shadow_unknown_edge_ignored(tmp_path):
    args, out = _shadow(tmp_path, JANUS_IO_SHADOW_EXCLUDE_EDGES="west")
    assert "unknown edge 'west'" in out
    assert args == "-exclude left:91.832-551.832 -exclude left:727.680-1187.680"


def test_cts_hook_wrap_logic():
    txt = _read(CTS_HOOK)
    assert "proc janus_cts_io_latency_track" in txt
    assert "set_clock_latency -source $med $vclk" in txt
    # both knobs share one wrapper; each step independently gated + caught
    assert "($janus_cts_unpad || $janus_cts_iolat || $janus_cts_noins || $janus_cts_diag || $janus_cts_mofs || $janus_cts_refpin || $janus_cts_refmulti || $janus_cts_malign || $janus_cts_tsplit || $janus_cts_lretap || $janus_cts_mretrim || $janus_cts_nochb || $janus_cts_agbal || $janus_cts_habort)" in txt  # §169-§172, §191, §198, §203, §219, §220, §224, §230
    assert "catch {janus_cts_io_latency_track}" in txt and "catch {janus_cts_icg_unpad}" in txt


def _sta():
    for p in (os.environ.get("JANUS_TEST_STA", ""),
              os.path.expanduser("~/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/sta")):
        if p and os.access(p, os.X_OK):
            return p
    return None


LIBS = os.path.expanduser("~/OpenROAD-flow-scripts/flow/platforms/asap7/lib/NLDM")
STUB = """module user_domain(user_clk, a, y);
input user_clk; input a; output y;
wire q, c1, c2, c3;
BUFx2_ASAP7_75t_R b1(.A(user_clk), .Y(c1));
BUFx2_ASAP7_75t_R b2(.A(c1), .Y(c2));
BUFx2_ASAP7_75t_R b3(.A(c2), .Y(c3));
DFFHQNx1_ASAP7_75t_R r1(.CLK(c3), .D(a), .QN(q));
BUFx2_ASAP7_75t_R o1(.A(q), .Y(y));
endmodule
"""


def test_sta_hook_sets_measured_latency(tmp_path):
    """OpenSTA on a stub: after a mocked CTS (propagate core_clk) the hook sets
    core_clk_io -source latency to the register clock arrival, and write_sdc keeps it."""
    sta = _sta()
    seq = os.path.join(LIBS, "asap7sc7p5t_SEQ_RVT_TT_nldm_220123.lib")
    if not sta or not os.path.isfile(seq):
        pytest.skip("OpenSTA / ASAP7 libs not available")
    import glob
    import gzip
    for tag in ("INVBUF", "SIMPLE"):
        src = glob.glob(os.path.join(LIBS, f"asap7sc7p5t_{tag}_RVT_TT_nldm_*.lib.gz"))
        if not src:
            pytest.skip("ASAP7 libs not available")
        (tmp_path / f"{tag}.lib").write_bytes(gzip.open(src[0]).read())
    (tmp_path / "stub.v").write_text(STUB)
    (tmp_path / "run.tcl").write_text(f"""
read_liberty {seq}; read_liberty INVBUF.lib; read_liberty SIMPLE.lib
read_verilog stub.v; link_design user_domain
read_sdc {os.path.join(CFG, 'constraints', 'user_domain.sdc')}
proc estimate_parasitics {{args}} {{}}
proc clock_tree_synthesis {{args}} {{ set_propagated_clock [get_clocks core_clk]; return 0 }}
source {CTS_HOOK}
clock_tree_synthesis
write_sdc out.sdc
""")
    e = dict(os.environ, JANUS_CTS_IO_LATENCY_TRACK="1", JANUS_CTS_ICG_UNPAD="0")
    out = subprocess.run([sta, "-no_init", "-exit", "run.tcl"], cwd=tmp_path, env=e,
                         capture_output=True, text=True, timeout=120).stdout
    m = re.search(r"JANUS_CTS_IO_LAT: 1 reg clock pins: .* -> set_clock_latency -source ([\d.]+) core_clk_io", out)
    assert m, out[-2000:]
    lat = float(m.group(1))
    assert 0 < lat < 500
    sdc = (tmp_path / "out.sdc").read_text()
    assert re.search(r"set_clock_latency -source [\d.]+ \[get_clocks \{core_clk_io\}\]", sdc)
