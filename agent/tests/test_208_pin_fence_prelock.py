"""§208 (run 20261104_172222): 3_3 global place died with GPL-0307 at iter 190.
The §206 fence was a GPL region (ucut_mn_grp): it reached overflow ~0.15 by
iter ~100 while the top-level region was still ~0.73, and the shared HPWL
rising during normal spreading tripped the per-region divergence check
(overflow < 0.2 and HPWL growing). Fix: JANUS_PIN_FENCE_MODE=prelock (default)
pre-places members at their macro pins (FIRM) during global_placement with NO
GPL region, then releases them and binds the region so resize/DPL keep them in
the strip. mode=region keeps the §206 behaviour.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PF = ROOT / "asic_flow/scripts/hooks/janus_pin_fence.tcl"
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
FIXD = Path(__file__).resolve().parent / "fixtures" / "t198d"
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")


def _run(tmp_path, mode, **extra):
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")
    d = tmp_path / f"t208_{mode}"
    shutil.copytree(FIXD, d)
    base = (d / "t.tcl").read_text().split("global_placement")[0]
    (d / "pf.tcl").write_text(base + f"""
source {PF}
set rx [$b findInst rx]
puts "T208 pre status [$rx getPlacementStatus] regions [llength [$b getRegions]]"
global_placement -skip_io -density 0.4
lassign [$rx getLocation] x y
puts [format "T208 gp rx %.1f %.1f status %s regions %d wrapped %d" [expr {{$x/1000.0}}] \\
      [expr {{$y/1000.0}}] [$rx getPlacementStatus] [llength [$b getRegions]] \\
      [llength [info commands ::janus_pf::gpl_orig]]]
detailed_placement
check_placement
lassign [$rx getLocation] x y
puts [format "T208 dp rx %.1f %.1f" [expr {{$x/1000.0}}] [expr {{$y/1000.0}}]]
""")
    env = dict(os.environ, T198_PLATFORM=PLAT, JANUS_PIN_FENCE="fx:u_m:rx",
               JANUS_PIN_FENCE_MODE=mode, **extra)
    r = subprocess.run([OR, "-no_init", "-exit", "pf.tcl"], cwd=d, env=env,
                       capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def _in_strip(out, tag):
    m = re.search(rf"T208 {tag} rx ([\d.]+) ([\d.]+)", out)
    assert m, out[-3000:]
    x, y = float(m.group(1)), float(m.group(2))
    # strip (190.0,222.1)-(230.0,262.1) (u_m W edge, see test_206)
    assert 190.0 <= x <= 230.0 and 222.0 <= y <= 262.1, (tag, x, y)


def test_prelock_no_gpl_region_member_stays_in_strip(tmp_path):
    # §209: post-GPL bind is opt-in (JANUS_PIN_FENCE_POST_BIND=all = §208)
    rc, out = _run(tmp_path, "prelock", JANUS_PIN_FENCE_POST_BIND="all")
    assert rc == 0, out[-3000:]
    assert "T208 pre status FIRM regions 0" in out, out[-3000:]
    assert "fx members 1 area" in out and "prelocked 1 (mode prelock)" in out
    assert "Initialize Region: fx_grp" not in out          # GPL never saw a region
    assert re.search(r"T208 gp rx .* status PLACED regions 1 wrapped 0", out), out[-3000:]
    assert "fx released 1 prelocked member(s)" in out
    assert "fx bound members 1/1 (readback 1)" in out
    _in_strip(out, "gp")
    _in_strip(out, "dp")


def test_region_mode_keeps_206_behaviour(tmp_path):
    rc, out = _run(tmp_path, "region")
    assert rc == 0, out[-3000:]
    assert "T208 pre status PLACED regions 1" in out or "T208 pre status NONE regions 1" in out \
        or re.search(r"T208 pre status \w+ regions 1", out), out[-3000:]
    assert "Initialize Region: fx_grp" in out
    assert "prelocked 0 (mode region)" in out
    _in_strip(out, "dp")


def test_mk_default_mode_prelock():
    m = re.search(r"^export JANUS_PIN_FENCE_MODE\s*=\s*(\S+)", MK, re.M)
    assert m and m.group(1) == "prelock"


def test_stale_stages_after_failure_not_reused_or_flagged(tmp_path):
    """Run 20261104_172222 failed at 3_place but mailed the old 4_cts as
    'reused' with 'RED 4_cts: 6191 hold buffers > 5000'."""
    import sys
    import time
    sys.path[:0] = [str(ROOT / "asic_flow/agent/tests"), str(ROOT / "asic_flow/utils")]
    import stage_report as sr
    from test_175_notify_emails import _mk
    logs, rpt, res = _mk(str(tmp_path))
    old = time.time() - 3600
    for d in (logs, rpt, res):
        for f in os.listdir(d):
            os.utime(os.path.join(d, f), (old, old))
    open(os.path.join(rpt, "4_cts_final.rpt"), "a").write("hold buffers 6191\n")
    run_start = time.time() - 60
    open(os.path.join(logs, "3_3_place_gp.log"), "w").write("[ERROR GPL-0307] x\n")
    rows = sr.build_rows(logs, rpt, res, failed="3_place", run_start=run_start)
    st = {r["key"]: r["status"] for r in rows}
    assert st["3_place"] == "FAILED"
    assert st["2_floorplan"] in ("REUSED", "DONE")
    assert all(st[k] == "PENDING" for k in sr.STAGE_KEYS[sr.STAGE_KEYS.index("3_place") + 1:]), st
    assert not [f for f in sr.red_flags(rows) if "4_cts" in f[1]], sr.red_flags(rows)
    # §210: even without a failure barrier, the last stage logged in THIS run
    # is a barrier (make reruns downstream) -> old 4_cts is never "reused"
    st2 = {r["key"]: r["status"] for r in sr.build_rows(logs, rpt, res, run_start=run_start)}
    assert st2["4_cts"] == "PENDING", st2
