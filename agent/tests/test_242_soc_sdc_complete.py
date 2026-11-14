"""§242 soc_top constraint completion, §243 tie relocation, §244 margin/RC policy."""
import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ASIC = os.path.abspath(os.path.join(HERE, "..", ".."))
HOOKS = os.path.join(ASIC, "scripts", "hooks")
HOOK = os.path.join(HOOKS, "janus_soc_sdc_complete.tcl")
CFOLD = os.path.join(HOOKS, "janus_const_fold.tcl")
MK = os.path.join(ASIC, "configs", "janus_soc_top.mk")
ORFS = os.environ.get("ORFS_DIR", "/home/ubuntu/OpenROAD-flow-scripts")
OR_BIN = os.path.join(ORFS, "tools", "OpenROAD", "build", "bin", "openroad")
GCD = os.path.join(ORFS, "flow", "results", "asap7", "gcd", "base")

sys.path.insert(0, os.path.join(ASIC, "utils"))
import flow_state  # noqa: E402


def _mk():
    return open(MK).read()


def _mkval(name):
    m = re.search(rf"^export {name}\s*=\s*(\S*)\s*$", _mk(), re.M)
    assert m, name
    return m.group(1)


# ---------------------------------------------------------------- static ----
def test_hook_wired_post_floorplan_post_dpl_and_after_fold():
    pf = open(os.path.join(HOOKS, "post_floorplan.tcl")).read()
    assert "janus_soc_sdc_complete.tcl" in pf
    # before the stage dump (and therefore before floorplan.tcl's orfs_write_sdc)
    assert pf.index("janus_soc_sdc_complete.tcl] }") < pf.index("] janus_stage_dump.tcl]")
    pd = open(os.path.join(HOOKS, "janus_post_dpl_repair.tcl")).read()
    assert pd.index("janus_soc_sdc_complete.tcl") < pd.rindex("janus_pdr::run")
    cf = open(CFOLD).read()
    assert cf.rindex("janus_soc_sdc_complete.tcl] }") > cf.index("catch {janus_cfold::run} err")


def test_mk_knobs():
    assert _mkval("JANUS_SOC_SDC_COMPLETE") == "1"
    assert _mkval("JANUS_SOC_SDC_DRIVE_CELL") == "BUFx4_ASAP7_75t_R/Y"
    assert _mkval("JANUS_CONST_FOLD_CASE_IGNORE") == "u_ucut_s3n"
    assert float(_mkval("JANUS_TIE_RELOCATE_UM")) == 20
    # §244 decision: no blanket repair margin on top of the SDC uncertainty
    assert _mkval("SETUP_SLACK_MARGIN") == "0"
    assert _mkval("HOLD_SLACK_MARGIN") == "0"
    s = _mk()
    assert "JANUS_SOC_LAYER_RC_FILE ?= $(JANUS_RTL_ROOT)/asic_flow/configs/rc/janus_soc_top_setRC.tcl" in s
    assert "export LAYER_PARASITICS_FILE = $(JANUS_SOC_LAYER_RC_FILE)" in s


def test_sdc_not_edited_by_hook():
    sdc = open(os.path.join(ASIC, "configs", "constraints", "janus_soc_top.sdc")).read()
    assert "janus_soc_sdc_complete" not in sdc


@pytest.mark.parametrize("key,stage", [
    ("JANUS_SOC_SDC_COMPLETE", "2_floorplan"),
    ("JANUS_SOC_SDC_DRIVE_CELL", "2_floorplan"),
    ("JANUS_SOC_LAYER_RC", "2_floorplan"),
    ("LAYER_PARASITICS_FILE", "2_floorplan"),
    ("JANUS_TIE_RELOCATE_UM", "3_place"),
    ("JANUS_CONST_FOLD_CASE_IGNORE", "3_place"),
])
def test_flow_state_stage(key, stage):
    assert flow_state._earliest_stage_for_keys([key]) == stage


