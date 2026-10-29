"""§163 user_core: setup-ECO hold guard + chained hold pass, auto re-stamp.
Run: python3 -m pytest asic_flow/agent/tests/test_uc_163_eco_restamp.py -q"""
import os
import sys
import time

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))
import janus_agent as ja  # noqa: E402
import flow_state as fs  # noqa: E402
from test_agent_adaptive import _agent  # noqa: E402
from test_post_route_eco import _eco_agent, _FakeProc, _patch_gds  # noqa: E402

TCL = os.path.join(HERE, "..", "..", "scripts", "post_route_opt.tcl")
UC_MK = os.path.join(HERE, "..", "..", "configs", "user_core.mk")

# run 20261028_181202 shape: setup ECO gains setup, reroute+RCX opens hold
SETUP_TXT = ("JANUS_PR_PRE setup wns -144.85\nJANUS_PR_PRE hold wns 0.549\nJANUS_PR drc 0\n"
             "JANUS_PR setup wns -6.22\nJANUS_PR hold wns -9.297\nJANUS_PR wrote {odb}\n")
HOLD_OK = ("JANUS_PR_PRE setup wns -6.22\nJANUS_PR_PRE hold wns -9.297\nJANUS_PR drc 0\n"
           "JANUS_PR setup wns -9.10\nJANUS_PR hold wns 1.20\nJANUS_PR wrote {odb}\n")
HOLD_BAD = HOLD_OK.replace("hold wns 1.20", "hold wns -2.00")


def _patch_by_stem(monkeypatch, a, res, texts, seen):
    """Fake Popen keyed on RUN_LOG_NAME_STEM (janus_eco_<tag>)."""
    def fake(cmd, **kw):
        seen.append(cmd)
        stem = next(t.split("=", 1)[1] for t in cmd if t.startswith("RUN_LOG_NAME_STEM="))
        tag = stem[len("janus_eco_"):]
        outs = list(a._eco_outputs(str(res), tag).values())
        return _FakeProc(texts[tag].format(odb=os.path.join(str(res), stem + ".odb")), 0, outs)
    monkeypatch.setattr(ja.subprocess, "Popen", fake)


