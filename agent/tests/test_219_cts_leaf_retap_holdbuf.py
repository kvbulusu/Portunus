"""§219: run 20261106_110339 hold follow-ups.
 - janus_cts_leaf_retap.tcl: late leaf re-tap of the clk_agent capture leaf
   (leaf_96/97 detour, ~1046 um hop) onto a nearer tree-internal net; accept
   only if hold TNS improves without setup loss, else revert.
 - janus_cts_balance.tcl: sourced/called after tap_split legalize, before DIAG.
 - stage_report / milestones: hold buffers summed across RSZ passes (8052 + 292
   = 8344); reverses the §218 "final RSZ-0032 wins" rule.
 - soc_hold_probe.tcl: SHP_ONLY, no PathEnd reuse (segfault), unconstrained
   fallback, section R (in-memory retap trial).
NOT an OpenROAD run on soc_top.
Run: python3 -m pytest asic_flow/agent/tests/test_219_cts_leaf_retap_holdbuf.py -q"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE), str(HERE.parent), str(ROOT / "asic_flow" / "utils")]

import _tcl_lint as tl  # noqa: E402
import milestones as ms  # noqa: E402
import stage_report as sr  # noqa: E402

HOOK = ROOT / "asic_flow/scripts/hooks/janus_cts_leaf_retap.tcl"
BAL = ROOT / "asic_flow/scripts/hooks/janus_cts_balance.tcl"
PROBE = ROOT / "asic_flow/scripts/debug/soc_hold_probe.tcl"
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"
TCLSH = shutil.which("tclsh")

# real 4_1_cts.log shape: Buffers column restarts inside one table
HOLD_LOG = """[INFO RSZ-0046] Found 3599 endpoints with hold violations.
Iteration | Resized | Buffers | Cloned Gates |   Area   |   WNS   |   TNS   | Endpoint
--------------------------------------------------------------------------------------
        0 |       0 |       0 |            0 |    +0.0% | -346.984 | -171056.250 | a/D
     4140 |       7 |    8052 |            0 |    +0.0% | -229.274 | -23865.221 | b/D
     4150 |       7 |       2 |            0 |    +0.0% | -229.274 | -23860.100 | b/D
    final |       7 |     292 |            0 |    +0.0% | -229.274 | -23702.512 | b/D
