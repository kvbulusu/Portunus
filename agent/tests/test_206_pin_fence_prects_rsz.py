"""§206 (run 20261104_113025, sap_geom on 3_place / 4_cts): the 4_cts worst
paths (-1261.9 u_user m_m2s_o[21] -> u_ucut_m, msip -904.3) each carry one
unbuffered 710/754 um stage around u_user's NE corner; the u_user pins sit on
the far-west NORTH edge. Fixes:
  RTL  - u_ucut_mn pin-side link slice before u_ucut_m; clint_msip registered.
  PD   - janus_pin_fence.tcl fences those regs next to the u_user pins;
         janus_cts_pre_rsz.tcl runs repair_design + DPL before CTS.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
PF = HOOKS / "janus_pin_fence.tcl"
RSZ = HOOKS / "janus_cts_pre_rsz.tcl"
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
TOP = (ROOT / "rtl/top/janus_soc_top.sv").read_text()
BAL = (HOOKS / "janus_cts_balance.tcl").read_text()
ADP = (HOOKS / "janus_adaptive_pre.tcl").read_text()
FIXD = Path(__file__).resolve().parent / "fixtures" / "t198d"
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")
GCD = Path(os.environ.get("JANUS_TEST_GCD",
                          "/home/ubuntu/OpenROAD-flow-scripts/flow/results/asap7/gcd/base"))


def _need_or():
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")


def test_rtl_pin_side_cut_and_msip_reg():
    cut = TOP[TOP.index("`ifndef JANUS_SOC_NO_UCUT"):TOP.index("`else", TOP.index("`ifndef JANUS_SOC_NO_UCUT"))]
    assert re.search(r"axi4_link_slice u_ucut_mn \(.*?\.s_m2s_i\(user_m_req\),\s*\.s_s2m_o\(user_m_rsp\),"
                     r"\s*\.m_m2s_o\(user_m_mid_req\), \.m_s2m_i\(user_m_mid_rsp\)\);", cut, re.S)
    assert re.search(r"axi4_link_slice u_ucut_m \(.*?\.s_m2s_i\(user_m_mid_req\), \.s_s2m_o\(user_m_mid_rsp\),"
                     r"\s*\.m_m2s_o\(noc_m_req\[0\]\)", cut, re.S)
    assert ".clint_msip_i   (clint_msip_q)" in TOP
    assert "clint_msip_q <= clint_msip;" in TOP
    assert ".clint_mtip_i   (clint_mtip_q)" in TOP          # §205 kept


def test_mk_and_wiring():
    assert re.search(r"^export PRE_GLOBAL_PLACE_TCL\s*=.*hooks/janus_pin_fence\.tcl$", MK, re.M)
    m = re.search(r"^export JANUS_PIN_FENCE\s*=\s*(.+)$", MK, re.M)
    assert m and m.group(1).split() == ["ucut_mn:u_user:u_ucut_mn/*", "msip_q:u_user:clint_msip_q*"]
    assert re.search(r"^export JANUS_CTS_PRE_RSZ\s*=\s*1$", MK, re.M)
    assert re.search(r"^export JANUS_CTS_PRE_RSZ_MAX_WIRE_UM\s*=\s*400$", MK, re.M)
    i = BAL.index("janus_cts_pre_rsz.tcl")
    assert i < BAL.index("rename clock_tree_synthesis janus_orig_clock_tree_synthesis")
    assert "janus_pin_fence.tcl" in ADP
    assert "set_dont_use $::env(DONT_USE_CELLS)" in RSZ.read_text()
    # tap-split GROUP still covers the new slice
    assert re.search(r"^export JANUS_CTS_TAP_SPLIT_GROUP\s*=\s*u_ucut_\*$", MK, re.M)


def test_strip_geometry_tclsh():
    if not shutil.which("tclsh"):
        pytest.skip("tclsh missing")
    t = f"""
proc ord::get_db_block {{}} {{}}
source {PF}
set m {{40 80 1320 1360}}
set core {{20 20 2440 1420}}
puts "E [janus_pf::edge_of 53 1360 $m] [janus_pf::edge_of 1320 1000 $m] [janus_pf::edge_of 1101 80 $m] [janus_pf::edge_of 40 500 $m]"
puts "N [janus_pf::strip N 41 849 $m $core {{}} 20 40]"
puts "C [janus_pf::strip N 41 849 $m $core {{{{500 1390 600 1500}}}} 20 40]"
puts "W [janus_pf::strip W 500 600 $m $core {{}} 20 40]"
puts "X [janus_pf::strip E 100 200 $m $core {{{{1320 0 1400 2000}}}} 20 40]"
"""
    r = subprocess.run(["tclsh"], input="namespace eval ord {}\n" + t, capture_output=True, text=True)
    out = r.stdout + r.stderr
    assert "E N E S W" in out, out
    assert "N 21 1360 869 1400" in out, out            # pin span 41..849 +/- 20, 40 deep
    assert "C 21 1360 869 1390" in out, out            # depth trimmed by macro above
    assert "W 20 480 40 620" in out, out               # clipped at core x0
    assert re.search(r"^X\s*$", out, re.M), out         # fully blocked -> empty


def test_toy_pin_fence_places_reg_next_to_macro_pin(tmp_path):
    _need_or()
    d = tmp_path / "t206"
    shutil.copytree(FIXD, d)
    base = (d / "t.tcl").read_text().split("global_placement")[0]
    (d / "pf.tcl").write_text(base + f"""
