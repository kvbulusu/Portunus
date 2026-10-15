"""§134: --adaptive auto visibility + scheduler slot refill.
- the rolling scheduler launches newly-ready dependents while a DONE block's
  worker is still busy (adaptive pass), instead of idling until it returns;
- the dashboard shows ADAPT + 1-2 word reason + variant stage, counted running;
- the variant-stage poller follows the newest ORFS log.
Run: python3 -m pytest asic_flow/agent/tests/test_adaptive_dashboard_sched.py -q"""
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "utils"))
import janus_agent as ja  # noqa: E402
from dependency_manager import DependencyManager, Status  # noqa: E402


class _UI:
    def __getattr__(self, _):
        return lambda *a, **k: None


def _bare(*argv):
    a = ja.JANUSAutonomousAgent.__new__(ja.JANUSAutonomousAgent)
    a.args = ja.build_parser().parse_args(list(argv))
    a.ui = _UI()
    a._stop = threading.Event()
    a._results_lock = threading.Lock()
    a._dashboard_ui = None
    a._refresh_schedule_ui = types.MethodType(lambda self: None, a)
    return a


def test_scheduler_refills_slot_while_done_block_still_adapting():
    a = _bare("--parallel", "3")
    a.dep = DependencyManager().restrict_to(["security", "noc", "ampo", "agent_domain"])
    started, release = {}, threading.Event()

    def proc(self, b):
        started[b] = time.time()
        with self._results_lock:
            self.dep.mark(b, Status.DONE)          # unblocks dependents ...
        if b == "security":
            release.wait(30)                       # ... then a long adaptive pass

    a._process_and_mark = types.MethodType(proc, a)
    t = threading.Thread(target=a._main_loop, daemon=True)
    t.start()
    deadline = time.time() + 15
    while time.time() < deadline and not {"ampo", "agent_domain"} <= set(started):
        time.sleep(0.2)
    launched = set(started)
    release.set()
    t.join(30)
    # ampo/agent_domain must start while security's worker is still busy
    assert {"ampo", "agent_domain"} <= launched, launched
    assert not t.is_alive()


def test_reason_tags():
    tag = ja.JANUSAutonomousAgent._adaptive_reason_tag
    assert tag("DRV_LIMITED", {"slew_viol": 587, "cap_viol": 0}) == "slew/cap 587"
    assert tag("MODERATE", {"groups": {"reg2reg": {"wns_ps": -42.4}}}) == "setup -42ps"
    assert tag("CLOCK_SKEW_LIMITED", {}) == "clk skew"
    assert tag("CONGESTION_LIMITED", None) == "congestion"


def test_dashboard_row_shows_adapt():
    a = _bare()
    a.dep = DependencyManager().restrict_to(["security"])
    r = ja.BlockResult("security") if hasattr(ja, "BlockResult") else None
    r.status, r.wns_ns, r.hold_ps = "DONE", 0.0, 11.0
    a.results = {"security": r}
    a._live_blocks, a._post_step = {}, {}
    a._macro_lib_missing = lambda b: []
    a._adapt_live = {"security": {"tag": "slew/cap 587", "stage": "5_route",
                                  "t0": time.time() - 65}}
    ui = ja.DashboardUI.__new__(ja.DashboardUI)
    ui.agent = a
    row = ui._rows()[0]
    assert row["status"] == "ADAPT"
    assert row["stage"] == "slew/cap 587→5_route"
    assert row["idx"] == 5 and row["elapsed"].startswith("+")
    assert ui._counts([row])["RUNNING"] == 1
    a._adapt_live = {}
    assert ui._rows()[0]["stage"] == "6_final"     # back to base verdict row


def test_stage_poll_follows_newest_log(tmp_path):
    a = _bare()
    live, stop = {"stage": "3_place"}, threading.Event()
    (tmp_path / "3_3_place_gp.log").write_text("x")
    th = threading.Thread(target=a._adaptive_stage_poll,
                          args=(str(tmp_path), live, stop), daemon=True)
    th.start()
    time.sleep(0.2)
    p = tmp_path / "5_2_route.log"
    p.write_text("x")
    os.utime(p, (time.time() + 10, time.time() + 10))
    deadline = time.time() + 12
    while time.time() < deadline and live["stage"] != "5_route":
        time.sleep(0.5)
    stop.set()
    assert live["stage"] == "5_route"
