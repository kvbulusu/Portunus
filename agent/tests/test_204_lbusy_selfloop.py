"""§204: cut the u_user self-loop npu_l_job_busy_o -> u_jobsched -> npu_l_launch_i.

Probe #5 (slow): -864.6 ps, skew-invariant (macro clk->out 3568 + 1629 soc path
vs 2857 period + 1535.5 macro-internal capture latency). job_arbiter now
registers npu_l_busy_i once; the capture flop is an ordinary u_user-output
capture register that the §203 LATE tap retimes.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ARB = (ROOT / "rtl/periph/job_arbiter.sv").read_text()
RUN = (ROOT / "scripts/run_tests.sh").read_text()


def _code(src):
    return "\n".join(l.split("//")[0] for l in src.splitlines())


def test_busy_registered_and_only_q_used():
    c = _code(ARB)
    assert re.search(r"npu_l_busy_q\s*<=\s*npu_l_busy_i", c)
    uses = [l for l in c.splitlines() if "npu_l_busy_i" in l]
    # only the port declaration and the flop D input may mention the raw input
    assert len(uses) == 2, uses
    assert c.count("!npu_l_busy_q") == 2


def test_launch_still_guarded_by_inflight():
    c = _code(ARB)
    assert "route_l && !l_inflight_q && !npu_l_busy_q" in c
    assert "assign npu_l_launch_o = can_dispatch && route_l;" in c


def test_tb_registered_in_regression():
    assert '[tb_204_lbusy_reg]="rtl/periph/job_arbiter.sv tb/tb_204_lbusy_reg.sv"' in RUN
    assert "tb_197b_leastu tb_204_lbusy_reg" in RUN


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_tb_204_passes(tmp_path):
    tb = "tb_204_lbusy_reg"
    cmd = ["verilator", "--binary", "--timing", "-j", "4", "-Wno-fatal", "-Wno-lint",
           "-Wno-style", "-Wno-WIDTH", "+incdir+rtl/cfg", "+incdir+rtl/pkg",
           "+incdir+rtl/common", "--Mdir", str(tmp_path), "--top-module", tb,
           "rtl/pkg/janus_pkg.sv", "rtl/periph/job_arbiter.sv", f"tb/{tb}.sv"]
    b = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert b.returncode == 0, b.stderr[-2000:]
    r = subprocess.run([str(tmp_path / f"V{tb}")], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    assert "TB_204: ALL TESTS PASS" in r.stdout, (r.stdout + r.stderr)[-2000:]
