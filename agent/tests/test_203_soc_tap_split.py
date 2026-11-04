"""§203 (probe #5, per-corner ETMs): after the §198d isolate push the u_user
boundary is balanced but both sides fail (movable a -738.4 out / b -731.7 in,
fast in-hold -177.2). janus_cts_tap_split moves the REGISTERS of each side on
their own clock tap (LATE: capture regs of failing outputs + launch regs of
hold-failing inputs; EARLY: launch regs of setup-failing inputs), each within
the budget of its other side. Clock-structure only, no timing exceptions.
The probe gets SAP_MODE=split|splitdry plus WINDOW / SELFLOOP lines.
"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
BAL = (HOOKS / "janus_cts_balance.tcl").read_text()
TSP = (HOOKS / "janus_cts_tap_split.tcl").read_text()
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
PROBE = (ROOT / "asic_flow/scripts/debug/soc_cts_align_probe.tcl").read_text()
FIXD = Path(__file__).resolve().parent / "fixtures" / "t198d"
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))


def _wrapper():
    i = BAL.index("proc clock_tree_synthesis { args }")
    return BAL[i:BAL.index("\n  }\n", i)]


def test_procs_defined_and_sourced():
    for p in ("janus_cts_tap_split", "janus_tsp_collect", "janus_tsp_budget", "janus_tsp_late",
              "janus_tsp_early", "janus_tsp_up_net", "janus_tsp_groups"):
        assert re.search(r"^proc %s \{" % p, TSP, re.M), p
    assert "janus_cts_tap_split.tcl" in BAL and "[info script]" in BAL


def test_wrapper_order_and_arming():
    w = _wrapper()
    assert w.index("catch {$mal}") < w.index("catch {janus_cts_tap_split}") < w.index("janus_cts_diag") \
        < w.index("janus_cts_hold_abort")
    arm = BAL[BAL.index("set janus_cts_malign"):BAL.index("rename clock_tree_synthesis janus_orig")]
    assert "$janus_cts_tsplit" in arm


def test_no_exceptions_no_sdc_no_gate_cloning():
    for kw in ("set_false_path", "set_multicycle_path", "set_max_delay", "set_disable_timing",
               "set_clock_latency", "-no_clock_gate_cloning", "write_sdc"):
        assert kw not in TSP, kw
    sdc = (ROOT / "asic_flow/configs/constraints/janus_soc_top.sdc").read_text()
    assert "§203" not in sdc


def test_budget_formula():
    b = TSP[TSP.index("proc janus_tsp_budget"):TSP.index("proc janus_tsp_collect")]
    # late: setup OUT of the reg + hold INTO it; early: setup INTO + hold OUT
    assert 'set s [janus_mal_pins_slack $outs max]\n    set h [janus_mal_pins_slack $ins min]' in b
    assert 'set s [janus_mal_pins_slack $ins max]\n    set h [janus_mal_pins_slack $outs min]' in b
    assert "$s - $guard + $sc" in b and "$h + $hc" in b
    # u_clkgen (clk_agent gate) never moves; regs wanted by both sides skipped
    t = TSP[TSP.index("proc janus_cts_tap_split"):]
    assert "janus_mal_early $i" in t and "incr nboth; continue" in t


def test_mk_enables_split_in_hier_block_and_restamps_cts():
    hier = MK[MK.index("ifneq ($(JANUS_FLAT),1)"):]
    hier = hier[:hier.index("\nendif")]
    for k, v in (("JANUS_CTS_TAP_SPLIT", "1"), ("JANUS_CTS_TAP_SPLIT_MACROS", "u_user"),
                 ("JANUS_CTS_TAP_SPLIT_CLOCK", "clk_user")):
        assert re.search(r"^export %s\s*=\s*%s\s*$" % (k, re.escape(v)), hier, re.M), k
    import importlib
    fs = importlib.import_module("flow_state")
    stage = next(st for pre, st in fs._KEY_STAGE_PREFIX if "JANUS_CTS_TAP_SPLIT".startswith(pre))
    assert stage == "4_cts"                                  # restamp resumes at CTS


def test_probe_split_modes_and_lines():
    assert "split -" in PROBE and "splitdry {" in PROBE
    assert "set ::env(JANUS_CTS_TAP_SPLIT) 1" in PROBE
    assert PROBE.index("set ::env(JANUS_CTS_TAP_SPLIT) 1") < PROBE.index("source_step_tcl PRE CTS")
    assert '"WINDOW %s %s %s regs' in PROBE and '"SELFLOOP slack' in PROBE
    assert "write_db" not in PROBE                            # still read-only


# ---- toy OpenROAD: t198d with the ETM din setup 1000 / hold -200 -> after
# isolate both u_m sides fail (r3->u_m setup -42, u_m->rx -48), self loop -70.
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")


def _toy(tmp_path, extra):
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")
    d = tmp_path / "t203"
    shutil.copytree(FIXD, d)
    lib = (d / "mu.lib").read_text()
    i, j = lib.index("pin(din)"), lib.index("pin(din2)")
    blk = lib[i:j]
    blk = blk.replace('setup_rising;\n      rise_constraint(scalar) { values("50"); } fall_constraint(scalar) { values("50"); }',
                      'setup_rising;\n      rise_constraint(scalar) { values("1000"); } fall_constraint(scalar) { values("1000"); }')
    blk = blk.replace('values("500")', 'values("-200")')
    assert blk != lib[i:j]
    (d / "mu.lib").write_text(lib[:i] + blk + lib[j:])
    (d / "t2.tcl").write_text((d / "t.tcl").read_text() + """
