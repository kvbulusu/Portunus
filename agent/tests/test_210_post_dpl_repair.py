"""§210 (run 20261104_201235): 3_4 WNS -1644 -> 3_5 -3964. 3_4 repair_design
put rsz buffers on top of u_user, 3_5 DPL dragged them out (max disp 468.6 um),
leaving ~700 um unbuffered wires. Fix: janus_post_dpl_repair.tcl re-buffers
after DPL with in-macro cells evicted to the macro edge. Also: a clean
--stop-flow-at stop is not a failure (no stale 4_cts row / RED flag, no
"oom-killed" triage).
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
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
FIXD = Path(__file__).resolve().parent / "fixtures" / "t210"
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")
sys.path[:0] = [str(ROOT / "asic_flow/agent/tests"), str(ROOT / "asic_flow/utils")]


def _toy(tmp_path, tag, **env):
    """Toy: 400x400 macro, start regs above it, end regs east of it;
    repair_design -max_wire_length 60 then DPL (stock 3_4 -> 3_5), then the
    T210_HOOK_DPL hook (= POST_DETAIL_PLACE)."""
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")
    d = tmp_path / f"t210_{tag}"
    shutil.copytree(FIXD, d)
    (d / "none.tcl").write_text("")
    e = dict(os.environ, T198_PLATFORM=PLAT, T210_HOOK_PRE="none.tcl",
             T210_HOOK_POST="none.tcl", JANUS_DUMP_REPORTS="0",
             JANUS_DUMP_NETLISTS="0", JANUS_DUMP_IMAGES="0", **env)
    r = subprocess.run([OR, "-no_init", "-exit", "t.tcl"], cwd=d, env=e,
                       capture_output=True, text=True, timeout=600)
    out = r.stdout + r.stderr
    assert r.returncode == 0, out[-3000:]
    return out


def _f(out, pat):
    m = re.search(pat, out)
    assert m, out[-3000:]
    return float(m.group(1))


def test_toy_reproduces_dpl_drag(tmp_path):
    out = _toy(tmp_path, "base")
    assert _f(out, r"T210 after_rsz buffers_in_macro (\d+)") > 100
    assert _f(out, r"T210 dpl '' buffers_in_macro (\d+)") == 0
    assert _f(out, r"T210 wns (-?[\d.]+)") < -200          # -229.9


def test_post_dpl_repair_via_debug_wrapper(tmp_path):
    # agent path: POST_DETAIL_PLACE_TCL=post_detail_place.tcl chains JANUS_POST_DPL_TCL
    out = _toy(tmp_path, "wrap", T210_HOOK_DPL=str(HOOKS / "post_detail_place.tcl"),
               JANUS_POST_DPL_TCL=str(HOOKS / "janus_post_dpl_repair.tcl"))
    assert "JANUS_POST_DPL: before wns -229.9" in out, out[-3000:]
    assert re.search(r"JANUS_POST_DPL: iter 1 repair_design .*-max_wire_length 200 evicted [1-9]", out)
    assert "JANUS_POST_DPL: repair_timing" in out
    assert "WARN" not in "".join(l for l in out.splitlines() if "JANUS_POST_DPL" in l), out[-3000:]
    assert _f(out, r"T210 final buffers_in_macro (\d+)") == 0
    assert "T210 check_placement ''" in out
    assert _f(out, r"T210 wns (-?[\d.]+)") > -100          # -33.4 measured


def test_post_dpl_repair_disabled_is_stock(tmp_path):
    out = _toy(tmp_path, "off", T210_HOOK_DPL=str(HOOKS / "janus_post_dpl_repair.tcl"),
               JANUS_POST_DPL_REPAIR="0")
    assert "JANUS_POST_DPL: disabled" in out
    assert abs(_f(out, r"T210 wns (-?[\d.]+)") - _f(out, r"T210 wns_dpl (-?[\d.]+)")) < 0.01


def test_cts_pre_rsz_evicts_before_dpl(tmp_path):
    d = tmp_path / "cts.tcl"
    d.write_text("set ::env(JANUS_CTS_PRE_RSZ) 1\nset ::env(JANUS_CTS_PRE_RSZ_MAX_WIRE_UM) 60\n"
                 f"source {HOOKS / 'janus_cts_pre_rsz.tcl'}\n")
    out = _toy(tmp_path, "cts", T210_HOOK_DPL=str(d))
    assert re.search(r"JANUS_CTS_PRE_RSZ: evicted [1-9]\d* cells from macro areas", out), out[-3000:]
    assert "JANUS_CTS_PRE_RSZ: WARN" not in out
    assert _f(out, r"T210 final buffers_in_macro (\d+)") == 0
    assert _f(out, r"T210 wns (-?[\d.]+)") > _f(out, r"T210 wns_dpl (-?[\d.]+)")


def test_mk_and_wrappers_wired():
    assert re.search(r"^export JANUS_POST_DPL_TCL\s*=.*janus_post_dpl_repair\.tcl", MK, re.M)
    assert re.search(r"^export POST_DETAIL_PLACE_TCL\s*=\s*\$\(JANUS_POST_DPL_TCL\)", MK, re.M)
    assert re.search(r"^export JANUS_POST_DPL_REPAIR\s*=\s*1", MK, re.M)
    w = (HOOKS / "post_detail_place.tcl").read_text()
    assert w.index("JANUS_POST_DPL_TCL") < w.rindex("janus_stage_dump.tcl")   # repair before dump
    a = (HOOKS / "janus_adaptive_post.tcl").read_text()
    assert "JANUS_POST_DPL_TCL" in a


# ---------------------------------------------------------------- reports
def test_stop_flow_at_stage_email_no_stale_cts(tmp_path):
    import stage_report as sr
    from test_175_notify_emails import _mk
    logs, rpt, res = _mk(str(tmp_path))
    old = time.time() - 3600
    for d in (logs, rpt, res):
        for f in os.listdir(d):
            os.utime(os.path.join(d, f), (old, old))
    open(os.path.join(rpt, "4_cts_final.rpt"), "a").write("hold buffers 6191\n")
    run_start = time.time() - 60
    open(os.path.join(logs, "3_5_place_dp.log"), "w").write("Elapsed time: 5:12.68[h:]min:sec.\n")
    # the agent's stage email after the stop: stage=3_place, current=None
    r = sr.build_report("janus_soc_top", "stage", stage="3_place", logs_dir=logs,
                        reports_dir=rpt, results_dir=res, run_start=run_start)
    st = {x["key"]: x["status"] for x in r["rows"]}
    assert st["3_place"] in ("DONE", "REUSED") and st["4_cts"] == "PENDING", st
    assert not [f for f in r["flags"] if "4_cts" in f[1]], r["flags"]
    assert "6191" not in r["text"]


def test_resume_reused_prefix_still_reused(tmp_path):
    import stage_report as sr
    from test_175_notify_emails import _mk
    logs, rpt, res = _mk(str(tmp_path))
    old = time.time() - 3600
    for d in (logs, rpt, res):
        for f in os.listdir(d):
            os.utime(os.path.join(d, f), (old, old))
    run_start = time.time() - 60
    open(os.path.join(logs, "3_3_place_gp.log"), "w").write("x\n")
    st = {x["key"]: x["status"] for x in
          sr.build_rows(logs, rpt, res, current="3_place", reused=["1_synth"], run_start=run_start)}
    assert st["1_synth"] == "REUSED" and st["3_place"] == "RUNNING" and st["4_cts"] == "PENDING", st


def test_bundle_stopped_is_not_a_failure(tmp_path):
    import report_bundle as rb
    from test_report_bundle import _tree, _w
    flow, L, R, S = _tree(str(tmp_path), "janus_soc_top",
                          stages_done=("1_synth", "2_floorplan"), finish=False)
    p = _w(os.path.join(R, "janus_08_detail_route_paths.rpt"), "old\n")
    os.utime(p, (time.time() - 5000, time.time() - 5000))
    _w(os.path.join(L, "3_5_place_dp.log"),
       "Elapsed time: 5:12.68[h:]min:sec. CPU time: user 398.40 sys 35.94 (138%). "
       "Peak memory: 14335028KB.\n")
    res = rb.build_bundle("janus_soc_top", "STOPPED", "", logs_dir=L, reports_dir=R,
                          results_dir=S, dry_run=True)
    q = res["qor"]
    assert q["failed_stage"] is None and q["stopped_at"] == "place", q
    assert q["triage"] is None
    assert "FAILURE TRIAGE" not in res["body"] and "oom" not in res["body"].lower()
    assert "Failed at" not in res["body"] and "Stopped at   : place" in res["body"]
    assert any(a == "reports/janus_08_detail_route_paths.rpt" for a, _ in res["skipped"])
    # FAILED keeps the §198 triage
    q2 = rb.build_qor("janus_soc_top", "FAILED", L, R, S)
    assert q2["failed_stage"] == "place" and q2["triage"] is not None
