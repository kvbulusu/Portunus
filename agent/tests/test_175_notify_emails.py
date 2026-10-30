"""§175: meaningful stage-table notification emails."""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "utils")]

import stage_report as sr  # noqa: E402
from milestones import MilestoneTracker, is_debug_event  # noqa: E402
from notification import NotificationManager  # noqa: E402
import janus_agent as ja  # noqa: E402

EL_CTS = ("Elapsed time: 26:31.76[h:]min:sec. CPU time: user 2810.81 sys 219.55 (190%). "
          "Peak memory: 7430816KB.")
EL_GRT = ("Elapsed time: 2:05:58.17[h:]min:sec. CPU time: user 16937.59 sys 463.88 (230%). "
          "Peak memory: 16590932KB.")

CTS_LOG = """[INFO RSZ-0094] Found 11021 endpoints with setup violations.
   Iter   | Removed | Resized | Inserted | Cloned |  Pin  |   Area   |    WNS   |   StTNS    |   EnTNS    |  Viol  |  Worst
        0 |       0 |       0 |        0 |      0 |     0 |    +0.0% | -7100.0 | -1.0 | -1.0 |  11021 | a/D
    final |      14 |     232 |      334 |      0 |   475 |    +0.0% | -7069.985 | -5946543.0 | -6815134.5 |  11021 | a/D
[INFO RSZ-0045] Inserted 334 buffers, 1 to split loads.
[WARNING RSZ-0062] Unable to repair all setup violations.
[INFO RSZ-0046] Found 163 endpoints with hold violations.
Iteration | Resized | Buffers | Cloned Gates |   Area   |   WNS   |   TNS   | Endpoint
        0 |       0 |       0 |            0 |    +0.0% | -67.601 | -3373.181 | b/D
    final |       0 |     267 |            0 |    +0.0% |   0.122 |   0.000 | b/D
[INFO RSZ-0032] Inserted 267 hold buffers.
JANUS_CTS_MACRO_OFS: after reg median 3339.1 macro clk max 1542.4 D 1796.7 (shift +10.0; inserted 4 removed 0) (pre-IO_LAT x)
""" + EL_CTS + "\n"

CTS_RPT = """tns max -51402036.00
wns max -1499.12
 223.96 setup skew
Startpoint: u/occ_q[3] (rising edge-triggered flip-flop clocked by core_clk)
Endpoint: occupancy_o[7] (output port clocked by core_clk)
Path Type: max
                               -1499.12   slack (VIOLATED)
Startpoint: u/a (rising edge-triggered flip-flop clocked by core_clk)
Endpoint: u/b (rising edge-triggered flip-flop clocked by core_clk)
Path Type: min
                                 12.13   slack (MET)
setup violation count 85047
hold violation count 0
max slew violation count 47
"""


def _mk(tmp, failed_drt=False):
    logs, rpt, res = (os.path.join(tmp, d) for d in ("logs", "rpt", "res"))
    for d in (logs, rpt, res):
        os.makedirs(d)
    open(os.path.join(logs, "4_1_cts.log"), "w").write(CTS_LOG)
    open(os.path.join(logs, "5_1_grt.log"), "w").write(
        "[INFO GRT-0096] Final congestion report:\n"
        "Total         13691145       1219839            8.91%             0 /  0 /  0\n" + EL_GRT + "\n")
    open(os.path.join(rpt, "4_cts_final.rpt"), "w").write(CTS_RPT)
    for a in ("1_synth.v", "2_floorplan.odb", "3_place.odb", "4_cts.odb"):
        open(os.path.join(res, a), "w").close()
    if failed_drt:
        lines = [f"[INFO DRT-0195] line {i}" for i in range(200)]
        lines.insert(10, "[ERROR DRT-0305] Net x of signal type POWER cannot be connected")
        p = os.path.join(logs, "5_2_route.log")
        open(p, "w").write("\n".join(lines) + "\n")
        os.utime(p, None)
    return logs, rpt, res


def test_parse_elapsed_and_format():
    e = sr.parse_elapsed(EL_CTS + "\nnoise\n" + EL_GRT)
    assert len(e) == 2
    assert abs(e[0]["wall"] - (26 * 60 + 31.76)) < 1e-6
    assert abs(e[1]["wall"] - (2 * 3600 + 5 * 60 + 58.17)) < 1e-6
    assert abs(e[0]["cpu"] - (2810.81 + 219.55)) < 1e-6
    assert e[1]["peak_kb"] == 16590932
    assert sr.fmt_dur(e[1]["wall"]) == "2h05m"
    assert sr.fmt_dur(e[0]["wall"]) == "26m32s"
    assert sr.fmt_mem(16590932) == "15.8G"
    assert sr.parse_elapsed("garbage") == []