source {PF}
global_placement -skip_io -density 0.4
detailed_placement
check_placement
lassign [[$b findInst rx] getLocation] x y
puts [format "T206 rx %.1f %.1f" [expr {{$x/1000.0}}] [expr {{$y/1000.0}}]]
write_db pf.odb
""")
    (d / "rd.tcl").write_text(f"""
read_lef {PLAT}/lef/asap7_tech_1x_201209.lef
read_lef {PLAT}/lef/asap7sc7p5t_28_R_1x_220121a.lef
read_lef m.lef
read_db pf.odb
set r [[ord::get_db_block] findRegion fx]
puts "T206 readback [$r getRegionType] groups [llength [$r getGroups]] insts [llength [[lindex [$r getGroups] 0] getInsts]]"
""")
    env = dict(os.environ, T198_PLATFORM=PLAT, JANUS_PIN_FENCE="fx:u_m:rx")
    r = subprocess.run([OR, "-no_init", "-exit", "pf.tcl"], cwd=d, env=env,
                       capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    # u_m at (230,230) 20x20; rx/D <- u_m/dout pin on the W edge at y=242.05
    assert "JANUS_PIN_FENCE: fx edge W pins 1 strip (190.0,222.1)-(230.0,262.1) um" in out, out[-3000:]
    m = re.search(r"T206 rx ([\d.]+) ([\d.]+)", out)
    assert m, out[-3000:]
    x, y = float(m.group(1)), float(m.group(2))
    assert 190.0 <= x <= 230.0 and 222.0 <= y <= 262.1, (x, y)
    r2 = subprocess.run([OR, "-no_init", "-exit", "rd.tcl"], cwd=d, capture_output=True,
                        text=True, timeout=120)
    assert "T206 readback INCLUSIVE groups 1 insts 1" in r2.stdout + r2.stderr, r2.stdout[-2000:]


def test_pin_fence_noop_and_bad_spec(tmp_path):
    _need_or()
    d = tmp_path / "t206n"
    shutil.copytree(FIXD, d)
    base = (d / "t.tcl").read_text().split("global_placement")[0]
    (d / "pf.tcl").write_text(base + f"""
source {PF}
puts "T206 regions [llength [$b getRegions]]"
""")
    for spec, want in (("", "T206 regions 0"),
                       ("bad nomatch:u_m:zz* nomac:u_q:rx", "T206 regions 0")):
        env = dict(os.environ, T198_PLATFORM=PLAT, JANUS_PIN_FENCE=spec)
        r = subprocess.run([OR, "-no_init", "-exit", "pf.tcl"], cwd=d, env=env,
                           capture_output=True, text=True, timeout=180)
        out = r.stdout + r.stderr
        assert want in out, out[-2000:]
        if spec:
            assert "WARN bad spec 'bad'" in out and "matched 0 insts" in out \
                and "macro u_q not found" in out, out[-2000:]


def test_pre_rsz_on_gcd_fixes_slew(tmp_path):
    _need_or()
    if not (GCD / "3_place.odb").exists():
        pytest.skip("gcd 3_place.odb not available")
    t = tmp_path / "rsz.tcl"
    t.write_text(f"""
read_lef {PLAT}/lef/asap7_tech_1x_201209.lef
read_lef {PLAT}/lef/asap7sc7p5t_28_R_1x_220121a.lef
foreach l [glob {PLAT}/lib/NLDM/asap7sc7p5t_*RVT_FF_nldm*] {{ read_liberty $l }}
read_db {GCD}/3_place.odb
read_sdc {GCD}/3_place.sdc
source {PLAT}/setRC.tcl
set_max_transition 45 [current_design]
source {RSZ}
""")
    env = dict(os.environ, JANUS_CTS_PRE_RSZ="1", SLEW_MARGIN="20", CAP_MARGIN="20",
               DONT_USE_CELLS="*x1p*_ASAP7* *sdf*")
    r = subprocess.run([OR, "-no_init", "-exit", str(t)], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    b = re.search(r"JANUS_CTS_PRE_RSZ: before slew_viol (\d+)", out)
    a = re.search(r"JANUS_CTS_PRE_RSZ: after  slew_viol (\d+)", out)
    assert b and a, out[-3000:]
    assert int(b.group(1)) > 0 and int(a.group(1)) == 0, out[-3000:]
    assert "JANUS_CTS_PRE_RSZ: WARN" not in out
    # disabled = no-op
    env["JANUS_CTS_PRE_RSZ"] = "0"
    r = subprocess.run([OR, "-no_init", "-exit", str(t)], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=300)
    assert "JANUS_CTS_PRE_RSZ" not in r.stdout + r.stderr
