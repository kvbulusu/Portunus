"""§245 janus_soc_top (run 20261113_171312: RCX S -205.9 / H -18.3, max slew 4.3 ns).
 A setup: u_ucut_s3n -> axi4_link_slice MODE 6 (MODE 5 + MODE 4 egress on AW/W/AR)
   cuts the u_user aw_valid -> ar_ready combinational loop at both pins; mr_q
   pin-fenced (ucut_s3e).
 B RC: JANUS_SOC_LAYER_RC default 0 (calibrated file ~145 ps more optimistic
   than platform RC on the worst path, diag_grt).
 C slew: janus_cfold::relocate_ties targets the macro PIN (not the macro corner)
   for ties whose load is a hard macro.
Static + tclsh mock + verilator sim. NOT an OpenROAD run.
Run: python3 -m pytest asic_flow/agent/tests/test_245_soc_top_ucut_s3n_req.py -q"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "asic_flow/utils"))

MK = ROOT / "asic_flow/configs/janus_soc_top.mk"
TOP = ROOT / "rtl/top/janus_soc_top.sv"
LINK = ROOT / "rtl/noc/axi4_link_slice.sv"
CFOLD = ROOT / "asic_flow/scripts/hooks/janus_const_fold.tcl"
TB = "tb_245_ucut_s3n_req"
TCLSH = shutil.which("tclsh")


def test_link_slice_mode6():
    s = LINK.read_text()
    assert "MODE_SM  = (MODE == 5) ? 0 : (MODE == 6) ? 4 : MODE" in s
    assert "MODE_MS5 = (MODE == 5 || MODE == 6) ? 3 : MODE" in s
    assert re.search(r"MODE_MS\s*=\s*\(MODE == 3\) \? 4 : MODE_MS5", s)
    assert "MODE inside {1, 2, 3, 5, 6}" in s


def test_top_uses_mode6_once():
    s = TOP.read_text()
    assert s.count("axi4_link_slice #(.MODE(6), .INGRESS_BYPASS(1'b1)) u_ucut_s3n (") == 1
    assert ".MODE(5)" not in s
    assert "axi4_link_slice u_ucut_s3 (" in s          # NoC-side slice unchanged


def test_tb_registered():
    rt = (ROOT / "scripts/run_tests.sh").read_text()
    assert f'[{TB}]="rtl/noc/axi4_register_slice.sv rtl/noc/axi4_link_slice.sv tb/{TB}.sv"' in rt
    assert f"tb_224_ucut_s3n {TB} tb_mailbox" in rt


def _verilator():
    v = shutil.which("verilator")
    if v:
        return v
    try:
        import verilator  # noqa: F401
        b = Path(verilator.__file__).parent / "bin/verilator"
        return str(b) if b.exists() else None
    except Exception:
        return None


@pytest.mark.skipif(_verilator() is None, reason="verilator not installed")
def test_sim_tb_245(tmp_path):
    files = ["rtl/pkg/janus_pkg.sv", "rtl/pkg/axi4_pkg.sv", "rtl/noc/axi4_register_slice.sv",
             "rtl/noc/axi4_link_slice.sv", f"tb/{TB}.sv"]
    cmd = [_verilator(), "--binary", "--timing", "-j", "4", "-Wno-fatal", "-Wno-lint", "-Wno-style",
           "-Wno-TIMESCALEMOD", "-MAKEFLAGS", "CFG_CXXFLAGS_PCH_I=-include",
           "--top-module", TB, "-Irtl/pkg", "-Irtl/cfg",
           "--Mdir", str(tmp_path), "-o", "sim", *files]
    b = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert b.returncode == 0, b.stdout[-2000:] + b.stderr[-3000:]
    r = subprocess.run([str(tmp_path / "sim")], cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert f"RESULT: PASS {TB}" in r.stdout, r.stdout[-3000:]


def _mk(k):
    m = re.search(rf"^export {k}\s*\??=\s*(.+?)\s*$", MK.read_text(), re.M)
    return m.group(1) if m else None


def test_mk_rc_default_off_and_fence():
    s = MK.read_text()
    assert re.search(r"^JANUS_SOC_LAYER_RC\s*\?=\s*0\s*$", s, re.M)
    f = _mk("JANUS_PIN_FENCE").split()
    assert "ucut_s3e:u_user:u_ucut_s3n/u_*/g_egress/mr_q*" in f
    assert "ucut_s3n:u_user:u_ucut_s3n/u_*/g_ingress/in_*" in f
    assert _mk("JANUS_PIN_FENCE_SPLIT") == "mrq:u_user:u_ucut_mn/u_r/g_egress/mr_q*:0.4"


def test_flow_state_mapping():
    import flow_state as fs
    assert fs._earliest_stage_for_keys(["JANUS_SOC_LAYER_RC"]) == "2_floorplan"
    assert fs._earliest_stage_for_keys(["JANUS_PIN_FENCE"]) is not None


def _ns():
    s = CFOLD.read_text()
    return s[:s.index("# Planning is read-only")]


@pytest.mark.skipif(TCLSH is None, reason="tclsh missing")
def test_push_out_of_box(tmp_path):
    t = tmp_path / "t.tcl"
    t.write_text(_ns() + """
