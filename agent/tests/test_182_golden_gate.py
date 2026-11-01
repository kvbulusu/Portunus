"""§182: golden gate defaults to the 80 ps setup uncertainty; promote script."""
import os
import subprocess
import sys
from types import SimpleNamespace

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import janus_agent as ja  # noqa: E402

SCRIPT = os.path.join(HERE, "..", "..", "scripts", "golden_promote.sh")


def _gate(setup, hold, drc=0, **args):
    fake = SimpleNamespace(args=SimpleNamespace(**args))
    res = SimpleNamespace(wns_ns=setup, hold_ps=hold, drc_violations=drc)
    return ja.JANUSAutonomousAgent._golden_converged(fake, res)[0]


def test_cli_default_margin_is_80():
    p = ja.build_parser() if hasattr(ja, "build_parser") else None
    if p is None:
        return
    a = p.parse_args(["--blocks", "noc"])
    assert a.golden_wns_margin_ps == 80.0
    assert a.golden_hold_margin_ps == 0.0


def test_gate_accepts_within_uncertainty():
    assert _gate(-13.18, 10.3)             # user_core freeze run
    assert _gate(-79.9, 0.0)
    assert not _gate(-241.9, 16.9)         # user_domain 211854
    assert not _gate(-10.0, -1.0)          # hold must close
    assert not _gate(-10.0, 5.0, drc=500)
    assert not _gate(-10.0, 5.0, golden_wns_margin_ps=0.0)  # strict still possible


def test_promote_and_trim(tmp_path):
    flow = tmp_path / "flow"
    res = flow / "results/asap7/blk/base"
    res.mkdir(parents=True)
    (flow / "reports/asap7/blk/base").mkdir(parents=True)
    for n in ("1_synth.odb", "5_route.odb", "janus_06_cts.def", "6_final.odb",
              "6_final.sdc", "6_final.gds", "6_final.odb.preeco", "blk.lef",
              "blk_slow.lib"):
        (res / n).write_text("x")
    env = dict(os.environ, ORFS_DIR=str(tmp_path), GOLDEN_DIR=str(tmp_path / "g"),
               TRIM="1")
    r = subprocess.run(["bash", SCRIPT, "blk"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    kept = sorted(os.listdir(res))
    assert kept == ["6_final.gds", "6_final.odb", "6_final.sdc", "blk.lef", "blk_slow.lib"]
    gfinal = sorted(os.listdir(tmp_path / "g/asap7/blk/final"))
    assert gfinal == kept


def test_promote_refuses_trim_of_active_block(tmp_path):
    res = tmp_path / "flow/results/asap7/user_domain/base"
    res.mkdir(parents=True)
    for n in ("4_cts.odb", "6_final.odb"):
        (res / n).write_text("x")
    env = dict(os.environ, ORFS_DIR=str(tmp_path), GOLDEN_DIR=str(tmp_path / "g"),
               TRIM="1")
    r = subprocess.run(["bash", SCRIPT, "user_domain"], env=env, capture_output=True,
                       text=True)
    assert r.returncode == 0 and "SKIP trim" in r.stdout
    assert (res / "4_cts.odb").exists()
