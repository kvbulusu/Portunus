"""§250: the ECO db is written BEFORE _rcx (extract_parasitics -> odb::orderWires
made every adopted ECO odb un-reroutable: DRT-0206), plus --eco-from-preeco
recovery (hold ECO from the ORFS-original 6_final.*.preeco)."""
import os
import re
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import janus_agent as ja  # noqa: E402
from incremental_opt import ECOResult  # noqa: E402
from test_post_route_eco import _eco_agent, _patch_popen, _patch_gds  # noqa: E402

BLK = "user_core_ara"
PRO = os.path.join(HERE, "..", "..", "scripts", "post_route_opt.tcl")
MT = os.path.join(HERE, "..", "..", "utils", "mail_trigger.py")

HOLD_OUT = """JANUS_PR_BEGIN hold
JANUS_PR_PRE setup wns -65.970
JANUS_PR_PRE setup tns -65.970
JANUS_PR_PRE hold wns -170.590
JANUS_PR_PRE hold tns -16000.000
JANUS_PR_PRE hold viol 422
JANUS_PR drc 0
JANUS_PR db_written pre_rcx 1
JANUS_PR setup wns {setup}
JANUS_PR setup tns {setup}
JANUS_PR hold wns {hold}
JANUS_PR hold tns -10.000
JANUS_PR hold viol 2
JANUS_PR wrote {{odb}}
JANUS_PR_END hold
"""
INFORCE = {"setup_ws": -68.674, "setup_tns": -68.674, "hold_ws": -7.313,
           "hold_tns": -11.094, "drc": 0}


# --------------------------------------------------------------- Tcl order --
def _branch(s, start, end):
    i = s.index(start)
    return s[i:s.index(end, i)]


def test_write_outputs_between_reroute_and_rcx_in_eco_branches():
    s = open(PRO).read()
    hold = _branch(s, "if {$mode in {hold setup timing}} {", '} elseif {$mode eq "drv"} {')
    drv = _branch(s, '} elseif {$mode eq "drv"} {', '} elseif {$mode eq "drc"} {')
    for body in (hold, drv):
        r, w, x = body.index("_reroute\n"), body.index("_write_outputs"), body.index("[_rcx]")
        assert r < w < x


def test_single_write_db_and_side_outputs_inside_proc():
    s = open(PRO).read()
    assert s.count("write_db $out") == 1
    proc = _branch(s, "proc _write_outputs {} {", '\nputs "JANUS_PR_BEGIN $mode"')
    for t in ("write_db $out", "write_def $::env(JANUS_OUT_DEF)",
              "write_verilog $::env(JANUS_OUT_V)", "deleteRoutingObstructions",
              "if {$::janus_pr_written} return"):
        assert t in proc, t
    tail = s[s.index("JANUS_PR_RPT)]}"):]
    # drc mode (no RCX) still writes at the end; the agent-parsed line stays last
    assert tail.index("_write_outputs") < tail.index('puts "JANUS_PR wrote $out"') \
        < tail.index('puts "JANUS_PR_END $mode"')
    assert "_rcx" not in tail


# ---------------------------------------------------------- in-force rule --
def _e(setup, hold, drc=0, htns=-10.0, stns=None):
    return ECOResult(ok=True, mode="hold", setup_wns=setup, hold_wns=hold, drc=drc,
                     hold_tns=htns, setup_tns=setup if stns is None else stns, wrote=True)


KN = dict(ja.JANUSAutonomousAgent._ECO_TRADEOFF_DEFAULTS, JANUS_ECO_HOLD_MIN_GAIN_PS=20.0)


@pytest.mark.parametrize("eco,ok,frag", [
    (_e(-68.7, -7.3), True, "vs in force: hold"),          # reproduction
    (_e(-72.0, -8.0), True, "vs in force"),                 # within 1 ps / 5 ps
    (_e(-68.7, -9.0), False, "vs in force"),                # hold worse, no gain
    (_e(-80.0, -7.3), False, "vs in force"),                # setup -11 ps
    (_e(-68.7, -7.3, drc=1), False, "DRC 1 > in-force 0"),
    (_e(-76.0, 20.0), True, "CLOSED"),                      # loss 7.3 > floor: §232 K rule
])
def test_eco_vs_inforce(eco, ok, frag):
    got, why = ja.JANUSAutonomousAgent._eco_vs_inforce(eco, INFORCE, KN)
    assert got is ok and frag in why, why


def test_eco_vs_inforce_without_inforce_or_timing():
    f = ja.JANUSAutonomousAgent._eco_vs_inforce
    assert f(_e(-68.7, -7.3), {}, KN)[0] is True
    bad = _e(-68.7, -7.3)
    bad.hold_wns = None
    assert f(bad, INFORCE, KN)[0] is False
    assert f(_e(-60.0, -1.0), dict(INFORCE, hold_ws=0.5), KN)[0] is False


# ---------------------------------------------------- from-preeco end2end --
def _preeco_agent(tmp_path):
    a, res = _eco_agent(tmp_path)
    for k in ("odb", "spef", "def", "v", "gds"):
        (res / f"6_final.{k}").write_text(f"eco1-{k}")          # RCX-ordered ECO in force
        (res / f"6_final.{k}.preeco").write_text(f"orig-{k}")   # ORFS originals
    return a, res


