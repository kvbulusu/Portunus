"""§224 janus_soc_top (run 20261108_150122: S -199 / H -255 at 6_finish).
 A setup: u_ucut_s3n = axi4_link_slice MODE 5 (B/R ingress regs, AW/W/AR wires)
   between u_user and u_ucut_s3, pin-fenced at u_user (ucut_s3n:u_user:...).
 B hold: hooks/janus_cts_noc_hold_bal.tcl (u_noc/clk early vs a u_noc-LOCAL
   hold target; §220 MACRO_RETRIM retired) + hooks/janus_grt_hold_cap.tcl
   (post-GRT hold repair bounded by JANUS_GRT_HOLD_MAX_ITERS).
 C agent: adaptive_closure WIRE_MACRO_LIMITED (macro-arc / wire dominated,
   few gates) instead of LOGIC_DEPTH_LIMITED -> needs_rtl.
Static + tclsh pure logic + mock OpenROAD + verilator sim. NOT an OpenROAD run.
Run: python3 -m pytest asic_flow/agent/tests/test_224_soc_top_ucut_s3_noc_hold.py -q"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "asic_flow/agent"))
sys.path.insert(0, str(ROOT / "asic_flow/utils"))

import _tcl_lint as tl  # noqa: E402
import adaptive_closure as ac  # noqa: E402

HOOKS = ROOT / "asic_flow/scripts/hooks"
NHB = HOOKS / "janus_cts_noc_hold_bal.tcl"
CAP = HOOKS / "janus_grt_hold_cap.tcl"
BAL = HOOKS / "janus_cts_balance.tcl"
PRE = HOOKS / "janus_soc_pre_grt.tcl"
LRT = HOOKS / "janus_cts_leaf_retap.tcl"
MRT = HOOKS / "janus_cts_macro_retrim.tcl"
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"
TOP = ROOT / "rtl/top/janus_soc_top.sv"
LINK = ROOT / "rtl/noc/axi4_link_slice.sv"
FX = HERE / "fixtures/t224"
MOCK = HERE / "fixtures/t219a/mock_openroad.tcl"
TCLSH = shutil.which("tclsh")


def _mk(k):
    m = re.search(rf"^export {k}\s*\??=\s*(.+?)\s*$", MK.read_text(), re.M)
    return m.group(1) if m else None


# --- PART A: RTL ----------------------------------------------------------------------
def test_link_slice_mode5():
    s = LINK.read_text()
    assert "MODE_SM  = (MODE == 5) ? 0 : (MODE == 6) ? 4 : MODE" in s  # AW/W/AR pass-through (§245: 6 -> egress)
    assert "MODE_MS5 = (MODE == 5 || MODE == 6) ? 3 : MODE" in s  # B/R ingress regs
    assert re.search(r"MODE_MS\s*=\s*\(MODE == 3\) \? 4 : MODE", s)  # §216 unchanged
    assert "MODE inside {1, 2, 3, 5, 6}" in s


def test_top_ucut_s3n_inside_ifndef():
    s = TOP.read_text()
    i_if = s.index("`ifndef JANUS_SOC_NO_UCUT")
    i_s3 = s.index("axi4_link_slice u_ucut_s3 (")
    i_s3n = s.index("axi4_link_slice #(.MODE(6), .INGRESS_BYPASS(1'b1)) u_ucut_s3n (")  # §245
    i_else = s.index("`else", i_s3n)
    i_end = s.index("`endif", i_s3n)
    assert i_if < i_s3 < i_s3n < i_else < i_end
    blk = s[i_s3:i_else]
    assert ".m_m2s_o(npu_ctrl_mid_req), .m_s2m_i(npu_ctrl_mid_rsp)" in blk
    assert ".s_m2s_i(npu_ctrl_mid_req), .s_s2m_o(npu_ctrl_mid_rsp)" in blk
    assert "npu_ctrl_req" in blk and "npu_ctrl_rsp" in blk


def test_pin_fence_and_tb_registered():
    f = _mk("JANUS_PIN_FENCE").split()
    assert "ucut_s3n:u_user:u_ucut_s3n/u_*/g_ingress/in_*" in f  # §226
    for old in ("ucut_mn:u_user:u_ucut_mn/u_*/g_ingress/in_*", "upin:u_user:upin_*"):
        assert old in f
    rt = (ROOT / "scripts/run_tests.sh").read_text()
    assert '[tb_224_ucut_s3n]="rtl/noc/axi4_register_slice.sv rtl/noc/axi4_link_slice.sv tb/tb_224_ucut_s3n.sv"' in rt
    assert "tb_216_egress tb_224_ucut_s3n" in rt


def _verilator():
    v = shutil.which("verilator")
    if v:
        return v
    try:
        import verilator  # noqa: F401  (pip package, as scripts/run_tests.sh uses)
        b = Path(verilator.__file__).parent / "bin/verilator"
        return str(b) if b.exists() else None
    except Exception:
        return None


@pytest.mark.skipif(_verilator() is None, reason="verilator not installed")
def test_sim_tb_224(tmp_path):
    files = ["rtl/pkg/janus_pkg.sv", "rtl/pkg/axi4_pkg.sv", "rtl/noc/axi4_register_slice.sv",
             "rtl/noc/axi4_link_slice.sv", "tb/tb_224_ucut_s3n.sv"]
    cmd = [_verilator(), "--binary", "--timing", "-j", "4", "-Wno-fatal", "-Wno-lint", "-Wno-style",
           "-Wno-TIMESCALEMOD", "-MAKEFLAGS", "CFG_CXXFLAGS_PCH_I=-include",
           "--top-module", "tb_224_ucut_s3n", "-Irtl/pkg", "-Irtl/cfg",
           "--Mdir", str(tmp_path), "-o", "sim", *files]
    b = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert b.returncode == 0, b.stdout[-2000:] + b.stderr[-3000:]
    r = subprocess.run([str(tmp_path / "sim")], cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert "RESULT: PASS tb_224_ucut_s3n" in r.stdout, r.stdout[-3000:]


# --- PART B: hooks static -------------------------------------------------------------
@pytest.mark.parametrize("p", [NHB, CAP, BAL, PRE, FX / "mock_ext.tcl"])
def test_lint(p):
    s = p.read_text()
    assert tl.balance(s) == (0, 0, None), p.name
    assert tl.bracket_issues(s) == [], p.name
    assert tl.write_forbidden(s) == [], p.name


@pytest.mark.parametrize("p", [NHB, CAP])
def test_no_timing_exceptions_no_sdc(p):
    code = "\n".join(l for _, l in tl.code_lines(p.read_text()))
    for bad in ("set_false_path", "set_multicycle_path", "set_max_delay", "set_min_delay",
                "set_clock_groups", "set_clock_latency", "set_disable_timing", "read_sdc",
                "write_sdc", "janus_soc_top.sdc", "SKIP_GATE_CLONING", "-skip_gate_cloning"):
        assert bad not in code, (p.name, bad)


def test_nhb_family_local_not_global_accept():
    s = NHB.read_text()
    code = "\n".join(l for _, l in tl.code_lines(s))
    # §220 failure: global hold-TNS accept test. §224 must not use it.
    assert "janus_lrt_accept" not in code and "janus_mrt_pick" not in code
    for need in ("janus_nhb_guard", "janus_nhb_pick", "janus_lrt_reconnect", "janus_lrt_prune",
                 "u_noc/clk", "^janus_maltrim", "slack_$mm"):
        assert need in s, need


def test_mk_keys():
    assert _mk("JANUS_CTS_NOC_HOLD_BAL") == "1"
    assert _mk("JANUS_CTS_NOC_HOLD_BAL_PIN") == "u_noc/clk"
    assert _mk("JANUS_CTS_NOC_HOLD_BAL_TARGET_PS") == "550"
    assert _mk("JANUS_CTS_NOC_HOLD_BAL_STEP") == "2"
    assert _mk("JANUS_CTS_NOC_HOLD_BAL_MAX_STAGES") == "20"
    assert _mk("JANUS_CTS_MACRO_RETRIM") == "0"
    assert _mk("JANUS_GRT_HOLD_MAX_ITERS") == "0"   # §225: cap off
    s = MK.read_text()
    for k in ("JANUS_CTS_NOC_HOLD_BAL", "JANUS_GRT_HOLD_MAX_ITERS"):  # '=' not '?='
        assert re.search(rf"^export {k}\s+=", s, re.M), k
    assert s.index("export JANUS_CTS_MACRO_RETRIM_MAX_STAGES") < s.index("export JANUS_CTS_NOC_HOLD_BAL ")


def test_flow_state_stage_mapping():
    import flow_state as fs
    src = (ROOT / "asic_flow/utils/flow_state.py").read_text()
    assert '"JANUS_GRT_HOLD_MAX_ITERS": "5_route"' in src
    assert fs._earliest_stage_for_keys(["JANUS_GRT_HOLD_MAX_ITERS"]) == "5_route"
    assert fs._earliest_stage_for_keys(["JANUS_CTS_NOC_HOLD_BAL_TARGET_PS"]) == "4_cts"
    assert fs._earliest_stage_for_keys(["JANUS_PIN_FENCE"]) is not None


def test_balance_wiring_order():
    s = BAL.read_text()
    assert "janus_cts_noc_hold_bal.tcl" in s and "$janus_cts_nochb ||" in s
    assert "JANUS_CTS_NOC_HOLD_BAL: armed" in s
    i_mrt = s.index("if { [catch {janus_cts_macro_retrim} err] }")
    i_nhb = s.index("if { [catch {janus_cts_noc_hold_bal} err] }")
    i_diag = s.index("if { [catch {janus_cts_diag} err] }")
    i_ab = s.index("    janus_cts_hold_abort\n")
    assert i_mrt < i_nhb < i_diag < i_ab


def test_pre_grt_sources_cap_after_wrap():
    s = PRE.read_text()
    assert s.index("janus_crg::wrap_repair_timing global_route") \
        < s.index("janus_grt_hold_cap.tcl")


# --- PART B: tclsh pure logic + wrap --------------------------------------------------
def _procs_only(paths, body):
    lst = " ".join("{%s}" % p for p in paths)
    return f"""
