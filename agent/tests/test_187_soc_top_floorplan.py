"""§187: janus_soc_top explicit dataflow floorplan + SDC audit.

The macro-placement script is executed in plain tclsh against a mocked OpenDB
(real child sizes: user_domain 1280.285^2, agent_domain 640^2, security
~397.3^2, noc ~91.7^2, ampo unknown -> swept) so the geometry, halo and
fail-fast logic are exercised without OpenROAD.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path[:0] = [os.path.join(HERE, "..")]

MK = os.path.join(ROOT, "asic_flow", "configs", "janus_soc_top.mk")
SDC = os.path.join(ROOT, "asic_flow", "configs", "constraints", "janus_soc_top.sdc")
MACROS = os.path.join(ROOT, "asic_flow", "scripts", "soc_top_macros.tcl")
IOTCL = os.path.join(ROOT, "asic_flow", "scripts", "hooks", "soc_top_io_constraints.tcl")


def _read(p):
    return open(p).read()


def _active(txt):
    return "\n".join(l for l in txt.splitlines() if not l.lstrip().startswith("#"))


def _mk_val(key):
    m = re.search(r"^export %s\s*=\s*(.+?)\s*$" % re.escape(key), _active(_read(MK)), re.M)
    assert m, key
    return m.group(1)


# ---------------------------------------------------------------- .mk -------
def test_mk_explicit_floorplan():
    mk = _active(_read(MK))
    assert "CORE_UTILIZATION" not in mk and "CORE_ASPECT_RATIO" not in mk
    die = [float(v) for v in _mk_val("DIE_AREA").split()]
    core = [float(v) for v in _mk_val("CORE_AREA").split()]
    assert die[:2] == [0, 0] and core[0] >= 20 and core[1] >= 20
    assert die[2] - core[2] >= 20 and die[3] - core[3] >= 20  # M5/M6 core ring
    assert _mk_val("MACRO_PLACE_HALO") == "20 20"
    assert _mk_val("MACRO_BLOCKAGE_HALO") == "20"
    assert _mk_val("SYNTH_KEEP_MODULES") == "clock_gen"


def test_mk_hook_paths_exist():
    for key in ("MACRO_PLACEMENT_TCL", "PDN_TCL", "POST_PDN_TCL",
                "PRE_IO_PLACEMENT_TCL", "PRE_CTS_TCL"):
        rel = _mk_val(key).replace("$(JANUS_RTL_ROOT)/", "")
        assert os.path.isfile(os.path.join(ROOT, rel)), (key, rel)
    assert _mk_val("JANUS_IO_SHADOW_EXCLUDE_EDGES") == "left"
    assert _mk_val("JANUS_IO_SHADOW_SPAN_EDGES") == "bottom top"
    shadow = _read(os.path.join(ROOT, "asic_flow", "scripts", "hooks", "janus_io_macro_shadow.tcl"))
    for env in ("JANUS_IO_SHADOW_EXCLUDE_EDGES", "JANUS_IO_SHADOW_SPAN_EDGES",
                "JANUS_IO_SHADOW_PIN_PITCH_UM"):
        assert "env %s" % env in shadow


def test_pdn_connects_m6_macro_pins():
    pdn = _read(os.path.join(ROOT, "asic_flow", "scripts", "pdn", "user_core_grid_strategy.tcl"))
    assert re.search(r"add_pdn_connect[^\n]*-layers \{M5 M6\}", pdn)
    assert "-macro" in pdn


def test_frozen_children_untouched():
    out = subprocess.run(["git", "diff", "--name-only", "origin/main", "--"],
                         cwd=ROOT, capture_output=True, text=True).stdout.split()
    for frozen in ("security.mk", "noc.mk", "ampo.mk", "agent_domain.mk", "user_domain.mk",
                   "user_core.mk", "bootrom_controller.sv"):
        assert not any(p.endswith(frozen) for p in out), frozen


# ------------------------------------------------------- macro placement ----
_MOCK = r"""
set ::placed {}
set ::DBU 1000
proc _r {x0 y0 x1 y1} {
  set n rect[incr ::rid]
  proc $n {m} "switch -- \$m xMin {return $x0} yMin {return $y0} xMax {return $x1} yMax {return $y1}"
  return $n
}
proc mkmaster {name w h} {
  set n m_$name
  proc $n {m args} "switch -- \$m isBlock {return 1} getName {return $name} \
      getWidth {return [expr {round($w*1000)}]} getHeight {return [expr {round($h*1000)}]} \
      getMTerms {return {}}"
  return $n
}
proc mkinst {iname master} {
  set n i_$iname
  set ::inst_master($n) $master
  set ::inst_name($n) $iname
  proc $n {m args} "inst_method $n \$m"
  return $n
}
proc inst_method {n m} {
  switch -- $m {
    getMaster { return $::inst_master($n) }
    getName   { return $::inst_name($n) }
    getOrient { return $::orient($n) }
    getPlacementStatus { return FIRM }
    getBBox {
      lassign $::loc($n) x y
      set w [$::inst_master($n) getWidth]; set h [$::inst_master($n) getHeight]
      set x0 [expr {round($x*1000)}]; set y0 [expr {round($y*1000)}]
      return [_r $x0 $y0 [expr {$x0+$w}] [expr {$y0+$h}]]
    }
  }
}
proc place_macro {args} {
  array set a $args
  set n i_$a(-macro_name)
  set ::loc($n) $a(-location)
  set ::orient($n) $a(-orientation)
  lappend ::placed [list $a(-macro_name) {*}$a(-location) $a(-orientation)]
}
namespace eval odb { proc dbBlockage_create {args} { return blk1 } }
proc blk1 {args} {}
proc theblock {m args} {
  switch -- $m {
    getDbUnitsPerMicron { return 1000 }
    getCoreArea { return [_r {*}$::CORE] }
    getRows { return {} }
    getInsts { return $::INSTS }
  }
}
namespace eval ord { proc get_db_block {} { return theblock } }
set ::INSTS [list \
  [mkinst u_user  [mkmaster user_cluster_top 1280.285 1280.285]] \
  [mkinst u_agent [mkmaster agent_cluster_top 640 640]] \
  [mkinst u_kv_secure_subsystem [mkmaster janus_kv_secure_subsystem __SEC__ __SEC__]] \
  [mkinst u_noc   [mkmaster axi4_crossbar 91.692 91.692]] \
  [mkinst u_periph/u_ampo [mkmaster ampo_top __AMPO__ __AMPO__]]]
