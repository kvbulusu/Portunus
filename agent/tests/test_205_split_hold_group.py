"""§205 (split probe on the old 3_place odb): §203 fixed the fast corner but
leaked hold (2363 -> 5674 endpoints, slow -282.7 -> -330.2) and left two
clock-unfixable setup paths. Fixes:
  hook - HOLD_CREDIT 0, EXCLUDE reset syncs / u_debug, GROUP co-move of
         link-slice regs with their same-hier fanin, EARLY hold guard;
  RTL  - clint mtip registered before u_user (-656.0, all 43 regs unfixable),
         npu_s launch + descriptor registered before the clk_agent CDC (-234.2,
         capture regs the clk_user tap cannot move).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
TSP = (HOOKS / "janus_cts_tap_split.tcl").read_text()
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
TOP = (ROOT / "rtl/top/janus_soc_top.sv").read_text()
FIXD = Path(__file__).resolve().parent / "fixtures" / "t198d"


def test_hold_credit_default_zero_and_mk():
    t = TSP[TSP.index("proc janus_cts_tap_split"):]
    assert "set hc [janus_tsp_env HOLD_CREDIT 0]" in t
    assert "JANUS_CTS_MACRO_ALIGN_HOLD_CREDIT" not in t       # no 300 inheritance
    assert re.search(r"^export JANUS_CTS_TAP_SPLIT_HOLD_CREDIT\s*=\s*0\s*$", MK, re.M)


def test_exclude_and_group_defaults():
    assert 'EXCLUDE "u_rst_* u_debug"' in TSP
    assert 'GROUP "u_ucut_*"' in TSP
    assert "all_fanin -to" not in TSP and "get_property $pe startpoint" in TSP
    # intra-group paths are dropped from both budgets
    assert "janus_tsp_slack_excl $ins min in EX" in TSP
    assert "janus_tsp_slack_excl $jouts max out EX" in TSP


def test_early_hold_guard_present():
    e = TSP[TSP.index("proc janus_tsp_early"):TSP.index("proc janus_tsp_hier")]
    assert "HOLD_GUARD 1" in e and "min($H0($pn), -$hc)" in e and "tsg" in e


def test_no_exceptions_added():
    for bad in ("set_false_path", "set_multicycle_path", "set_max_delay", "set_disable_timing"):
        assert bad not in TSP


def test_rtl_mtip_registered_before_user():
    assert ".clint_mtip_i   (clint_mtip_q)" in TOP
    assert re.search(r"clint_mtip_q <= clint_mtip;", TOP)


def test_rtl_npu_s_launch_and_job_registered_together():
    assert ".src_pulse_i(npu_s_launch_q)" in TOP
    assert ".data_i   (npu_s_job_q)" in TOP
    blk = TOP[TOP.index("npu_s_launch_q <= npu_s_launch;") - 200:TOP.index("npu_s_launch_q <= npu_s_launch;") + 80]
    assert "npu_s_job_q    <= npu_s_job;" in blk                  # same edge, same lead


# ---- toy: t198d with u_m's output captured by a 2-reg link slice u_ucut_t
# (sk captures mo; rx = mo | sk). rx is hold-limited by sk -> rx; only the
# GROUP co-move can retime it.
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")


def _toy(tmp_path, extra):
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")
    d = tmp_path / "t205"
    shutil.copytree(FIXD, d)
    v = (d / "t.v").read_text()
    old = "wire x; DFFHQNx1_ASAP7_75t_R rx (.CLK(g), .D(mo), .QN(x));"
    assert old in v
    v = v.replace(old, "wire mob2, skq, rxd, x;\n"
                  "BUFx2_ASAP7_75t_R usb (.A(mo), .Y(mob2));\n"
                  "DFFHQNx1_ASAP7_75t_R \\u_ucut_t/sk  (.CLK(g), .D(mob2), .QN(skq));\n"
                  "OR2x2_ASAP7_75t_R uor (.A(mo), .B(skq), .Y(rxd));\n"
                  "DFFHQNx1_ASAP7_75t_R \\u_ucut_t/rx  (.CLK(g), .D(rxd), .QN(x));")
    (d / "t.v").write_text(v)
    t = (d / "t.tcl").read_text().replace("rx/D", "u_ucut_t/rx/D")
    (d / "t2.tcl").write_text(t + """
puts "T205 out [ws -from [get_cells u_m] -to [get_pins u_ucut_t/rx/D] -path_delay max] skrx_hold [ws -from [get_pins u_ucut_t/sk/CLK] -to [get_pins u_ucut_t/rx/D] -path_delay min]"
foreach p {u_ucut_t/rx/CLK u_ucut_t/sk/CLK r4/CLK} { puts [format "T205 arr %s %.1f" $p [get_property [get_pins $p] arrival_max_rise]] }
""")
    env = dict(os.environ, T198_PLATFORM=PLAT, T198_HOOK=str(HOOKS / "janus_cts_balance.tcl"),
               JANUS_CTS_MACRO_ALIGN="1", JANUS_CTS_MACRO_ALIGN_ISOLATE="u_m/clk",
               JANUS_CTS_MACRO_ALIGN_CLOCKS="clk_user", JANUS_CTS_TAP_SPLIT="1",
               JANUS_CTS_TAP_SPLIT_MACROS="u_m", JANUS_CTS_TAP_SPLIT_TARGET="600",
               JANUS_CTS_TAP_SPLIT_ITERS="1", **extra)
    r = subprocess.run([OR, "-no_init", "-exit", "t2.tcl"], cwd=d, env=env, capture_output=True,
                       text=True, timeout=180)
    out = r.stdout + r.stderr
    m = re.search(r"T205 out (-?[\d.]+) skrx_hold (-?[\d.]+)", out)
    assert m, out[-3000:]
    res = {"out": float(m.group(1)), "skrx_hold": float(m.group(2))}
    res["arr"] = {k: float(x) for k, x in re.findall(r"T205 arr (\S+) (-?[\d.]+)", out)}
    return res, out


def test_toy_group_comove_retimes_hold_limited_slice(tmp_path):
    v0, out0 = _toy(tmp_path / "nogrp", {"JANUS_CTS_TAP_SPLIT_GROUP": "none"})
    v1, out1 = _toy(tmp_path / "grp", {})
    assert "GROUP u_ucut_t/rx +1 fanin reg(s)" in out1, out1[-3000:]
    assert "GROUP" not in out0.split("JANUS_CTS_TAP_SPLIT: before")[1].split("after")[0]
    # rx gets retimed further with the group, and both slice regs move together
    assert v1["out"] > v0["out"] + 25
    a = v1["arr"]
    assert a["u_ucut_t/rx/CLK"] == pytest.approx(a["u_ucut_t/sk/CLK"], abs=30)
    assert v1["skrx_hold"] >= 0                                  # no intra-slice hold leak
    assert a["r4/CLK"] == pytest.approx(v0["arr"]["r4/CLK"], abs=15)
