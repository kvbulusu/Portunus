"""§199 (probe sap_isolate 2026-11-04 after §198f: S -1417.5 / H -1231.6).
kv (hold_in -1231.6 = top hold WNS, movable want 1291.9) and u_noc (hold_in
-939.8, want 971.2) rode the common push unchanged -> add both to
JANUS_CTS_MACRO_ALIGN_ISOLATE (each gets its own trim to its own target).
The hook logs ISO_CROSS slacks between isolated macros (excluded from a/b);
the probe adds per-corner CORNER lines (ETM arcs vs corner-scaling push chain).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HOOKS = ROOT / "asic_flow/scripts/hooks"
BAL = (HOOKS / "janus_cts_balance.tcl").read_text()
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()
PROBE = (ROOT / "asic_flow/scripts/debug/soc_cts_align_probe.tcl").read_text()
FIXD = Path(__file__).resolve().parent / "fixtures" / "t198d"
OR = os.environ.get("JANUS_TEST_OPENROAD", "/home/ubuntu/dk/bin/openroad")
PLAT = os.environ.get("JANUS_TEST_ASAP7",
                      "/home/ubuntu/dk/rootfs/OpenROAD-flow-scripts/flow/platforms/asap7")


def _hier():
    h = MK[MK.index("ifneq ($(JANUS_FLAT),1)"):]
    return h[:h.index("\nendif")]


def test_mk_isolates_user_kv_noc_not_ampo():
    m = re.search(r"^export JANUS_CTS_MACRO_ALIGN_ISOLATE\s*=\s*(.+?)\s*$", _hier(), re.M)
    assert m
    iso = m.group(1).split()
    assert iso[:3] == ["u_user/user_clk", "u_kv_secure_subsystem/clk", "u_noc/clk"]
    # §200 adds the ampo user_clk pin only (never its agent_clk pin)
    assert [g for g in iso if "ampo" in g] == ["u_periph/u_ampo/user_clk"]


def test_sdc_untouched_and_no_exceptions():
    sdc = (ROOT / "asic_flow/configs/constraints/janus_soc_top.sdc").read_text()
    assert "§199" not in sdc
    for kw in ("set_false_path", "set_multicycle_path", "set_max_delay", "set_disable_timing"):
        assert kw not in BAL


def test_iso_cross_logged_after_isolate():
    assert re.search(r"^proc janus_mal_iso_cross \{", BAL, re.M)
    iso = BAL[BAL.index("proc janus_cts_macro_isolate"):BAL.index("proc janus_cts_hold_abort")]
    assert iso.index("done (isolate)") < iso.index("janus_mal_iso_cross $excl")


def test_probe_per_corner_lines():
    assert "proc sap_corner_paths" in PROBE and '"CORNER %s %s slack' in PROBE
    assert "sap_env CORNERS" in PROBE
    for m in ("u_user", "u_kv_secure_subsystem", "u_noc"):
        assert m in PROBE[PROBE.index("# ---- 2c"):]
    assert "write_db" not in PROBE


def test_toy_multi_isolate(tmp_path):
    if not (os.access(OR, os.X_OK) and os.path.isdir(PLAT)):
        pytest.skip("openroad / asap7 platform not available")

    def run(name, extra):
        d = tmp_path / name
        shutil.copytree(FIXD, d)
        env = dict(os.environ, T198_PLATFORM=PLAT, T198_HOOK=str(HOOKS / "janus_cts_balance.tcl"),
                   JANUS_CTS_MACRO_ALIGN="1", JANUS_CTS_MACRO_ALIGN_CLOCKS="clk_user", **extra)
        r = subprocess.run([OR, "-no_init", "-exit", "t.tcl"], cwd=d, env=env, capture_output=True,
                           text=True, timeout=600)
        out = r.stdout + r.stderr
        m = re.search(r"T198D FINAL setup (-?[\d.]+) hold (-?[\d.]+)", out)
        assert m, out[-3000:]
        v = {k: float(x) for k, x in re.findall(r"(\S+ (?:setup|hold)) (-?[\d.]+)", out)}
        return float(m.group(1)), float(m.group(2)), v, out

    s1, h1, v1, _ = run("one", {"JANUS_CTS_MACRO_ALIGN_ISOLATE": "u_m/clk"})
    s2, h2, v2, out = run("two", {"JANUS_CTS_MACRO_ALIGN_ISOLATE": "u_m/clk u_m2/clk"})
    assert "2 macro(s) re-tapped" in out
    # u_m2 rode the push with hold -47.6 (like kv); isolated it gets its own trim
    assert v1["in hold"] < 0 and v2["in hold"] > v1["in hold"] + 50     # u_m2 in hold
    assert h2 >= 0 and h2 > h1
    assert v2["out setup"] >= v1["out setup"] - 5                        # u_m2 out setup
    assert v2["u_m->u_m setup"] == pytest.approx(v1["u_m->u_m setup"], abs=5)
    assert v2["r3->u_m hold"] == pytest.approx(v1["r3->u_m hold"], abs=10)
    assert v2["reg2reg setup"] == pytest.approx(v1["reg2reg setup"], abs=5)
    assert v2["reg2reg hold"] == pytest.approx(v1["reg2reg hold"], abs=5)
    assert s2 >= s1 - 5
