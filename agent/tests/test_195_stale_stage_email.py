"""§195: stage email must not show the previous run's artifacts for a stage
that is re-running / pending in this run (run 20261102_221045: the 3_place DONE
mail carried the old 4_cts hold -1417 / 14470 hbuf KILL while 4_cts had just
started)."""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "utils")]

import stage_report as sr  # noqa: E402

OLD_CTS = """Iteration | Resized | Buffers | Cloned Gates |   Area   |   WNS   |   TNS   | Endpoint
        0 |       0 |       0 |            0 |    +0.0% | -1417.109 | -1121986.875 | clone_4_u_clkgen/_147_/A
    final |      41 |   14470 |            0 |    +0.1% | -1066.125 | -3250.172 | clone_4_u_clkgen/_147_/A
[INFO RSZ-0032] Inserted 14470 hold buffers.
Elapsed time: 16:53.00[h:]min:sec. CPU time: user 2400.00 sys 58.00 (240%). Peak memory: 15400000KB.
"""
OLD_RPT = "wns max -2488.19\ntns max -53290.30\n"
PLACE_RPT = "wns max -2535.00\ntns max -334000.00\n"


def _mk(tmp, old_age=3600):
    logs, rpt, res = (os.path.join(tmp, d) for d in ("logs", "rpt", "res"))
    for d in (logs, rpt, res):
        os.makedirs(d)
    now = time.time()
    old = now - old_age
    for p, txt in ((os.path.join(logs, "4_1_cts.log"), OLD_CTS),
                   (os.path.join(rpt, "4_cts_final.rpt"), OLD_RPT),
                   (os.path.join(res, "4_cts.odb"), "")):
        open(p, "w").write(txt)
        os.utime(p, (old, old))
    open(os.path.join(logs, "3_4_place_resized.log"), "w").write("ok\n")
    open(os.path.join(rpt, "3_detailed_place.rpt"), "w").write(PLACE_RPT)
    for a in ("1_synth.v", "2_floorplan.odb", "3_place.odb"):
        open(os.path.join(res, a), "w").close()
    return logs, rpt, res, now - old_age / 2


def test_stale_cts_not_reported_195(tmp_path):
    logs, rpt, res, t0 = _mk(str(tmp_path))
    r = sr.build_report("janus_soc_top", "stage", stage="3_place", logs_dir=logs,
                        reports_dir=rpt, results_dir=res, current="4_cts",
                        reused=["1_synth"], run_start=t0)
    assert not [f for f in r["flags"] if f[0] == "KILL"], r["flags"]
    assert "KILL" not in r["subject"]
    cts = [x for x in r["rows"] if x["key"] == "4_cts"][0]
    assert cts["status"] == "RUNNING"
    assert "hold_bufs" not in cts and "hold_pre_wns" not in cts and cts.get("wall") is None
    assert "14470" not in r["text"] and "1417" not in r["text"]


def test_fresh_cts_still_flags_195(tmp_path):
    logs, rpt, res, t0 = _mk(str(tmp_path))
    os.utime(os.path.join(logs, "4_1_cts.log"), None)  # written by this run
    r = sr.build_report("janus_soc_top", "stage", stage="4_cts", logs_dir=logs,
                        reports_dir=rpt, results_dir=res, current="5_grt",
                        reused=["1_synth"], run_start=t0)
    assert any("14470 hold buffers" in f[1] for f in r["flags"])


def test_reused_stage_keeps_old_qor_195(tmp_path):
    logs, rpt, res, t0 = _mk(str(tmp_path))
    r = sr.build_report("janus_soc_top", "stage", stage="5_grt", logs_dir=logs,
                        reports_dir=rpt, results_dir=res, current="5_drt",
                        reused=["1_synth", "4_cts"], run_start=t0)
    cts = [x for x in r["rows"] if x["key"] == "4_cts"][0]
    assert cts["status"] == "REUSED" and cts.get("hold_bufs") == 14470
