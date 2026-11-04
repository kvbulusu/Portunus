"""§202: abstract written one openroad session per corner + --abstract-only."""
import os
import subprocess
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import janus_agent as ja  # noqa: E402
from test_post_route_eco import _eco_agent  # noqa: E402

MC = ["make", "DESIGN_CONFIG=x", "CORNER=WC", "CORNERS=slow fast",
      "SLOW_LIB_FILES=a", "FAST_LIB_FILES=b", "-o", "r/6_final.odb", "generate_abstract"]


def test_corner_passes(monkeypatch):
    monkeypatch.delenv("JANUS_ETM_PER_CORNER", raising=False)
    A = ja.JANUSAutonomousAgent
    assert A._abstract_corner_passes(MC) == ["slow", "fast"]
    assert A._abstract_corner_passes(["make", "CORNER=WC", "t"]) == [None]
    assert A._abstract_corner_passes(["make", "CORNERS=fast", "t"]) == [None]
    monkeypatch.setenv("JANUS_ETM_PER_CORNER", "0")
    assert A._abstract_corner_passes(MC) == [None]


def test_with_corners_replaces_only_corners():
    out = ja.JANUSAutonomousAgent._with_corners(MC, "fast")
    assert out.count("CORNERS=fast") == 1 and "CORNERS=slow fast" not in out
    assert [t for t in out if not t.startswith("CORNERS=")] == \
           [t for t in MC if not t.startswith("CORNERS=")]
    assert ja.JANUSAutonomousAgent._with_corners(MC, None) == MC


def _abs_agent(tmp_path, monkeypatch, rcs):
    a, res = _eco_agent(tmp_path)
    a.args.no_abstract = False
    a._detect_abstract_target = lambda: "generate_abstract"
    a._post_build_make_cmd = lambda b, t: MC[:-1] + list(t)
    a._block_log = lambda b: str(tmp_path / "blk.log")
    a._check_abstract_corners = lambda b: True
    a._find_abstract = lambda b, ext: None
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, rcs[len(calls) - 1])
    monkeypatch.setattr(ja.subprocess, "run", fake_run)
    monkeypatch.delenv("JANUS_ETM_PER_CORNER", raising=False)
    return a, calls


def test_run_abstract_one_session_per_corner(tmp_path, monkeypatch):
    a, calls = _abs_agent(tmp_path, monkeypatch, [0, 0])
    a._run_abstract("user_core_ara")
    assert len(calls) == 2
    assert "CORNERS=slow" in calls[0] and "CORNERS=fast" in calls[1]
    assert all(c[-1] == "generate_abstract" and "SLOW_LIB_FILES=a" in c for c in calls)
    assert "+abstract" in a.results["user_core_ara"].note


def test_run_abstract_stops_on_failed_corner(tmp_path, monkeypatch):
    a, calls = _abs_agent(tmp_path, monkeypatch, [2, 0])
    a._run_abstract("user_core_ara")
    assert len(calls) == 1 and "+abstract" not in a.results["user_core_ara"].note


def test_abstract_only_children_first_and_rc(tmp_path, monkeypatch):
    a, _ = _eco_agent(tmp_path)
    a.args.abstract_only, a.args.dry_run = True, False
    a.args.blocks = ["user_domain", "user_core", "user_core_cva6", "user_core_ara"]
    a._find_routed_odb = lambda b: "x.odb"
    seen = []

    def fake_abs(b):
        seen.append(b)
        a.results[b].note += "  +abstract" + ("  ETM-FAST-SUSPECT" if b == "user_core" else "")
    a._run_abstract = fake_abs
    assert a.run() == 5
    assert seen.index("user_core_ara") < seen.index("user_core") < seen.index("user_domain")
    assert seen.index("user_core_cva6") < seen.index("user_core")
    assert "security" not in seen and len(seen) == 4
    a._run_abstract = lambda b: seen.append(b)
    assert a._abstract_only() == 1
    a.args.blocks = []
    assert a._abstract_only() == 2


def test_cli_flag_and_exclusive():
    ns = ja.build_parser().parse_args(["--abstract-only", "--blocks", "user_core"])
    assert ns.abstract_only
    assert "abstract_only" in ja._EXCLUSIVE_MODES
