"""§246: --eco-hold-only (one more post-route hold ECO on the existing,
already ECO'd 6_final) + soc_top ECO hold margin 25 ps."""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import janus_agent as ja  # noqa: E402
from test_183_port_slew_eco import _closed_agent, BLK  # noqa: E402

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _wire(a, res, calls, adopt):
    a._find_routed_odb = lambda b: str(res / "6_final.odb")
    a._classify_timing = lambda b: None

    def _pass(b, mode, s, h):
        calls.append(mode)
        if adopt:
            a.results[b].note += "  +eco(hold) RCX WS -68.7/+3.1"
        return s, h
    a._eco_pass_orfs = _pass
    a._run_abstract = lambda b: calls.append("abs")
    a._run_signoff = lambda b: calls.append("so") or "ok"
    a._archive_golden = lambda b, r=None: calls.append("gold")


def test_eco_hold_only_adopt_runs_abstract_signoff_golden(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.eco_hold_only, a.args.blocks = True, [BLK]
    calls = []
    _wire(a, res, calls, adopt=True)
    assert a.run() == 0
    assert calls == ["hold", "abs", "so", "gold"]


def test_eco_hold_only_discard_returns_5_and_skips_downstream(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.eco_hold_only, a.args.blocks = True, [BLK]
    calls = []
    _wire(a, res, calls, adopt=False)
    assert a._eco_hold_only() == 5
    assert calls == ["hold"]


def test_eco_hold_only_needs_blocks_and_routed_odb(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.blocks = []
    assert a._eco_hold_only() == 2
    a.args.blocks = [BLK]
    a._find_routed_odb = lambda b: None
    assert a._eco_hold_only() == 3


def test_eco_hold_only_parser_and_exclusive():
    ns = ja.build_parser().parse_args(["--eco-hold-only", "--blocks", "janus_soc_top"])
    assert ns.eco_hold_only and not ns.eco_drv_only
    assert "eco_hold_only" in ja._EXCLUSIVE_MODES


def test_soc_top_eco_hold_margin_25():
    with open(os.path.join(_ROOT, "asic_flow", "configs", "janus_soc_top.mk")) as fh:
        mk = fh.read()
    m = re.search(r"^export\s+JANUS_ECO_HOLD_MARGIN_PS\s*=\s*(\S+)", mk, re.M)
    assert m and float(m.group(1)) == 25.0
