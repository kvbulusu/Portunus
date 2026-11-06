"""§214: --post-custom-script rows in the stage / QoR-detail tables,
JANUS_CTS_HOLD_ABORT triage, pre-repair 4_cts QoR fill.
Fixture mimics run 20261105_172042 (--stop-flow-at cts, soc_cts_probe.tcl,
4_cts FAILED at JANUS_CTS_HOLD_ABORT).
Run: python3 -m pytest asic_flow/agent/tests/test_214_post_script_rows.py -q"""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "utils")]

import stage_report as sr  # noqa: E402
import report_bundle as rb  # noqa: E402
import janus_agent as ja  # noqa: E402
from notification import render_run_table_text  # noqa: E402

PROBE = "asic_flow/scripts/debug/soc_cts_probe.tcl"
EL = ("Elapsed time: {w}[h:]min:sec. CPU time: user {u} sys {s} (617%). "
      "Peak memory: {m}KB.")
PLACE_LOG = "[INFO GPL-0001] x\n" + EL.format(w="55:24.00", u="6900.0", s="120.0", m="18874368") + "\n"
CTS_FAIL_LOG = (
    "JANUS_CTS_DIAG: reg clock latency histogram (100 ps buckets, median 812.0): 700:10 800:900\n"
    "JANUS_CTS_DIAG: late outlier regs (>400 ps from median) by top hierarchy: u_user:812 u_periph:40\n"
    "JANUS_CTS_TAP_SPLIT: after setup -1003.0 hold -865.3\n"
    "JANUS_CTS_LEGALIZE: tap_split before setup -1003.0 hold -865.3 after setup -1003.1 hold -865.3\n"
    "JANUS_CTS_DIAG: wrote ./reports/asap7/janus_soc_top/base/4_cts_pre_repair.rpt\n"
    "Error: cts.tcl, 36 JANUS_CTS_HOLD_ABORT: pre-repair hold WNS -865.3 ps < -700 ps -- aborting "
    "before repair_timing (clock skew, not logic; see JANUS_CTS_MACRO_ALIGN / 4_cts_pre_repair.rpt)\n"
    + EL.format(w="32:11.02", u="11674.79", s="240.93", m="18139888") + "\n")
PLACE_RPT = "tns max -100.0\nwns max -280.0\ntns min 0.0\nwns min 3.9\n"


def _mk(tmp, cts_log=CTS_FAIL_LOG, cts_ok=False):
    logs, rpt, res = (os.path.join(str(tmp), d) for d in ("logs", "rpt", "res"))
    for d in (logs, rpt, res):
        os.makedirs(d, exist_ok=True)
    open(os.path.join(logs, "3_3_place_gp.log"), "w").write(PLACE_LOG)
    open(os.path.join(rpt, "3_detailed_place.rpt"), "w").write(PLACE_RPT)
    open(os.path.join(logs, "4_1_cts.log"), "w").write(cts_log)
    for a in ("1_synth.v", "2_floorplan.odb", "3_place.odb"):
        open(os.path.join(res, a), "w").close()
    if cts_ok:
        open(os.path.join(rpt, "4_cts_final.rpt"), "w").write(
            "tns max -10.0\nwns max -50.0\ntns min 0.0\nwns min 1.0\n")
        open(os.path.join(res, "4_cts.odb"), "w").close()
    return logs, rpt, res


def _skipped_probe():
    return [{"script": PROBE, "after": "4_cts", "status": "SKIPPED", "reason": "stage failed",
             "notes": "not run: block FAILED (--post-custom-on-fail runs it anyway)"}]


def _line(text, prefix):
    return next(ln for ln in text.splitlines() if ln.startswith(prefix))


