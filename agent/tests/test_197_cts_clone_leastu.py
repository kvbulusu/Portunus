"""§197 (run 20261102_221045 failed at 4_cts, 10 clock-root clones).

197a  TritonCTS clones clock gaters inside clock_tree_synthesis
      (cloneClockGaters; skip only if driver inst isFixed). The guard now
      FIRMs the create_clock-owning roots for that call only, restores the
      status after, and checks clones right after TritonCTS (fail fast).
197b  job_scheduler POL_LEASTU serial best_u chain -> balanced min-tree
      (pre-CTS WNS -2716 ps on affinity_o -> sched_core_o).
197c  report_bundle: no QoR / route DRC quoted from a later-stage report
      left by an older run.
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

CRG = (HOOKS / "janus_clkroot_guard.tcl").read_text()
BAL = (HOOKS / "janus_cts_balance.tcl").read_text()
JS = (ROOT / "rtl/periph/job_scheduler.sv").read_text()


def _proc(src, name):
    m = re.search(r"proc %s \{.*?\n  \}\n" % re.escape(name), src, re.S)
    assert m, name
    return m.group(0)


# ---- 197a -------------------------------------------------------------------
def test_wrap_cts_firms_roots_only_around_tritoncts():
    body = _proc(CRG, "wrap_cts")
    assert "rename ::clock_tree_synthesis ::janus_crg_orig_cts" in body
    assert "janus_crg::roots" in body               # create_clock owners only
    i_fix = body.index("fix_roots $r 1")
    i_call = body.index("::janus_crg_orig_cts {*}$args")
    i_rest = body.index("fix_roots $r 0")
    i_chk = body.index("janus_crg::check")
    assert i_fix < i_call < i_rest < i_chk          # restore before check/error
    assert "JANUS_CLKROOT_CTS_FIX" in body and "JANUS_CLKROOT_GUARD" in body


def test_fix_roots_restores_saved_status_and_skips_fixed():
    body = _proc(CRG, "fix_roots")
    assert "setPlacementStatus FIRM" in body
    assert "setPlacementStatus $st" in body
    assert "FIRM LOCKED COVER" in body


def test_cts_balance_installs_wrap_before_its_own_wrapper():
    i_wrap = BAL.index("janus_crg::wrap_cts cts")
    assert BAL.index("janus_crg::wrap_repair_timing cts") < i_wrap
    # balance's own clock_tree_synthesis wrapper must chain on top of it
    assert i_wrap < BAL.index("rename clock_tree_synthesis janus_orig_clock_tree_synthesis")


def test_gate_cloning_not_disabled_globally():
    # Kiran's rule: never disable gate cloning globally (rsz or CTS).
    assert "SKIP_GATE_CLONING) 1" not in CRG
    assert "-no_clock_gate_cloning" not in CRG + BAL


# ---- 197b -------------------------------------------------------------------
def test_leastu_is_tree_not_serial_chain():
    assert "best_u =" not in JS and "logic [7:0] best_u" not in JS
    assert "POL_LEASTU: sched_core_c = leastu_core;" in JS
    assert re.search(r"lu_key\[2\*n\+1\] <= lu_key\[2\*n\+2\]", JS)  # ties -> lower index


PKGS = ["rtl/pkg/janus_pkg.sv", "rtl/pkg/axi4_pkg.sv", "rtl/pkg/agent_pkg.sv", "rtl/pkg/user_pkg.sv"]
JOBS = ["rtl/periph/job_queue.sv", "rtl/periph/job_arbiter.sv", "rtl/periph/agent_affinity_table.sv",
        "rtl/periph/perf_counter_subsystem.sv", "rtl/periph/job_scheduler.sv"]


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_leastu_tree_equals_serial_reference(tmp_path):
    tb = "tb_197b_leastu"
    cmd = ["verilator", "--binary", "--timing", "-j", "4", "-Wno-fatal", "-Wno-lint",
           "-Wno-style", "-Wno-WIDTH", "+incdir+rtl/cfg", "+incdir+rtl/pkg",
           "+incdir+rtl/common", "+incdir+tb", "--Mdir", str(tmp_path), "--top-module", tb,
           *PKGS, *JOBS, f"tb/{tb}.sv"]
    b = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert b.returncode == 0, b.stderr[-2000:]
    r = subprocess.run([str(tmp_path / f"V{tb}")], cwd=ROOT, capture_output=True,
                       text=True, timeout=300)
    assert "ALL TESTS PASS" in r.stdout, (r.stdout + r.stderr)[-2000:]


# ---- 197c -------------------------------------------------------------------
def _rpt(wns, tns):
    return f"JANUS per-stage timing dump\n\nSETUP report_wns\n----\nwns max {wns}\n\nSETUP report_tns\n----\ntns max {tns}\n"


def test_after_stage():
    assert rb._after_stage("janus_08_detail_route_paths.rpt", "cts")
    assert not rb._after_stage("janus_06_cts_paths.rpt", "cts")
    assert not rb._after_stage("janus_eco_adopted.json", "cts")


def test_failed_cts_run_drops_stale_route_timing(tmp_path):
    logs, reps = tmp_path / "logs", tmp_path / "reports"
    logs.mkdir(); reps.mkdir()
    old = time.time() - 86400
    (reps / "janus_08_detail_route_paths.rpt").write_text(_rpt(-779.83, -9999.0))
    (reps / "5_route_drc.rpt").write_text("violation type: Short\n")
    for n in ("janus_08_detail_route_paths.rpt", "5_route_drc.rpt"):
        os.utime(reps / n, (old, old))
    (reps / "janus_06_cts_paths.rpt").write_text(_rpt(-2488.19, -54067.94))
    for n in ("3_4_place_resized.log", "4_1_cts.log"):
        (logs / n).write_text("x\n")
    q = rb.build_qor("janus_soc_top", "FAILED", str(logs), str(reps), None)
    assert q["failed_stage"] == "cts"
    t = q["timing"]
    assert t["source_file"] == "janus_06_cts_paths.rpt"
    assert t["setup_wns"] == -2488.19
    assert t["stale_dropped"] == "janus_08_detail_route_paths.rpt"
    assert q["route_drc"] is None
    body = rb.render_email_body(q)
    assert "-779.83" not in body
    assert "not quoted" in body


def test_done_run_keeps_route_timing(tmp_path):
    reps = tmp_path / "reports"; reps.mkdir()
    (reps / "janus_08_detail_route_paths.rpt").write_text(_rpt(-12.5, -40.0))
    q = rb.build_qor("janus_soc_top", "DONE", None, str(reps), None)
    assert q["timing"]["source_file"] == "janus_08_detail_route_paths.rpt"
    assert "stale_dropped" not in q["timing"]