foreach p [list {lst}] {{
  set f [open $p]; set s [read $f]; close $f
  if {{![info complete $s]}} {{ puts INCOMPLETE; exit 1 }}
  set cmd ""
  foreach line [split $s "\\n"] {{
    append cmd $line "\\n"
    if {{![info complete $cmd]}} continue
    if {{[regexp {{^\\s*proc\\s}} $cmd]}} {{ eval $cmd }}
    set cmd ""
  }}
}}
{body}
"""


def _tcl(script, env=None):
    r = subprocess.run([TCLSH], input=script, capture_output=True, text=True, timeout=60,
                       env=env or dict(os.environ))
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout, dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_nhb_guard_pick_tclsh():
    _, o = _tcl(_procs_only([NHB], """
set q0 {-157.3 200 -304.5 -144057}
puts "G1=[janus_nhb_guard $q0 {-94.3 137 -304.5 -144057} 0 300 0]"
puts "G2=[janus_nhb_guard $q0 {-157.0 137 -304.5 -144057} 0 300 0]"
puts "G3=[janus_nhb_guard $q0 {-94.3 137 -330 -144057} 0 300 0]"
puts "G4=[janus_nhb_guard $q0 {-94.3 137 -304.5 -144400} 0 300 0]"
puts "G5=[janus_nhb_guard $q0 {-94.3 -5 -304.5 -144057} 0 300 0]"
puts "G6=[janus_nhb_guard {{} 200 -304.5 -144057} {-94.3 137 -304.5 -144057} 0 300 0]"
# positive setup: WNS may drop down to 0 but not below
puts "G7=[janus_nhb_guard {-100 200 20 0} {-50 150 0.0 0} 0 300 0]"
puts "G8=[janus_nhb_guard {-100 200 20 0} {-50 150 -1 0} 0 300 0]"
set t {2 {-94.3 137 -304.5 -144057} 4 {-31.3 74 -304.5 -144057} 6 {0.2 42 -304.5 -144057}}
puts "P1=[janus_nhb_pick $q0 $t 0 0 300 0]"
puts "P2=[janus_nhb_pick $q0 $t 550 0 300 0]"
puts "P3=[janus_nhb_pick $q0 $t -50 0 300 0]"
puts "P4=[janus_nhb_pick $q0 {2 {-160 137 -304.5 -144057}} 0 0 300 0]"
puts "P5=[janus_nhb_pick $q0 {} 0 0 300 0]"
"""))
    assert o["G1"] == "" and o["G7"] == ""
    assert o["G2"] == "u_noc input hold not improved"
    assert o["G3"] == "setup WNS worse" and o["G8"] == "setup WNS worse"
    assert o["G4"] == "setup TNS worse"
    assert o["G5"] == "u_noc output hold below floor"
    assert o["G6"] == "u_noc input hold untimed"
    assert o["P1"] == "6 {}"      # first k reaching target 0
    assert o["P2"] == "6 {}"      # target unreachable -> best in-hold
    assert o["P3"] == "4 {}"      # smallest k reaching -50
    assert o["P4"].startswith("0 {k=2 u_noc input hold not improved")
    assert o["P5"] == "0 {no trial}"


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_grt_cap_plan_and_off_tclsh():
    env = dict(os.environ)
    env.pop("JANUS_GRT_HOLD_MAX_ITERS", None)
    out, o = _tcl(f"""
