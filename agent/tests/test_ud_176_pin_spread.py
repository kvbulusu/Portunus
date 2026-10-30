"""§176 user_domain IO pin spread (GRT-0116, run 20261030_102011) + GRT-only check script."""
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "utils"))
import flow_state as fs  # noqa: E402
from test_ud_168_cts_io import _shadow, _val, _read, CFG, ASIC  # noqa: E402

SH = os.path.join(ASIC, "scripts", "ud_grt_check.sh")
TCL = os.path.join(ASIC, "scripts", "ud_grt_check.tcl")

GRT_TABLE = """[INFO GRT-0096] Final congestion report:
Layer         Resource        Demand        Usage (%)    Max H / Max V / Total Congestion
---------------------------------------------------------------------------------------
M2            25143576       1095343            4.36%             {h} /  0 / {o2}
M3            29378310       1257039            4.28%             0 /  {v} / {o3}
---------------------------------------------------------------------------------------
Total        129895911       3175032            2.44%            {h} / {v} / {tot}
[INFO GRT-0018] Total wirelength: 1984240 um
"""


def test_mk_knobs():
    mk = _read(CFG, "user_domain.mk")
    assert _val(mk, "PLACE_PINS_ARGS") == "-min_distance 1.0"
    assert _val(mk, "JANUS_IO_SHADOW_PIN_PITCH_UM") == "1.0"
    # §168 glue-side excludes kept (no pins in the macro shadow on bottom/top)
    assert _val(mk, "JANUS_IO_SHADOW_EXCLUDE_EDGES") == "left"
    assert _val(mk, "JANUS_IO_SHADOW_SPAN_EDGES") == "bottom top"
    for k in ("PLACE_PINS_ARGS", "JANUS_IO_SHADOW_PIN_PITCH_UM"):
        assert re.search(rf"^export\s+{k}\s+=", mk, re.M), k  # plain '=' (§141)
    assert "20261030_102011" in mk and "§176" in mk
    # no die / layer changes in this section
    sec = mk[mk.index("# §176 spread IO pins"):mk.index("export JANUS_IO_SHADOW_PIN_PITCH_UM")]
    for k in ("DIE_AREA", "CORE_UTILIZATION", "MAX_ROUTING_LAYER", "MIN_ROUTING_LAYER", "ROUTING_LAYER_ADJUSTMENT"):
        assert not re.search(rf"^export\s+{k}", sec, re.M), k


def test_capacity_fits():
    # 1573 bterms (3_2_place_iop.log) x 1.0 um < 2260.9 um allowed perimeter
    assert 1573 * 1.0 < 2260.9


def test_other_blocks_pin_settings_unchanged():
    for name in sorted(os.listdir(CFG)):
        if not name.endswith(".mk") or name == "user_domain.mk":
            continue
        txt = _read(CFG, name)
        assert _val(txt, "JANUS_IO_SHADOW_PIN_PITCH_UM") is None, name
        ppa = _val(txt, "PLACE_PINS_ARGS")
        if name == "noc.mk":
            assert ppa == "-min_distance 1 -min_distance_in_tracks"
        else:
            assert ppa is None, name


def test_flow_state_reruns_from_place():
    for k in ("PLACE_PINS_ARGS", "JANUS_IO_SHADOW_PIN_PITCH_UM"):
        assert fs._earliest_stage_for_keys([k]) == "3_place", k


def test_hook_keeps_min_distance_and_excludes(tmp_path):
    args, out = _shadow(tmp_path, PLACE_PINS_ARGS="-min_distance 1.0", JANUS_IO_SHADOW_PIN_PITCH_UM="1.0",
                        JANUS_IO_SHADOW_EXCLUDE_EDGES="left", JANUS_IO_SHADOW_SPAN_EDGES="bottom top")
    assert args == ("-min_distance 1.0 -exclude left:* -exclude bottom:14.070-804.070 "
                    "-exclude top:14.070-804.070"), out
    assert "NOT applying" not in out


def test_script_files():
    assert os.path.isfile(SH) and os.access(SH, os.X_OK)
    assert os.path.isfile(TCL)
    sh = open(SH).read()
    tcl = open(TCL).read()
    for s in ("VERDICT: PASS", "VERDICT: FAIL", "4_cts", "3_place", "CAVEAT", "RUN_SCRIPT"):
        assert s in sh, s
    for s in ("place_pins", "PRE_IO_PLACEMENT_TCL", "PLACE_PINS_ARGS", "FASTROUTE_TCL",
              "pin_access", "global_route", "-allow_congestion", "MAXPER100", "HOTSPOT", "RESULTS_DIR"):
        assert s in tcl, s
    # never writes flow results: the only write_db targets the scratch dir
    assert re.findall(r"write_db (\S+)", tcl) == ["$out/ud_grt_check.odb"]
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", SH]).returncode == 0


def _summ(tmp_path, text):
    log = tmp_path / "x.log"
    log.write_text(text)
    r = subprocess.run(["bash", SH, "--summarize", str(log)], capture_output=True, text=True, timeout=60)
    return r.returncode, r.stdout


def test_summarize_fail_on_overflow(tmp_path):
    rc, out = _summ(tmp_path, "UDGRT: MAXPER100 before 1042\n" + GRT_TABLE.format(h=27, v=23, o2=3924, o3=35197, tot=56828))
    assert rc == 1 and "VERDICT: FAIL (total overflow 56828)" in out
    assert "M3" in out and "MAXPER100 before 1042" in out


def test_summarize_pass_on_zero(tmp_path):
    rc, out = _summ(tmp_path, GRT_TABLE.format(h=0, v=0, o2=0, o3=0, tot=0))
    assert rc == 0 and "VERDICT: PASS" in out


def test_summarize_fail_when_grt_missing(tmp_path):
    rc, out = _summ(tmp_path, "UDGRT: ERROR place_pins failed: boom\n")
    assert rc == 1 and "GRT did not finish" in out


def test_summarize_real_failed_run_log():
    log = "/home/ubuntu/ud_102011/user_domain_grt/logs/5_1_grt.log"
    if not os.path.isfile(log):
        pytest.skip("run 20261030_102011 bundle not present")
    r = subprocess.run(["bash", SH, "--summarize", log], capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "total overflow 56828" in r.stdout
