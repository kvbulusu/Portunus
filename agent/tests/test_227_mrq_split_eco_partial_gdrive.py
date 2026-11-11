"""§227-§229 (run 20261109_210551, 6_finish S -139.94 / H -193.44, Drive FAILED):
  §227 JANUS_PIN_FENCE_SPLIT: u_ucut_mn.u_r/g_egress.mr_q placed between the
       u_user r_ready pin and its R read-mux fanout (frac 0.4) after GPL.
  §228 post-route hold ECO that gains >= 20 ps hold (not closed) within the
       5 ps setup tolerance is ADOPTED (was discarded: -193.44 -> -120.73 for
       setup -139.94 -> -142.62).
  §229 Drive preflight (real token refresh) at launch + gdrive_upload.py CLI.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import janus_agent as ja  # noqa: E402
import notification as nt  # noqa: E402
import gdrive_upload as gu  # noqa: E402
from incremental_opt import ECOResult  # noqa: E402

PF = ROOT / "asic_flow/scripts/hooks/janus_pin_fence.tcl"
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
A = ja.JANUSAutonomousAgent


def _mk(name):
    m = re.findall(rf"(?m)^export {name}\s*=\s*(.*?)\s*$", MK)
    assert m, name
    return m[-1]


def _tcl(body, procs=("env", "name_match", "split_xy", "mean", "mid", "split_specs")):
    if not shutil.which("tclsh"):
        pytest.skip("tclsh missing")
    src = PF.read_text()
    pat = "|".join(procs)
    got = re.findall(rf"(?ms)^proc janus_pf::(?:{pat}) .*?^\}}", src)
    assert len(got) == len(procs)
    t = "namespace eval janus_pf {}\n" + "\n".join(got) + "\n" + body
    r = subprocess.run(["tclsh"], input=t, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


# ------------------------------------------------------------------ §227 --
def test_split_spec_in_mk_targets_mrq_only():
    s = _mk("JANUS_PIN_FENCE_SPLIT").split()
    assert s == ["mrq:u_user:u_ucut_mn/u_r/g_egress/mr_q*:0.4"]
    # the §226 fence is unchanged (mr_q stays out of the ingress fence)
    assert "g_egress" not in _mk("JANUS_PIN_FENCE")
    assert not re.search(r"(?m)^export JANUS_PIN_FENCE_SPLIT\s*\?=", MK)


def test_split_glob_matches_real_mrq_name_only():
    out = _tcl(r"""
foreach n {
  {u_ucut_mn.u_r/g_egress.mr_q$_DFF_PN0_}
  {u_ucut_mn.u_b/g_egress.mr_q$_DFF_PN0_}
  {u_ucut_mn.u_r/g_egress.out_data_q[3]$_DFFE_PN0P_}
  {u_ucut_s3n.u_r/g_egress.mr_q$_DFF_PN0_}
} { puts [janus_pf::name_match {u_ucut_mn/u_r/g_egress/mr_q*} $n] }
""")
    assert out.split() == ["1", "0", "0", "0"]


def test_split_xy_and_specs():
    out = _tcl(r"""