puts "T203 in [ws -to [get_pins u_m/din] -path_delay max] out [ws -from [get_cells u_m] -to [get_pins rx/D] -path_delay max] inhold [ws -to [get_pins u_m/din] -path_delay min]"
foreach p {r3/CLK rx/CLK r4/CLK} { puts [format "T203 arr %s %.1f" $p [get_property [get_pins $p] arrival_max_rise]] }
""")
    env = dict(os.environ, T198_PLATFORM=PLAT, T198_HOOK=str(HOOKS / "janus_cts_balance.tcl"),
               JANUS_CTS_MACRO_ALIGN="1", JANUS_CTS_MACRO_ALIGN_ISOLATE="u_m/clk",
               JANUS_CTS_MACRO_ALIGN_CLOCKS="clk_user", JANUS_CTS_TAP_SPLIT_MACROS="u_m", **extra)
    r = subprocess.run([OR, "-no_init", "-exit", "t2.tcl"], cwd=d, env=env, capture_output=True,
                       text=True, timeout=180)
    out = r.stdout + r.stderr
    m = re.search(r"T203 in (-?[\d.]+) out (-?[\d.]+) inhold (-?[\d.]+)", out)
    assert m, out[-3000:]
    v = dict(zip(("in", "out", "inhold"), map(float, m.groups())))
    v["self"] = float(re.search(r"T198D selfloop u_m->u_m setup (-?[\d.]+)", out).group(1))
    v["arr"] = {k: float(x) for k, x in re.findall(r"T203 arr (\S+) (-?[\d.]+)", out)}
    return v, out


def test_toy_tap_split_fixes_both_sides(tmp_path):
    v0, _ = _toy(tmp_path / "iso", {})
    assert v0["in"] < -20 and v0["out"] < -20                  # balanced, both failing
    v1, out = _toy(tmp_path / "split", {"JANUS_CTS_TAP_SPLIT": "1"})
    assert "JANUS_CTS_TAP_SPLIT: armed" in out and "EARLY re-tapped" in out and "LATE 1 group(s)" in out
    assert "EST-0104" not in out
    assert v1["in"] >= 0 and v1["out"] >= 0                    # both sides fixed
    assert v1["inhold"] >= 0
    assert v1["self"] == pytest.approx(v0["self"], abs=5)      # skew-invariant, untouched
    assert v1["arr"]["r3/CLK"] < v0["arr"]["r3/CLK"] - 25      # launch reg earlier
    assert v1["arr"]["rx/CLK"] > v0["arr"]["rx/CLK"] + 25      # capture reg later
    assert v1["arr"]["r4/CLK"] == pytest.approx(v0["arr"]["r4/CLK"], abs=15)  # others stay


def test_toy_tap_split_dryrun_changes_nothing(tmp_path):
    v0, _ = _toy(tmp_path / "iso", {})
    v1, out = _toy(tmp_path / "dry", {"JANUS_CTS_TAP_SPLIT": "1", "JANUS_CTS_TAP_SPLIT_DRYRUN": "1"})
    assert "PLAN early in_setup r3" in out and "PLAN late out_setup rx" in out
    assert "re-tapped" not in out.split("JANUS_CTS_TAP_SPLIT: before")[1]
    assert v1["in"] == pytest.approx(v0["in"], abs=0.5) and v1["out"] == pytest.approx(v0["out"], abs=0.5)
