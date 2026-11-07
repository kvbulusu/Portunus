"""§218: run 20261106_110339 follow-ups.
 - stage_report: hold-buffer accounting (the §218 "final RSZ-0032 wins" rule
   was wrong and is reversed in §219: 8052 + 292 = 8344 across passes).
 - soc_cts_probe.tcl: brackets in the bbox format string escaped (rc=2).
 - janus_soc_top.mk / janus_cts_balance.tcl: JANUS_CTS_LEGALIZE_PRE, REDISP ITERS=5.
 - janus_soc_top.sv: u_ucut_npun NoC-side cut on NoC master 1, gated by
   JANUS_SOC_NO_UCUT.
Run: python3 -m pytest asic_flow/agent/tests/test_218_npun_cut_cts_prelegalize.py -q"""
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(ROOT, "asic_flow", "utils")]

import stage_report as sr  # noqa: E402


def _read(*p):
    return open(os.path.join(ROOT, *p)).read()


HOLD_LOG = """[INFO RSZ-0046] Found 3599 endpoints with hold violations.
Iteration | Resized | Buffers | Cloned Gates |   Area   |   WNS   |   TNS   | Endpoint
--------------------------------------------------------------------------------------
        0 |       0 |       0 |            0 |    +0.0% | -346.984 | -171056.250 | a/D
     4140 |       7 |    8052 |            0 |    +0.0% | -229.274 | -23865.221 | b/D
     4220 |       7 |     167 |            0 |    +0.0% | -229.274 | -22573.902 | b/D
    final |       7 |     292 |            0 |    +0.0% | -229.274 | -22066.512 | b/D
--------------------------------------------------------------------------------------
[WARNING RSZ-0066] Unable to repair all hold violations.
[INFO RSZ-0032] Inserted 292 hold buffers.
"""


def test_parse_repair_final_vs_peak():
    r = sr.parse_repair(HOLD_LOG)
    assert r["hold_bufs_peak"] == 8052
    assert r["hold_bufs"] == 292
    assert r["hold_bufs_final"] is True
    assert r["hold_pre_wns"] == pytest.approx(-346.984)
    assert r["hold_post_wns"] == pytest.approx(-229.274)


def test_red_flags_use_cross_pass_total():
    # §219 reverses §218: the Buffers column restarts per RSZ pass and
    # RSZ-0032 covers the last pass only, so 8052 + 292 = 8344 is real.
    r = sr.parse_repair(HOLD_LOG)
    assert r["hold_bufs_total"] == 8344
    row = dict(r, key="4_cts", status="FAILED")
    assert ("RED", "4_cts: 8344 hold buffers > %d" % sr.HOLD_BUF_WARN) in sr.red_flags([row])


def test_red_flags_peak_while_running():
    # no RSZ-0032 yet (stage still in repair_timing): the running peak counts
    row = {"key": "4_cts", "status": "RUNNING", "hold_bufs_peak": 8052, "hold_bufs": 8052}
    f = sr.red_flags([row])
    assert ("RED", "4_cts: 8052 hold buffers > %d" % sr.HOLD_BUF_WARN) in f or \
        any(lvl == "KILL" and "8052 hold buffers" in t for lvl, t in f)


def test_red_flags_final_still_kills_when_large():
    row = {"key": "4_cts", "status": "FAILED", "hold_bufs_peak": 30000,
           "hold_bufs": sr.HOLD_BUF_KILL + 1, "hold_bufs_final": True}
    assert any(lvl == "KILL" and "hold buffers" in t for lvl, t in sr.red_flags([row]))


def test_hold_not_closed_kill_kept():
    row = {"key": "4_cts", "status": "FAILED", "hold_ws": -229.3,
           "hold_bufs": 292, "hold_bufs_final": True}
    assert any(lvl == "KILL" and "after repair" in t for lvl, t in sr.red_flags([row]))


def test_probe_format_brackets_escaped():
    src = _read("asic_flow", "scripts", "debug", "soc_cts_probe.tcl")
    for line in src.splitlines():
        if "format" in line and "bbox" in line:
            assert "x\\[%s..%s\\]" in line and "y\\[%s..%s\\]" in line, line
            break
    else:
        pytest.fail("bbox format line not found")


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="tclsh not installed")
def test_probe_format_tcl_eval():
    tcl = 'puts [format "bbox x\\[%s..%s\\] y\\[%s..%s\\]" 1 2 3 4]'
    out = subprocess.run(["tclsh"], input=tcl, capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "bbox x[1..2] y[3..4]"


def test_mk_prelegalize_and_iters():
    mk = _read("asic_flow", "configs", "janus_soc_top.mk")
    assert re.search(r"^export JANUS_CTS_LEGALIZE_PRE\s*=\s*1\s*$", mk, re.M)
    assert re.search(r"^export JANUS_CTS_CLK_REDISP_ITERS\s*=\s*5\s*$", mk, re.M)
    assert re.search(r"^export JANUS_CTS_LEGALIZE\s*=\s*1\s*$", mk, re.M)


def test_hook_prelegalize_before_hooks():
    src = _read("asic_flow", "scripts", "hooks", "janus_cts_balance.tcl")
    i_pre = src.index("janus_cts_env JANUS_CTS_LEGALIZE_PRE 0] ne \"0\" } {\n      janus_cts_legalize post_cts")
    i_orig = src.index("janus_orig_clock_tree_synthesis {*}$args")
    i_icg = src.index("[janus_cts_env JANUS_CTS_ICG_UNPAD 0] ne \"0\" } {\n      if { [catch {janus_cts_icg_unpad}")
    assert i_orig < i_pre < i_icg
    redisp = _read("asic_flow", "scripts", "hooks", "janus_cts_clk_redisp.tcl")
    assert "JANUS_CTS_CLK_REDISP_ITERS" in redisp


def test_rtl_ucut_npun_gated():
    sv = _read("rtl", "top", "janus_soc_top.sv")
    m = re.search(r"`ifndef JANUS_SOC_NO_UCUT\n(?:\s*//[^\n]*\n)*\s*axi4_link_slice u_ucut_npun \((.*?)\);\n`else\n(.*?)`endif",
                  sv, re.S)
    assert m, "u_ucut_npun must be under `ifndef JANUS_SOC_NO_UCUT with a direct-assign `else"
    ports = m.group(1)
    assert ".s_m2s_i(npu_kv_split_req[1])" in ports and ".s_s2m_o(npu_kv_split_rsp[1])" in ports
    assert ".m_m2s_o(noc_m_req[1])" in ports and ".m_s2m_i(noc_m_rsp[1])" in ports
    assert ".clk(user_clk)" in ports and ".rst_n(user_rst_n)" in ports
    assert "assign noc_m_req[1]        = npu_kv_split_req[1];" in m.group(2)
    # must live inside the JANUS_NPU_KV_PORT branch (the else branch drives
    # noc_m_req[1] from u_npu_l_iopmp directly)
    kv = sv.index("`ifdef JANUS_NPU_KV_PORT")
    assert kv < sv.index("u_ucut_npun") < sv.index("kv_npu_axi_bridge #(")
