"""§190b janus_soc_top setup: registered selects + u_user<->u_noc AXI cuts.

After the §190 trees, three timing paths were still open:
  1. u_user m_m2s_o -> u_noc -> u_user npu_ctrl -> u_noc -> {debug sba, u_user
     m_s2m_i, kv split}: -3.2..-4.3 ns after resize. The u_user ETM clk->q
     alone is 1.90 ns. Fix: axi4_link_slice (5x MODE-1 skid) on u_user's master
     port (u_ucut_m) and on NoC slave 3 / NPU-L ctrl (u_ucut_s3).
  2. job_queue: tree -> deq_* -> u_jobsched deq_ready -> vld/count. The head
     selection is now registered (sel_valid_q / sel_idx_q), gated by live vld.
  3. plic_top: tree -> claim decode -> gateways / prdata. The choice is now
     registered per target (eip_q / best_id_q), gated by live ip.
Static checks always run. The verilator testbenches run when verilator is on
PATH (skipped otherwise).
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
JOBQ = ROOT / "rtl/periph/job_queue.sv"
PLIC = ROOT / "rtl/periph/plic_top.sv"
TOP = ROOT / "rtl/top/janus_soc_top.sv"
LINK = ROOT / "rtl/noc/axi4_link_slice.sv"
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"
FLIST = ROOT / "scripts/filelist.f"


def _code(path):
    return "\n".join(l.split("//", 1)[0] for l in path.read_text().splitlines())


def test_job_queue_head_is_registered_190b():
    src = _code(JOBQ)
    assert re.search(r"sel_valid_q\s*<=\s*sel_valid", src)
    assert re.search(r"sel_idx_q\s*<=\s*sel_idx", src)
    # live-vld gate: a slot freed last cycle must never be offered again
    assert re.search(r"assign\s+deq_valid_o\s*=\s*sel_valid_q\s*&&\s*vld\[sel_idx_q\]", src)
    assert re.search(r"assign\s+deq_job_o\s*=\s*mem\[sel_idx_q\]", src)
    assert re.search(r"vld\[sel_idx_q\]\s*<=\s*1'b0", src)


def test_plic_choice_is_registered_190b():
    src = _code(PLIC)
    assert re.search(r"best_id_q\s*<=\s*best_id_c\[gt\]", src)
    assert re.search(r"eip_q\s*<=\s*eip_c\[gt\]", src)
    # live-ip gate: a source claimed last cycle can't be claimed twice
    assert re.search(r"assign\s+eip_o\[gt\]\s*=\s*eip_q\s*&&\s*ip\[best_id_q\]", src)
    assert re.search(r"assign\s+best_id\[gt\]\s*=\s*eip_o\[gt\]\s*\?\s*best_id_q", src)
    assert re.search(r"\.eip_o\s*\(\s*eip_c\[gt\]\s*\)", src)


def test_link_slice_registers_all_five_channels_190b():
    src = _code(LINK)
    for ch in ("aw", "w", "ar", "b", "r"):
        assert re.search(rf"\.MODE\(1\)\)\s*u_{ch}\b", src), f"{ch} channel not MODE-1"
    assert ".MODE(0)" not in src


def test_soc_top_cuts_user_noc_190b():
    src = _code(TOP)
    m = re.search(r"`ifndef\s+JANUS_SOC_NO_UCUT(.*?)`else(.*?)`endif", src, re.S)
    assert m, "u_ucut block (with JANUS_SOC_NO_UCUT bypass) missing"
    cut, byp = m.group(1), m.group(2)
    assert re.search(r"axi4_link_slice\s+u_ucut_m\s*\(", cut)
    assert re.search(r"axi4_link_slice\s+u_ucut_s3\s*\(", cut)
    for sig in ("user_m_req", "noc_m_req[0]", "npu_ctrl_req", "noc_s_req[3]"):
        assert sig in cut and sig in byp, sig
    # u_user must talk to the cut nets, not straight to the NoC
    assert re.search(r"\.m_m2s_o\s*\(\s*user_m_req\s*\)", src)
    assert re.search(r"\.m_s2m_i\s*\(\s*user_m_rsp\s*\)", src)
    assert re.search(r"\.npu_ctrl_m2s_i\s*\(\s*npu_ctrl_req\s*\)", src)
    assert re.search(r"\.npu_ctrl_s2m_o\s*\(\s*npu_ctrl_rsp\s*\)", src)


def test_flow_filelists_have_link_slice_190b():
    mk = MK.read_text()
    i_rs = mk.find("$(RTL)/noc/axi4_register_slice.sv")
    i_ls = mk.find("$(RTL)/noc/axi4_link_slice.sv")
    assert i_rs >= 0 and i_ls > i_rs, "mk must list register_slice before link_slice"
    fl = FLIST.read_text().splitlines()
    assert fl.index("rtl/noc/axi4_register_slice.sv") < fl.index("rtl/noc/axi4_link_slice.sv")


# ---- simulations (verilator) -------------------------------------------------
_SIMS = {
    "job_queue": ("tb_job_queue_regsel", "TB_JOB_QUEUE_REGSEL: *** ALL TESTS PASS ***",
                  ["rtl/pkg/janus_pkg.sv", "rtl/periph/job_queue.sv", "tb/tb_job_queue_regsel.sv"]),
    "plic": ("tb_plic_regsel", "TB_PLIC_REGSEL: *** ALL TESTS PASS ***",
             ["rtl/periph/plic_gateway.sv", "rtl/periph/plic_target.sv",
              "rtl/periph/plic_top.sv", "tb/tb_plic_regsel.sv"]),
    "link_slice": ("tb_axi4_link_slice", "TB_AXI4_LINK_SLICE: *** ALL TESTS PASS ***",
                   ["rtl/pkg/janus_pkg.sv", "rtl/pkg/axi4_pkg.sv", "rtl/noc/axi4_register_slice.sv",
                    "rtl/noc/axi4_link_slice.sv", "tb/tb_axi4_link_slice.sv"]),
}


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
@pytest.mark.parametrize("name", sorted(_SIMS))
def test_regsel_sim_190b(name, tmp_path):
    top, banner, files = _SIMS[name]
    cmd = ["verilator", "--binary", "--timing", "-j", "4", "-Wno-fatal", "-Wno-lint",
           "-Wno-style", "-Wno-TIMESCALEMOD", "--top-module", top,
           "-Irtl/pkg", "-Irtl/cfg", "-Irtl/common", "--Mdir", str(tmp_path), "-o", "sim", *files]
    b = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert b.returncode == 0, b.stderr[-3000:]
    r = subprocess.run([str(tmp_path / "sim")], cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert banner in r.stdout, r.stdout[-3000:]
