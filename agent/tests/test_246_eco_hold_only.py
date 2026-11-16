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

    def _pass(b, mode, s, h, strategy="repair"):          # §248 strategy kw
        calls.append(mode)
        if adopt:
            a.results[b].note += "  +eco(hold) RCX WS -68.7/+3.1"
        return s, h
    a._eco_pass_orfs = _pass
    a._run_abstract = lambda b: calls.append("abs")
    a._run_signoff = lambda b: calls.append("so") or "ok"
    a._archive_golden = lambda b, r=None: calls.append("gold")
    a._notify_settled = lambda b, r: calls.append(("mail", r.status, r.note.split("; ")[-1]))


def test_eco_hold_only_adopt_runs_abstract_signoff_golden(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.eco_hold_only, a.args.blocks = True, [BLK]
    calls = []
    _wire(a, res, calls, adopt=True)
    assert a.run() == 0
    assert calls == ["hold", "abs", "so", "gold",
                     ("mail", "DONE", "eco-hold-only exit 0: hold ECO ADOPTED")]


def test_eco_hold_only_discard_returns_5_and_skips_downstream(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.eco_hold_only, a.args.blocks = True, [BLK]
    calls = []
    _wire(a, res, calls, adopt=False)
    assert a._eco_hold_only() == 5
    assert calls == ["hold", ("mail", "DONE",
                              "eco-hold-only exit 5: hold ECO DISCARDED, previous 6_final kept")]


def test_eco_hold_only_needs_blocks_and_routed_odb(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.blocks = []
    assert a._eco_hold_only() == 2
    a.args.blocks = [BLK]
    a._find_routed_odb = lambda b: None
    mails = []
    a._notify_settled = lambda b, r: mails.append((r.status, r.note))
    assert a._eco_hold_only() == 3
    assert mails and mails[0][0] == "FAILED" and "exit 3" in mails[0][1]


def test_eco_hold_only_parser_and_exclusive():
    ns = ja.build_parser().parse_args(["--eco-hold-only", "--blocks", "janus_soc_top"])
    assert ns.eco_hold_only and not ns.eco_drv_only
    assert "eco_hold_only" in ja._EXCLUSIVE_MODES


def test_soc_top_eco_hold_margin_10_after_247():
    """§246 set 25; §247 back to 10 (25 on the ECO'd 6_final -> DRT-0206, 370 opens)."""
    with open(os.path.join(_ROOT, "asic_flow", "configs", "janus_soc_top.mk")) as fh:
        mk = fh.read()
    m = re.search(r"^export\s+JANUS_ECO_HOLD_MARGIN_PS\s*=\s*(\S+)", mk, re.M)
    assert m and float(m.group(1)) == 10.0


def test_sr_apply_eco_rewrites_stale_subject_and_body():
    """§246b: soc_top 20261114_200411 mailed 6_finish (pre-ECO) slack."""
    subj = "[JANUS] janus_soc_top DONE · WNS -66.0ps · hold -171ps · 7/7 · 12h28m · RED FLAG"
    sr = {"subject": subj, "text": subj + "\n\nSTAGES", "html": "<p><b>" + subj + "</b></p>"}
    eco = {"mode": "hold", "setup_ws": -68.674, "setup_tns": -68.674, "hold_ws": -7.313,
           "hold_tns": -11.094, "drc": 0, "at": "2026-11-15T08:28:43"}
    ja.JANUSAutonomousAgent._sr_apply_eco(sr, eco, "eco-hold-only exit 0: hold ECO ADOPTED")
    assert sr["subject"] == ("[JANUS] janus_soc_top DONE · WNS -68.7ps · hold -7.3ps (post-ECO)"
                             " · 7/7 · 12h28m · RED FLAG")
    assert sr["text"].startswith(sr["subject"] + "\nTiming in force")
    assert "exit 0" in sr["text"] and sr["text"].endswith("\n\nSTAGES")
    assert "-171" not in sr["html"] and "exit 0" in sr["html"]


def test_sr_apply_eco_noop_without_eco_or_note():
    sr = {"subject": "[JANUS] x DONE · WNS 1.0ps · 7/7", "text": "t", "html": "h"}
    ja.JANUSAutonomousAgent._sr_apply_eco(sr, None, "")
    assert sr == {"subject": "[JANUS] x DONE · WNS 1.0ps · 7/7", "text": "t", "html": "h"}
