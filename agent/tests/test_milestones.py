"""§157: milestone emails — parser behaviour on real and synthetic ORFS output."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(HERE, "..", "..", "utils")]

from milestones import MilestoneTracker, milestones_enabled  # noqa: E402
import janus_agent as ja  # noqa: E402

DETECT = ja.JANUSAutonomousAgent._detect_stage


def replay(text, expect_io_shadow=True, block="user_core"):
    t, st, subs = MilestoneTracker(block, expect_io_shadow), None, []
    for line in text.splitlines():
        st = DETECT(line) or st
        subs += [e[1] for e in t.feed(line, st)]
    return subs + [e[1] for e in t.finish()]


GRT_FAIL_TABLE = """[INFO GRT-0096] Final congestion report:
Layer         Resource        Demand        Usage (%)    Max H / Max V / Total Congestion
---------------------------------------------------------------------------------------
M1                   0             0            0.00%             0 /  0 /  0
M2             2673222        345241           12.91%             1 /  0 / 711
M5             2189542        139052            6.35%             0 /  1 /  6
---------------------------------------------------------------------------------------
Total         13691145       1219839            8.91%             1 /  1 / 717
[ERROR GRT-0116] Global routing finished with congestion. Check the congestion regions in the DRC Viewer.
Error: global_route.tcl, 125 GRT-0116
"""


def test_failed_user_core_run_20261026():
    subs = replay("Running io_placement.tcl, stage 3_2_place_iop\n"
                  "Running global_place.tcl, stage 3_3_place_gp\n"
                  "[WARNING RSZ-0064] Unable to repair all hold checks within margin.\n"
                  "[INFO RSZ-0032] Inserted 3587 hold buffers.\n" + GRT_FAIL_TABLE)
    assert any("FAIL io_placement ran with NO JANUS_IO_SHADOW" in s for s in subs), subs
    assert any("WARN CTS inserted 3587 hold buffers (RSZ-0064" in s for s in subs), subs
    assert any("FAIL global route overflow 717 (M2: overflow 711, M5: overflow 6)" in s for s in subs), subs
    assert not any("error at" in s for s in subs), subs   # no duplicate mail for the GRT error


def test_good_resumed_run():
    good = ("Running global_place_skip_io.tcl, stage 3_1_place_gp_skip_io\n"
            "Running io_placement.tcl, stage 3_2_place_iop\n"
            "JANUS_IO_SHADOW: i_ara gap 17.10 um to left edge -> exclude 27.30-432.80\n"
            "JANUS_IO_SHADOW: i_cva6 gap 17.20 um to right edge -> exclude 69.90-390.00\n"
            "JANUS_IO_SHADOW: 530 pins, allowed perimeter 960.0 of 2492.0 um; PLACE_PINS_ARGS = x\n"
            "Running global_place.tcl, stage 3_3_place_gp\n"
            "[ERROR GUI-0056] Invalid syntax for -filter. Use -filter attribute=value.\n"
            "[INFO RSZ-0032] Inserted 212 hold buffers.\n"
            "[INFO GRT-0096] Final congestion report:\n"
            "M2             2673222        345241           12.91%             0 /  0 /  0\n"
            "Total         13691145       1219839            8.91%             0 /  0 /  0\n"
            "[INFO DRT-0199]   Number of violations = 52113.\n"
            "[INFO DRT-0199]   Number of violations = 40.\n"
            "[INFO DRT-0199]   Number of violations = 0.\n"
            "[INFO DRT-0198] Complete detail routing.\n"
            "[INFO DRT-0198] Complete detail routing.\n")
    subs = replay(good)
    assert subs[0].endswith("(earlier stages reused)"), subs
    assert "[JANUS] user_core: OK IO shadow hook applied (2 edge excludes)" in subs
    assert "[JANUS] user_core: OK CTS inserted 212 hold buffers" in subs
    assert "[JANUS] user_core: OK global route clean (overflow 0)" in subs
    assert any("52113 violations after iteration 1" in s for s in subs)
    assert "[JANUS] user_core: OK detailed route complete: 0 violations after 3 iterations" in subs
    assert not any("FAIL" in s for s in subs), subs          # GUI-0056 is benign
    assert len(subs) == len(set(subs)) == 6, subs              # each milestone once


def test_shadow_warn_and_unexpected_block():
    subs = replay("Running io_placement.tcl, stage 3_2_place_iop\n"
                  "JANUS_IO_SHADOW: WARN allowed perimeter 10.0 um < 300.0 um needed for 500 pins -- NOT applying excludes\n"
                  "Running global_place.tcl, stage 3_3_place_gp\n")
    assert any("FAIL IO shadow hook did not apply excludes" in s for s in subs), subs
    # blocks without the hook: no io_shadow mail at all
    subs = replay("Running io_placement.tcl, stage 3_2_place_iop\n"
                  "Running global_place.tcl, stage 3_3_place_gp\n", expect_io_shadow=False, block="noc")
    assert not any("IO shadow" in s or "JANUS_IO_SHADOW" in s for s in subs), subs


def test_real_error_and_synth_start():
    subs = replay("Running synth.sh, stage 1_1_yosys_canonicalize\n"
                  "[ERROR PDN-0233] Failed to generate full power grid.\n", block="noc",
                  expect_io_shadow=False)
    assert "WARN started at" in subs[0] and "full rebuild" in subs[0], subs
    assert any("FAIL error at" in s and "PDN-0233" in s for s in subs), subs


def test_enable_env():
    assert milestones_enabled("user_core", {})
    assert not milestones_enabled("user_core", {"JANUS_NOTIFY_MILESTONES": "0"})
    assert milestones_enabled("user_core", {"JANUS_NOTIFY_MILESTONES": "user_core,noc"})
    assert not milestones_enabled("user_core_ara", {"JANUS_NOTIFY_MILESTONES": "user_core,noc"})


# Real lines from user_core run 20261027_115052 (3_2_place_iop.log / 5_1_grt.log).
RUN_1027 = """Running io_placement.tcl, stage 3_2_place_iop
JANUS_IO_SHADOW: i_ara_system.i_ara gap 17.16 um to left edge -> exclude 27.31-432.82
JANUS_IO_SHADOW: i_ara_system.i_ara gap 27.31 um to bottom edge -> exclude 17.16-422.66
JANUS_IO_SHADOW: i_ara_system.i_ara gap 27.18 um to top edge -> exclude 17.16-422.66
JANUS_IO_SHADOW: i_ara_system.i_ariane gap 17.13 um to right edge -> exclude 69.94-390.07
JANUS_IO_SHADOW: 532 pins, allowed perimeter 963.3 of 2500.0 um; PLACE_PINS_ARGS = -exclude left:27.312-432.820 -exclude right:69.936-390.071 -exclude bottom:17.156-422.664 -exclude top:17.156-422.664
Running global_place.tcl, stage 3_3_place_gp
JANUS_DETOUR: block=ara_integration_wrapper macros=1 (i_ara_system.i_ara) buf=BUFx4f_ASAP7_75t_R dry=0
JANUS_DETOUR: 0 wrap-around net(s) found
"""


def test_run_20261027_shadow_count_and_detour():
    subs = replay(RUN_1027)
    # 4 per-edge spans; the PLACE_PINS_ARGS summary line must not count as a 5th
    assert "[JANUS] user_core: OK IO shadow hook applied (4 edge excludes)" in subs, subs
    assert "[JANUS] user_core: OK macro detour pre-buffered 0 net(s) before GRT" in subs, subs


def test_detour_straddle_and_warn():
    subs = replay("Running global_route.tcl, stage 5_1_grt\n"
                  "JANUS_DETOUR: net net32642 straddles i_ara_system.i_ara W->E (no macro-pin trigger) pins=2 moved=1 wrap=N corners drv (8.58,446.00) far (437.6,446.00)\n"
                  "JANUS_DETOUR: WARN net9 straddles i_ara_system.i_ara with 0 drivers -- skipped\n"
                  "JANUS_DETOUR: 1 wrap-around net(s) found\n", expect_io_shadow=False)
    assert any("WARN macro detour pre-buffered 1 net(s) before GRT (some straddling nets skipped)" in s
               for s in subs), subs
