#!/usr/bin/env python3
"""§124 tests for utils/report_bundle.py + the notifier wiring.

* parse tests on REAL reports (Ara 6_finish.rpt from the box, route DRC
  fixture, real 5_1_grt.log excerpt, real janus_09_final_debug.rpt produced by
  janus_debug_reports.tcl on ORFS gcd in the sandbox);
* synthetic two-block trees (noc + ampo / user_core_ara) proving only the
  target block lands in the tar, cap ordering, masku gating;
* stage-failure scenarios (synth / cts / route-OOM / GRT / SDC-empty) with
  triage category + stale-file skipping;
* Notification.notify() end-to-end with SMTP unset (send skipped) and the mock
  email printed to stdout (pytest -s).

Run: python3 -m pytest asic_flow/agent/tests/test_report_bundle.py -q [-s]
"""
import json
import os
import sys
import tarfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT = os.path.dirname(HERE)
UTILS = os.path.join(os.path.dirname(AGENT), "utils")
sys.path.insert(0, AGENT)
sys.path.insert(0, UTILS)

import report_bundle as rb          # noqa: E402
import notification as nt           # noqa: E402

FIX = os.path.join(HERE, "fixtures")
ARA = os.path.join(FIX, "ara_finish_run")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _w(path, text="", size=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        if size is not None:
            line = "x" * 99 + "\n"
            fh.write(line * (size // 100))
        else:
            fh.write(text)
    return path


def _tree(tmp, blk, stages_done=("1_synth", "2_floorplan", "3_place", "4_cts",
                                 "5_1_grt", "5_2_route", "6_report"),
          finish=True):
    flow = os.path.join(tmp, "flow")
    L = os.path.join(flow, "logs", "asap7", blk, "base")
    R = os.path.join(flow, "reports", "asap7", blk, "base")
    S = os.path.join(flow, "results", "asap7", blk, "base")
    t = time.time() - 1000
    for i, st in enumerate(stages_done):
        p = _w(os.path.join(L, f"{st}.log"), f"[INFO] {blk} {st} ok\n")
        os.utime(p, (t + i, t + i))
    if finish:
        with open(os.path.join(ARA, "6_finish.rpt")) as fh:
            _w(os.path.join(R, "6_finish.rpt"), fh.read())
        _w(os.path.join(S, "6_final.sdc"),
           "create_clock -name core_clk -period 2850 [get_ports clk]\n")
        _w(os.path.join(S, "6_final.odb"), "BINARY")
        _w(os.path.join(S, "6_final.v"), "module x; endmodule\n")
    _w(os.path.join(R, "janus_02_macro_place_masku_groups.rpt"), "masku\n")
    _w(os.path.join(R, "4_cts_final.rpt"), "cts\n")
    return flow, L, R, S


def _run_dir(tmp, blocks):
    rd = os.path.join(tmp, "runs", "20261011_124432")
    for b in blocks:
        _w(os.path.join(rd, f"{b}.log"), f"agent log for {b}\n")
        _w(os.path.join(rd, "signoff", b, "lvs_interconnect.json"), json.dumps({"b": b}))
    _w(os.path.join(rd, "checkpoint.json"), "{}")
    _w(os.path.join(rd, "manifest.txt"), "m")
    return rd


def _names(tar):
    with tarfile.open(tar) as t:
        return t.getnames()


# --------------------------------------------------------------------------
# parse tests on real reports
# --------------------------------------------------------------------------
def test_ara_6_finish_qor():
    q = rb.build_qor("user_core_ara", "DONE", None, ARA, None)
    t = q["timing"]
    assert t["setup_wns"] == -1948.44
    assert t["setup_worst_slack"] == -1948.44
    assert t["setup_tns"] == -85378.1
    assert t["hold_worst_slack"] == 8.95
    assert t["groups"]["setup"]["reg2reg"] == -1948.44
    assert t["groups"]["setup"]["reg2out"] == 5.24
    assert t["violators"] == {"setup": 279, "hold": 0}
    assert q["drv"] == {"max_slew": 471, "max_cap": 8, "max_fanout": 0}
    assert q["clock_tree"]["setup_skew"] == 1074.05
    assert q["clock_tree"]["source_latency"] == 1487.02
    assert q["clock_tree"]["target_latency"] == -483.37
    assert q["clock"]["achieved_mhz"] == 208.09
    assert q["clock"]["period_min"] == 4805.58
    assert q["critical_path"]["delay"] == 2403.552
    assert q["power"]["total"] == pytest.approx(2.59e-03)
    assert q["drv_detail"]["max_slew"]["worst_slack"] == -741.61
    assert q["units"]["time"] == "ps"
    body = rb.render_email_body(q)
    assert "reg2reg" in body and "-1948.44" in body and "max slew 471" in body


def test_route_drc_fixture():
    d = rb.parse_route_drc(os.path.join(FIX, "route_congestion_run", "5_route_drc.rpt"))
    assert d["total"] == 4
    assert d["by_type"] == {"Metal Spacing": 3, "Short": 1}


def test_grt_overflow_and_antenna_real_log():
    txt = open(os.path.join(FIX, "grt_log", "5_1_grt.log")).read()
    assert rb.parse_grt_overflow(txt) == {"h": 0, "v": 0, "total": 0}
    assert rb.parse_antenna(txt) == {"nets": 0, "pins": 0}


def test_debug_report_class_split_real_gcd():
    """janus_09_final_debug.rpt produced by janus_debug_reports.tcl on real
    OpenROAD + ASAP7 gcd: reg2reg + reg2out TNS == 6_finish tns (-87.25)."""
    g, src = rb.parse_debug_groups(os.path.join(FIX, "gcd_debug_rpt"))
    assert src == "janus_09_final_debug.rpt"
    assert g["reg2reg"] == {"endpoints_sampled": 34, "setup_ws": -16.68, "violators": 1, "tns": -16.68}
    assert g["reg2out"]["violators"] == 5 and g["reg2out"]["tns"] == -70.57
    assert g["in2reg"]["setup_ws"] == 170.58
    assert round(sum(v["tns"] for v in g.values()), 2) == -87.25


def test_old_reporter_label_is_ps():
    import reporter
    src = open(reporter.__file__).read()
    assert "WNS (ps)" in src and "WNS (ns)" not in src


# --------------------------------------------------------------------------
# per-block scoping
# --------------------------------------------------------------------------
def test_only_target_block_in_bundle(tmp_path):
    tmp = str(tmp_path)
    flow, L, R, S = _tree(tmp, "noc")
    _tree(tmp, "ampo")
    rd = _run_dir(tmp, ["noc", "ampo", "security"])
    res = rb.build_bundle("noc", "DONE", os.path.join(tmp, "out"),
                          logs_dir=L, reports_dir=R, results_dir=S,
                          agent_log=os.path.join(rd, "noc.log"),
                          signoff_dir=os.path.join(rd, "signoff", "noc"))
    names = _names(res["tar"])
    assert names[0] == "noc/INDEX.md" and names[1] == "noc/qor.json"
    assert "noc/reports/6_finish.rpt" in names
    assert "noc/results/6_final.sdc" in names            # results/ present
    assert "noc/agent/noc.log" in names
    assert "noc/signoff/lvs_interconnect.json" in names
    joined = "\n".join(names)
    for other in ("ampo", "security", "checkpoint.json", "manifest.txt"):
        assert other not in joined, other
    assert "6_final.odb" not in joined and "6_final.v" not in joined
    # masku report for noc is skipped (Ara-only) and the reason is in INDEX.md
    assert "masku_groups" not in joined
    assert "masku debug report (Ara-only" in res["index"]
    assert os.path.isfile(os.path.join(tmp, "out", "qor.json"))
    q = json.load(open(os.path.join(tmp, "out", "qor.json")))
    assert q["block"] == "noc" and q["schema"] == "janus.qor/1"


def test_masku_included_for_ara_and_env_override(tmp_path, monkeypatch):
    tmp = str(tmp_path)
    flow, L, R, S = _tree(tmp, "user_core_ara")
    res = rb.build_bundle("user_core_ara", "DONE", "", logs_dir=L, reports_dir=R,
                          results_dir=S, dry_run=True)
    assert any("masku_groups" in a for a, *_ in res["included"])
    monkeypatch.setenv("JANUS_MASKU_GROUPS", "0")
    assert rb.masku_wanted("user_core_ara") is False
    monkeypatch.setenv("JANUS_MASKU_GROUPS", "1")
    assert rb.masku_wanted("noc") is True


def test_cap_keeps_critical_files_truncates_paths(tmp_path):
    tmp = str(tmp_path)
    flow, L, R, S = _tree(tmp, "noc")
    _w(os.path.join(R, "janus_08_detail_route_paths.rpt"), size=6 << 20)
    _w(os.path.join(R, "janus_07_global_route_paths.rpt"), size=6 << 20)
    res = rb.build_bundle("noc", "DONE", os.path.join(tmp, "o"), logs_dir=L,
                          reports_dir=R, results_dir=S, cap_bytes=3 << 20)
    inc = {a: (sz, kept, how) for a, sz, kept, how, _ in res["included"]}
    assert "reports/6_finish.rpt" in inc and inc["reports/6_finish.rpt"][2] == "full"
    assert "results/6_final.sdc" in inc
    # the paths dumps are either truncated slices or skipped for size, never
    # blocking the files after them
    for a in ("reports/janus_08_detail_route_paths.rpt", "reports/janus_07_global_route_paths.rpt"):
        assert (a in inc and "truncated" in inc[a][2]) or any(a == s for s, _ in res["skipped"])
    assert os.path.getsize(res["tar"]) < 3 << 20


# --------------------------------------------------------------------------
# stage-failure scenarios
# --------------------------------------------------------------------------
def _fail_tree(tmp, blk, done, fail_log, fail_text):
    flow, L, R, S = _tree(tmp, blk, stages_done=done, finish=False)
    # stale later-stage report from an earlier run
    p = _w(os.path.join(R, "5_route_drc.rpt"), "violation type: Short\n")
    os.utime(p, (time.time() - 5000, time.time() - 5000))
    p = _w(os.path.join(L, fail_log), fail_text)
    return L, R, S


def test_synth_failure(tmp_path):
    L, R, S = _fail_tree(str(tmp_path), "noc", (), "1_2_yosys.log",
                         "...\nERROR: Module `foo' referenced in module `top' in cell `u' "
                         "is not part of the design.\n")
    res = rb.build_bundle("noc", "FAILED", "", logs_dir=L, reports_dir=R,
                          results_dir=S, dry_run=True)
    q = res["qor"]
    assert q["failed_stage"] == "synth"
    assert q["triage"]["category"] == "synth-missing-module"
    assert any(a == "reports/5_route_drc.rpt" and "stale" in why for a, why in res["skipped"])
    assert "FAILURE TRIAGE" in res["body"]


def test_cts_failure(tmp_path):
    L, R, S = _fail_tree(str(tmp_path), "noc", ("1_synth", "2_floorplan", "3_place"),
                         "4_1_cts.log", "[ERROR CTS-0012] Clock net has no sinks.\n")
    q = rb.build_qor("noc", "FAILED", L, R, S)
    assert q["failed_stage"] == "cts"
    assert q["triage"]["category"] == "cts"
    assert q["triage"]["code"] == "CTS-0012"


def test_route_oom_silent(tmp_path):
    L, R, S = _fail_tree(str(tmp_path), "noc",
                         ("1_synth", "2_floorplan", "3_place", "4_cts", "5_1_grt"),
                         "5_2_route.log",
                         "[INFO DRT-0195] Start detail routing.\nPeak memory: 61234567KB\n")
    q = rb.build_qor("noc", "FAILED", L, R, S)
    assert q["failed_stage"] == "route"
    assert q["triage"]["category"] == "oom-killed"


def test_grt_overflow_failure(tmp_path):
    L, R, S = _fail_tree(str(tmp_path), "noc",
                         ("1_synth", "2_floorplan", "3_place", "4_cts"),
                         "5_1_grt.log", "[ERROR GRT-0116] Global routing finished with congestion.\n")
    q = rb.build_qor("noc", "FAILED", L, R, S)
    assert q["failed_stage"] == "grt"
    assert q["triage"]["category"] == "grt-overflow"
    assert q["triage"]["knob"]


def test_sdc_empty_and_ignored_errors(tmp_path):
    L, R, S = _fail_tree(str(tmp_path), "noc", ("1_synth",), "2_1_floorplan.log",
                         "[ERROR STA-2204] ignored cosmetic\n"
                         "Error: JANUS SDC: non_clk_inputs is empty -- check port names\n")
    q = rb.build_qor("noc", "FAILED", L, R, S)
    assert q["triage"]["category"] == "sdc-empty-list"
    assert "STA-2204" not in (q["triage"]["first_error"] or "")


# --------------------------------------------------------------------------
# notifier end-to-end (no SMTP) + mock email
# --------------------------------------------------------------------------
def test_notify_mock_email(tmp_path, capsys, monkeypatch):
    for v in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(v, raising=False)
    tmp = str(tmp_path)
    flow, L, R, S = _tree(tmp, "noc")
    _tree(tmp, "ampo")
    rd = _run_dir(tmp, ["noc", "ampo"])
    n = nt.NotificationManager(recipient="kiran@example.com")
    q = n.notify(block="noc", status="DONE", stage="", run_dir=rd, results_dir=S,
                 reports_dir=R, logs_dir=L, log_path=os.path.join(rd, "noc.log"),
                 signoff_dir=os.path.join(rd, "signoff", "noc"),
                 qor_out_dir=R, bundle_dir=os.path.join(rd, "bundles", "noc"))
    assert q and q["block"] == "noc"
    assert os.path.isfile(os.path.join(R, "qor.json"))
    assert n.last_subject.startswith("JANUS noc completed")
    assert "setup WS -1948.44 ps" in n.last_subject
    assert "Path group" in n.last_body and "Route DRC" in n.last_body
    tars = [f for f in os.listdir(os.path.join(rd, "bundles", "noc")) if f.endswith(".tar.gz")]
    assert len(tars) == 1
    assert not any("ampo" in x for x in _names(os.path.join(rd, "bundles", "noc", tars[0])))
    with capsys.disabled():
        print("\n----- MOCK EMAIL -----\nSubject: " + n.last_subject + "\n\n" + n.last_body)
