"""§200 (probe #3 after §199: S -1405.9 / H -798.8). kv/noc fixed; hold WNS is
u_periph/u_ampo/user_clk hold_in -798.8 (50 LIB_CTP delaybufs on its CTS
branch, pin 4710 vs reg median 3981; movable want 634.2) -> isolate the ampo
user_clk pin (flat-netlist escaped hierarchical instance name). Probe: CORNER
all_* fixed (-sort_by_slack), new pre-CTS IDEAL lines (3_place S -214 vs the
skew-invariant post-CTS u_user pair a -1405.9 + b -1359.5).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
PROBE = (ROOT / "asic_flow/scripts/debug/soc_cts_align_probe.tcl").read_text()
BAL = (HOOKS / "janus_cts_balance.tcl").read_text()
FIXD = Path(__file__).resolve().parent / "fixtures" / "t198d"
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")


def _hier():
    h = MK[MK.index("ifneq ($(JANUS_FLAT),1)"):]
    return h[:h.index("\nendif")]


def test_mk_isolates_ampo_user_clk_only():
    m = re.search(r"^export JANUS_CTS_MACRO_ALIGN_ISOLATE\s*=\s*(.+?)\s*$", _hier(), re.M)
    assert m
    iso = m.group(1).split()
    assert iso == ["u_user/user_clk", "u_kv_secure_subsystem/clk", "u_noc/clk",
                   "u_periph/u_ampo/user_clk"]
    assert re.search(r"^export JANUS_CTS_MACRO_ALIGN_CLOCKS\s*=\s*clk_user\s*$", _hier(), re.M)


def test_sdc_untouched_and_no_exceptions():
    sdc = (ROOT / "asic_flow/configs/constraints/janus_soc_top.sdc").read_text()
    assert "§200" not in sdc
    for kw in ("set_false_path", "set_multicycle_path", "set_max_delay", "set_disable_timing"):
        assert kw not in BAL


def test_probe_corner_sorted_and_ideal_section():
    c = PROBE[PROBE.index("proc sap_corner_paths"):PROBE.index("sap_corner_paths all_setup")]
    assert "-sort_by_slack" in c
    assert "proc sap_ideal" in PROBE and "SAP: IDEAL" in PROBE
    assert PROBE.index("SAP: IDEAL summary") < PROBE.index("clock_tree_synthesis {*}$cts_args")
    assert PROBE.index("load_design 3_place.odb") < PROBE.index("proc sap_ideal")
    assert "u_periph/u_ampo" in PROBE[PROBE.index("# ---- 2c"):]
    assert "write_db" not in PROBE


def test_toy_isolate_escaped_hier_macro(tmp_path):
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")

    def run(name, iso):
        d = tmp_path / name
        shutil.copytree(FIXD, d)
        v = (d / "t.v").read_text().replace("FAKEM2 u_m2 ", "FAKEM2 \\u_periph/u_ampo  ")
        (d / "t.v").write_text(v)
        t = (d / "t.tcl").read_text()
        t = t.replace("u_m2 60 220", "{u_periph\\/u_ampo} 60 220").replace("u_m2/", "u_periph/u_ampo/")
        t = t.replace("get_cells u_m2", "get_cells u_periph/u_ampo")
        (d / "t.tcl").write_text(t)
        env = dict(os.environ, T198_PLATFORM=PLAT, T198_HOOK=str(HOOKS / "janus_cts_balance.tcl"),
                   JANUS_CTS_MACRO_ALIGN="1", JANUS_CTS_MACRO_ALIGN_CLOCKS="clk_user",
                   JANUS_CTS_MACRO_ALIGN_ISOLATE=iso)
        r = subprocess.run([OR, "-no_init", "-exit", "t.tcl"], cwd=d, env=env, capture_output=True,
                           text=True, timeout=600)
        out = r.stdout + r.stderr
        m = re.search(r"T198D FINAL setup (-?[\d.]+) hold (-?[\d.]+)", out)
        assert m, out[-3000:]
        h = re.search(r"T198D u_m2 in hold (-?[\d.]+)", out)
        assert h, out[-3000:]
        return float(m.group(1)), float(m.group(2)), float(h.group(1)), out

    s1, h1, a1, _ = run("one", "u_m/clk")
    s2, h2, a2, out = run("two", "u_m/clk u_periph/u_ampo/clk")
    assert "2 macro(s) re-tapped" in out and "u_periph/u_ampo/clk=" in out
    assert a1 < 0 and a2 > a1 + 50
    assert h2 >= 0 and h2 > h1
    assert s2 >= s1 - 5
