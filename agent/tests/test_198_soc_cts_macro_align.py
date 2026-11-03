"""§198 (run 20261103_130839 failed at 4_cts: pre-repair hold -3690, 47400
hold buffers, RSZ-0060).
198   janus_cts_macro_align: per-macro slack-driven parent/child clock alignment
      (macro-only delaybuf removal, register-branch push, u_clkgen early tap,
      per-macro trim). Toy OpenROAD run with a fake ETM macro when available.
198b  JANUS_CTS_HOLD_ABORT_PS fail-fast before repair_timing.
198c  report_bundle: a failed run does not quote reports / fmax / "Stage
      reached" from an older run's files.
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
HOOKS = ROOT / "asic_flow/scripts/hooks"
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import report_bundle as rb  # noqa: E402

BAL = (HOOKS / "janus_cts_balance.tcl").read_text()
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
PROBE = (ROOT / "asic_flow/scripts/debug/soc_cts_align_probe.tcl").read_text()
FIX = Path(__file__).resolve().parent / "fixtures" / "t198"


def _wrapper():
    i = BAL.index("proc clock_tree_synthesis { args }")
    return BAL[i:BAL.index("\n  }\n", i)]


# ---- 198 static -------------------------------------------------------------
def test_align_procs_defined():
    for p in ("janus_cts_macro_align", "janus_mal_macro_slacks", "janus_mal_macro_delaybufs",
              "janus_mal_up_nets", "janus_mal_chain", "janus_mal_early", "janus_cts_hold_abort"):
        assert re.search(r"^proc %s \{" % p, BAL, re.M), p


def test_wrapper_order_refpin_align_diag_abort():
    w = _wrapper()
    i_ref = w.index("janus_cts_io_refpin_multi")
    i_al = w.index("janus_cts_macro_align")
    i_dg = w.index("janus_cts_diag")
    i_ab = w.index("janus_cts_hold_abort")
    assert i_ref < i_al < i_dg < i_ab
    # abort is NOT caught (it must fail the stage); align is caught
    assert "catch {janus_cts_hold_abort}" not in w
    assert "catch {janus_cts_macro_align}" in w


def test_wrapper_armed_by_new_flags():
    arm = BAL[BAL.index("set janus_cts_malign"):BAL.index("rename clock_tree_synthesis janus_orig")]
    assert "$janus_cts_malign" in arm and "$janus_cts_habort" in arm


def test_want_formula_and_hold_credit():
    body = BAL[BAL.index("proc janus_mal_macro_slacks"):BAL.index("proc janus_mal_fmt")]
    assert "set a [janus_mal_min $so [expr {$hi eq \"\" ? \"\" : $hi + $hc}]]" in body
    assert "set b [janus_mal_min $si [expr {$ho eq \"\" ? \"\" : $ho + $hc}]]" in body
    assert "($b - $a) / 2.0" in body


def test_early_tap_and_no_gate_cloning_change():
    al = BAL[BAL.index("proc janus_cts_macro_align"):BAL.index("proc janus_cts_hold_abort")]
    assert "$rit disconnect" in al and "$rit connect $top_net" in al
    assert "-no_clock_gate_cloning" not in BAL           # Kiran's review rule
    assert 'string map {\\\\ ""} $name' in BAL           # escaped odb names


def test_mk_enables_align_and_abort_in_hier_block():
    hier = MK[MK.index("ifneq ($(JANUS_FLAT),1)"):]
    hier = hier[:hier.index("\nendif")]
    for k, v in (("JANUS_CTS_MACRO_ALIGN", "1"), ("JANUS_CTS_MACRO_ALIGN_CLOCKS", "clk_user clk_agent"),
                 ("JANUS_CTS_MACRO_ALIGN_EARLY", "u_clkgen/*"), ("JANUS_CTS_HOLD_ABORT_PS", "-1500")):
        assert re.search(r"^export %s\s*=\s*%s\s*$" % (k, re.escape(v)), hier, re.M), k


def test_probe_is_read_only_and_disables_abort():
    assert "orfs_write_db" not in PROBE and "write_db" not in PROBE
    assert "repair_timing" not in PROBE.split("=====\n", 2)[-1].split("proc sap_env")[1]
    assert 'set ::env(JANUS_CTS_HOLD_ABORT_PS) ""' in PROBE
    assert PROBE.index("set ::env(JANUS_CTS_MACRO_ALIGN)") < PROBE.index("source_step_tcl PRE CTS")


# ---- 198 toy OpenROAD run (fake ETM macro, hold 500 / clk->q 1000) ----------
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")


def _toy(tmp_path, extra):
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")
    d = tmp_path / "t198"
    shutil.copytree(FIX, d)
    env = dict(os.environ, T198_PLATFORM=PLAT, T198_HOOK=str(HOOKS / "janus_cts_balance.tcl"), **extra)
    r = subprocess.run([OR, "-exit", "t.tcl"], cwd=d, env=env, capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    m = re.search(r"T198 FINAL setup (-?[\d.]+) hold (-?[\d.]+)", out)
    assert m, out[-3000:]
    arr = dict(re.findall(r"T198 arr (\S+) (-?[\d.]+)", out))
    return float(m.group(1)), float(m.group(2)), {k: float(v) for k, v in arr.items()}, out


def test_toy_align_fixes_macro_boundary(tmp_path):
    s0, h0, a0, _ = _toy(tmp_path / "off", {"JANUS_CTS_DIAG": "1"})
    assert h0 < -300                                    # hold into the ETM macro
    s1, h1, a1, out = _toy(tmp_path / "on", {"JANUS_CTS_MACRO_ALIGN": "1",
                                             "JANUS_CTS_MACRO_ALIGN_CLOCKS": "clk_user"})
    assert "(A) removed" in out and "(B) iter" in out
    assert h1 >= 0 and s1 > s0                          # hold fixed, setup improved
    assert a1["r1/CLK"] - a1["u_m/clk"] > 400           # regs late vs the ETM macro
    assert abs(a1["u_m2/clk"] - a1["r1/CLK"]) < 100     # positive-slack macro trimmed back
    assert "early tap: 1 reg(s)" in out
    assert a1["u_clkgen/enq/CLK"] < 100                 # clock-gen flop stays early


def test_toy_hold_abort(tmp_path):
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")
    d = tmp_path / "t198"
    shutil.copytree(FIX, d)
    env = dict(os.environ, T198_PLATFORM=PLAT, T198_HOOK=str(HOOKS / "janus_cts_balance.tcl"),
               JANUS_CTS_HOLD_ABORT_PS="-100")
    r = subprocess.run([OR, "-exit", "t.tcl"], cwd=d, env=env, capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    assert "JANUS_CTS_HOLD_ABORT: pre-repair hold WNS" in out and "aborting before repair_timing" in out
    assert "T198 FINAL" not in out


# ---- 198c report_bundle -----------------------------------------------------
def _rpt(setup, tns, hold=-5.0):
    # janus_stage_dump.tcl header format (what _la.parse_stage_timing_rpt reads)
    return ("JANUS per-stage timing dump\n\nSETUP report_wns\n----\nwns max %.2f\n\n"
            "SETUP report_tns\n----\ntns max %.2f\n\n"
            "HOLD report_worst_slack -min\n----\nworst slack min %.2f\n") % (setup, tns, hold)


def test_failed_run_drops_older_run_same_stage_report(tmp_path):
    logs, reps, res = tmp_path / "logs", tmp_path / "reports", tmp_path / "results"
    for d in (logs, reps, res):
        d.mkdir()
    old = time.time() - 86400
    (reps / "janus_06_cts_paths.rpt").write_text(_rpt(-2488.19, -54067.94, -1066.13))
    (reps / "6_finish.rpt").write_text("clk_user period_min = 5138.85 fmax = 194.60\n")
    (logs / "6_report.log").write_text("x\n")
    for n in ("janus_06_cts_paths.rpt", "6_finish.rpt"):
        os.utime(reps / n, (old, old))
    os.utime(logs / "6_report.log", (old, old))
    (reps / "janus_05_place_paths.rpt").write_text(_rpt(-214.0, -14000.0, 3.9))
    for n in ("3_3_place_gp.log", "3_4_place_resized.log", "4_1_cts.log"):
        (logs / n).write_text("x\n")
    q = rb.build_qor("janus_soc_top", "FAILED", str(logs), str(reps), str(res))
    assert q["failed_stage"] == "cts"
    assert q["stage_reached"] == "cts"                  # not "finish"
    t = q["timing"]
    assert t["setup_worst_slack"] != -2488.19
    assert t["hold_worst_slack"] != -1066.13
    assert t["setup_worst_slack"] == -214.0 and t["source_file"] == "janus_05_place_paths.rpt"
    body = rb.render_email_body(q)
    assert "-2488.19" not in body and "194.60" not in body
    # janus_06 preferred by the primary selector (no 6_finish timing) -> dropped
    (reps / "6_finish.rpt").unlink()
    q = rb.build_qor("janus_soc_top", "FAILED", str(logs), str(reps), str(res))
    assert q["timing"].get("stale_dropped") == "janus_06_cts_paths.rpt"
    assert q["timing"]["setup_worst_slack"] == -214.0
    assert "older than this run's earlier-stage logs" in rb.render_email_body(q)


def test_fresh_same_stage_report_kept(tmp_path):
    logs, reps = tmp_path / "logs", tmp_path / "reports"
    logs.mkdir(); reps.mkdir()
    old = time.time() - 3600
    for n in ("3_4_place_resized.log",):
        (logs / n).write_text("x\n")
        os.utime(logs / n, (old, old))
    (logs / "4_1_cts.log").write_text("x\n")
    (reps / "janus_06_cts_paths.rpt").write_text(_rpt(-50.0, -100.0))
    q = rb.build_qor("janus_soc_top", "FAILED", str(logs), str(reps), None)
    assert q["timing"]["source_file"] == "janus_06_cts_paths.rpt"
    assert "stale_dropped" not in q["timing"]


def test_stale_vs_logs_unit(tmp_path):
    logs = tmp_path / "logs"; logs.mkdir()
    f = tmp_path / "janus_06_cts_paths.rpt"; f.write_text("x")
    (logs / "3_4_place_resized.log").write_text("x")
    old = time.time() - 7200
    os.utime(f, (old, old))
    assert rb._stale_vs_logs(str(f), str(logs))
    assert not rb._stale_vs_logs(str(f), None)
    os.utime(f, None)
    assert not rb._stale_vs_logs(str(f), str(logs))