# ------------------------------------------------------- run fixture ------
def test_failed_cts_with_post_script_row(tmp_path):
    logs, rpt, res = _mk(tmp_path)
    r = sr.build_report("janus_soc_top", "failed", stage="4_cts", logs_dir=logs,
                        reports_dir=rpt, results_dir=res, post_scripts=_skipped_probe())
    keys = [x["key"] for x in r["rows"]]
    i = keys.index("4_cts")
    assert keys[i + 1] == "4_cts+post (soc_cts_probe)" and keys[i + 2] == "5_grt"
    t = r["text"]
    main = _line(t, "4_cts+post (soc_cts_probe)")
    assert "skipped (stage failed)" in main
    stages, detail = t.split("QoR DETAIL", 1)
    assert "4_cts+post (soc_cts_probe)" in stages and "4_cts+post (soc_cts_probe)" in detail
    assert "--post-custom-on-fail" in _line(detail.split("\n", 1)[1], "4_cts+post")
    # §214 2b: pre-repair numbers instead of '—/— —/—'
    cts_main = _line(stages.split("STAGES", 1)[1], "4_cts ")
    assert "S -1003" in cts_main and "H -865" in cts_main and "pre-repair" in cts_main
    cts_det = _line(detail.split("\n", 1)[1], "4_cts ")
    assert "-1003" in cts_det and "-865" in cts_det and "pre-repair (JANUS_CTS_LEGALIZE)" in cts_det
    row = next(x for x in r["rows"] if x["key"] == "4_cts")
    assert row["setup_ws"] == -1003.1 and row["hold_ws"] == -865.3
    # no false 'hold not closed after repair' KILL for pre-repair numbers
    assert not any("after repair" in f for _, f in r["flags"])
    assert "4_cts+post (soc_cts_probe)" in r["html"] and "skipped (stage failed)" in r["html"]
    assert r["subject"].startswith("[JANUS] janus_soc_top FAILED at 4_cts")


def test_success_post_script_row_measured(tmp_path):
    logs, rpt, res = _mk(tmp_path, cts_log="ok\n" + EL.format(
        w="10:00.00", u="100", s="1", m="1048576") + "\n", cts_ok=True)
    out = tmp_path / "post"; out.mkdir()
    log = out / "post_custom_script.log"
    log.write_text("# JANUS header\nSCP die (0,0)\nSCP SUMMARY: 812 early regs under u_user\n"
                   + EL.format(w="3:20.00", u="400", s="20", m="4194304") + "\n")
    (out / "scp_probe.rpt").write_text("x\n")
    ps = [{"script": PROBE, "after": "4_cts", "status": "DONE", "rc": 0, "wall": 205.0,
           "cpu": None, "peak_kb": None, "log": str(log), "out_dir": str(out)}]
    r = sr.build_report("janus_soc_top", "stage", stage="4_cts", logs_dir=logs,
                        reports_dir=rpt, results_dir=res, done={"4_cts"}, post_scripts=ps)
    main = _line(r["text"], "4_cts+post (soc_cts_probe)")
    assert "  done  " in main and "3m25s" in main and "7m00s" in main and "4.0G" in main
    assert "SCP SUMMARY: 812 early regs" in main and "out: scp_probe.rpt" in main
    cts = next(x for x in r["rows"] if x["key"] == "4_cts")
    assert not cts.get("pre_repair") and cts["setup_ws"] == -50.0


def test_post_failed_and_multiple_and_default_after(tmp_path):
    logs, rpt, res = _mk(tmp_path, cts_ok=True)
    ps = [{"script": "a/x.py", "status": "FAILED", "rc": 3, "notes": ""},
          {"script": "b/y.tcl", "name": "probe2", "status": "DONE", "rc": 0, "notes": "ok"}]
    r = sr.build_report("b", "stage", stage="4_cts", logs_dir=logs, reports_dir=rpt,
                        results_dir=res, done={"4_cts"}, post_scripts=ps)
    keys = [x["key"] for x in r["rows"]]
    i = keys.index("4_cts")
    assert keys[i + 1:i + 3] == ["4_cts+post (x)", "4_cts+post (probe2)"]
    assert "FAILED (rc 3)" in _line(r["text"], "4_cts+post (x)")


def test_no_post_script_output_unchanged(tmp_path):
    logs, rpt, res = _mk(tmp_path, cts_ok=True)
    kw = dict(logs_dir=logs, reports_dir=rpt, results_dir=res, done={"4_cts"})
    a = sr.build_report("b", "stage", stage="4_cts", **kw)
    b = sr.build_report("b", "stage", stage="4_cts", post_scripts=None, **kw)
    c = sr.build_report("b", "stage", stage="4_cts", post_scripts=[], **kw)
    assert a["text"] == b["text"] == c["text"] and a["html"] == b["html"] == c["html"]
    assert "+post" not in a["text"]
    rows = [{"key": "1_synth", "status": "DONE"}]
    assert sr.merge_post_rows(rows, None) is rows