puts [janus_pf::split_xy 100 1000 1100 500 0.4]
puts [janus_pf::split_xy 100 1000 1100 500 0]
puts [janus_pf::split_xy 100 1000 1100 500 1]
puts [janus_pf::mean {{0 0} {10 20}}]
puts "<[janus_pf::mean {}]>"
puts [janus_pf::mid {0 0 10 4}]
puts [llength [janus_pf::split_specs "a:b:c:0.4 a:b:c x:y:z:1.5 x:y:z:abc p:q:r:1"]]
""")
    lines = out.strip().splitlines()
    xy = [[float(v) for v in l.split()] for l in lines[:3]]
    assert xy == [[700.0, 700.0], [1100.0, 500.0], [100.0, 1000.0]]  # f 0.4 / 0 (Q) / 1 (D)
    assert lines[3] == "5.0 10.0" and lines[4] == "<>" and lines[5] == "5.0 2.0"
    assert sum("WARN bad spec" in l for l in lines) == 3
    assert lines[-1] == "2"


def test_split_wiring_in_hook():
    s = PF.read_text()
    run = s[s.index("proc janus_pf::run "):]
    fin = s[s.index("proc janus_pf::finish "):s.index("proc janus_pf::wrap ")]
    # split alone (no JANUS_PIN_FENCE) still runs; wrapped GPL kept while pending
    assert 'if { $specs eq "" && $sspec eq "" } { return }' in run
    assert "set ::janus_pf::split_pending 1" in run
    assert "!$::janus_pf::split_pending } { janus_pf::unwrap }" in run
    assert "janus_pf::split_all" in fin and fin.index("split_all") < fin.index("janus_pf::unwrap")
    one = s[s.index("proc janus_pf::split_one "):s.index("proc janus_pf::split_all ")]
    assert "setPlacementStatus PLACED" in one and "FIRM" not in one   # not locked
    assert "janus_pf::pass_other $s" in one      # Q side follows the QN inverter
    assert "janus_pf::macro_pins $net $mac $maxfo" in one
    assert "janus_pf::clamp $tx $ty $core" in one
    assert "moved 0 insts" in one
    # whole hook still parses (no OpenROAD -> run is not invoked)
    if shutil.which("tclsh"):
        r = subprocess.run(["tclsh"], input=f"source {PF}\nputs ok\n",
                           capture_output=True, text=True)
        assert r.returncode == 0 and "ok" in r.stdout, r.stderr


# ------------------------------------------------------------------ §228 --
def _eco(**kw):
    e = ECOResult(ok=True, mode="hold", drc=0)
    for k, v in kw.items():
        setattr(e, k, v)
    return e


@pytest.mark.parametrize("kw,b_setup,b_hold,ok", [
    # run 20261109_210551 janus_eco_hold: -193.44 -> -120.73, setup -139.94 -> -142.62
    (dict(hold_wns=-120.73, setup_wns=-142.62, hold_tns=-646.0), -139.94, -193.44, True),
    (dict(hold_wns=-180.0, setup_wns=-139.94), -139.94, -193.44, False),     # < 20 ps
    (dict(hold_wns=-120.73, setup_wns=-146.0), -139.94, -193.44, False),     # setup -6
    (dict(hold_wns=1.0, setup_wns=-140.0), -139.94, -193.44, False),         # closed: other rule
    (dict(hold_wns=-120.0, setup_wns=-139.0), -139.94, 2.0, False),          # hold was closed
    (dict(hold_wns=-120.0, setup_wns=None), -139.94, -193.44, False),
    (dict(hold_wns=-120.0, setup_wns=-139.0, ok=False), -139.94, -193.44, False),
])
def test_partial_hold_rule(kw, b_setup, b_hold, ok):
    assert A._eco_hold_partial_ok(_eco(**kw), b_setup, b_hold, 20.0, 5.0) is ok


def test_partial_hold_rule_disabled_and_tns_guard():
    e = _eco(hold_wns=-120.73, setup_wns=-142.62)
    assert not A._eco_hold_partial_ok(e, -139.94, -193.44, 0.0, 5.0)
    e.pre_hold_tns, e.hold_tns = -31427.0, -40000.0
    assert not A._eco_hold_partial_ok(e, -139.94, -193.44, 20.0, 5.0)


def test_min_gain_knob():
    stub = types.SimpleNamespace(_ECO_HOLD_MIN_GAIN_PS=20.0)
    for v, want in ((None, 20.0), ("35", 35.0), ("x", 20.0), ("0", 0.0)):
        stub._janus_knob = lambda b, n, v=v: v
        assert A._eco_hold_min_gain(stub, "janus_soc_top") == want


R210551 = """JANUS_PR loader orfs 6_final.odb
JANUS_PR_BEGIN hold
JANUS_PR_PRE setup wns -139.941
JANUS_PR_PRE hold wns -193.440
JANUS_PR drc 0
JANUS_PR setup wns -142.621
JANUS_PR setup tns -2476.0
JANUS_PR hold wns -120.730
JANUS_PR hold tns -646.0
JANUS_PR wrote {odb}
JANUS_PR_END hold
"""


def test_run_210551_hold_eco_now_adopted(tmp_path, monkeypatch):
    from test_post_route_eco import _eco_agent, _patch_popen, _patch_gds
    a, res = _eco_agent(tmp_path)
    a.results["user_core_ara"].wns_ns = -139.94
    a.results["user_core_ara"].hold_ps = -193.44
    _patch_popen(monkeypatch, a, res, R210551)
    _patch_gds(monkeypatch, res)
    s, h = a._eco_pass_orfs("user_core_ara", "hold", -139.94, -193.44)
    assert (s, h) == (-142.621, -120.73)
    log = open(a._block_log("user_core_ara")).read()
    assert "hold ECO ADOPTED" in log and "hold IMPROVED" in log
    assert (res / "6_final.odb").read_text() == "eco"


def test_run_210551_hold_eco_with_new_drc_still_discarded(tmp_path, monkeypatch):
    from test_post_route_eco import _eco_agent, _patch_popen, _patch_gds
    a, res = _eco_agent(tmp_path)
    _patch_popen(monkeypatch, a, res, R210551.replace("JANUS_PR drc 0", "JANUS_PR drc 3"))
    _patch_gds(monkeypatch, res)
    assert a._eco_pass_orfs("user_core_ara", "hold", -139.94, -193.44) == (-139.94, -193.44)
    assert "left 3 DRC" in open(a._block_log("user_core_ara")).read()


# ------------------------------------------------------------------ §229 --
GRANT = "invalid_grant: Token has been expired or revoked."


@pytest.fixture
def user_creds(tmp_path, monkeypatch):
    pytest.importorskip("google.oauth2.credentials")
    pytest.importorskip("googleapiclient")
    for k in ("JANUS_GDRIVE_ROOT", "JANUS_GDRIVE_ROOT_ID"):
        monkeypatch.delenv(k, raising=False)
    c = tmp_path / "tok.json"
    c.write_text(json.dumps({"type": "authorized_user", "client_id": "cid",
                             "client_secret": "sec", "refresh_token": "rt",
                             "token_uri": "https://oauth2.googleapis.com/token"}))
    return str(c)


def _raise(msg):
    def f(creds):
        raise RuntimeError(msg)
    return f


def test_preflight_revoked_token_gets_publish_hint(user_creds):
    m = nt.NotificationManager(gdrive_enabled=True, gdrive_creds=user_creds)
    msg = m.gdrive_preflight(refresh=_raise(GRANT))
    assert msg.startswith("token refresh FAILED") and "invalid_grant" in msg
    assert "In production" in msg and "7 days" in msg and "gdrive_auth.py" in msg
    assert m.gdrive_preflight_msg == msg
    note = m.drive_note("/r/runs/20261109_210551/b.tar.gz", "janus_soc_top",
                        "2026-11-09_21-05-51")
    assert note.startswith("Google Drive preflight FAILED at launch")
    assert ("gdrive_upload.py --block janus_soc_top --subfolder 2026-11-09_21-05-51 "
            "/r/runs/20261109_210551/b.tar.gz") in note


def test_preflight_ok_with_pinned_root(user_creds, monkeypatch):
    monkeypatch.setenv("JANUS_GDRIVE_ROOT_ID", "PINNED")
    m = nt.NotificationManager(gdrive_enabled=True, gdrive_creds=user_creds)
    seen = []
    assert m.gdrive_preflight(refresh=seen.append) == ""
    assert len(seen) == 1 and m.gdrive_identity == "OAuth user token"


def test_preflight_cheap_failures_and_off(tmp_path):
    assert nt.NotificationManager().gdrive_preflight().startswith("off")
    m = nt.NotificationManager(gdrive_enabled=True, gdrive_creds=str(tmp_path / "no"))
    assert "not found" in m.gdrive_preflight()


def test_upload_failure_suffix_mentions_publish_and_cli():
    m = nt.NotificationManager(gdrive_enabled=True)
    m.last_drive_error = f"('{GRANT}', ...)"
    s = m._drive_err_suffix()
    assert "In production" in s and "gdrive_upload.py" in s


class _NM:
    def __init__(self, pre="", ids=("id1",)):
        self.pre, self.ids, self.calls = pre, list(ids), []
        self.gdrive_identity, self.last_drive_links = "OAuth user token", ["https://d/1"]
        self.last_drive_outcome = "uploaded 1 file(s)"

    def gdrive_preflight(self):
        return self.pre

    def gdrive_target_desc(self, b):
        return "janus_ec2_runs/" + b

    def upload_to_drive(self, files, block, subfolder=None):
        self.calls.append((files, block, subfolder))
        return self.ids


def test_cli_derives_run_subfolder_and_uploads(tmp_path, capsys):
    b = tmp_path / "runs" / "20261109_210551" / "bundles" / "janus_soc_top"
    b.mkdir(parents=True)
    f = b / "janus_soc_top_20261110_164458.tar.gz"
    f.write_bytes(b"x")
    nm = _NM()
    assert gu.main(["--block", "janus_soc_top", str(f)], nm=nm) == 0
    assert nm.calls == [([str(f)], "janus_soc_top", "2026-11-09_21-05-51")]
    assert gu.main(["--block", "x", "--subfolder", "S", str(f)], nm=nm) == 0
    assert nm.calls[-1][2] == "S"
    assert gu.main(["--block", "x", "--no-subfolder", str(f)], nm=nm) == 0
    assert nm.calls[-1][2] is None
    assert gu.run_stamp("/a/b.tar.gz") is None


def test_cli_check_and_failures(tmp_path):
    assert gu.main(["--check"], nm=_NM()) == 0
    assert gu.main(["--check"], nm=_NM(pre="token refresh FAILED: x")) == 1
    assert gu.main([], nm=_NM()) == 2                          # no block/files
    assert gu.main(["--block", "b", str(tmp_path / "nope")], nm=_NM()) == 2
    f = tmp_path / "a.tar.gz"
    f.write_bytes(b"x")
    assert gu.main(["--block", "b", str(f)], nm=_NM(ids=())) == 1


def _stub_agent(tmp_path, pre, enabled=True):
    sent, logs = [], []
    nm = types.SimpleNamespace(
        enabled=enabled, gdrive_preflight=lambda: pre, gdrive_problem=lambda: "cheap",
        send_email=lambda s, b: sent.append((s, b)) or True)
    stub = types.SimpleNamespace(notifier=nm, _notify_log=logs.append)
    return stub, sent, logs


def test_agent_startup_preflight_emails_immediately(tmp_path, monkeypatch):
    monkeypatch.delenv("JANUS_GDRIVE_PREFLIGHT", raising=False)
    stub, sent, logs = _stub_agent(tmp_path, "token refresh FAILED: invalid_grant")
    assert A._gdrive_preflight_startup(stub, "") == "token refresh FAILED: invalid_grant"
    assert len(sent) == 1 and "preflight FAILED" in sent[0][0]
    assert "gdrive_upload.py" in sent[0][1]
    # OK token -> no mail; email misconfigured -> no mail attempt
    stub, sent, _ = _stub_agent(tmp_path, "")
    assert A._gdrive_preflight_startup(stub, "") == "" and not sent
    stub, sent, _ = _stub_agent(tmp_path, "bad")
    assert A._gdrive_preflight_startup(stub, "SMTP_HOST unset") == "bad" and not sent
    monkeypatch.setenv("JANUS_GDRIVE_PREFLIGHT", "0")
    stub, sent, _ = _stub_agent(tmp_path, "bad")
    assert A._gdrive_preflight_startup(stub, "") == "cheap" and not sent


def test_agent_startup_uses_preflight():
    src = (HERE.parent / "janus_agent.py").read_text()
    blk = src[src.index("# §138: Drive channel state"):][:400]
    assert "self._gdrive_preflight_startup(_np)" in blk
