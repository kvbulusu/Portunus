"""§201: ETM corner sanity check (fast lib carrying slow clk->out arrivals)."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))
DBG = os.path.join(REPO, "scripts", "debug")
sys.path.insert(0, DBG)
import etm_corner_check as ecc  # noqa: E402


def _lib(volt, temp, outs):
    pins = ""
    for name, v in outs.items():
        pins += f'''
    pin ({name}) {{
      direction : output;
      timing() {{
        related_pin : "clk";
        timing_type : rising_edge;
        cell_rise(template_1) {{
          index_1("1.44, 2.88");
          values("{v:.3f}, {v + 10:.3f}");
        }}
        cell_fall(template_1) {{
          index_1("1.44, 2.88");
          values("{v + 5:.3f}, {v + 15:.3f}");
        }}
      }}
    }}
    pin (din) {{
      direction : input;
      timing() {{
        related_pin : "clk";
        timing_type : setup_rising;
        rise_constraint(scalar) {{ values("-70.0"); }}
      }}
    }}'''
    return f'''library (blk) {{
  nom_process : 1.0;
  nom_temperature : {temp};
  nom_voltage : {volt};
  cell (blk) {{{pins}
  }}
}}
'''


SLOW = _lib(0.63, 100.0, {"q": 1000.0, "busy_o": 2612.8})
FAST_OK = _lib(0.77, 0.0, {"q": 500.0, "busy_o": 1300.0})
FAST_BAD = _lib(0.63, 100.0, {"q": 999.0, "busy_o": 2611.2})   # EC2 signature


def test_clk_to_out_parses_zero_load_min():
    d = ecc.clk_to_out(SLOW)
    assert d == {"q": 1000.0, "busy_o": 2612.8}


def test_healthy_fast_lib_ok():
    ok, msgs = ecc.check(SLOW, FAST_OK)
    assert ok, msgs


def test_ec2_signature_flagged():
    ok, msgs = ecc.check(SLOW, FAST_BAD)
    assert not ok
    joined = " ".join(msgs)
    assert "nom_voltage=0.63" in joined and "corner-INVARIANT" in joined


def test_header_only_or_invariant_only_each_flag():
    assert not ecc.check(SLOW, _lib(0.77, 0.0, {"q": 999.0, "busy_o": 2611.0}))[0]
    assert not ecc.check(SLOW, _lib(0.63, 100.0, {"q": 500.0, "busy_o": 1300.0}))[0]


def test_cli_exit_codes(tmp_path):
    s, g, b = tmp_path / "s.lib", tmp_path / "g.lib", tmp_path / "b.lib"
    s.write_text(SLOW); g.write_text(FAST_OK); b.write_text(FAST_BAD)
    cli = os.path.join(DBG, "etm_corner_check.py")
    assert subprocess.run([sys.executable, cli, str(s), str(g)]).returncode == 0
    r = subprocess.run([sys.executable, cli, str(s), str(b)], capture_output=True, text=True)
    assert r.returncode == 1 and "VERDICT SUSPECT" in r.stdout
    assert subprocess.run([sys.executable, cli, str(s), str(tmp_path / "x")]).returncode == 2


def test_selftest_script_variants():
    sh = open(os.path.join(DBG, "etm_corner_selftest.sh")).read()
    for frag in ('mk A "slow fast" "slow fast"', 'mk B "fast slow" "slow fast"',
                 'mk C "fast"      "fast"', "write_timing_model -scene",
                 "set_propagated_clock", "set_clock_latency -source 0",
                 "etm_corner_check.py"):
        assert frag in sh, frag


def test_agent_hooks_check_after_abstract():
    src = open(os.path.join(REPO, "agent", "janus_agent.py")).read()
    assert "def _check_abstract_corners" in src
    i = src.index("def _run_abstract")
    j = src.index("def _abstract_base_dirs")
    assert "self._check_abstract_corners(block)" in src[i:j]
    assert "ETM-FAST-SUSPECT" in src