foreach p {{100 50} {990 500} {500 995} {3 500} {-5 500}} {
  lassign $p x y
  puts "R [janus_cfold::push_out_of_box $x $y 0 0 1000 1000 21]"
}
""")
    out = subprocess.run([TCLSH, str(t)], capture_output=True, text=True).stdout.split("\n")
    got = [ln[2:] for ln in out if ln.startswith("R ")]
    assert got == ["100 -21", "1021 500", "500 1021", "-21 500", "-5 500"]


@pytest.mark.skipif(TCLSH is None, reason="tclsh missing")
def test_macro_pin_target_mock(tmp_path):
    """Macro (1000,0)-(2000,1000), pin centre (1001,600) on its west edge, DBU 1,
    halo 20 -> target (979,600): at the pin, just outside the macro (not its
    corner). South-edge pin of a macro on the core boundary is clamped in-core."""
    t = tmp_path / "t.tcl"
    t.write_text(_ns() + """
proc mkrect {name x0 y0 x1 y1} {
  proc $name {m} "switch \\$m {xMin {return $x0} yMin {return $y0} xMax {return $x1} yMax {return $y1}}"
}
mkrect pinr 1000 595 1002 605
mkrect pins 1500 0 1502 2
mkrect macr 1000 0 2000 1000
mkrect corer 0 0 3000 3000
proc mac {m} { switch $m { getBBox { return macr } } }
proc pin {m} { switch $m { getInst { return mac } getBBox { return pinr } } }
proc pin2 {m} { switch $m { getInst { return mac } getBBox { return pins } } }
proc blk {m} { switch $m { getCoreArea { return corer } } }
namespace eval ord { proc get_db_block {} { return blk } }
set ::env(MACRO_BLOCKAGE_HALO) 20
puts "T <[janus_cfold::macro_pin_target mac {pin} 1]>"
puts "S <[janus_cfold::macro_pin_target mac {pin2} 1]>"
puts "E <[janus_cfold::macro_pin_target mac {} 1]>"
""")
    r = subprocess.run([TCLSH, str(t)], capture_output=True, text=True)
    assert "T <979 600>" in r.stdout, r.stdout + r.stderr
    assert "S <1501 0>" in r.stdout, r.stdout + r.stderr
    assert "E <>" in r.stdout, r.stdout + r.stderr


def test_relocate_uses_pin_target_for_macros():
    s = CFOLD.read_text()
    i = s.index("proc relocate_ties")
    body = s[i:s.index("proc macro_pin_target")]
    assert "[[$li getMaster] isBlock]" in body
    assert "macro_pin_target $li $loadt $dbu" in body
    assert "macro-pin target(s)" in body
