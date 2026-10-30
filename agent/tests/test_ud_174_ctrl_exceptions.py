"""§174 user_domain: control-signal SDC exceptions, D=1800 + IO_REFPIN CTS,
probe PROBE_SDC reload, robust corner label / report_dcalc scene."""
import os
import re
import shutil
import subprocess

import pytest

ASIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SDC = os.path.join(ASIC, "configs", "constraints", "user_domain.sdc")
MK = os.path.join(ASIC, "configs", "user_domain.mk")
SCRIPTS = os.path.join(ASIC, "scripts")
SH = os.path.join(SCRIPTS, "ud_cts_probe.sh")
TCL = os.path.join(SCRIPTS, "ud_cts_probe.tcl")
HOOK = os.path.join(SCRIPTS, "hooks", "janus_cts_balance.tcl")


def _read(p):
    with open(p) as f:
        return f.read()


def _cmds(txt, name):
    """Non-comment SDC command lines starting with `name`."""
    return [ln.strip() for ln in txt.splitlines()
            if ln.strip().startswith(name) and not ln.strip().startswith("#")]


def test_sdc_reset_false_path_is_narrow():
    t = _read(SDC)
    assert "set _rst_port [get_ports -quiet user_rst_n]" in t
    assert "set _rst_pins [get_pins -quiet {g_core[*].u_core/rst_ni}]" in t
    fps = _cmds(t, "set_false_path")
    assert fps == ["set_false_path -from $_rst_port -to $_rst_pins              "
                   ";# janus_soc_top.sv:149 + reset_sync.sv:20-35 (synced, quasi-static)"]
    assert "-through" not in fps[0]


def test_sdc_clock_en_multicycle_is_narrow():
    t = _read(SDC)
    assert "get_cells -quiet {core_clk_en[*]*}" in t  # core_clk_en[0]$_DFF_P_, not core_clk_en_q1
    assert "get_pins -quiet {g_core[*].u_core/clock_en_i}" in t
    mcps = _cmds(t, "set_multicycle_path")
    assert len(mcps) == 2
    assert mcps[0].startswith("set_multicycle_path 2 -setup -from $_cen_regs -to $_cen_pins")
    assert mcps[1].startswith("set_multicycle_path 1 -hold  -from $_cen_regs -to $_cen_pins")


def test_sdc_ara_busy_only_output_budget_no_exception():
    t = _read(SDC)
    for line in _cmds(t, "set_false_path") + _cmds(t, "set_multicycle_path"):
        assert "busy" not in line
    assert "set_output_delay -clock $io_clk_name 0 $_busy_port" in t
    # the per-port override comes AFTER the blanket output delay
    assert t.index("set_output_delay -clock $io_clk_name $out_dly [all_outputs]") \
        < t.index("set_output_delay -clock $io_clk_name 0 $_busy_port")
    assert "janus_soc_top OWNS" in t


def test_sdc_guards_and_evidence():
    t = _read(SDC)
    assert t.count("janus_sdc_174_warn \"") == 3  # every group guarded by an empty-collection check
    for ev in ("user_cluster_top.sv:57", "user_cluster_top.sv:208", "janus_soc_top.sv:149",
               "reset_sync.sv:20-35", "user_cluster_top.sv:164", "user_cluster_top.sv:165",
               "janus_soc_top.sv:394", "ara_integration_wrapper.sv:84-91",
               "ara_integration_wrapper.sv:255", "user_cluster_top.sv:284", "janus_soc_top.sv:549"):
        assert ev in t, ev
    # every exception / override command line carries an inline evidence comment
    for line in (_cmds(t, "set_false_path") + _cmds(t, "set_multicycle_path")
                 + [ln for ln in _cmds(t, "set_output_delay") if "busy" in ln]):
        assert re.search(r";#.*\.sv:\d+", line), line


def test_mk_d1800_refpin():
    mk = _read(MK)
    assert re.search(r"^export\s+JANUS_CTS_MACRO_OFFSET\s+=\s*1800\s*$", mk, re.M)
    assert re.search(r"^export\s+JANUS_CTS_IO_REFPIN\s+=\s*1\s*$", mk, re.M)
    assert re.search(r"^export\s+JANUS_CTS_IO_OUT_OVERRIDE\s+=\s*ara_busy_o=0\s*$", mk, re.M)
    assert re.search(r"^export\s+JANUS_CTS_IO_LATENCY_TRACK\s+=\s*1", mk, re.M)
    assert re.search(r"^export\s+LEC_CHECK\s+=\s*0\s*$", mk, re.M)
    for d in ("77.9", "39.5", "12.3", "6.8"):
        assert d in mk


def test_hook_reapplies_port_override_after_blanket():
    h = _read(HOOK)
    body = h[h.index("proc janus_cts_io_refpin"):h.index("proc janus_cts_io_out_override")]
    assert body.index("set_output_delay -clock $clk -reference_pin $best $outd $outs") \
        < body.index("janus_cts_io_out_override $clk $best")
    assert "JANUS_CTS_IO_OUT_OVERRIDE" in h


def test_probe_sdc_reload():
    sh, tcl = _read(SH), _read(TCL)
    assert "PROBE_SDC=$REPO/asic_flow/configs/constraints/$BLOCK.sdc" in sh
    assert "export PROBE_SDC" in sh and "^sdc:" in sh
    assert "JANUS_CTS_IO_OUT_OVERRIDE" in sh
    assert "read_sdc $probe_sdc" in tcl
    assert 'P "sdc: loaded $probe_sdc (exceptions' in tcl
    # reload happens before CTS
    assert tcl.index("read_sdc $probe_sdc") < tcl.index("clock_tree_synthesis {*}$cts_args")


def test_corner_fallback_chain_and_dcalc_scene():
    t = _read(TCL)
    for frag in ("sta::scenes", "sta::corners", "get_name $c", "$c name", "::env(CORNERS)",
                 "from CORNERS env", "-scene $sc", "-corner $sc", "proc probe_dcalc"):
        assert frag in t, frag
    assert "report_dcalc -from $cp" not in t  # all calls go through probe_dcalc
    # corner naming never get_property's a name string
    obj = t[t.index("proc obj_name"):t.index("set cnames {}")]
    assert "get_property" not in obj


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_tcl_files_parse():
    for p in (TCL, HOOK, SDC):
        out = subprocess.run(["tclsh"], input=f"puts [info complete [read [open {{{p}}}]]]\n",
                             capture_output=True, text=True, timeout=30)
        assert out.stdout.strip() == "1", (p, out.stderr)


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_obj_name_and_dcalc_fallback_in_tclsh():
    t = _read(TCL)
    procs = t[t.index("proc obj_name"):t.index("set cnames {}")]
    procs += t[t.index("proc dcalc_scene"):t.index("# ---- register clock latency")]
    script = procs + r"""
proc get_name { o } { error "STA-2204 get_property $o is not an object" }
proc report_dcalc { args } {
  global seen; lappend seen $args
  if { [lsearch $args -scene] >= 0 } { error "unknown -scene" }
}
puts "N:[obj_name slow]|[obj_name _5a0f_p_sta__Scene]"
set cnames {slow fast}
set seen {}
probe_dcalc a b min
puts "D:[lindex $seen end]"
"""
    out = subprocess.run(["tclsh"], input=script, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert "N:slow|" in out.stdout
    assert "D:-from a -to b -min -digits 3 -corner fast" in out.stdout