def test_rtl_constant_bit_map_matches_pkg():
    """B = {id, resp[1:0], user[3:0]}, R = {id, data, resp[1:0], last, user[3:0]}
    packed LSB-first: case bits must be user=0, (R) last=1, resp=0."""
    pkg = open(os.path.join(ASIC, "..", "rtl", "pkg", "axi4_pkg.sv")).read()
    b = re.search(r"typedef struct packed \{([^}]*)\}\s*axi_b_t", pkg).group(1)
    r = re.search(r"typedef struct packed \{([^}]*)\}\s*axi_r_t", pkg).group(1)
    fields = lambda t: re.findall(r"\b(\w+);", t)  # noqa: E731
    assert fields(b) == ["id", "resp", "user"]
    assert fields(r) == ["id", "data", "resp", "last", "user"]
    assert re.search(r"AXI_RESP_W\s*=\s*2\b", pkg)
    jp = open(os.path.join(ASIC, "..", "rtl", "pkg", "janus_pkg.sv")).read()
    assert re.search(r"JANUS_USER_WIDTH\s*=\s*4\b", jp)
    hook = open(HOOK).read()
    assert "{0 0 1 0 2 0 3 0 4 0 5 0}" in hook
    assert "{0 0 1 0 2 0 3 0 4 1 5 0 6 0}" in hook
    bridge = open(os.path.join(ASIC, "..", "rtl", "noc", "axi4_to_axil_bridge.sv")).read()
    assert "s_s2m_o.r.last   = 1'b1" in bridge


# ------------------------------------------------------------- OpenROAD ----
needs_or = pytest.mark.skipif(not (os.path.exists(OR_BIN) and os.path.exists(GCD + "/4_cts.odb")),
                              reason="sandbox OpenROAD/gcd not available")


def _run(tmp_path, body, env_extra=None):
    tcl = tmp_path / "t.tcl"
    tcl.write_text(f"""
foreach l [glob {ORFS}/flow/platforms/asap7/lib/NLDM/*RVT_FF_nldm*] {{ catch {{read_liberty $l}} }}
read_db {GCD}/4_cts.odb
read_sdc {GCD}/4_cts.sdc
{body}
""")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = (f"{ORFS}/dependencies/lib:{ORFS}/dependencies/or-tools/lib:"
                              + env.get("LD_LIBRARY_PATH", ""))
    for k in list(env):
        if k.startswith(("JANUS_SOC_SDC", "JANUS_TIE_", "JANUS_CONST_FOLD")):
            del env[k]
    env.update(env_extra or {})
    p = subprocess.run([OR_BIN, "-no_init", "-exit", str(tcl)], capture_output=True,
                       text=True, timeout=150, env=env, cwd=tmp_path)
    return p.stdout + p.stderr


GCD_TABLES = """
set ::janus_ssc_lib_only 1
source %s
set ::janus_ssc::in_map {{{req_msg*} core_clock 12.5} {nosuch* core_clock 1} {reset nosuchclk 1}}
# gcd: u135/D fan-in = u124 only, u136/D fan-in = u79 (not u124) -> must be skipped
set ::janus_ssc::const_d {{{^u13([56])$} u124 {5 1 6 0}}}
""" % HOOK


@needs_or
def test_disabled_is_noop(tmp_path):
    out = _run(tmp_path, f"source {HOOK}\nwrite_sdc out.sdc\n", {"JANUS_SOC_SDC_COMPLETE": "0"})
    assert "JANUS_SDC_COMPLETE" not in out
    assert "set_driving_cell" not in (tmp_path / "out.sdc").read_text()


