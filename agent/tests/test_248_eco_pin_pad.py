"""§248 --eco-hold-only strategy "pin": pad only the RCX hold violators'
input pins (janus_hold_pin_pad), no repair_timing."""
import os
import re
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import janus_agent as ja  # noqa: E402
from test_183_port_slew_eco import _closed_agent, BLK  # noqa: E402
from test_241_eco_hold_pad import ORFS, OR_BIN, GCD, HOOK, PRO, needs_or  # noqa: E402

ASIC = os.path.abspath(os.path.join(HERE, "..", ".."))
MK = os.path.join(ASIC, "configs", "janus_soc_top.mk")
_KNOBS = {"JANUS_ECO_HOLD_ONLY_STRATEGY": "pin", "JANUS_ECO_HOLD_PIN_TARGET_PS": "30",
          "JANUS_ECO_HOLD_PIN_SELECT_PS": "0", "JANUS_ECO_HOLD_PIN_MAX_PINS": "16",
          "JANUS_ECO_HOLD_PIN_MAX_STAGES": "12"}


def test_post_route_opt_pin_branch_skips_repair_timing():
    s = open(PRO).read()
    assert "set strategy [_env JANUS_PR_HOLD_STRATEGY repair]" in s
    i = s.index('if {$mode eq "hold" && $strategy eq "pin"} {')
    j = s.index("} elseif {$mode in {hold timing} || $hold_after_setup} {")
    pin_body = s[i:j]
    assert "janus_hold_pin_pad" in pin_body and "repair_timing" not in pin_body
    assert "exit 1" in pin_body                      # 0 buffers -> no reroute
    assert "janus_hold_pin_pad_post" in s[j:]
    assert s.count("drv slew [_slew_viol]") >= 3     # drv mode + PRE/post here


def test_soc_top_knobs_and_no_restamp():
    mk = open(MK).read()
    for k, v in _KNOBS.items():
        assert re.search(rf"^export\s+{k}\s*=\s*{v}\s*$", mk, re.M), k
    sys.path.insert(0, os.path.join(ASIC, "utils"))
    import flow_state
    assert flow_state._changed_keys({k: "x" for k in _KNOBS}, dict(_KNOBS)) == []


def test_strategy_knob_parsing(tmp_path):
    a, _ = _closed_agent(tmp_path)
    for raw, want in ((None, "repair"), ("pin", "pin"), (" PIN ", "pin"),
                      ("repair", "repair"), ("bogus", "repair")):
        a._janus_knob = lambda b, n, raw=raw: raw if n == "JANUS_ECO_HOLD_ONLY_STRATEGY" else None
        assert a._eco_hold_only_strategy(BLK) == want
    a._janus_knob = lambda b, n: {"JANUS_ECO_HOLD_PIN_TARGET_PS": "25"}.get(n)
    assert a._eco_pin_target(BLK) == 25.0
    a._janus_knob = lambda b, n: None
    assert a._eco_pin_target(BLK) == 30.0


@pytest.mark.parametrize("strategy,extra", [("pin", ["JANUS_PR_HOLD_STRATEGY=pin"]),
                                            ("repair", None)])
def test_eco_pass_passes_strategy_to_make(tmp_path, strategy, extra):
    a, _ = _closed_agent(tmp_path)
    seen = {}

    def _fake(b, mode, **kw):
        seen.update(kw)
        r = ja.ECOResult(mode=mode)
        r.error = "stub"
        return r, {}
    a._run_eco_orfs = _fake
    a._eco_pass_orfs(BLK, "hold", -0.068, -7.3, strategy=strategy)
    assert seen.get("extra") == extra
    assert a._eco_strategy == strategy


def test_eco_hold_only_uses_block_strategy(tmp_path):
    a, res = _closed_agent(tmp_path)
    a.args.eco_hold_only, a.args.blocks = True, [BLK]
    a._find_routed_odb = lambda b: str(res / "6_final.odb")
    a._classify_timing = lambda b: None
    a._eco_hold_only_strategy = lambda b: "pin"
    got = []
    a._eco_pass_orfs = lambda b, m, s, h, strategy="repair": got.append(strategy) or (s, h)
    a._notify_settled = lambda b, r: None
    assert a._eco_hold_only() == 5
    assert got == ["pin"]


def _run_pin(tmp_path, env_extra):
    tcl = tmp_path / "p.tcl"
    tcl.write_text(f"""
foreach l [glob {ORFS}/flow/platforms/asap7/lib/NLDM/*RVT_FF_nldm*] {{ catch {{read_liberty $l}} }}
read_db {GCD}/4_cts.odb
read_sdc {GCD}/4_cts.sdc
source {ORFS}/flow/platforms/asap7/setRC.tcl
set_propagated_clock [all_clocks]
estimate_parasitics -placement
source {HOOK}
puts "PRE [sta::format_time [sta::worst_slack_cmd min] 3]"
puts "N [janus_hold_pin_pad]"
puts "POST [sta::format_time [sta::worst_slack_cmd min] 3]"
""")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = (f"{ORFS}/dependencies/lib:{ORFS}/dependencies/or-tools/lib:"
                              + env.get("LD_LIBRARY_PATH", ""))
    env.update(env_extra)
    p = subprocess.run([OR_BIN, "-no_init", "-exit", str(tcl)], capture_output=True,
                       text=True, timeout=150, env=env, cwd=tmp_path)
    return p.stdout + p.stderr


def _val(out, tag):
    m = re.search(rf"^{tag} (-?[\d.]+)", out, re.M)
    assert m, out[-3000:]
    return float(m.group(1))


@needs_or
def test_pin_pad_reaches_prediction_without_guard(tmp_path):
    out = _run_pin(tmp_path, {"JANUS_ECO_HOLD_PIN_SELECT_PS": "60",
                              "JANUS_ECO_HOLD_PIN_TARGET_PS": "80",
                              "JANUS_ECO_HOLD_PIN_MAX_PINS": "4",
                              "JANUS_ECO_HOLD_PAD_SETUP_GUARD_PS": "-1000"})
    assert _val(out, "N") > 0, out[-3000:]
    assert _val(out, "POST") > _val(out, "PRE")
    pins = re.findall(r"JANUS_PR pin_pad pin \S+ rcx_hold (\S+) gain (\S+) -> pred (\S+)", out)
    assert pins, out[-3000:]
    for h0, g, pred in pins:
        assert float(g) > 0 and float(pred) > float(h0)
    assert "ERROR" not in out


@needs_or
def test_pin_pad_default_guard_refuses_setup_tight_pins(tmp_path):
    out = _run_pin(tmp_path, {"JANUS_ECO_HOLD_PIN_SELECT_PS": "60",
                              "JANUS_ECO_HOLD_PIN_TARGET_PS": "80",
                              "JANUS_ECO_HOLD_PIN_MAX_PINS": "4"})
    assert _val(out, "N") == 0
    assert "stop setup_guard" in out