def test_stage_runtime_sums_substeps(tmp_path):
    logs = tmp_path / "l"
    logs.mkdir()
    (logs / "3_3_place_gp.log").write_text(EL_CTS + "\n")
    (logs / "3_4_place_resized.log").write_text(EL_CTS + "\n")
    rt = sr.stage_runtime(str(logs), "3_place")
    assert rt["n"] == 2 and abs(rt["wall"] - 2 * (26 * 60 + 31.76)) < 1e-6
    assert rt["peak_kb"] == 7430816


def test_repair_and_paths_parse():
    r = sr.parse_repair(CTS_LOG)
    assert r["hold_pre_wns"] == -67.601 and r["hold_post_wns"] == 0.122
    assert r["setup_pre_wns"] == -7100.0 and r["hold_bufs"] == 267
    p = sr.parse_paths(CTS_RPT)
    assert p["max"][0] == ("u/occ_q[3]", "occupancy_o[7]", -1499.12)
    assert p["min"][0][2] == 12.13
    assert sr.parse_stage_rpt(CTS_RPT)["skew"] == 223.96


def test_table_with_missing_data(tmp_path):
    r = sr.build_report("user_domain", "stage", stage="4_cts", logs_dir=None,
                        reports_dir=str(tmp_path / "nope"), results_dir=None)
    assert "error" not in r
    assert "STAGES" in r["text"] and sr.DASH in r["text"]
    assert "<table" in r["html"]
    assert r["subject"].startswith("[JANUS] user_domain 4_cts DONE")


def test_stage_email_content(tmp_path):
    logs, rpt, res = _mk(str(tmp_path))
    r = sr.build_report("user_domain", "stage", stage="4_cts", logs_dir=logs,
                        reports_dir=rpt, results_dir=res, current="5_grt",
                        elapsed=8040, ctx={"git": "abc123 (main) clean", "cores": 26})
    t = r["text"]
    assert r["subject"] == "[JANUS] user_domain 4_cts DONE · WNS -1499ps · 4/7 · 2h14m", r["subject"]
    assert "4_cts        done     26m32s" in t
    assert "hbuf 267" in t and "D 1797" in t and "skew 224" in t
    assert "u/occ_q[3] -> occupancy_o[7]" in t
    assert "W RSZ-0062 x1" in t
    assert "Git      : abc123 (main) clean" in t
    assert not r["flags"]


def test_failure_email_tail_errors_and_drive(tmp_path, monkeypatch):
    logs, rpt, res = _mk(str(tmp_path), failed_drt=True)
    r = sr.build_report("user_domain", "failed", logs_dir=logs, reports_dir=rpt,
                        results_dir=res)
    assert r["failed_stage"] == "5_drt"
    assert r["subject"] == "[JANUS] user_domain FAILED at 5_drt (DRT-0305)"
    assert len(r["tail"]) == 50 and r["tail"][-1] == "[INFO DRT-0195] line 199"
    assert "[ERROR DRT-0305]" in r["text"] and "LAST 50 LINES of 5_2_route.log" in r["text"]
    assert "&lt;" not in r["text"]
    monkeypatch.setenv("JANUS_NOTIFY_TAIL_LINES", "7")
    assert len(sr.build_report("b", "failed", logs_dir=logs)["tail"]) == 7

    # merged into ONE email with Drive links on top
    nm = NotificationManager(recipient="a@b", smtp_host="h", smtp_user="u", smtp_pass="p",
                             gdrive_enabled=True, gdrive_creds="/x")
    sent = []
    nm.send_email = lambda s, b, a=None, html=None: sent.append((s, b, html)) or True

    def fake_up(files, block, subfolder=None):
        nm.last_drive_links = ["https://drive.google.com/file/d/XYZ/view"]
        nm.last_drive_outcome = "uploaded 1 file(s)"
        return ["XYZ"]
    nm.upload_to_drive = fake_up
    nm.notify("user_domain", "FAILED", run_dir=str(tmp_path), logs_dir=logs,
              reports_dir=rpt, results_dir=res, stage_report=r,
              run_table=[{"block": "user_domain", "status": "FAILED"}])
    assert len(sent) == 1
    s, body, html = sent[0]
    assert s == r["subject"]
    assert body.index("https://drive.google.com/file/d/XYZ/view") < body.index("STAGES")
    assert "[INFO DRT-0195] line 199" in body and "DETAIL (report bundle)" in body
    assert '<a href="https://drive.google.com/file/d/XYZ/view">' in html


