"""§241 post-route ECO macro-input hold pre-pad (janus_eco_hold_pad.tcl)."""
import os
import re
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ASIC = os.path.abspath(os.path.join(HERE, "..", ".."))
HOOK = os.path.join(ASIC, "scripts", "hooks", "janus_eco_hold_pad.tcl")
PRO = os.path.join(ASIC, "scripts", "post_route_opt.tcl")
ORFS = os.environ.get("ORFS_DIR", "/home/ubuntu/OpenROAD-flow-scripts")
OR_BIN = os.path.join(ORFS, "tools", "OpenROAD", "build", "bin", "openroad")
GCD = os.path.join(ORFS, "flow", "results", "asap7", "gcd", "base")


def test_wired_into_post_route_opt_before_repair_hold():
    s = open(PRO).read()
    assert "hooks janus_eco_hold_pad.tcl" in s
    i_pad = s.index("janus_hold_pad $hm")
    i_rh = s.index("repair_timing -hold -hold_margin $hm")
    assert i_pad < i_rh
    # only for hold/timing, never the setup-mode hold guard
    assert re.search(r"if \{\$mode in \{hold timing\}\} \{\s*\n\s*if \{\[catch \{janus_hold_pad", s)


def test_knobs_are_post_flow_no_restamp():
    import sys
    sys.path.insert(0, os.path.join(ASIC, "utils"))
    import flow_state
    assert flow_state._changed_keys({"JANUS_ECO_HOLD_PAD": "0"},
                                    {"JANUS_ECO_HOLD_PAD": "1"}) == []


def _run(tmp_path, env_extra, target=80):
    tcl = tmp_path / "t.tcl"
    tcl.write_text(f"""
foreach l [glob {ORFS}/flow/platforms/asap7/lib/NLDM/*RVT_FF_nldm*] {{ catch {{read_liberty $l}} }}
read_db {GCD}/4_cts.odb
read_sdc {GCD}/4_cts.sdc
source {ORFS}/flow/platforms/asap7/setRC.tcl
set_propagated_clock [all_clocks]
estimate_parasitics -placement
source {HOOK}
puts "PRE [sta::format_time [sta::worst_slack_cmd min] 3]"
puts "N [janus_hold_pad {target}]"
puts "POST [sta::format_time [sta::worst_slack_cmd min] 3]"
puts "SETUP [sta::format_time [sta::worst_slack_cmd max] 3]"
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


needs_or = pytest.mark.skipif(not (os.path.exists(OR_BIN) and os.path.exists(GCD + "/4_cts.odb")),
                              reason="sandbox OpenROAD/gcd not available")


@needs_or
def test_disabled_and_macro_scope_are_noops(tmp_path):
    out = _run(tmp_path, {"JANUS_ECO_HOLD_PAD": "0"})
    assert _val(out, "N") == 0
    out = _run(tmp_path, {"JANUS_ECO_HOLD_PAD": "1"})          # gcd has no macros
    assert _val(out, "N") == 0 and "pins 0" in out


@needs_or
@pytest.mark.parametrize("force_odb", ["0", "1"])
def test_pads_raise_hold_and_respect_setup_guard(tmp_path, force_odb):
    out = _run(tmp_path, {"JANUS_ECO_HOLD_PAD": "1", "JANUS_ECO_HOLD_PAD_SCOPE": "all",
                          "JANUS_ECO_HOLD_PAD_MAX_PINS": "4",
                          "JANUS_ECO_HOLD_PAD_SETUP_GUARD_PS": "-1000",
                          "JANUS_ECO_HOLD_PAD_FORCE_ODB": force_odb})
    n = _val(out, "N")
    assert n > 0, out[-3000:]
    assert _val(out, "POST") > _val(out, "PRE")
    for m in re.finditer(r"pin (\S+) hold (\S+) -> (\S+) setup (\S+) -> (\S+) bufs (\d+) stop (\S+)", out):
        h0, h1, s1 = float(m.group(2)), float(m.group(3)), float(m.group(5))
        assert h1 > h0
        assert s1 >= -1000 - 1e-6
    assert "ERROR" not in out


@needs_or
def test_setup_guard_blocks_padding_on_setup_tight_pins(tmp_path):
    # gcd hold endpoints have only 5..13 ps setup slack: default 50 ps guard refuses.
    out = _run(tmp_path, {"JANUS_ECO_HOLD_PAD": "1", "JANUS_ECO_HOLD_PAD_SCOPE": "all",
                          "JANUS_ECO_HOLD_PAD_MAX_PINS": "4"})
    assert _val(out, "N") == 0
    assert out.count("stop setup_guard") == 4
    assert _val(out, "POST") == _val(out, "PRE")


NHB = os.path.join(ASIC, "scripts", "hooks", "janus_cts_noc_hold_bal.tcl")


@needs_or
def test_noc_hold_bal_in_hold_metric_ignores_through_paths(tmp_path):
    """§241: in-hold = checks ENDING at the pins, not paths running through them."""
    tcl = tmp_path / "n.tcl"
    tcl.write_text(f"""
foreach l [glob {ORFS}/flow/platforms/asap7/lib/NLDM/*RVT_FF_nldm*] {{ catch {{read_liberty $l}} }}
read_db {GCD}/4_cts.odb
read_sdc {GCD}/4_cts.sdc
source {ORFS}/flow/platforms/asap7/setRC.tcl
set_propagated_clock [all_clocks]
estimate_parasitics -placement
source {NHB}
set ff [get_pins {{u124/D u79/D}}]
puts "FF [janus_nhb_worst_ep $ff min] [janus_nhb_worst $ff min]"
set pe [lindex [find_timing_paths -path_delay min -to [get_pins u124/D]] 0]
set thr ""
foreach pt [get_property $pe points] {{
  set p [get_property $pt pin]
  set n [get_full_name $p]
  if {{[get_property $p direction] eq "input" && ![string match */D $n] && ![string match */CLK $n]}} {{ set thr $p }}
}}
puts "THR <[janus_nhb_worst_ep $thr min]> [janus_nhb_worst $thr min]"
""")
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{ORFS}/dependencies/lib:{ORFS}/dependencies/or-tools/lib"
    out = subprocess.run([OR_BIN, "-no_init", "-exit", str(tcl)], capture_output=True,
                         text=True, timeout=150, env=env, cwd=tmp_path).stdout
    ff = re.search(r"^FF (\S+) (\S+)", out, re.M)
    assert ff and float(ff.group(1)) == pytest.approx(float(ff.group(2)), abs=0.01), out[-2000:]
    th = re.search(r"^THR <(\S*)> (\S+)", out, re.M)
    assert th and th.group(1) == "" and float(th.group(2)) < 1e6, out[-2000:]


def test_noc_hold_bal_qor_uses_endpoint_metric_with_fallback():
    s = open(NHB).read()
    body = s[s.index("proc janus_nhb_qor"):s.index("proc janus_nhb_fq")]
    assert "janus_nhb_worst_ep $ins min" in body
    assert 'if { $hin eq "" } { set hin [janus_nhb_worst $ins min] }' in body
    assert "-to $pins" in s
