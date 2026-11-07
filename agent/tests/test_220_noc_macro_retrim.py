"""§220: u_noc/m_i[283] hold stuck at -157.281 (run 20261106_110339, noc_hold.txt).
Cause: clk_user skew (launch 3027.1 vs u_noc/clk 3317.1 through ~97 MACRO_ALIGN
trim buffers), RSZ setup-limited by the m_i -> s_o feed-through (+49.4).
 - hooks/janus_cts_macro_retrim.tcl: re-tap u_noc/clk k stages up its own
   exclusive janus_maltrim chain, best accepted trial, prune the bypassed bufs.
 - janus_cts_balance.tcl: sourced, armed, called after LEAF_RETAP, before DIAG.
 - janus_soc_top.mk: JANUS_CTS_MACRO_RETRIM=1, PINS=u_noc/clk.
 - soc_hold_probe.tcl: section M (in-memory trial + before/after), before H1.
Static lint + tclsh pure-logic + mock-OpenROAD run. NOT an OpenROAD run.
Run: python3 -m pytest asic_flow/agent/tests/test_220_noc_macro_retrim.py -q"""
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

import _tcl_lint as tl  # noqa: E402

HOOK = ROOT / "asic_flow/scripts/hooks/janus_cts_macro_retrim.tcl"
LRT = ROOT / "asic_flow/scripts/hooks/janus_cts_leaf_retap.tcl"
BAL = ROOT / "asic_flow/scripts/hooks/janus_cts_balance.tcl"
PROBE = ROOT / "asic_flow/scripts/debug/soc_hold_probe.tcl"
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"
MOCK = HERE / "fixtures/t219a/mock_openroad.tcl"
EXT = HERE / "fixtures/t220/mock_ext.tcl"
TCLSH = shutil.which("tclsh")


# --- static -------------------------------------------------------------------------
@pytest.mark.parametrize("p", [HOOK, BAL, PROBE, EXT])
def test_lint(p):
    s = p.read_text()
    assert tl.balance(s) == (0, 0, None), p.name
    assert tl.bracket_issues(s) == [], p.name
    assert tl.write_forbidden(s) == [], p.name


def test_hook_clock_only_no_sdc():
    s = HOOK.read_text()
    code = "\n".join(l for _, l in tl.code_lines(s))
    for bad in ("set_false_path", "set_multicycle_path", "set_clock_latency",
                "set_input_delay", "set_output_delay", "read_sdc", "write_sdc",
                "set_disable_timing", "insert_buffer"):
        assert bad not in code, bad
    for need in ("janus_lrt_accept", "janus_lrt_reconnect", "janus_lrt_prune",
                 "JANUS_CTS_MACRO_RETRIM_PINS", "u_noc/clk", "^janus_maltrim"):
        assert need in s, need


def _mk(k):
    m = re.search(rf"^export {k}\s*\??=\s*(\S+)\s*$", MK.read_text(), re.M)
    return m.group(1) if m else None


def test_mk_keys():
    assert _mk("JANUS_CTS_MACRO_RETRIM") == "1"
    assert _mk("JANUS_CTS_MACRO_RETRIM_PINS") == "u_noc/clk"
    assert _mk("JANUS_CTS_MACRO_RETRIM_STEP") == "2"
    assert _mk("JANUS_CTS_MACRO_RETRIM_MAX_STAGES") == "12"
    # §219 untouched
    assert _mk("JANUS_CTS_LEAF_RETAP_CLOCKS") == "clk_agent"
    s = MK.read_text()
    assert s.index("JANUS_CTS_LEAF_RETAP_MAX") < s.index("JANUS_CTS_MACRO_RETRIM ") \
        < s.index("JANUS_CTS_HOLD_ABORT_PS")


def test_balance_wiring_order():
    s = BAL.read_text()
    assert "source [file join [file dirname [info script]] janus_cts_macro_retrim.tcl]" in s
    assert "$janus_cts_mretrim ||" in s
    assert "JANUS_CTS_MACRO_RETRIM: armed" in s
    i_lrt = s.index("if { [catch {janus_cts_leaf_retap} err] }")
    i_mrt = s.index("if { [catch {janus_cts_macro_retrim} err] }")
    i_diag = s.index("if { [catch {janus_cts_diag} err] }")
    i_ab = s.index("    janus_cts_hold_abort\n")
    assert i_lrt < i_mrt < i_diag < i_ab


def test_probe_section_m_before_h1():
    s = PROBE.read_text()
    assert 'if { [want M] }' in s
    assert s.index('hdr "M ') < s.index('hdr "H1 RSZ')
    assert s.index('hdr "R ') < s.index('hdr "M ')
    assert "janus_cts_macro_retrim.tcl" in s and "u_noc/m_i*" in s
    assert s.count("repair_timing {*}") == 1  # H1 stays the only repair


# --- tclsh: pure logic ---------------------------------------------------------------
def _procs_driver(body):
    return f"""
foreach p [list {{{LRT}}} {{{HOOK}}}] {{
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


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_pick_and_ks_tclsh():
    drv = _procs_driver("""