def test_from_preeco_adopts_and_keeps_orfs_originals(tmp_path, monkeypatch):
    a, res = _preeco_agent(tmp_path)
    seen = []
    _patch_popen(monkeypatch, a, res, HOLD_OUT.format(setup="-68.500", hold="-7.200"), seen=seen)
    _patch_gds(monkeypatch, res)
    kw = a._eco_preeco_setup(BLK, INFORCE)
    assert os.readlink(res / "janus_preeco_in.odb") == "6_final.odb.preeco"
    s, h = a._eco_pass_orfs(BLK, "hold", -68.674, -7.313, strategy="repair", **kw)
    a._eco_preeco_cleanup(BLK)
    cmd = seen[0]
    assert "JANUS_PR_IN_ODB=janus_preeco_in.odb" in cmd
    assert f"JANUS_SPEF={res}/6_final.spef.preeco" in cmd
    assert not any(t.startswith("JANUS_PR_HOLD_STRATEGY=") for t in cmd)   # repair
    assert (s, h) == (-68.5, -7.2)
    assert (res / "6_final.odb").read_text() == "eco"
    for k in ("odb", "spef", "def", "v", "gds"):
        assert (res / f"6_final.{k}.preeco").read_text() == f"orig-{k}"
        assert not (res / f"6_final.{k}.ecoprev").exists()
    assert not os.path.lexists(res / "janus_preeco_in.odb")


def test_from_preeco_discards_when_worse_than_inforce(tmp_path, monkeypatch):
    a, res = _preeco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, HOLD_OUT.format(setup="-68.500", hold="-30.000"))
    _patch_gds(monkeypatch, res)
    kw = a._eco_preeco_setup(BLK, INFORCE)
    s, h = a._eco_pass_orfs(BLK, "hold", -68.674, -7.313, strategy="repair", **kw)
    assert (s, h) == (-68.674, -7.313)
    assert (res / "6_final.odb").read_text() == "eco1-odb"
    assert (res / "6_final.odb.preeco").read_text() == "orig-odb"
    assert not (res / "janus_eco_hold.odb").exists()


def test_adopt_failure_restores_inforce_not_preeco(tmp_path, monkeypatch):
    a, res = _preeco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, HOLD_OUT.format(setup="-68.500", hold="-7.200"))
    _patch_gds(monkeypatch, res, rc=1)                     # gds merge fails
    kw = a._eco_preeco_setup(BLK, INFORCE)
    a._eco_pass_orfs(BLK, "hold", -68.674, -7.313, strategy="repair", **kw)
    assert (res / "6_final.odb").read_text() == "eco1-odb"
    assert (res / "6_final.odb.preeco").read_text() == "orig-odb"


def test_normal_adopt_still_backs_up_to_preeco(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, HOLD_OUT.format(setup="-62.000", hold="-7.200"))
    _patch_gds(monkeypatch, res)
    a.results[BLK].wns_ns, a.results[BLK].hold_ps = -65.97, -170.59
    a._eco_pass_orfs(BLK, "hold", -65.97, -170.59)
    assert (res / "6_final.odb").read_text() == "eco"
    assert (res / "6_final.odb.preeco").read_text() == "orig-odb"


# -------------------------------------------------------- CLI / dispatch --
def test_eco_hold_only_from_preeco_missing_backup_exit3(tmp_path):
    a, res = _eco_agent(tmp_path)
    a.args.eco_hold_only, a.args.eco_from_preeco, a.args.blocks = True, True, [BLK]
    a._find_routed_odb = lambda b: str(res / "6_final.odb")
    a._classify_timing = lambda b: None
    called, mails = [], []
    a._eco_pass_orfs = lambda *x, **k: called.append(1)
    a._notify_settled = lambda b, r: mails.append(r.note)
    assert a._eco_hold_only() == 3
    assert not called and mails and "--eco-from-preeco needs" in mails[0]
    assert (res / "6_final.odb").read_text() == "orig-odb"


def test_eco_hold_only_from_preeco_forces_repair_and_cleans_link(tmp_path):
    a, res = _preeco_agent(tmp_path)
    a.args.eco_hold_only, a.args.eco_from_preeco, a.args.blocks = True, True, [BLK]
    a._find_routed_odb = lambda b: str(res / "6_final.odb")
    a._classify_timing = lambda b: None
    a._eco_hold_only_strategy = lambda b: "pin"
    a._eco_adopted_timing = lambda b: dict(INFORCE)
    got = []

    def _pass(b, m, s, h, strategy="repair", **kw):
        got.append((strategy, kw["in_odb"], kw["inforce"]["hold_ws"],
                    os.path.islink(res / "janus_preeco_in.odb")))
        return s, h
    a._eco_pass_orfs = _pass
    a._notify_settled = lambda b, r: None
    assert a._eco_hold_only() == 5
    assert got == [("repair", "janus_preeco_in.odb", -7.313, True)]
    assert not os.path.lexists(res / "janus_preeco_in.odb")


def test_parser_flags_and_run_guard(tmp_path):
    ns = ja.build_parser().parse_args(["--eco-hold-only", "--eco-from-preeco",
                                       "--blocks", "janus_soc_top"])
    assert ns.eco_hold_only and ns.eco_from_preeco
    assert re.search(r"(^|\s)--eco-from-preeco(\s|$)", open(MT).read())
    a, _ = _eco_agent(tmp_path)
    a.args.eco_from_preeco, a.args.eco_hold_only, a.args.eco_drv_only = True, False, True
    assert a.run() == 2