def test_drive_failure_is_stated(tmp_path):
    nm = NotificationManager(recipient="a@b", gdrive_enabled=True, gdrive_creds="/x")
    nm.last_drive_outcome = "FAILED: 403"
    assert "Google Drive upload: FAILED: 403" in nm.drive_note("/t.tar.gz", "b")
    nm2 = NotificationManager(recipient="a@b")
    assert "off" in nm2.drive_note(None, "b")


def test_kill_red_flags():
    rows = [{"key": "4_cts", "status": "DONE", "hold_bufs": 20010, "hold_pre_wns": -628.3}]
    f = sr.red_flags(rows)
    assert [lv for lv, _ in f] == ["KILL", "KILL"]
    s = sr.subject_line("user_domain", "stage", "4_cts", rows, f)
    assert s.endswith("!! KILL RUN?")
    rows[0]["status"] = "PENDING"           # stale data from an old run: ignored
    assert sr.red_flags(rows) == []
    # live tracker fires during repair_timing (before it finishes)
    t = MilestoneTracker("user_domain")
    ev = []
    for ln in ["[INFO RSZ-0046] Found 1116 endpoints with hold violations.",
               "Iteration | Resized | Buffers | Cloned Gates |   Area   |   WNS   |   TNS   | Endpoint",
               "        0 |       0 |       0 |            0 |    +0.0% | -628.3 | -9.0 | x/D",
               "       10 |       0 |    6001 |            0 |    +1.0% | -400.0 | -8.0 | x/D"]:
        ev += t.feed(ln, "4_cts")
    keys = [e[0] for e in ev if e[0] != "start"]
    assert keys == ["kill_hold_pre", "kill_hold_bufs"], ev
    assert not any(is_debug_event(k) for k in keys)


def test_debug_events_gated_off_by_default(monkeypatch):
    monkeypatch.delenv("JANUS_NOTIFY_DEBUG", raising=False)
    assert not sr.notify_debug_enabled()
    assert is_debug_event("io_shadow") and is_debug_event("error") and is_debug_event("hold")
    sent = []
    fake = types.SimpleNamespace(run_dir="/tmp", _ms_notifier=types.SimpleNamespace(
        send_email=lambda *a, **k: sent.append(a)), _notify_log=lambda m: None,
        _block_log=lambda b: "x")
    ja.JANUSAutonomousAgent._send_milestones(fake, "user_core", [("io_shadow", "s", "b")])
    assert sent == []
    monkeypatch.setenv("JANUS_NOTIFY_DEBUG", "1")
    assert sr.notify_debug_enabled()


def test_stage_tracking_and_key_mapping():
    assert sr.stage_key("4_cts (clock tree)") == "4_cts"
    assert sr.stage_key("5_route (detailed)") == "5_drt"
    assert sr.stage_key("5_route", "Running global_route.tcl", 5) == "5_grt"
    assert sr.stage_key("starting") is None
    t = MilestoneTracker("b")
    assert t.track_stage("3_place") is None
    assert t.track_stage("4_cts") == "3_place"
    assert t.track_stage("3_place") is None          # regression ignored
    assert t.track_stage("5_grt") == "4_cts"
    assert t.track_stage("5_drt") == "5_grt"


def test_parse_errors_never_raise(tmp_path, monkeypatch):
    bad = tmp_path / "logs"
    bad.mkdir()
    (bad / "4_1_cts.log").write_bytes(b"\xff\xfe Elapsed time: ::[h:]min:sec. junk\n|||\n")
    (bad / "4_1_cts.json").write_text("{not json")
    r = sr.build_report("b", "failed", logs_dir=str(bad), reports_dir=str(bad))
    assert r["subject"].startswith("[JANUS] b FAILED")

    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(sr, "build_rows", boom)
    r = sr.build_report("b", "stage", stage="4_cts")
    assert "kaboom" in r["text"] and r["subject"] == "[JANUS] b 4_cts DONE"
    assert sr.collect_stage("4_cts", "/nonexistent", None)["key"] == "4_cts"