puts "K1=[janus_mrt_ks 97 2 12]"
puts "K2=[janus_mrt_ks 3 2 12]"
puts "K3=[janus_mrt_ks 1 2 12]"
puts "K4=[janus_mrt_ks 0 2 12]"
set q0 {-157.3 -1340 -304.5 -144057}
# k=2 better, k=4 best hold, k=6 setup worse
set t {2 {-94.3 -900 -304.5 -144057} 4 {-60.0 -500 -304.5 -144100} 6 {-30 -100 -330 -144057}}
puts "P1=[janus_mrt_pick $q0 $t 0 100]"
puts "P2=[janus_mrt_pick $q0 $t 0 100 1]"
# WNS tie (another endpoint is worst) -> higher hold TNS wins
set t {2 {-140.5 -900 -304.5 -144057} 4 {-140.5 -600 -304.5 -144057}}
puts "P3=[janus_mrt_pick $q0 $t 0 100]"
# nothing passes -> 0 + reason
set t {2 {-160 -1500 -304.5 -144057}}
puts "P4=[janus_mrt_pick $q0 $t 0 100]"
puts "P5=[janus_mrt_pick $q0 {} 0 100]"
""")
    r = subprocess.run([TCLSH], input=drv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    o = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    assert o["K1"] == "2 4 6 8 10 12"
    assert o["K2"] == "2"
    assert o["K3"] == "1"
    assert o["K4"] == ""
    assert o["P1"] == "4 {}"
    assert o["P2"] == "6 {}"
    assert o["P3"] == "4 {}"
    assert o["P4"].startswith("0 {k=2 hold")
    assert o["P5"] == "0 {no trial}"


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_hook_disabled_is_noop_tclsh():
    drv = f"""
source {{{HOOK}}}
catch {{unset ::env(JANUS_CTS_MACRO_RETRIM)}}
puts "rc=[catch {{janus_cts_macro_retrim}} e] e=$e"
"""
    r = subprocess.run([TCLSH], input=drv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0 e=" in r.stdout


# --- mock OpenROAD run of the probe (sections M + H1) --------------------------------
def _run(tmp_path, **extra):
    wd = tmp_path / "flow"
    wd.mkdir()
    (wd / "load.tcl").write_text("")
    env = dict(os.environ, SCRIPTS_DIR=str(wd), RESULTS_DIR=str(wd),
               DESIGN_NAME="janus_soc_top", SHP_LINES="10", SHP_ONLY="M H1",
               MOCK_EXT=str(EXT))
    env.pop("JANUS_POST_SCRIPT_OUT", None)
    for k in list(env):
        if k.startswith("JANUS_CTS_MACRO_RETRIM_"):
            env.pop(k)
    env.update(extra)
    before = sorted(p.name for p in wd.iterdir())
    r = subprocess.run([TCLSH, str(MOCK), str(PROBE)], capture_output=True, text=True,
                       cwd=wd, env=env, timeout=120)
    return r, wd, before


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_probe_m_accepts_best(tmp_path):
    r, wd, before = _run(tmp_path)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    o = r.stdout
    assert "######## SHP: M §220" in o and o.rstrip().endswith("MOCK_EXIT")
    assert o.index("######## SHP: M ") < o.index("######## SHP: H1 RSZ")
    assert "exclusive chain 6 stage(s)" in o
    assert "trial k=6" in o and "setup WNS worse" in o
    assert "ACCEPT k=4" in o and "pruned 4 trim buffers" in o
    assert re.search(r"SHPM u_noc/m_i\[283\]\s+hold\s+-157\.3 ->\s+-31\.3", o)
    assert re.search(r"SHPM u_noc/m_i\[280\]\s+hold\s+-152\.8 ->\s+-26\.8", o)
    assert "clk arrival min/max 3191.1/3191.1" in o
    # the m_i endpoints became H1 targets
    assert re.search(r"SHP1 u_noc/m_i\[283\]\s+hold\s+-31\.3", o)
    assert sorted(p.name for p in wd.iterdir()) == before
    assert "mock: write" not in r.stderr


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_probe_m_reverts_when_setup_tight(tmp_path):
    # any pull breaks setup: SETUP_TNS_TOL -1 rejects even an unchanged TNS
    r, _, _ = _run(tmp_path, JANUS_CTS_MACRO_RETRIM_SETUP_TNS_TOL_PS="-1")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "REVERT" in r.stdout and "ACCEPT" not in r.stdout
    assert re.search(r"SHPM u_noc/m_i\[283\]\s+hold\s+-157\.3 ->\s+-157\.3", r.stdout)


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_probe_m_dryrun(tmp_path):
    r, _, _ = _run(tmp_path, JANUS_CTS_MACRO_RETRIM_DRYRUN="1")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "exclusive chain 6 stage(s)" in r.stdout
    assert "trial k=" not in r.stdout and "accepted 0 pruned 0" in r.stdout
