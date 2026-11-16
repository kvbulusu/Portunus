"""§247 notify email / report bundle: post-route ECO rows (each attempt + the
ECO in force) after 6_finish, violator counts, superseded 5_drt DRC flag.

Fixtures (trimmed real data, fixtures/t247):
  r211511  soc_top 20261115_211511 --eco-hold-only (margin 25): bundle-truncated
           janus_eco_hold.log tail ending in DRT-0206 (370 open nets), agent
           DISCARDED line, in-force janus_eco_adopted.json from 20261114_200411.
  r200411  the adopted margin-10 pass log (PRE -65.967/-170.591 422 viol ->
           POST -68.674/-7.313, hold viol 2, DRC 0).
Run: python3 -m pytest asic_flow/agent/tests/test_247_email_eco_rows.py -q"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))
import janus_agent as ja  # noqa: E402
import stage_report as sr  # noqa: E402
import eco_history as eh  # noqa: E402
import report_bundle as rb  # noqa: E402
from incremental_opt import parse_eco  # noqa: E402
from test_post_route_eco import _eco_agent, _patch_popen, _patch_gds, GOOD_HOLD  # noqa: E402

FIX = os.path.join(HERE, "fixtures", "t247")
T0 = 1_763_200_000.0      # 6_finish.rpt mtime; everything else relative to it


def _touch(p, t):
    os.utime(p, (t, t))


def _r211511(tmp_path, with_log=True):
    """Copy of the r211511 bundle with the real relative mtime order:
    5_route_drc < 6_finish < eco rpt < adopted json  <  eco log == agent log."""
    root = tmp_path / "r211511"
    shutil.copytree(os.path.join(FIX, "r211511"), root)
    L, R, A = root / "logs", root / "reports", root / "agent" / "janus_soc_top.log"
    for p in L.iterdir():
        _touch(p, T0 - 3600)
    _touch(R / "5_route_drc.rpt", T0 - 6000)
    _touch(R / "6_finish.rpt", T0)
    _touch(R / "janus_eco_hold.rpt", T0 + 7200)
    _touch(R / "janus_eco_adopted.json", T0 + 7920)
    if with_log:
        _touch(L / "janus_eco_hold.log", T0 + 58000)
    else:
        os.remove(L / "janus_eco_hold.log")
    _touch(A, T0 + 58000)
    return str(L), str(R), str(A)


def _report(L, R, A, **kw):
    return sr.build_report("janus_soc_top", "final", logs_dir=L, reports_dir=R,
                           agent_log=A, done=sr.STAGE_KEYS, **kw)


# --------------------------------------------------------------- log parsing --
def test_parse_adopted_margin10_log_r200411():
    lg = eh.parse_eco_log(os.path.join(FIX, "r200411", "logs", "janus_eco_hold.log"))
    assert lg["pre"] == {"setup_ws": -65.967, "setup_tns": -65.967, "hold_ws": -170.591,
                         "hold_tns": -15532.366, "hold_viol": 422}
    assert lg["post"]["hold_ws"] == -7.313 and lg["post"]["hold_tns"] == -11.094
    assert lg["post"]["hold_viol"] == 2 and lg["drc"] == 0
    assert lg["margin"] == 10.0 and lg["wrote"] and lg["ended"] and not lg["error"]


def test_parse_discarded_margin25_log_r211511():
    lg = eh.parse_eco_log(os.path.join(FIX, "r211511", "logs", "janus_eco_hold.log"))
    assert lg["error"] == "drt: DRT-0206" and lg["open_nets"] == 370
    assert not lg["post"] and not lg["wrote"] and lg["drc"] is None
    assert eh.short_error(lg["error"], lg["open_nets"]) == (
        "drt: DRT-0206 (370 open nets after incremental DRT; no post-ECO RCX)")


def test_parse_eco_setup_violator_count():
    out = ("JANUS_PR_PRE setup viol 3\nJANUS_PR_PRE hold viol 422\nJANUS_PR setup wns -68.674\n"
           "JANUS_PR hold wns -7.313\nJANUS_PR setup viol 1\nJANUS_PR hold viol 2\n"
           "JANUS_PR drc 0\nJANUS_PR wrote /x/e.odb\n")
    r = parse_eco(out, "hold")
    assert (r.pre_setup_viol, r.setup_viol, r.pre_hold_viol, r.hold_viol) == (3, 1, 422, 2)


def test_tcl_prints_setup_viol_and_margin():
    tcl = open(os.path.join(HERE, "..", "..", "scripts", "post_route_opt.tcl")).read()
    assert 'puts "$tag setup viol $n"' in tcl and 'puts "JANUS_PR hold_margin $hm"' in tcl


# ------------------------------------------------------------ stage table ----
def test_r211511_email_rows_in_force_and_discarded(tmp_path):
    L, R, A = _r211511(tmp_path)
    r = _report(L, R, A, ctx={"eco_hold_margin": "25"})
    assert not r.get("error")
    keys = [x["key"] for x in r["rows"]]
    i = keys.index("6_finish")
    att, inf = r["rows"][i + 1], r["rows"][i + 2]
    assert att["_eco"] == "attempt" and att["status"] == "DISCARDED"
    assert "DRT-0206" in att["why"] and "370 open nets" in att["why"]
    assert att["margin"] == 25.0 and att["margin_cfg"]
    assert att["pre"]["hold_ws"] == -7.313 and att["pre_src"] == "in force"
    assert inf["key"] == "6_eco_hold (in force)" and inf["status"] == "IN FORCE"
    assert (inf["setup_ws"], inf["hold_ws"], inf["eco_drc"]) == (-68.674, -7.313, 0)
    t = r["text"]
    main = t.split("STAGES", 1)[1].split("QoR DETAIL", 1)[0]
    det = t.split("QoR DETAIL", 1)[1]
    # main table: in-force row clearly marked, discarded row with reason + margin
    assert "6_eco_hold (in force)   IN FORCE" in main
    assert "S -68.67 · H -7.31 · DRC 0 ◀ in force" in main
    assert "DISCARDED" in main and "margin 25 (cfg)" in main and "DRT-0206" in main
    # QoR detail: 0.01 ps slacks, TNS and violator counts of the ECO in force
    assert "-68.67/-68.67 1" in det and "-7.31/-11.09 2" in det
    assert "DISCARDED: drt: DRT-0206 (370 open nets" in det
    assert "IN FORCE · DRC 0 · @2026-11-15T08:28:43" in det
    # stale route DRC flag superseded, Delta on the in-force row
    assert "[RED] 5_drt" not in t and not any(f.startswith("5_drt") for _l, f in r["flags"])
    assert any("superseded by 6_eco_hold (in force): ECO reroute DRC 0" in n for n in r["notes"])
    assert "DRC 1 (superseded: ECO DRC 0)" in main
    assert "Delta 6_eco_hold (in force) vs 6_finish: setup -2.7 ps, hold +163.3 ps" in t
    assert "Worst hold paths @ 6_eco_hold (in force)" in t
    assert "IN FORCE" in r["html"] and "6_eco_hold (in force)" in r["html"]


def test_no_eco_log_still_shows_in_force_row(tmp_path):
    L, R, A = _r211511(tmp_path, with_log=False)
    r = _report(L, R, A)
    eco = [x for x in r["rows"] if x.get("_eco")]
    assert [x["key"] for x in eco] == ["6_eco_hold (in force)"]


def test_stale_adopted_json_older_than_6_finish_ignored(tmp_path):
    L, R, A = _r211511(tmp_path)
    _touch(os.path.join(R, "janus_eco_adopted.json"), T0 - 10)
    rows = eh.eco_rows(R, L, A)
    assert not any(x["_eco"] == "in_force" for x in rows)
    r = _report(L, R, A)
    assert any(f.startswith("5_drt") for _l, f in r["flags"])     # flag stays


def test_eco_drc_nonzero_keeps_drt_flag(tmp_path):
    L, R, A = _r211511(tmp_path)
    p = os.path.join(R, "janus_eco_adopted.json")
    d = json.load(open(p))
    d["drc"] = 3
    json.dump(d, open(p, "w"))
    _touch(p, T0 + 7920)
    r = _report(L, R, A)
    assert any(f.startswith("5_drt") for _l, f in r["flags"]) and not r["notes"]


def test_no_eco_rows_without_finished_6_finish(tmp_path):
    L, R, A = _r211511(tmp_path)
    r = sr.build_report("janus_soc_top", "stage", stage="5_drt", logs_dir=L, reports_dir=R,
                        agent_log=A, done=sr.STAGE_KEYS[:6])
    assert not any(x.get("_eco") for x in r["rows"])


# ------------------------------------------------- violators / old JSON ------
def test_adopted_violators_old_json_falls_back_to_log_and_rpt(tmp_path):
    L, R, A = _r211511(tmp_path)
    # no history, the r211511 eco log is the failed pass -> exact counts from
    # the adopted pass's janus_eco_hold.rpt (+ setup WNS == TNS -> 1)
    assert eh.adopted_violators(R, L) == (1, 2)
    # the adopted pass's own log (r200411) matches the JSON post numbers
    shutil.copy(os.path.join(FIX, "r200411", "logs", "janus_eco_hold.log"),
                os.path.join(L, "janus_eco_hold.log"))
    os.remove(os.path.join(R, "janus_eco_hold.rpt"))
    assert eh.adopted_violators(R, L)[1] == 2


def test_adopted_violators_json_wins(tmp_path):
    L, R, A = _r211511(tmp_path)
    p = os.path.join(R, "janus_eco_adopted.json")
    d = dict(json.load(open(p)), setup_viol=4, hold_viol=9)
    json.dump(d, open(p, "w"))
    _touch(p, T0 + 7920)
    assert eh.adopted_violators(R, L) == (4, 9)


def test_report_bundle_violators_and_superseded_drc(tmp_path):
    L, R, A = _r211511(tmp_path)
    q = rb.build_qor("janus_soc_top", "DONE", L, R, None, agent_log=A)
    t = q["timing"]
    assert t["source"] == "post-route ECO RCX"
    assert t["violators"]["setup"] == 1 and t["violators"]["hold"] == 2
    assert t["hold_tns"] == -11.094
    body = rb.render_email_body(q)
    assert "violators n/a" not in body and "TNS -11.09  violators 2" in body
    if q.get("route_drc"):
        assert q["route_drc"]["superseded_by_eco"] == 0
        assert "superseded: post-route ECO reroute DRC 0" in body


# ------------------------------------------------------ history persistence --
def test_history_rows_persist_and_old_builds_drop(tmp_path):
    L, R, A = _r211511(tmp_path, with_log=False)
    base = {"mode": "hold", "margin": 25.0, "wall_s": 4260.0,
            "pre": {"setup_ws": -68.674, "hold_ws": -7.313, "hold_viol": 2},
            "post": {"setup_ws": None, "hold_ws": None, "drc": None}}
    assert eh.append_history(R, dict(base, verdict="DISCARDED", why="old build",
                                     t=T0 - 100, at="x"))
    assert eh.append_history(R, dict(base, verdict="DISCARDED", t=T0 + 58000, at="y",
                                     why="ECO produced no trustworthy timing data (drt: DRT-0206)"))
    assert len(eh.read_history(R)) == 2
    rows = eh.eco_rows(R, L, A)
    att = [x for x in rows if x["_eco"] == "attempt"]
    assert len(att) == 1 and "DRT-0206" in att[0]["why"] and att[0]["margin"] == 25.0
    assert att[0]["wall"] == 4260.0 and not att[0]["margin_cfg"]
    assert rows[-1]["_eco"] == "in_force"


def test_history_cap_and_corrupt_file(tmp_path):
    R = str(tmp_path)
    open(os.path.join(R, eh.HISTORY_JSON), "w").write("{not json")
    assert eh.read_history(R) == []
    for i in range(eh.HISTORY_CAP + 5):
        eh.append_history(R, {"t": i})
    h = eh.read_history(R)
    assert len(h) == eh.HISTORY_CAP and h[-1]["t"] == eh.HISTORY_CAP + 4
    assert not eh.append_history(str(tmp_path / "missing"), {"t": 1})


# ------------------------------------------------------------ agent wiring ---
def test_agent_discard_appends_history(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, "JANUS_PR_PRE hold wns -4.55\nJANUS_PR_PRE hold viol 7\n"
                                      "JANUS_PR_ERROR drt: [ERROR DRT-0206]\n")
    _patch_gds(monkeypatch, res)
    a._eco_pass_orfs("user_core_ara", "hold", -60.07, -4.55)
    h = eh.read_history(a._reports_dir("user_core_ara"))
    assert len(h) == 1 and h[0]["verdict"] == "DISCARDED" and h[0]["mode"] == "hold"
    assert "DRT-0206" in h[0]["why"] and h[0]["margin"] == 10.0
    assert h[0]["pre"]["hold_ws"] == -4.55 and h[0]["pre"]["hold_viol"] == 7
    assert h[0]["wall_s"] is not None


def test_agent_adopt_appends_history_and_counts_in_json(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    txt = GOOD_HOLD.replace("JANUS_PR hold tns 0.000",
                            "JANUS_PR hold tns 0.000\nJANUS_PR setup viol 3\nJANUS_PR hold viol 0")
    _patch_popen(monkeypatch, a, res, txt)
    _patch_gds(monkeypatch, res)
    assert a._eco_pass_orfs("user_core_ara", "hold", -60.07, -4.55) == (-61.5, 6.2)
    rep = a._reports_dir("user_core_ara")
    d = json.load(open(os.path.join(rep, "janus_eco_adopted.json")))
    assert (d["setup_viol"], d["hold_viol"], d["hold_tns"], d["margin"]) == (3, 0, 0.0, 10.0)
    assert d["pre_hold_ws"] == -4.55
    h = eh.read_history(rep)
    assert [e["verdict"] for e in h] == ["ADOPTED"]
    assert h[0]["post"]["setup_viol"] == 3 and h[0]["post"]["drc"] == 0


def test_stage_ctx_carries_eco_hold_margin(tmp_path):
    a, _res = _eco_agent(tmp_path)
    assert a._stage_ctx("user_core_ara").get("eco_hold_margin") == "10"
