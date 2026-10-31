"""§180: run 20261030_185523 — ORFS finished (6_final.gds written) but the agent
marked user_domain FAILED because LogAnalyzer matched r"\\bfailed\\b" on the
agent-written make header (`-o .../5_1_grt-failed.odb`). That skipped the DONE
path (post-route opt, abstract, RCX signoff, golden archive)."""
import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))
from log_analyzer import LogAnalyzer  # noqa: E402
import flow_state as fs  # noqa: E402
import report_bundle as rb  # noqa: E402

HDR = ("===== user_domain : make DESIGN_CONFIG=/x/config.mk NUM_CORES=14 "
       "-o /x/results/asap7/user_domain/base/5_1_grt.odb "
       "-o /x/results/asap7/user_domain/base/5_1_grt-failed.odb finish "
       "@ 2026-10-30 18:55:23.1 =====")
PROV = "\n".join([
    "----- GIT PROVENANCE -------------------------------------------------",
    "  branch      : main",
    "  HEAD        : 64c14b9  fix sta error: failed Port name lookup",
    "  tracked src : CLEAN",
    "----------------------------------------------------------------------",
])
OK_TAIL = ("[INFO] No orphan cells in the final layout\n"
           "Elapsed time: 9:39.66[h:]min:sec. CPU time: user 567.37 sys 9.47 (99%). "
           "Peak memory: 9536108KB.\n")


def _an(text):
    return LogAnalyzer().analyze_text(text, "user_domain", "flow")


def test_header_with_failed_odb_not_failure():
    r = _an("\n" + HDR + "\n" + PROV + "\n" + OK_TAIL)
    assert not r.failed, r.error_lines


def test_provenance_subject_not_failure():
    assert not _an(PROV.replace("fix sta error", "ERROR fatal") + "\n" + OK_TAIL).failed


def test_nogit_provenance_line_skipped():
    assert not _an("GIT PROVENANCE: (not a git work tree)\n" + OK_TAIL).failed


def test_real_errors_still_detected():
    assert _an(HDR + "\n" + PROV + "\n[ERROR GRT-0116] Global routing failed.\n").failed
    assert _an(HDR + "\nmake: *** [Makefile:1] Error 2\n").failed
    assert _an(HDR + "\nError: something broke\n").failed
    # an untagged tool line with "failed" AFTER the provenance block still counts
    assert _an(PROV + "\nrouting failed after 64 iterations\n").failed


def test_partial_header_not_skipped():
    # header-style line must be full "===== blk : ... =====" to be skipped
    assert _an("===== not a header failed\n").failed


def test_sibling_artifacts_exclude_failed_dumps(tmp_path):
    d = str(tmp_path)
    for n in ("5_1_grt.odb", "5_1_grt-failed.odb", "5_2_route.odb", "5_route.sdc",
              "4_cts.odb"):
        open(os.path.join(d, n), "w").close()
    names = sorted(os.path.basename(p) for p in fs._stage_sibling_artifacts(d, "5_route"))
    assert "5_1_grt-failed.odb" not in names
    assert names == ["5_1_grt.odb", "5_2_route.odb", "5_route.sdc"]


def test_triage_clean_banner_not_oom():
    t = rb.triage_failure([("6_1_merge.log", OK_TAIL)], "finish")
    assert t["category"] == "no-tool-failure"


def test_triage_killed_banner_still_oom():
    txt = ("[INFO DRT-0195] Start detail routing.\nCommand terminated by signal 9\n"
           "Elapsed time: 1:00:00[h:]min:sec. CPU time: user 1 sys 1 (99%). "
           "Peak memory: 61234567KB.\n")
    assert rb.triage_failure([("5_2_route.log", txt)], "route")["category"] == "oom-killed"


def test_real_run_185523_log_if_present():
    p = "/home/ubuntu/ud_185523/user_domain_finish/agent/user_domain.log"
    if not os.path.isfile(p):
        return
    r = LogAnalyzer().analyze_file(p, "user_domain", "flow")
    assert not r.failed, r.error_lines[:5]