set ::env(MACRO_PLACE_HALO) "20 20"
set ::CORE {__CORE__}
if {[catch {source __SCRIPT__} err]} { puts "FAIL: $err"; exit 0 }
foreach p $::placed { puts "PLACED $p" }
"""


def _run_place(core_um, sec=397.3, ampo=120.0):
    core = " ".join(str(int(round(v * 1000))) for v in core_um)
    tcl = (_MOCK.replace("__SEC__", str(sec)).replace("__AMPO__", str(ampo))
           .replace("__CORE__", core).replace("__SCRIPT__", MACROS))
    r = subprocess.run(["tclsh"], input=tcl, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


SIZES = {"u_user": 1280.285, "u_agent": 640.0, "u_kv_secure_subsystem": 397.3,
         "u_noc": 91.692, "u_periph/u_ampo": 120.0}


def _parse(out):
    res = {}
    for l in out.splitlines():
        if l.startswith("PLACED "):
            _, name, x, y, o = l.split()
            res[name] = (float(x), float(y), o)
    return res


@pytest.mark.skipif(not shutil.which("tclsh"), reason="no tclsh")
@pytest.mark.parametrize("ampo", [40.0, 120.0, 250.0])
def test_macros_fit_with_halo(ampo):
    core = [float(v) for v in _mk_val("CORE_AREA").split()]
    out = _run_place(core, ampo=ampo)
    assert "FAIL" not in out, out
    pl = _parse(out)
    assert len(pl) == 5, out
    sizes = dict(SIZES, **{"u_periph/u_ampo": ampo})
    halo = 20.0
    boxes = {n: (x, y, x + sizes[n], y + sizes[n]) for n, (x, y, _) in pl.items()}
    for n, (x0, y0, x1, y1) in boxes.items():
        assert x0 - halo >= core[0] - 1e-6 and y0 - halo >= core[1] - 1e-6, n
        assert x1 + halo <= core[2] + 1e-6 and y1 + halo <= core[3] + 1e-6, n
    names = list(boxes)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            A, B = boxes[a], boxes[b]
            gx = max(B[0] - A[2], A[0] - B[2])
            gy = max(B[1] - A[3], A[1] - B[3])
            assert gx >= 2 * halo - 0.06 or gy >= 2 * halo - 0.3, (a, b, gx, gy)
    # dataflow: user_domain west, noc in the glue column, agent NE, security SE
    ux = boxes["u_user"][2]
    assert boxes["u_noc"][0] > ux and boxes["u_noc"][2] < boxes["u_agent"][0]
    assert boxes["u_agent"][1] > boxes["u_kv_secure_subsystem"][3]
    assert "glue column" in out and "macro area" in out


@pytest.mark.skipif(not shutil.which("tclsh"), reason="no tclsh")
def test_macros_fail_fast_when_die_too_small():
    out = _run_place([20, 20, 2100, 1420])
    assert "FAIL: soc_top_macros:" in out and "glue column" in out


@pytest.mark.skipif(not shutil.which("tclsh"), reason="no tclsh")
def test_io_hook_parses():
    for p in (MACROS, IOTCL):
        r = subprocess.run(["tclsh"], input="puts [info complete [read [open %s]]]" % p,
                           capture_output=True, text=True)
        assert r.stdout.strip() == "1", p


# ---------------------------------------------------------------- SDC -------
def test_sdc_clocks_and_exceptions():
    s = _active(_read(SDC))
    assert "-multiply_by" not in s and "-divide_by" not in s           # STA-0380
    assert "create_generated_clock" not in s
    assert re.search(r"create_clock -name clk_user -period 2857\.14 \$_user_root", s)
    assert re.search(r"create_clock -name clk_agent -period 5000\.0 \$_agent_root", s)
    assert "set_sense -type clock -stop_propagation -clocks [get_clocks ref_clk] $_user_root" in s
    assert "-stop_propagation -clocks [get_clocks {ref_clk clk_user}] $_agent_root" in s
    assert re.search(r"error \"JANUS SDC §187: user clock root not found", s)
    # IO no longer referenced to the (absent) clk_periph
    assert not re.search(r"set_(input|output)_delay[^\n]*clk_periph", s)
    assert re.search(r"janus_ports \{rst_n_async jtag_trst_n_i pll_en_i tensor_kv_occupancy_i\*\}", s)
    assert "set_false_path -from $_false_in" in s


def test_sdc_covers_every_top_port():
    rtl = _read(os.path.join(ROOT, "rtl", "top", "janus_soc_top.sv"))
    hdr = rtl[rtl.index("module janus_soc_top"):]
    hdr = hdr[:hdr.index(");")]
    ports = re.findall(r"^\s*(?:input|output)\s+logic\s*(?:\[[^\]]*\])?\s*(\w+)", hdr, re.M)
    assert len(ports) > 25
    s = _active(_read(SDC))
    for p in ports:
        assert p in s, p


# ------------------------------------------------------------ agent map -----
def test_agent_strips_child_obs_for_soc_top():
    src = _read(os.path.join(ROOT, "asic_flow", "agent", "janus_agent.py"))
    i = src.index("def _ensure_lane_abstract_obs_fixed")
    body = src[i:i + 6000]
    m = re.search(r'"janus_soc_top"\s*:\s*\(\s*\(([^)]*)\)', body)
    assert m, "janus_soc_top missing from children map"
    kids = re.findall(r'"(\w+)"', m.group(1))
    assert sorted(kids) == sorted(["security", "noc", "ampo", "agent_domain", "user_domain"])


def test_sdc_skips_dead_clock_roots_188():
    """§188: a clock root with no loads is removed by eliminate_dead_logic after
    read_sdc; write_sdc then emits a freed Pin* (garbage name) -> segfault in
    set_sense at 2_1_floorplan. janus_clk_root must reject load-less nets."""
    s = _active(_read(SDC))
    i = s.index("proc janus_clk_root")
    body = s[i:s.index("\n}\n", i)]
    assert 'direction == input' in body and "continue" in body
    # set_sense is only applied to roots that were actually created
    assert "if {$_have_periph} {\n  set_sense" in s
