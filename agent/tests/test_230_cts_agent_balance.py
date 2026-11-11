"""§230 clk_agent macro clock-branch delay (hooks/janus_cts_agent_balance.tcl).

Run 20261109_210551: pre-repair hold -320.1 u_periph/u_ampo -> u_done_irq_sync
(u_ampo/agent_clk 674.9 vs capture 1364.6), 12673 hold buffers. Tests the pure
decision procs and the trial/accept/revert loop with mocked OpenROAD
primitives (no real timing numbers; NOT PD validation)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow" / "scripts" / "hooks"
CAB = HOOKS / "janus_cts_agent_balance.tcl"
BAL = HOOKS / "janus_cts_balance.tcl"
MK = ROOT / "asic_flow" / "configs" / "janus_soc_top.mk"
TCLSH = shutil.which("tclsh")


def _mk(k):
    m = re.search(rf"^export {k}\s*\??=\s*(.*)$", MK.read_text(), re.M)
    return m.group(1).strip() if m else None


def test_mk_knobs():
    assert _mk("JANUS_CTS_AGENT_BAL") == "1"
    assert _mk("JANUS_CTS_AGENT_BAL_PINS") == "u_periph/u_ampo/agent_clk u_agent/agent_clk"
    assert _mk("JANUS_CTS_AGENT_BAL_SETUP_TOL_PS") == "0"
    assert _mk("JANUS_CTS_AGENT_BAL_RETAP") == "1"


def test_wired_after_noc_hold_bal_before_diag():
    s = BAL.read_text()
    assert "janus_cts_agent_balance.tcl" in s and "$janus_cts_agbal ||" in s
    assert "JANUS_CTS_AGENT_BAL: armed" in s
    i_n = s.index("if { [catch {janus_cts_noc_hold_bal} err] }")
    i_a = s.index("if { [catch {janus_cts_agent_balance} err] }")
    i_d = s.index("if { [catch {janus_cts_diag} err] }")
    assert i_n < i_a < i_d


def test_hook_no_exceptions_no_sdc():
    s = CAB.read_text()
    for bad in ("set_false_path", "set_multicycle_path", "set_max_delay", "read_sdc", "write_sdc"):
        assert bad not in s


PRELUDE = f"""
proc puts_ {{ args }} {{}}
source {{{HOOKS / 'janus_cts_leaf_retap.tcl'}}}
proc janus_nhb_pins {{ s }} {{ return {{}} }}
source {{{CAB}}}
"""


def _tcl(body, env=None):
    r = subprocess.run([TCLSH], input=PRELUDE + body, capture_output=True, text=True,
                       env=env or dict(os.environ), timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_pure_guard_want_k_pick():
    out = _tcl("""
