#!/usr/bin/env python3
"""
Dashboard timing-source regression (agent_domain run 20261007_104052).

The live dashboard showed agent_domain "DONE⚠" with hold reg2reg -122 ps because
it read janus_08_detail_route_paths.rpt (post-detail-route dump, GRT/estimated
RC, written BEFORE 6_final runs OpenRCX). The signoff 6_finish.rpt for the same
run reports hold reg2reg +9.95 ps and "hold violation count 0".

Fixture agent_domain_final_run/:
  6_finish.rpt                     REAL report, trimmed (path-group sections and
                                   the violation-count block kept verbatim; the
                                   17k max-slew violator rows elided).
  janus_08_detail_route_paths.rpt  SYNTHETIC, reproduces the -122.25 ps value.

Run: python3 tests/test_dashboard_final_timing.py   (or pytest)
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import log_analyzer as la  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "agent_domain_final_run")
FIN = os.path.join(FIX, "6_finish.rpt")
GRT = os.path.join(FIX, "janus_08_detail_route_paths.rpt")


def _stage(files_with_mtime):
    """Copy fixtures into a temp reports dir with controlled mtimes."""
    d = tempfile.mkdtemp(prefix="janus_rpt_")
    now = time.time()
    for src, age in files_with_mtime:
        dst = os.path.join(d, os.path.basename(src))
        shutil.copy(src, dst)
        os.utime(dst, (now - age, now - age))
    return d


def test_parse_final_sta_values():
    with open(FIN) as fh:
        t = la.parse_final_sta_rpt(fh)
    assert abs(t["hold_ws"] - 9.95) < 1e-6
    assert t["setup_ws"] == 0.0 and t["setup_tns"] == 0.0
    assert abs(t["setup_worst"] - 532.52) < 1e-6
    assert abs(t["fmax_mhz"] - 223.84) < 1e-6
    assert t["counts"] == {"setup": 0, "hold": 0, "max_slew": 17226,
                           "max_cap": 3, "max_fanout": 0}
    pg = t["path_groups"]
    assert abs(pg["hold"]["reg2reg"] - 9.95) < 1e-6
    assert abs(pg["hold"]["reg2out"] - 2076.62) < 1e-6
    # group names containing spaces must survive (was truncated to "gated")
    assert abs(pg["hold"]["gated clock"] - 23.01) < 1e-6
    assert abs(pg["setup"]["reg2reg"] - 1167.82) < 1e-6
    assert abs(pg["setup"]["reg2out"] - 532.52) < 1e-6


def test_prefers_6_finish_and_verdict_closed():
    # Real ORFS order: janus_08 written at detail route, 6_finish ~20 min later.
    d = _stage([(GRT, 1200), (FIN, 0)])
    try:
        t = la.read_block_timing(d)
        assert t["source"] == "6_finish" and t["final"] is True
        assert abs(t["hold_ws"] - 9.95) < 1e-6
        assert la.timing_verdict(t["setup_ws"], t["hold_ws"]) == "CLOSED"
    finally:
        shutil.rmtree(d)


def test_fallback_to_janus_08_when_no_final():
    d = _stage([(GRT, 0)])
    try:
        t = la.read_block_timing(d)
        assert t["source"] == "janus_08" and t["final"] is False
        assert abs(t["hold_ws"] - (-122.25)) < 1e-6
        assert abs(t["path_groups"]["hold"]["reg2reg"] - (-122.25)) < 1e-6
        # old behaviour reproduced on the estimate: DONE⚠
        assert la.timing_verdict(t["setup_ws"], t["hold_ws"]) == "VIOLATED"
    finally:
        shutil.rmtree(d)


def test_stale_6_finish_is_ignored():
    # 6_finish from a previous run, newer route died before finish.
    d = _stage([(FIN, 7200), (GRT, 0)])
    try:
        assert la.read_block_timing(d)["source"] == "janus_08"
    finally:
        shutil.rmtree(d)


def test_verdict_edges():
    assert la.timing_verdict(None, None) == "UNKNOWN"
    assert la.timing_verdict(0.0, 0.0) == "CLOSED"
    assert la.timing_verdict(-0.01, 5.0) == "VIOLATED"
    assert la.timing_verdict(None, 9.95) == "CLOSED"
    assert la.read_block_timing(None) is None
    assert la.read_block_timing("/nonexistent/dir") is None


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print(f"PASS {name}")
    print(f"{n} tests passed")