# --------------------------------------------------------- triage --------
def test_hold_abort_triage():
    tr = rb.triage_failure([("4_1_cts.log", CTS_FAIL_LOG)], "cts")
    assert tr["category"] == "cts_hold_abort"
    assert "4_cts_pre_repair.rpt" in tr["diagnosis"] and "JANUS_CTS_DIAG" in tr["diagnosis"]
    assert "u_user:812" in tr["diagnosis"] and "after setup -1003.1 hold -865.3" in tr["diagnosis"]
    assert "JANUS_CTS_HOLD_ABORT" in tr["first_error"]
    other = rb.triage_failure([("4_1_cts.log", "Error: cts.tcl, 12 JANUS_CTS_XCUT: bad port\n")], "cts")
    assert other["category"] == "cts_hook_abort"
    body = rb.render_email_body({"block": "b", "status": "FAILED", "triage": tr})
    assert "Category   : cts_hold_abort" in body


def test_pre_repair_fill_from_report(tmp_path):
    logs, rpt, res = _mk(tmp_path, cts_log="Error: cts.tcl, 36 JANUS_CTS_HOLD_ABORT: x\n")
    open(os.path.join(rpt, "4_cts_pre_repair.rpt"), "w").write(
        "worst slack max -1332.84\nworst slack min -190.08\n")
    row = sr.collect_stage("4_cts", logs, rpt)
    assert row["setup_ws"] == -1332.84 and row["hold_ws"] == -190.08
    assert row["pre_repair"] and row["timing_src"] == "4_cts_pre_repair.rpt"


# ---------------------------------------------------------- agent --------
class _UI:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _agent(tmp_path, *argv):
    a = ja.JANUSAutonomousAgent.__new__(ja.JANUSAutonomousAgent)
    a.args = ja.build_parser().parse_args(list(argv))
    ja._apply_skips(a.args)
    a.flow_dir = str(tmp_path / "flow")
    os.makedirs(a.flow_dir, exist_ok=True)
    a.run_dir = str(tmp_path / "run")
    a.ui = _UI()
    for m in ("_results_dir", "_reports_dir", "_logs_dir"):
        setattr(a, m, types.MethodType(lambda self, blk: None, a))
    a._config_path = types.MethodType(lambda self, blk: "c.mk", a)
    return a


def test_agent_records_skip_and_run(tmp_path):
    s = tmp_path / "probe.sh"
    s.write_text('echo "PROBE SUMMARY ok"; echo r > "$JANUS_POST_SCRIPT_OUT/p.rpt"\n')
    a = _agent(tmp_path, "--stop-flow-at", "cts", "--post-custom-script", str(s))
    assert a._run_post_custom_script("blk", types.SimpleNamespace(status="FAILED", note="")) is None
    rec = a._post_runs["blk"][0]
    assert rec["status"] == "SKIPPED" and rec["reason"] == "stage failed" and rec["after"] == "4_cts"
    assert a._post_note("blk") == "post probe: skipped (stage failed)"
    assert a._run_post_custom_script("blk", types.SimpleNamespace(status="STOPPED", note="")) == 0
    rec = a._post_runs["blk"][0]
    assert rec["status"] == "DONE" and rec["rc"] == 0 and rec["wall"] is not None
    assert rec["cpu"] is not None
    saved = json.load(open(os.path.join(a._post_script_out("blk"), "post_script.json")))
    assert saved[0]["status"] == "DONE"
    row = sr._post_row(rec, "4_cts")
    assert "PROBE SUMMARY ok" in row["post_note"] and "out: p.rpt" in row["post_note"]
    # run-summary table note
    txt = render_run_table_text([{"block": "blk", "status": "STOPPED", "note": a._post_note("blk")}])
    assert "post probe: done" in txt


def test_agent_timeout_rc124(tmp_path):
    s = tmp_path / "slow.sh"
    s.write_text("sleep 5\n")
    a = _agent(tmp_path, "--post-custom-script", str(s), "--post-custom-timeout", "0.5")
    assert a._run_post_custom_script("b", types.SimpleNamespace(status="DONE", note="")) == 124
    rec = a._post_runs["b"][0]
    assert rec["status"] == "FAILED" and rec["reason"] == "timeout" and rec["after"] == "6_finish"


def test_notify_passes_post_scripts():
    import inspect
    src = inspect.getsource(ja.JANUSAutonomousAgent._notify_settled)
    assert "post_scripts=" in src