set q0 {-320.1 -216886.4 -611.2 -44469.2 3955}
puts "G1=[janus_cab_guard $q0 {-288.8 -190000 -611.2 -44469.2 3700} 0 1]"
puts "G2=[janus_cab_guard $q0 {-288.8 -190000 -612.0 -44469.2 3700} 0 1]"
puts "G3=[janus_cab_guard $q0 {-288.8 -190000 -611.2 -44480.0 3700} 0 1]"
puts "G4=[janus_cab_guard $q0 {-330.0 -190000 -611.2 -44469.2 3700} 0 1]"
puts "G5=[janus_cab_guard $q0 {-288.8 -190000 -611.2 -44469.2 3999} 0 1]"
puts "G6=[janus_cab_guard $q0 {-320.1 -216886.0 -611.2 -44469.2 3955} 0 1]"
puts "G7=[janus_cab_guard $q0 {-288.8 -190000 -611.2 -44469.2 {}} 0 1]"
puts "W1=[janus_cab_want -320.1 2000 100 300 700]"
puts "W2=[janus_cab_want -320.1 500 100 300 700]"
puts "W3=[janus_cab_want 150 2000 100 300 700]"
puts "W4=[janus_cab_want -50 200 100 300 700]"
puts "W5=[janus_cab_want {} 2000 100 300 700]"
puts "K1=[janus_cab_k 420.1 17.5 40] K2=[janus_cab_k 2000 17.5 40] K3=[janus_cab_k 30 {} 40]"
puts "P=[janus_cab_pick $q0 {4 {-300 -200000 -611.2 -44469.2 3900} 24 {-288 -150000 -611.2 -44469.2 3000} 40 {-250 -140000 -700 -50000 2900}} 0 1]"
puts "R=[janus_cab_pick $q0 {4 {-330 -200000 -611.2 -44469.2 3900}} 0 1]"
""")
    o = dict(l.split("=", 1) for l in out.splitlines() if "=" in l and l[:1] in "GWKPR")
    assert o["G1"] == "" and o["G7"] == ""
    assert o["G2"] == "setup WNS worse" and o["G3"] == "setup TNS worse"
    assert o["G4"] == "hold WNS worse" and o["G5"] == "hold violators up"
    assert o["G6"] == "hold not improved"
    assert float(o["W1"]) == pytest.approx(420.1) and float(o["W2"]) == pytest.approx(200)
    assert o["W3"] == "" and o["W4"] == "" and o["W5"] == ""
    assert "K1=25 K2=40 K3=2" in out
    assert o["P"] == "24 {}"
    assert o["R"].startswith("0 {k=4 hold WNS worse")


# toy model: delay d = 17.5 ps/buf on u_ampo; macro-launched hold improves by d
# until setup out of the macro is hit (beyond k_setup bufs global setup WNS drops)
MOCK = """
set ::nb 0
proc janus_cab_iterm { s } { return IT }
proc janus_cab_arrival { it } { return [expr {674.9 + 17.5 * $::nb}] }
proc janus_cab_macro_out { s } { return {-320.1 2000.0} }
set ::inv 0
proc janus_cab_grow { spec it n cell } {
  if { $::inv } { error "EST-0104" }
  incr ::nb $n; lassign $::janus_cab_chain($spec) a b
  for {set i 0} {$i < $n} {incr i} { lappend b x }; set ::janus_cab_chain($spec) [list $a $b] }
proc janus_cab_undo { spec it } { set ::nb 0; set ::inv 1; set ::janus_cab_chain($spec) [list N {}] }
proc janus_cab_sync { } { set ::inv 0 }
proc janus_cab_qor { } {
  set d [expr {17.5 * $::nb}]
  set hw [expr {max(-320.1 + $d, -288.8)}]
  set ht [expr {-216886.4 + 40.0 * $d}]
  set sw [expr {$::nb > $::kset ? -611.2 - ($::nb - $::kset) : -611.2}]
  return [list $hw $ht $sw -44469.2 [expr {3955 - $::nb}]]
}
proc IT_getNet {} { return N }
rename janus_lrt_null _n; proc janus_lrt_null { o } { return 0 }
proc IT { m args } { return N }
"""


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_loop_accepts_calibrated_k():
    out = _tcl(MOCK + "set ::kset 99\nputs \"KB=[janus_cab_one u_periph/u_ampo/agent_clk BUF]\"\n")
    assert "trial k=4" in out and "trial k=25" in out
    assert "ACCEPT k=25" in out and "KB=25" in out


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_loop_backs_off_on_setup_guard():
    out = _tcl(MOCK + "set ::kset 12\nputs \"KB=[janus_cab_one u_periph/u_ampo/agent_clk BUF]\"\n")
    assert "trial k=25" in out and "setup WNS worse" in out
    m = re.search(r"KB=(\d+)", out)
    assert m and 4 <= int(m.group(1)) <= 12


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_loop_reverts_and_dryrun():
    out = _tcl(MOCK + "set ::kset 0\nputs \"KB=[janus_cab_one u_periph/u_ampo/agent_clk BUF]\"\nputs NB=$::nb\n")
    assert "REVERT" in out and "KB=0" in out and "NB=0" in out
    env = dict(os.environ, JANUS_CTS_AGENT_BAL_DRYRUN="1")
    out = _tcl(MOCK + "set ::kset 99\nputs \"KB=[janus_cab_one u_periph/u_ampo/agent_clk BUF]\"\n", env)
    assert "want +420.1" in out and "trial" not in out and "KB=0" in out
