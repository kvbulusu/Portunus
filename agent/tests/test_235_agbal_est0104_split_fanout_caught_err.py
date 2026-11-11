"""§235 fixes from run 20261110_203249 (NOT PD validation; tclsh mocks only).

(a) JANUS_CTS_AGENT_BAL: undo (raw odb edits) -> insert_buffer raised
    [ERROR EST-0104] inconsistent parasitics state; pin reverted, 0 delayed.
(b) JANUS_PIN_FENCE_SPLIT: mr_q D net (u_user r_ready, high fanout) dropped by
    the MAX_FANOUT 16 cap -> 0 insts moved.
(c) The caught EST-0104 marked the block FAILED on make rc=0 (skipped the DONE
    path: post-route hold ECO / signoff / golden) and was the triage 'first error'.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
FLOW = ROOT / "asic_flow"
HOOKS = FLOW / "scripts" / "hooks"
sys.path.insert(0, str(FLOW / "utils"))
sys.path.insert(0, str(FLOW / "agent"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import log_analyzer as la  # noqa: E402
import report_bundle as rb  # noqa: E402
import test_230_cts_agent_balance as t230  # noqa: E402

TCLSH = shutil.which("tclsh")

RUN_203249 = """\
JANUS_CTS_AGENT_BAL:   trial k=10 clk 1067.9 (37.0 ps/buf) | hold -417.8/-197295.1 (3608 viol) setup -539.3/-60836.8 | pass
JANUS_CTS_AGENT_BAL:   trial k=12 clk 1129.6 (36.0 ps/buf) | hold -417.8/-197334.9 (3612 viol) setup -539.3/-60836.8 | hold violators up
[ERROR EST-0104] inconsistent parasitics state
JANUS_CTS_AGENT_BAL: WARNING u_periph/u_ampo/agent_clk: EST-0104; reverting this pin
JANUS_CTS_AGENT_BAL: done: 0 pin(s) delayed | hold -417.8/-197336.6 (3609 viol) setup -539.3/-60836.8
"""


# ---- (a) agent_bal: undo is always followed by a parasitics re-sync ---------
def test_agbal_sync_after_every_undo_in_source():
    s = (HOOKS / "janus_cts_agent_balance.tcl").read_text()
    assert "proc janus_cab_sync { } { estimate_parasitics -placement }" in s
    one = s[s.index("proc janus_cab_one "):s.index("proc janus_cts_agent_balance ")]
    n_undo = one.count("janus_cab_undo $spec $it")
    assert n_undo == 2 and one.count("janus_cab_sync") == n_undo
    top = s[s.index("proc janus_cts_agent_balance "):]
    assert "catch { janus_cab_sync }" in top
    assert "macro-out hold %s" in one


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_agbal_bisect_after_failed_trial_no_est0104():
    # the 203249 shape: k=4 pass, calibrated k passes, a larger k fails the
    # guard -> bisect shrinks (undo + regrow) -> must not hit EST-0104
    out = t230._tcl(t230.MOCK + "set ::kset 12\n"
                    "puts \"KB=[janus_cab_one u_periph/u_ampo/agent_clk BUF]\"\nputs NB=$::nb\n")
    assert "EST-0104" not in out
    m = re.search(r"KB=(\d+)", out)
    assert m and 4 <= int(m.group(1)) <= 12
    assert f"NB={m.group(1)}" in out and "ACCEPT" in out


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_agbal_without_sync_reproduces_est0104():
    out = t230._tcl(t230.MOCK + "proc janus_cab_sync { } {}\nset ::kset 12\n"
                    "if {[catch {janus_cab_one u_periph/u_ampo/agent_clk BUF} e]} { puts ERR=$e }\n")
    assert "ERR=EST-0104" in out


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_agbal_keeps_chain_when_best_is_current():
    out = t230._tcl(t230.MOCK + "set ::kset 99\n"
                    "puts \"KB=[janus_cab_one u_periph/u_ampo/agent_clk BUF]\"\nputs INV=$::inv\n")
    assert "ACCEPT k=25" in out and "INV=0" in out


# ---- (b) pin-fence split D-side ---------------------------------------------
def test_split_d_side_has_no_fanout_cap_and_walks_buffers():
    s = (HOOKS / "janus_pin_fence.tcl").read_text()
    one = s[s.index("proc janus_pf::split_one "):s.index("proc janus_pf::split_all ")]
    assert "JANUS_PIN_FENCE_SPLIT_MAX_FANOUT 0" in one
    assert "janus_pf::d_macro_pins $net $mac $maxfo" in one
    assert "JANUS_PIN_FENCE_MAX_FANOUT" not in one
    assert "proc janus_pf::d_macro_pins " in s


TOY = r"""
namespace eval janus_pf {}
source {%s}
# toy odb: mac/o -> n0 -> buf1 -> n1 -> buf2 -> n2 -> flop/D ; n0 fans out to 40 loads
proc mk_it { name inst out net } { set ::IT($name) [list $inst $out $net] ; proc $name { m args } "it_m $name \$m" }
proc it_m { name m } {
  lassign $::IT($name) inst out net
  switch $m { getInst { return $inst } isOutputSignal { return $out } isInputSignal { return [expr {!$out}] }
    getNet { return $net } getMTerm { return MT } }
}
proc MT { m args } { return SIGNAL }
proc mk_inst { name master its } { set ::INST($name) [list $master $its]; proc $name { m args } "inst_m $name \$m" }
proc inst_m { name m } { lassign $::INST($name) master its
  switch $m { getMaster { return $master } getITerms { return $its } getName { return $name } } }
