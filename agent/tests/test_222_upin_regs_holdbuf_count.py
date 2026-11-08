"""§222: run 20261107_203342 follow-ups.
 - stage_report / milestones: a drop in the hold table 'Buffers' column inside
   one RSZ-0046 repair call is NOT a new pass (§219 summed 542 + 6061 + 5647 =
   12250 -> false KILL). Count = RSZ-0032 per call (5647 here).
 - janus_soc_top.sv: upin_dbg_req_q / upin_rst_n_q pin-side regs in front of
   u_user debug_req_i / user_rst_n (bypass JANUS_SOC_NO_UCUT); fenced via
   JANUS_PIN_FENCE upin:u_user:upin_*.
Run: python3 -m pytest asic_flow/agent/tests/test_222_upin_regs_holdbuf_count.py -q"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(HERE.parent), str(ROOT / "asic_flow" / "utils")]

import milestones as ms  # noqa: E402
import stage_report as sr  # noqa: E402

LOG = (HERE / "fixtures/t222/hold_repair_203342.log").read_text()
SV = (ROOT / "rtl/top/janus_soc_top.sv").read_text()
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()


def test_fixture_is_one_call_with_drops():
    assert LOG.count("RSZ-0046]") == 1
    assert "Inserted 5647 hold buffers" in LOG


def test_parse_repair_real_log():
    r = sr.parse_repair(LOG)
    assert r["hold_bufs"] == 5647
    assert r["hold_bufs_peak"] == 6061
    assert r["hold_bufs_total"] == 5647
    assert r["hold_passes"] == 1
    assert sr.hold_buf_count(r) == 5647


def test_no_kill_on_real_log():
    r = sr.parse_repair(LOG)
    row = dict(r, key="4_cts", status="FAILED", hold_ws=0.014)
    assert not any(lvl == "KILL" for lvl, _ in sr.red_flags([row]))


def test_live_watch_real_log_no_kill():
    w = ms.MilestoneTracker("janus_soc_top")
    out = []
    for ln in LOG.splitlines():
        w._hold_watch(ln, "repair_timing", out)
    assert not any("kill_hold_bufs" in str(e) for e in out), out
    assert w.hold_base == 5647


def test_two_calls_summed():
    r = sr.parse_repair(LOG + LOG)
    assert r["hold_bufs_total"] == 2 * 5647 and r["hold_passes"] == 2
    row = dict(r, key="4_cts", status="FAILED", hold_ws=0.0)
    assert any(lvl == "KILL" for lvl, _ in sr.red_flags([row]))


def test_rtl_upin_regs():
    assert ".user_rst_n     (upin_rst_n_q)" in SV
    assert ".debug_req_i    (upin_dbg_req_q)" in SV
    blk = SV[SV.index("logic [USER_CORES-1:0] upin_dbg_req_q;"):SV.index("`endif", SV.index("upin_rst_n_q   = user_rst_n"))]
    assert "`ifndef JANUS_SOC_NO_UCUT" in blk and "`else" in blk
    assert "upin_dbg_req_q <= dbg_debug_req[USER_HART_LO +: USER_CORES]" in blk
    assert "upin_rst_n_q <= 1'b0" in blk and "upin_rst_n_q <= 1'b1" in blk
    assert "negedge user_rst_n" in blk          # async assert kept
    # the AGENT harts still take dbg_debug_req directly
    assert ".debug_req_i    (dbg_debug_req[AGENT_HART_LO +: AGENT_CORES])" in SV
    # declared before u_user (slang use-before-declaration, §185)
    assert SV.index("upin_dbg_req_q;") < SV.index("user_cluster_top u_user (")


def test_mk_fence_spec():
    m = re.search(r"^export JANUS_PIN_FENCE\s*=\s*(.*)$", MK, re.M)
    assert m and "upin:u_user:upin_*" in m.group(1).split()
