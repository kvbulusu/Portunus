"""§191b-e janus_soc_top proactive fixes (same PR as §191).

b  job_scheduler: SCHED_REQ core selection registered (sched_core_q /
   sched_valid_q) — req_agent_q -> policy scan -> prdata -> u_bridge was -1.09 ns.
c  u_ucut_npu: axi4_link_slice on u_user's NPU-L DMA master port (3_detailed_place
   worst -1.76 ns was the u_user -> iopmp -> kv split -> iopmp -> u_user round trip).
d  report_bundle header: multi-clock blocks report the most critical clock with
   ITS OWN period (was 'clk_user period 10000' = ref_clk's period).
e  top mk: multi-VT, SLEW/CAP_MARGIN 20, constant-net fold (user_domain set).
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import report_bundle as rb  # noqa: E402

TOP = ROOT / "rtl/top/janus_soc_top.sv"
JS = ROOT / "rtl/periph/job_scheduler.sv"
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"


def _code(path):
    return "\n".join(l.split("//", 1)[0] for l in path.read_text().splitlines())


def test_job_scheduler_select_registered_191b():
    src = _code(JS)
    assert re.search(r"sched_core_q\s*<=\s*sched_core_c", src)
    assert re.search(r"sched_valid_q\s*<=\s*sched_valid_c", src)
    assert re.search(r"ctrl_prdata\[CIDX_W-1:0\]\s*=\s*sched_core_q", src)
    assert re.search(r"ctrl_prdata\[31\]\s*=\s*sched_valid_q", src)
    assert re.search(r"if\s*\(\s*sched_valid_q\s*\)", src)  # rr_ptr update
    assert re.search(r"assign\s+sched_core_o\s*=\s*sched_core_q", src)
    # declared before first use (rr_ptr update precedes the select block)
    assert src.index("sched_core_q;") < src.index("if (sched_valid_q)")


def test_npu_l_dma_port_cut_191c():
    src = _code(TOP)
    m = re.search(r"`ifndef\s+JANUS_SOC_NO_UCUT(.*?)`else(.*?)`endif", src, re.S)
    assert m
    cut, byp = m.group(1), m.group(2)
    assert re.search(r"axi4_link_slice\s+u_ucut_npu\s*\(", cut)
    assert re.search(r"\.s_m2s_i\(npu_l_m2s_raw\),\s*\.s_s2m_o\(npu_l_s2m_raw\)", cut)
    assert re.search(r"\.m_m2s_o\(npu_l_m2s_pre\),\s*\.m_s2m_i\(npu_l_s2m_pre\)", cut)
    assert re.search(r"assign\s+npu_l_m2s_pre\s*=\s*npu_l_m2s_raw", byp)
    assert re.search(r"assign\s+npu_l_s2m_raw\s*=\s*npu_l_s2m_pre", byp)
    # u_user drives the raw side; the IOPMP still sees *_pre
    assert re.search(r"\.npu_m_m2s_o\s*\(\s*npu_l_m2s_raw\s*\)", src)
    assert re.search(r"\.npu_m_s2m_i\s*\(\s*npu_l_s2m_raw\s*\)", src)
    assert re.search(r"\.s_i\s*\(\s*npu_l_m2s_pre\s*\)", src)


def test_top_mk_closure_knobs_191e():
    mk = MK.read_text()
    for pat in (r"^export ASAP7_USE_VT\s*=\s*RVT LVT SLVT",
                r"^export SLEW_MARGIN\s*=\s*20", r"^export CAP_MARGIN\s*=\s*20",
                r"^export PRE_RESIZE_TCL\s*=\s*\$\(JANUS_CONST_FOLD_TCL\)",
                r"^export JANUS_CONST_FOLD_TIEHI\s*=\s*\$\(TIEHI_CELL_AND_PORT\)"):
        assert re.search(pat, mk, re.M), pat
    assert (ROOT / "asic_flow/scripts/hooks/janus_const_fold.tcl").is_file()


FINISH = """finish report_clock_min_period
--------------------------------------------------------------------------
clk_agent period_min = 0.00 fmax = inf
clk_user period_min = 5138.85 fmax = 194.60
jtag_tck period_min = 29003.38 fmax = 34.48
ref_clk period_min = 6254.06 fmax = 159.90
"""
SDC = """create_clock -name ref_clk -period 10000.0000 [get_ports {ref_clk_i}]
create_clock -name jtag_tck -period 100000.0000 [get_ports {jtag_tck_i}]
create_clock -name clk_user -period 2857.1401 [get_pins {u_clkgen/_146_/Y}]
create_clock -name clk_agent -period 5000.0000 [get_pins {u_clkgen/_147_/Y}]
"""


def test_multi_clock_header_uses_clock_own_period_191d(tmp_path):
    rep, res, logs = tmp_path / "reports", tmp_path / "results", tmp_path / "logs"
    for d in (rep, res, logs):
        d.mkdir()
    (rep / "6_finish.rpt").write_text(FINISH)
    (res / "6_final.sdc").write_text(SDC)
    assert rb.parse_sdc_clocks(str(res)) == {"ref_clk": 10000.0, "jtag_tck": 100000.0,
                                             "clk_user": 2857.1401, "clk_agent": 5000.0}
    q = rb.build_qor("janus_soc_top", "FAIL", str(logs), str(rep), str(res))
    c = q["clock"]
    assert c["name"] == "clk_user"
    assert c["period"] == pytest.approx(2857.14, abs=0.01)
    assert c["target_mhz"] == pytest.approx(350.0, abs=0.1)
    assert c["period_min"] == 5138.85
    assert c["achieved_mhz"] == 194.6
    line = [l for l in rb.render_email_body(q).splitlines() if "Clock      :" in l][0]
    assert "clk_user period 2857.14" in line and "10000" not in line


def test_single_clock_header_unchanged_191d():
    assert rb.pick_primary_clock({"clk": 1000.0}, {}) == "clk"
    assert rb.pick_primary_clock({}, {}) is None
    # zero period_min (unconstrained clock) never wins
    assert rb.pick_primary_clock({"a": 5000.0, "b": 2000.0},
                                 {"a": (0.0, 0.0), "b": (1500.0, 666.0)}) == "b"


# ---- simulations (verilator) -------------------------------------------------
PKGS = ["rtl/pkg/janus_pkg.sv", "rtl/pkg/axi4_pkg.sv", "rtl/pkg/agent_pkg.sv", "rtl/pkg/user_pkg.sv"]
JOBS = ["rtl/periph/job_queue.sv", "rtl/periph/job_arbiter.sv", "rtl/periph/agent_affinity_table.sv",
        "rtl/periph/perf_counter_subsystem.sv", "rtl/periph/job_scheduler.sv"]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
@pytest.mark.parametrize("tb", ["tb_job_scheduler", "tb_multi_agent"])
def test_job_scheduler_tbs_191b(tb, tmp_path):
    cmd = ["verilator", "--binary", "--timing", "-j", "4", "-Wno-fatal", "-Wno-lint",
           "-Wno-style", "-Wno-WIDTH", "+incdir+rtl/cfg", "+incdir+rtl/pkg",
           "+incdir+rtl/common", "+incdir+tb", "--Mdir", str(tmp_path), "--top-module", tb,
           *PKGS, "tb/test_utils.sv", *JOBS, f"tb/{tb}.sv"]
    b = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert b.returncode == 0, b.stderr[-2000:]
    r = subprocess.run([str(tmp_path / f"V{tb}")], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    out = r.stdout + r.stderr
    assert "ALL TESTS PASS" in out or "RESULT: PASS" in out, out[-2000:]