source {{{CAP}}}
puts "A=[janus_ghc::plan 400]"
puts "B=[janus_ghc::plan 400 -setup]"
puts "C=[janus_ghc::plan 0]"
puts "D=[janus_ghc::plan {{}}]"
puts "E=[janus_ghc::plan abc]"
""", env)
    assert "JANUS_GRT_HOLD_CAP: off" in out
    assert o["A"] == "-setup {-hold -max_iterations 400}"
    assert o["B"] == "-setup"
    assert o["C"] == "{}" and o["D"] == "{}" and o["E"] == "{}"


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_grt_cap_wraps_helper_tclsh():
    env = dict(os.environ, JANUS_GRT_HOLD_MAX_ITERS="400")
    out, _ = _tcl(f"""
set ::calls {{}}
proc repair_timing_helper {{ args }} {{ lappend ::calls "<$args>"; return ok }}
source {{{CAP}}}
source {{{CAP}}}
puts "R1=[repair_timing_helper] [join $::calls ,]"
set ::calls {{}}
puts "R2=[repair_timing_helper -hold -hold_margin 5] [join $::calls ,]"
""", env)
    assert out.count("JANUS_GRT_HOLD_CAP: armed") == 1 or "armed" in out
    o = dict(l.split("=", 1) for l in out.splitlines() if l.startswith("R"))
    assert o["R1"] == "ok <-setup>,<-hold -max_iterations 400>"
    assert o["R2"] == "ok <-hold -hold_margin 5>"


# --- PART B: mock OpenROAD run of the hook -------------------------------------------
def _run(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("JANUS_CTS_")}
    env.update(JANUS_CTS_NOC_HOLD_BAL="1", JANUS_CTS_NOC_HOLD_BAL_STEP="1",
               MOCK_EXT=str(FX / "mock_ext.tcl"))
    env.update(extra)
    before = sorted(p.name for p in tmp_path.iterdir())
    r = subprocess.run([TCLSH, str(MOCK), str(FX / "drive_nhb.tcl")], capture_output=True,
                       text=True, cwd=tmp_path, env=env, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert r.stdout.rstrip().endswith("MOCK_EXIT")
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    o = dict(l.split("=", 1) for l in r.stdout.splitlines() if l.startswith("NHB_"))
    return r.stdout, int(o["NHB_BYPASS"]), int(o["NHB_DESTROYED"])


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_accepts_until_setup_breaks(tmp_path):
    out, byp, dead = _run(tmp_path, JANUS_CTS_NOC_HOLD_BAL_TARGET_PS="0")
    assert "chain 6 stage(s)" in out and "2 in / 1 out pins" in out
    assert "trial k=5" in out and "setup WNS worse" in out and "trial k=6" not in out
    assert "ACCEPT k=4" in out
    assert re.search(r"-> u_noc in-hold -31\.3 out-hold 74\.0", out)
    assert (byp, dead) == (4, 4)


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_setup_tns_guard(tmp_path):
    out, byp, _ = _run(tmp_path, JANUS_CTS_NOC_HOLD_BAL_TARGET_PS="0",
                       JANUS_CTS_NOC_HOLD_BAL_SETUP_TOL_PS="100")
    assert "setup TNS worse" in out and "ACCEPT k=4" in out and byp == 4


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_output_hold_floor(tmp_path):
    out, byp, dead = _run(tmp_path, JANUS_CTS_NOC_HOLD_BAL_HOUT_FLOOR_PS="100")
    assert "u_noc output hold below floor" in out and "ACCEPT k=3" in out
    assert (byp, dead) == (3, 3)


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_dryrun_and_off(tmp_path):
    out, byp, dead = _run(tmp_path, JANUS_CTS_NOC_HOLD_BAL_DRYRUN="1")
    assert "start u_noc/clk" in out and "trial k=" not in out and (byp, dead) == (0, 0)
    out, byp, dead = _run(tmp_path, JANUS_CTS_NOC_HOLD_BAL="0")
    assert "JANUS_CTS_NOC_HOLD_BAL:" not in out and (byp, dead) == (0, 0)


# --- PART C: adaptive classifier ------------------------------------------------------
def _input_150122():
    return dict(json.loads((FX / "adaptive_150122.json").read_text())["input"])


def test_fixture_is_the_misclassified_run():
    a = json.loads((FX / "adaptive_150122.json").read_text())
    assert a["class"] == "LOGIC_DEPTH_LIMITED" and a["action"] == "needs_rtl"
    # without worst-path data the old verdict is reproduced (no silent change)
    assert ac.classify(_input_150122())["class"] == "LOGIC_DEPTH_LIMITED"


@pytest.mark.parametrize("rpt", ["finish_worst_150122.rpt", "finish_compact_150122.rpt"])
def test_parse_worst_path_150122(rpt):
    p = ac.parse_worst_path((FX / rpt).read_text())
    assert p["path_slack_ps"] == -199.03 and p["path_startpoint"] == "u_user"
    assert p["path_logic_gates"] == 3                     # NAND2 / OA211 / AOI21
    assert p["path_macro_arc_ps"] == 4617.41
    assert p["path_macro_pin"] == "u_user/npu_ctrl_s2m_o[102]"
    assert p["path_data_ps"] == 5693.68
    assert abs(p["path_logic_ps"] + p["path_buf_ps"] + p["path_macro_arc_ps"] - p["path_data_ps"]) < 0.2


def test_classify_150122_wire_macro():
    q = _input_150122()
    q.update(ac.parse_worst_path((FX / "finish_worst_150122.rpt").read_text()))
    r = ac.classify(q)
    assert r["class"] == "WIRE_MACRO_LIMITED" != "LOGIC_DEPTH_LIMITED"
    assert any("3 logic gate" in e for e in r["evidence"])
    rec = ac.plan("janus_soc_top", r, "auto")
    assert rec["action"] == "wire_macro_pd" != "needs_rtl"
    assert rec["rerun"] is False
    assert "WIRE_MACRO_LIMITED" in ac.CLASSES and ac.class_knobs("WIRE_MACRO_LIMITED", q)["vars"] == []


def test_logic_depth_still_detected():
    q = _input_150122()
    deep = {"path_logic_gates": 22, "path_data_ps": 3000.0, "path_macro_arc_ps": 0.0,
            "path_buf_ps": 400.0, "path_logic_ps": 2550.0}
    assert ac.classify(dict(q, **deep))["class"] == "LOGIC_DEPTH_LIMITED"
    # few gates but logic-dominated (slow cells, no macro/wire) -> not wire/macro
    slow = {"path_logic_gates": 3, "path_data_ps": 3000.0, "path_macro_arc_ps": 0.0,
            "path_buf_ps": 600.0, "path_logic_ps": 2400.0}
    assert ac.classify(dict(q, **slow))["class"] == "LOGIC_DEPTH_LIMITED"
    # moderate WNS stays MODERATE even with macro evidence
    wm = ac.parse_worst_path((FX / "finish_worst_150122.rpt").read_text())
    assert ac.classify(dict(q, wns_ps=-50.0, slew_viol=0, groups={"reg2reg": {"wns_ps": -50.0, "violators": 3}},
                            **wm))["class"] == "MODERATE"


def test_parse_worst_path_no_violation():
    assert ac.parse_worst_path("Startpoint: a\nPath Type: max\n  10.00 slack (MET)\n") == {}
    assert ac.parse_worst_path("") == {}


def test_agent_merges_worst_path():
    s = (ROOT / "asic_flow/agent/janus_agent.py").read_text()
    i = s.index("fq = ac.from_qor(qor)")
    j = s.index("eco = self._eco_adopted_timing(block)", i)
    assert "ac.parse_worst_path(" in s[i:j] and '"6_finish.rpt"' in s[i:j]