--------------------------------------------------------------------------------------
[WARNING RSZ-0066] Unable to repair all hold violations.
[INFO RSZ-0032] Inserted 292 hold buffers.
"""


# --- static -----------------------------------------------------------------------
@pytest.mark.parametrize("p", [HOOK, BAL, PROBE])
def test_tcl_balanced(p):
    assert tl.balance(p.read_text()) == (0, 0, None), p.name


def test_hook_no_writers_and_knobs():
    s = HOOK.read_text()
    assert tl.write_forbidden(s) == []
    assert tl.bracket_issues(s) == []
    for k in ("JANUS_CTS_LEAF_RETAP", "_CLOCKS", "_RADIUS_UM", "_MIN_GAIN_PS", "_TRIG_PS",
              "_MAX", "_TRIES", "_MAX_FANOUT", "_SETUP_TOL_PS", "_SETUP_TNS_TOL_PS",
              "_RCN_UM", "_ALLOW_LEAF_NETS", "_DRYRUN", "_ACCEPT_ANY"):
        assert k in s, k
    # every accepted/rejected move is reverted or pruned explicitly
    assert "janus_lrt_accept" in s and "janus_lrt_prune" in s and "REVERT" in s


def test_mk_keys():
    mk = MK.read_text()
    for k, v in (("JANUS_CTS_LEAF_RETAP", "1"), ("JANUS_CTS_LEAF_RETAP_CLOCKS", "clk_agent"),
                 ("JANUS_CTS_LEAF_RETAP_RADIUS_UM", "150"),
                 ("JANUS_CTS_LEAF_RETAP_MIN_GAIN_PS", "150"),
                 ("JANUS_CTS_LEAF_RETAP_MAX", "8")):
        assert re.search(r"^export %s\s*\??=\s*%s\s*$" % (k, v), mk, re.M), k
    # §218 knobs kept
    assert re.search(r"^export JANUS_CTS_LEGALIZE_PRE\s*=\s*1\s*$", mk, re.M)


def test_balance_wiring_order():
    s = BAL.read_text()
    assert "set janus_cts_lretap [expr {[janus_cts_env JANUS_CTS_LEAF_RETAP 0]" in s
    assert "$janus_cts_lretap ||" in s or "|| $janus_cts_lretap" in s
    i_src = s.index("janus_cts_leaf_retap.tcl]")
    i_ts = s.index("janus_cts_legalize tap_split")
    i_call = s.index("catch {janus_cts_leaf_retap}")
    i_diag = s.index("[janus_cts_env JANUS_CTS_DIAG 0] ne \"0\"", i_ts)
    i_abort = s.index("janus_cts_hold_abort\n", i_call)
    assert i_src < i_ts < i_call < i_diag < i_abort


def test_probe_static():
    s = PROBE.read_text()
    assert tl.write_forbidden(s) == []
    assert tl.bracket_issues(s) == []
    assert "SHP_ONLY" in s and "proc want" in s
    for sec in ("H2", "H3", "H4", "R"):
        assert "if { [want %s] }" % sec in s, sec
    assert "-unconstrained" in s
    assert "proc clk_pin_of" in s
    # PathEnds are read once into allrows; never touched after that loop
    h4 = s[s.index("if { [want H4] }"):s.index("if { [want R] }")]
    loop_end = h4.index('out "SHP4 hold violators scanned')
    assert "get_property $pe" in h4[:loop_end]
    assert "get_property $pe" not in h4[loop_end:]
    # R before H1, nothing written
    assert s.index('hdr "R ') < s.index('hdr "H1 RSZ')


# --- accounting -------------------------------------------------------------------
def test_parse_repair_sums_passes():
    r = sr.parse_repair(HOLD_LOG)
    assert r["hold_bufs"] == 292
    assert r["hold_bufs_peak"] == 8052
    assert r["hold_bufs_total"] == 8344
    assert r["hold_passes"] == 2
    assert sr.hold_buf_count(r) == 8344


def test_red_flags_total():
    r = sr.parse_repair(HOLD_LOG)
    row = dict(r, key="4_cts", status="FAILED")
    assert ("RED", "4_cts: 8344 hold buffers > %d" % sr.HOLD_BUF_WARN) in sr.red_flags([row])


def test_single_pass_small_not_flagged():
    log = HOLD_LOG.replace("8052", "100").replace("|       2 |", "|     150 |")
    r = sr.parse_repair(log)
    assert r["hold_bufs_total"] == 292 and r["hold_passes"] == 1
    row = dict(r, key="4_cts", status="FAILED", hold_ws=0.0)
    assert not any("hold buffers" in t for _, t in sr.red_flags([row]))


def _feed(lines):
    w = ms.MilestoneTracker("janus_soc_top")
    out = []
    for ln in lines.splitlines():
        w._hold_watch(ln, "repair_timing", out)
    return out


def test_live_kill_uses_cross_pass_sum():
    big = HOLD_LOG.replace("|       2 |", "|    2100 |").replace("    292 |", "   2200 |") \
        .replace("Inserted 292", "Inserted 2200")
    # 8052 + 2200 = 10252 > 10000 even though no single pass exceeds it
    ev = _feed(big)
    assert any("kill_hold_bufs" in str(e) or "KILL" in str(e) for e in ev), ev


def test_live_no_kill_small():
    assert not any("KILL" in str(e) for e in _feed(HOLD_LOG))


# --- tclsh: pure accept test ------------------------------------------------------
@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_accept_rules_tclsh():
    drv = f"""
set f [open {{{HOOK}}}]; set s [read $f]; close $f
if {{![info complete $s]}} {{ puts INCOMPLETE; exit 1 }}
set cmd ""
foreach line [split $s "\\n"] {{
  append cmd $line "\\n"
  if {{![info complete $cmd]}} continue
  if {{[regexp {{^\\s*proc\\s}} $cmd]}} {{ eval $cmd }}
  set cmd ""
}}
set b {{-229.3 -23702 50 0}}
puts "A=[janus_lrt_accept $b {{-100 -23000 50 0}} 0 100]"
puts "B=[janus_lrt_accept $b {{-229.3 -23702 50 0}} 0 100]"
puts "C=[janus_lrt_accept $b {{-240 -23000 50 0}} 0 100]"
puts "D=[janus_lrt_accept $b {{-100 -23000 -5 -5}} 0 100]"
puts "E=[janus_lrt_accept {{-229 -23702 -10 -50}} {{-100 -23000 -10 -120}} 0 100]"
puts "F=[janus_lrt_accept {{-229 -23702 -10 -50}} {{-100 -23000 -10 -200}} 0 100]"
"""
    r = subprocess.run([TCLSH], input=drv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    o = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
    assert o["A"] == ""
    assert o["B"] == "hold TNS not improved"
    assert o["C"] == "hold WNS worse"
    assert o["D"] == "setup WNS worse"
    assert o["E"] == ""
    assert o["F"] == "setup TNS worse"


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_hook_disabled_is_noop_tclsh():
    drv = f"""
source {{{HOOK}}}
catch {{unset ::env(JANUS_CTS_LEAF_RETAP)}}
puts "rc=[catch {{janus_cts_leaf_retap}} e] e=$e"
"""
    r = subprocess.run([TCLSH], input=drv, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "rc=0 e=" in r.stdout