# ------------------------------------------------------------ ECO chaining --
def test_setup_gain_hold_loss_chains_hold_and_adopts(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    seen = []
    _patch_by_stem(monkeypatch, a, res, {"setup": SETUP_TXT, "setup_hold": HOLD_OK}, seen)
    _patch_gds(monkeypatch, res)
    s, h = a._eco_pass_orfs("user_core_ara", "setup", -144.85, 0.549)
    assert (s, h) == (-9.1, 1.2)
    assert len(seen) == 2
    chain = seen[1]
    assert "JANUS_PR_MODE=hold" in chain and "JANUS_PR_IN_ODB=janus_eco_setup.odb" in chain
    assert f"JANUS_SPEF={res}/janus_eco_setup.spef" in chain
    assert "RUN_LOG_NAME_STEM=janus_eco_setup_hold" in chain
    for k in ("odb", "spef", "def", "v"):
        assert (res / f"6_final.{k}").read_text() == "eco"
        assert (res / f"6_final.{k}.preeco").read_text() == f"orig-{k}"
    assert not any(n.startswith("janus_eco_") for n in os.listdir(res))
    assert "+eco(setup_hold)" in a.results["user_core_ara"].note
    log = open(a._block_log("user_core_ara")).read()
    assert "chaining a hold pass" in log and "ADOPTED" in log


def test_chain_rejected_when_hold_still_negative(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    seen = []
    _patch_by_stem(monkeypatch, a, res, {"setup": SETUP_TXT, "setup_hold": HOLD_BAD}, seen)
    _patch_gds(monkeypatch, res)
    assert a._eco_pass_orfs("user_core_ara", "setup", -144.85, 0.549) == (-144.85, 0.549)
    for k in ("odb", "spef", "def", "v", "gds"):
        assert (res / f"6_final.{k}").read_text() == f"orig-{k}"
    assert not any(n.startswith("janus_eco_") for n in os.listdir(res))
    log = open(a._block_log("user_core_ara")).read()
    assert "DISCARDED" in log and "setup+hold chain: HOLD WS regressed" in log


def test_no_chain_when_setup_did_not_gain(tmp_path, monkeypatch):
    a, res = _eco_agent(tmp_path)
    seen = []
    flat = SETUP_TXT.replace("setup wns -6.22", "setup wns -144.5")
    _patch_by_stem(monkeypatch, a, res, {"setup": flat}, seen)
    _patch_gds(monkeypatch, res)
    assert a._eco_pass_orfs("user_core_ara", "setup", -144.85, 0.549) == (-144.85, 0.549)
    assert len(seen) == 1


def test_tcl_setup_mode_has_hold_guard():
    s = open(TCL).read()
    assert "JANUS_PR_SETUP_HOLD_GUARD" in s
    assert "$hold_after_setup" in s


# ------------------------------------------------------ config / flow_state --
def test_eco_knob_invalidates_no_stage():
    old = {"JANUS_ECO_HOLD_MARGIN_PS": "10", "CORE_UTILIZATION": "40"}
    new = {"JANUS_ECO_HOLD_MARGIN_PS": "20", "CORE_UTILIZATION": "40"}
    assert fs._changed_keys(old, new) == []


def test_user_core_eco_hold_margin_20():
    assert "export JANUS_ECO_HOLD_MARGIN_PS   = 20" in open(UC_MK).read()


# ------------------------------------------------------------ auto re-stamp --
def _stamp_agent(tmp_path, *argv, local=False, sdc=True):
    a = _agent(tmp_path, *argv)
    a.configs_dir = str(tmp_path / "cfg")
    a.design_root = str(tmp_path / "designs")
    os.makedirs(os.path.join(a.configs_dir, "constraints"))
    b = "user_core"
    (tmp_path / "cfg" / f"{b}.mk").write_text("export CORE_UTILIZATION = 40\n")
    if local:
        (tmp_path / "cfg" / f"{b}_local.mk").write_text("export X = 1\n")
    if sdc:
        (tmp_path / "cfg" / "constraints" / f"{b}.sdc").write_text("create_clock\n")
    a._stamp_users = lambda p: []
    return a, b


def _stamp_now(a, b):
    d = os.path.join(a.design_root, b)
    os.makedirs(d, exist_ok=True)
    for src, dst in a._stamp_pairs(b):
        if src:
            with open(src, "rb") as f, open(dst, "wb") as g:
                g.write(f.read())


def test_restamp_fresh_does_not_touch(tmp_path):
    a, b = _stamp_agent(tmp_path)
    _stamp_now(a, b)
    cfg = a._config_path(b)
    old = time.time() - 1000
    os.utime(cfg, (old, old))
    assert a._auto_restamp(b) == ("fresh", "")
    assert abs(os.path.getmtime(cfg) - old) < 1


def test_restamp_refreshes_changed_files_and_drops_stale_local(tmp_path):
    a, b = _stamp_agent(tmp_path)
    _stamp_now(a, b)
    d = os.path.join(a.design_root, b)
    open(os.path.join(d, f"{b}_local.mk"), "w").write("stale\n")
    (tmp_path / "cfg" / f"{b}.mk").write_text("export CORE_UTILIZATION = 40\nexport JANUS_ECO_HOLD_MARGIN_PS = 20\n")
    (tmp_path / "cfg" / "constraints" / f"{b}.sdc").write_text("create_clock -period 2.857\n")
    state, detail = a._auto_restamp(b)
    assert state == "restamped"
    assert "config.mk" in detail and "constraint.sdc" in detail and "removed" in detail
    assert "JANUS_ECO_HOLD_MARGIN_PS" in open(a._config_path(b)).read()
    assert not os.path.exists(os.path.join(d, f"{b}_local.mk"))
    assert not any(n.endswith(".janus_restamp.tmp") for n in os.listdir(d))
    assert a._auto_restamp(b) == ("fresh", "")
    assert a._verify_stamp_fresh(b) is None


def test_restamp_skips_skip_flag_never_stamped_and_live_user(tmp_path):
    a, b = _stamp_agent(tmp_path, "--skip-stamp-check")
    _stamp_now(a, b)
    (tmp_path / "cfg" / f"{b}.mk").write_text("changed\n")
    assert a._auto_restamp(b)[0] == "skipped"

    a, b = _stamp_agent(tmp_path / "n")
    st, why = a._auto_restamp(b)
    assert st == "skipped" and "03_setup_janus_configs" in why

    a, b = _stamp_agent(tmp_path / "u")
    _stamp_now(a, b)
    (tmp_path / "u" / "cfg" / f"{b}.mk").write_text("changed\n")
    a._stamp_users = lambda p: [4242]
    st, why = a._auto_restamp(b)
    assert st == "skipped" and "4242" in why
    assert open(a._config_path(b)).read() != "changed\n"


def test_restamp_no_repo_config_is_quiet(tmp_path):
    a, b = _stamp_agent(tmp_path)
    assert a._auto_restamp("not_a_block") == ("skipped", "no repo config")