proc mk_master { name blk } { proc $name { m args } "switch \$m { isBlock { return $blk } getName { return $name } }" }
proc mk_net { name its } { set ::NET($name) $its; proc $name { m args } "net_m $name \$m" }
proc net_m { name m } { switch $m { getITerms { return $::NET($name) } isSpecial { return 0 } } }
mk_master MACM 1; mk_master BUFx3_ASAP7_75t_L 0; mk_master DFF 0
mk_it mac_o MAC 1 n0; mk_inst MAC MACM {mac_o}
mk_it b1_a B1 0 n0; mk_it b1_y B1 1 n1; mk_inst B1 BUFx3_ASAP7_75t_L {b1_a b1_y}
mk_it b2_a B2 0 n1; mk_it b2_y B2 1 n2; mk_inst B2 BUFx3_ASAP7_75t_L {b2_a b2_y}
mk_it f_d F 0 n2; mk_inst F DFF {f_d}
set loads {mac_o b1_a}
for {set i 0} {$i < 40} {incr i} { mk_it l$i L$i 0 n0; lappend loads l$i }
mk_net n0 $loads; mk_net n1 {b1_y b2_a}; mk_net n2 {b2_y f_d}
puts "D=[janus_pf::d_macro_pins n2 MAC 1000000]"
puts "D1=[janus_pf::macro_pins n2 MAC 16]"
"""


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_split_d_trace_through_two_buffers_toy():
    src = HOOKS / "janus_pin_fence.tcl"
    body = TOY % src
    r = subprocess.run([TCLSH], input=body, capture_output=True, text=True, timeout=60,
                       env=dict(os.environ))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "D=mac_o" in r.stdout          # §235 walk: 2 buffer hops back to the macro
    assert "D1=\n" in r.stdout + "\n"     # old one-hop lookup found nothing


# ---- (c) caught hook error is not a failure ---------------------------------
def test_log_analyzer_caught_est0104_not_failed():
    rep = la.LogAnalyzer().analyze_text(RUN_203249, "janus_soc_top", "cts")
    assert not rep.failed, rep.error_lines
    bad = RUN_203249.replace("JANUS_CTS_AGENT_BAL: WARNING u_periph/u_ampo/agent_clk: EST-0104; reverting this pin\n", "")
    rep2 = la.LogAnalyzer().analyze_text(bad, "janus_soc_top", "cts")
    assert rep2.failed and any("EST-0104" in x for x in rep2.error_lines)


def test_hook_caught_requires_janus_warning_next():
    L = ["[ERROR GRT-0116] x", "Took 3 seconds: foo"]
    assert not la.hook_caught(L, 0, "GRT-0116")
    L = ["[ERROR GRT-0116] x", "", "JANUS_X: WARNING something unrelated"]
    assert not la.hook_caught(L, 0, "GRT-0116")
    L = ["[ERROR GRT-0116] x", "JANUS_X: WARNING foo failed harmlessly"]
    assert la.hook_caught(L, 0, "GRT-0116")


def test_report_bundle_first_error_skips_caught():
    first, code, last = rb.find_first_error(RUN_203249)
    assert first is None and code is None
    first, code, _ = rb.find_first_error(RUN_203249 + "[ERROR DRT-0001] real\n")
    assert code == "DRT-0001"
