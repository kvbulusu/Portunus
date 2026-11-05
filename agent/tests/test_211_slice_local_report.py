"""§211: run 20261104_224244 follow-ups.

(1) RTL: axi4_register_slice MODE 2 (per-slice replicated control, payload
    enables without s_valid_i) on u_ucut_mn / u_ucut_m. Post-DPL worst -1435.4
    (u_user ar_valid -> 24 x u_ucut_mn/u_ar skid_data_q) and -559.5
    (u_ucut_mn/u_r out_valid_q -> 80 x out_data_q enables).
(2) janus_post_dpl_repair: wns = design worst (worst_slack -max), not the
    first path group's path (logged +481.9 = in2reg while reg2reg -1435.4).
(3) stage_report: the JANUS post-hook dump (janus_05) beats the pre-hook ORFS
    3_detailed_place.rpt / metrics json (-3963.6 headline was pre-repair).
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import stage_report as sr  # noqa: E402

RS = (ROOT / "rtl/noc/axi4_register_slice.sv").read_text()
LS = (ROOT / "rtl/noc/axi4_link_slice.sv").read_text()
TOP = (ROOT / "rtl/top/janus_soc_top.sv").read_text()
RUN = (ROOT / "scripts/run_tests.sh").read_text()
PDR = (ROOT / "asic_flow/scripts/hooks/janus_post_dpl_repair.tcl").read_text()


def _code(src):
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def test_mode1_default_untouched():
    c = _code(RS)
    assert "parameter int MODE  = 1" in c
    assert "end else if (MODE == 1) begin : g_skid" in c
    assert "end else if (s_valid_i && s_ready_o) begin" in c  # legacy branch intact


def test_mode2_enables_do_not_use_s_valid():
    blk = _code(RS).split("g_skid_local", 1)[1]
    assert "(* keep *) logic out_valid_q;" in blk and "(* keep *) logic skid_valid_q;" in blk
    assert re.search(r"wire\s+adv = m_ready_i \|\| !out_valid_q;", blk)
    assert "if (!skid_valid_q) skid_data_q <= s_data_i[HI:LO];" in blk
    for l in blk.splitlines():
        if "out_data_q   <=" in l or "skid_data_q <=" in l:
            assert "s_valid_i" not in l


def test_link_slice_param_and_top_usage():
    assert "parameter int MODE         = 1" in LS
    assert _code(LS).count(".MODE(MODE), .CTRL_SLICE_W(CTRL_SLICE_W))") == 5
    c = _code(TOP)
    assert "axi4_link_slice #(.MODE(2)) u_ucut_mn (" in c
    assert "axi4_link_slice #(.MODE(2)) u_ucut_m (" in c
    assert "axi4_link_slice u_ucut_s3 (" in c  # others unchanged


def test_tb_registered():
    assert '[tb_211_slice_local]="rtl/noc/axi4_register_slice.sv tb/tb_211_slice_local.sv"' in RUN
    assert "tb_204_lbusy_reg tb_211_slice_local" in RUN


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_tb_211_lockstep_passes(tmp_path):
    tb = "tb_211_slice_local"
    cmd = ["verilator", "--binary", "--timing", "-j", "4", "-Wno-fatal", "-Wno-lint",
           "-Wno-style", "-Wno-WIDTH", "--Mdir", str(tmp_path), "--top-module", tb,
           "rtl/noc/axi4_register_slice.sv", f"tb/{tb}.sv"]
    b = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert b.returncode == 0, b.stderr[-2000:]
    r = subprocess.run([str(tmp_path / f"V{tb}")], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    assert "RESULT: PASS" in r.stdout, (r.stdout + r.stderr)[-2000:]


def test_pdr_wns_is_design_worst():
    body = PDR.split("proc janus_pdr::wns", 1)[1].split("\nproc ", 1)[0]
    assert "worst_slack -max" in body
    assert "lindex [find_timing_paths -path_delay max] 0" not in body


def _mk(tmp, dump_newer=True, hook_log=True):
    rep = tmp / "reports"; logs = tmp / "logs"
    rep.mkdir(); logs.mkdir()
    (rep / "3_detailed_place.rpt").write_text(
        "wns max -3963.60\ntns max -817000\nsetup violation count 1715\n"
        "Startpoint: a\nEndpoint: b\nPath Type: max\n -3963.60   slack (VIOLATED)\n")
    (rep / "janus_05_detail_place_paths.rpt").write_text(
        "wns max -1435.41\ntns max -261967.08\nworst slack max -1435.41\n"
        "worst slack min 4.73\nStartpoint: u_user\nEndpoint: u_ucut_mn/x\n"
        "Path Type: max\n -1435.41   slack (VIOLATED)\n")
    log = "Report metrics stage 3, detailed place...\n"
    if hook_log:
        log += "JANUS_POST_DPL: after  wns -1435.4 (was -3963.6) slew_viol 63 cap_viol 0 insts 1\n"
    log += "Elapsed time: 11:29.83[h:]min:sec. CPU time: user 943.05 sys 64.97 (146%). Peak memory: 14365120KB.\n"
    (logs / "3_5_place_dp.log").write_text(log)
    now = time.time()
    a, b = (now - 100, now) if dump_newer else (now, now - 100)
    os.utime(rep / "3_detailed_place.rpt", (a, a))
    os.utime(rep / "janus_05_detail_place_paths.rpt", (b, b))
    return str(logs), str(rep)


def test_report_uses_post_hook_dump(tmp_path):
    logs, rep = _mk(tmp_path)
    r = sr.collect_stage("3_place", logs, rep, metrics={})
    assert r["setup_ws"] == -1435.41 and r["hold_ws"] == 4.73
    assert r["setup_tns"] == -261967.08
    assert "setup_viol" not in r
    assert (r["slew_viol"], r["cap_viol"]) == (63, 0)
    assert r["paths"]["max"][0] == ("u_user", "u_ucut_mn/x", -1435.41)
    assert r["timing_src"] == "janus_05_detail_place_paths.rpt"


def test_report_keeps_orfs_when_dump_older(tmp_path):
    logs, rep = _mk(tmp_path, dump_newer=False)
    r = sr.collect_stage("3_place", logs, rep, metrics={})
    assert r["setup_ws"] == -3963.60 and "timing_src" not in r


# (4) fetch_enable_i registered on agent_clk before u_agent (113025 4_cts
#     in2reg -510.3: 1500 ps input delay on a ~1300 ps refpin vs isolated
#     u_agent/agent_clk ~684 ps). u_user keeps the raw port.
def test_fetch_enable_agent_reg():
    assert re.search(r"always_ff @\(posedge agent_clk or negedge agent_rst_n\) begin\s*"
                     r"if \(!agent_rst_n\) fetch_enable_agent_q <= 1'b0;\s*"
                     r"else\s+fetch_enable_agent_q <= fetch_enable_i;", TOP)
    agent = TOP[TOP.index("agent_cluster_top u_agent ("):]
    agent = agent[:agent.index(");")]
    assert ".fetch_enable_i (fetch_enable_agent_q)" in agent
    user = TOP[TOP.index("user_cluster_top u_user ("):]
    assert ".fetch_enable_i (fetch_enable_i)" in user[:user.index(");")]


# (5) post-DPL repair_design iterates while the slew count drops (max 4) and
#     cleans up after repair_timing (224244: 60 -> 63 slew).
def test_post_dpl_adaptive_iters_and_cleanup():
    mk = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
    assert re.search(r"^export JANUS_POST_DPL_ITERS\s*=\s*4\b", mk, re.M)
    assert "proc janus_pdr::nslew" in PDR
    assert re.search(r"if \{ \$k >= 2 && \(\$cur == 0 \|\| \(\$prev >= 0 && \$cur >= \$prev\)\) \} \{ break \}", PDR)
    assert "JANUS_POST_DPL: cleanup repair_design" in PDR