@needs_or
def test_applies_and_is_idempotent(tmp_path):
    body = GCD_TABLES + """
janus_ssc::run
write_sdc a.sdc
janus_ssc::run
write_sdc b.sdc
puts "SIM135 [sta::pin_sim_logic_value [get_pins u135/D]]"
puts "SIM136 [sta::pin_sim_logic_value [get_pins u136/D]]"
"""
    out = _run(tmp_path, body, {"JANUS_SOC_SDC_COMPLETE": "1"})
    assert "input_delay 32 port(s), case_analysis 1 pin(s), driving_cell 35 input(s)" in out, out[-3000:]
    assert "WARN clock 'nosuchclk' missing" in out and "'nosuch*' matched nothing" in out
    assert "skip u136/D (fan-in not only u124)" in out
    assert re.search(r"^SIM135 1$", out, re.M) and not re.search(r"^SIM136 [01]$", out, re.M)
    a = (tmp_path / "a.sdc").read_text()
    b = (tmp_path / "b.sdc").read_text()
    assert a == b
    assert re.search(r"set_input_delay 12\.5\d* -clock \[get_clocks \{core_clock\}\].*req_msg\[0\]", a)
    assert re.search(r"set_case_analysis 1 \[get_pins \{u135/D\}\]", a)
    assert "set_driving_cell" in a and "BUFx4_ASAP7_75t_R" in a
    drv = [ln for ln in a.splitlines() if ln.startswith("set_driving_cell")]
    assert not any("{clk}" in ln for ln in drv)            # clock source excluded


@needs_or
def test_bad_drive_cell_skips_only_drive(tmp_path):
    out = _run(tmp_path, GCD_TABLES + "janus_ssc::run\n",
               {"JANUS_SOC_SDC_COMPLETE": "1", "JANUS_SOC_SDC_DRIVE_CELL": "NOPE_X1/Y"})
    assert "lib cell 'NOPE_X1' not loaded" in out
    assert "driving_cell 0 input(s)" in out


@needs_or
def test_const_fold_case_ignore(tmp_path):
    sdc = tmp_path / "2_floorplan.sdc"
    sdc.write_text("set_case_analysis 0 [get_pins {u_ucut_s3n.u_b.g_ingress.in_d_q[0]$_DFF_PN0_/D}]\n")
    body = f"""
set ::env(RESULTS_DIR) {tmp_path}
source {CFOLD}
puts "HAS<[janus_cfold::sdc_has_case]>"
"""
    out = _run(tmp_path, body, {"JANUS_CONST_FOLD": "0"})
    assert f"HAS<{sdc}>" in out
    out = _run(tmp_path, body, {"JANUS_CONST_FOLD": "0", "JANUS_CONST_FOLD_CASE_IGNORE": "u_ucut_s3n"})
    assert "HAS<>" in out
    sdc.write_text(sdc.read_text() + "set_case_analysis 0 [get_ports scan_en]\n")
    out = _run(tmp_path, body, {"JANUS_CONST_FOLD": "0", "JANUS_CONST_FOLD_CASE_IGNORE": "u_ucut_s3n"})
    assert f"HAS<{sdc}>" in out


@needs_or
def test_tie_relocation(tmp_path):
    body = f"""
set blk [ord::get_db_block]
set m [[ord::get_db] findMaster TIEHIx1_ASAP7_75t_R]
set ld [$blk findInst u135]
set t [odb::dbInst_create $blk $m far_tie]
lassign [$ld getLocation] lx ly
$t setLocation [expr {{$lx + 300000}}] $ly
$t setPlacementStatus PLACED
set n [odb::dbNet_create $blk far_tie_n]
[$t findITerm H] connect $n
set it [$ld findITerm SE]
if {{$it eq "NULL"}} {{ set it [$ld findITerm D] }}
$it disconnect
$it connect $n
source {CFOLD}
lassign [$t getLocation] tx ty
puts "MOVED [expr {{$tx == $lx && $ty == $ly}}]"
"""
    env = {"JANUS_CONST_FOLD": "0", "JANUS_CONST_FOLD_TIEHI": "TIEHIx1_ASAP7_75t_R H",
           "JANUS_CONST_FOLD_TIELO": "TIELOx1_ASAP7_75t_R L"}
    out = _run(tmp_path, body, dict(env, JANUS_TIE_RELOCATE_UM="20"))
    assert "MOVED 1" in out, out[-3000:]
    assert re.search(r"JANUS_TIE_RELOC: moved [1-9]\d* tie cell\(s\) > 20 um", out)
    out = _run(tmp_path, body, env)                              # knob unset = off
    assert "MOVED 0" in out and "JANUS_TIE_RELOC" not in out
