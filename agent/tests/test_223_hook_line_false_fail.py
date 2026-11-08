"""§223: run 20261108_121357 (janus_soc_top --stop-flow-at cts) exited rc=0 with
4_cts setup +0.8 / hold 0.0, yet was settled FAILED: LogAnalyzer matched
r"\\bERROR\\b" on our own hook diagnostic "JANUS_CTS_TAP_SPLIT: ... error min
-24.3 max -24.3 ps". The RED milestone flags (7946 hbuf, pre-hold -414) only
tag the subject; they never set FAILED. Also the setup-WNS fallback read
"JANUS_CTS_HOLD_ABORT: pre-repair hold WNS -414.1" as setup WNS."""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from log_analyzer import LogAnalyzer  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "t223", "cts_false_fail_121357.log")


def _an(text):
    return LogAnalyzer().analyze_text(text, "janus_soc_top", "flow")


def test_fixture_run_121357_not_failed():
    with open(FIX) as fh:
        text = fh.read()
    assert "error min" in text and "RSZ-0032" in text      # fixture is the real excerpt
    r = _an(text)
    assert not r.failed, r.error_lines


def test_fixture_setup_wns_not_from_hold_line():
    with open(FIX) as fh:
        r = _an(fh.read())
    assert r.wns_ns != -414.1


def test_hook_lines_never_fail():
    for ln in ("JANUS_CTS_TAP_SPLIT: iter 0 LATE 33 group(s) 150 buf, error min -16.5 max 21.0 ps",
               "JANUS_PRNC: FAIL 2 clkgen clone(s): a b",
               "JANUS_DETOUR: ERROR during planning (no db change, GRT unaffected)",
               "JANUS_CTS_DIAG: WARNING report_checks failed (x)",
               "[JANUS-DUMP] stage=cts failed to write image"):
        assert not _an(ln + "\n").failed, ln


def test_real_errors_still_fail():
    for ln in ("[ERROR RSZ-0001] something real",
               "Error: post_cts.tcl, 12 invalid command name \"foo\"",
               "make: *** [Makefile:123: do-4_cts] Error 1"):
        assert _an(ln + "\n").failed, ln


def test_hold_wns_line_skipped_setup_wns_kept():
    r = _an("JANUS_POST_DPL: after  wns 52.8 (was -465.5)\n"
            "JANUS_CTS_HOLD_ABORT: pre-repair hold WNS -414.1 ps >= -700 ps; continue\n")
    assert r.wns_ns == 52.8


def test_stopped_stage_timing_reads_fresh_rpt(tmp_path):
    import time
    import types
    import janus_agent as ja
    cls = next(c for c in vars(ja).values()
               if isinstance(c, type) and hasattr(c, "_stopped_stage_timing"))
    old = tmp_path / "janus_05_detail_place_paths.rpt"
    old.write_text("wns max -9.00\ntns max -9.00\nworst slack min -9.00\n")
    os.utime(old, (1, 1))
    new = tmp_path / "janus_06_cts_paths.rpt"
    new.write_text("SETUP report_wns\nwns max 0.80\nSETUP report_tns\ntns max 0.00\n"
                   "HOLD report_worst_slack\nworst slack min 0.00\n")
    me = types.SimpleNamespace(_reports_dir=lambda b: str(tmp_path),
                               _block_t0={"b": time.time() - 60},
                               ui=types.SimpleNamespace(debug=lambda *a: None))
    t = cls._stopped_stage_timing(me, "b")
    assert t["_source"] == "janus_06_cts_paths.rpt"
    assert t["setup_ws"] == 0.8 and t["hold_ws"] == 0.0
    me._block_t0 = {"b": time.time() + 60}          # nothing fresh -> None
    assert cls._stopped_stage_timing(me, "b") is None
