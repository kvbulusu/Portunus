"""§219a read-only soc_top hold probe (soc_hold_probe.tcl).

Static: no db/def/verilog writers, balanced braces/brackets, no unescaped
[ ] in format/puts strings (generalised §218 bbox check, also applied to
soc_cts_probe.tcl), H1 repair experiment last and followed by exit.
tclsh: `info complete`, every proc definable, pure helpers behave, and the
whole script executes against a tiny mock OpenROAD (fixtures/t219a) with
bounded output and no file written except the RSZ scratch log.
NOT an OpenROAD run: real numbers only come from the EC2 make run.
Run: python3 -m pytest asic_flow/agent/tests/test_219a_soc_hold_probe.py -q"""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))

import _tcl_lint as tl  # noqa: E402

TCL = ROOT / "asic_flow/scripts/debug/soc_hold_probe.tcl"
OLD = ROOT / "asic_flow/scripts/debug/soc_cts_probe.tcl"
MOCK = HERE / "fixtures/t219a/mock_openroad.tcl"
TCLSH = shutil.which("tclsh")


def src():
    return TCL.read_text()


# --- static ---------------------------------------------------------------------
def test_never_writes():
    assert tl.write_forbidden(src()) == []
    s = src()
    assert "load_design $stage.odb $stage.sdc" in s
    # the only file written is the RSZ scratch log
    assert re.findall(r"\bopen\s+(\S+)\s+(\w+)", s) == [("$log", "r")]
    assert "utl::redirect_file_begin $log" in s


def test_balanced():
    for p in (TCL, OLD):
        br, bk, neg = tl.balance(p.read_text())
        assert (br, bk, neg) == (0, 0, None), p.name


def test_no_unescaped_brackets_in_strings():
    assert tl.bracket_issues(src()) == []
    assert tl.bracket_issues(OLD.read_text()) == []


@pytest.mark.parametrize("bad", [
    # the exact §218 crash (rc=2): Tcl evaluates [%s..%s]
    'puts [format "SCP %-26s bbox x[%s..%s] y[%s..%s]" $r 1 2 3 4]',
    'out "pin data_o[12] slack"',
    'puts "bus[i] value"',
    'out "x ] y"',
])
def test_bracket_checker_catches(bad):
    assert tl.bracket_issues(bad), bad


@pytest.mark.parametrize("good", [
    'puts [format "bbox x\\[%s..%s\\]" 1 2]',
    'out "SHP n=[llength $x] obj [$inst getName] m=[[$i getMaster] getName]"',
    'proc xys { a } { }\nout "[xys 1]"',
])
def test_bracket_checker_allows(good):
    assert tl.bracket_issues(good) == [], good


def test_sections_order_and_exit():
    s = src()
    keys = ['hdr "H2a', 'hdr "H2b', 'hdr "H2c', 'hdr "H2d', 'hdr "H3 ',
            'hdr "H4 ', 'hdr "H1 RSZ', "repair_timing {*}$rt_args", 'hdr "H1b',
            'hdr "DONE']
    idx = [s.index(k) for k in keys]
    assert idx == sorted(idx)
    assert s.rstrip().endswith("exit")
    assert s.count("repair_timing {*}") == 1
    assert "set_debug_level RSZ repair_hold $lvl" in s
    for knob in ("SHP_STAGE", "SCP_STAGE", "SHP_LINES", "SHP_MAXLINES", "SHP_REPAIR"):
        assert knob in s


def test_header_has_make_command():
    head = src().split("source $::env(SCRIPTS_DIR)/load.tcl")[0]
    assert "RUN_SCRIPT=$PWD/../../scripts/debug/soc_hold_probe.tcl run" in head
    assert "tee ~/soc_hold_probe.txt" in head
    assert 'CORNERS="slow fast"' in head


# --- tclsh ------------------------------------------------------------------------
def _tcl(script, cwd=None, env=None, timeout=60):
    return subprocess.run([TCLSH], input=script, capture_output=True, text=True,
                          cwd=cwd, env=env, timeout=timeout)


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_tclsh_parse_and_procs():
    drv = f"""
set f [open {{{TCL}}}]; set s [read $f]; close $f
if {{![info complete $s]}} {{ puts INCOMPLETE; exit 1 }}
# define every proc (catches bad arg lists / bodies that are not complete)
set n 0
foreach line [split $s "\\n"] {{ }}
set cmd ""
foreach line [split $s "\\n"] {{
  append cmd $line "\\n"
  if {{![info complete $cmd]}} continue
  if {{[regexp {{^\\s*proc\\s}} $cmd]}} {{ eval $cmd; incr n }}
  set cmd ""
}}
set ::dbu 1000
puts $n
puts [hier clone_4_u_clkgen/_147_],[hier u_npu_s_job_cdc.data_o\\[171\\]\\$_DFF_PN0_]
puts [unesc {{a\\[1\\]}}],[fmt 1e30],[fmt -99],[fmt x],[um 1500]
puts [lim -99 150],[lim -99 50],[lim 5 1],[lim ? 1]
set tail "data_o\\[171\\]\\$_DFF_PN0_"
regexp {{^([A-Za-z0-9_]+)\\\\?\\[([0-9]+)\\\\?\\]}} $tail -> b i
puts $b,$i
set tail {{sync_q0\\[7\\]$_DFF_PN0_}}
regexp {{^([A-Za-z0-9_]+)\\\\?\\[([0-9]+)\\\\?\\]}} $tail -> b i
puts $b,$i
"""
    out = _tcl(drv)
    assert out.returncode == 0, out.stdout + out.stderr
    lines = out.stdout.split()
    assert int(lines[0]) >= 30
    assert lines[1] == "u_clkgen,u_npu_s_job_cdc"
    assert lines[2] == "a[1],inf,-99.0,?,1.5"
    assert lines[3] == "room,SETUP,met,?"
    assert lines[4:6] == ["data_o,171", "sync_q0,7"]


def _run_mock(tmp_path, **extra):
    wd = tmp_path / "flow"
    wd.mkdir()
    (wd / "load.tcl").write_text("")
    env = dict(os.environ, SCRIPTS_DIR=str(wd), RESULTS_DIR=str(wd),
               DESIGN_NAME="janus_soc_top", SHP_LINES="5")
    env.pop("JANUS_POST_SCRIPT_OUT", None)
    env.update(extra)
    before = sorted(p.name for p in wd.iterdir())
    r = subprocess.run([TCLSH, str(MOCK), str(TCL)], capture_output=True, text=True,
                       cwd=wd, env=env, timeout=120)
    return r, wd, before


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_run_post_script_out(tmp_path):
    out = tmp_path / "post"
    out.mkdir()
    r, wd, before = _run_mock(tmp_path, JANUS_POST_SCRIPT_OUT=str(out))
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    o = r.stdout
    for sec in ("H2a", "H2b", "H2c", "H2d", "H3 per", "H4 clk_user", "H1 RSZ", "H1b", "DONE"):
        assert f"######## SHP: {sec}" in o, sec
    assert o.rstrip().endswith("MOCK_EXIT")
    assert "SHP3   12 clk 812.5/812.5" in o
    assert "-> u_agent/npu_s_job_i[12]" in o and "rep 1" in o
    assert "setup-limited=1" in o
    # RSZ log capped: 500 matching lines, <= SHP_LOG_PER_PIN printed
    assert o.count("SHP1 log:") == 4 and "lines naming targets 500" in o
    assert "SHP1 msg: [WARNING RSZ-0066]" in o
    assert "hold buffers after: 1 (added 1)" in o
    # nothing written into the flow dir; only the scratch log in post dir
    assert sorted(p.name for p in wd.iterdir()) == before
    assert [p.name for p in out.iterdir()] == ["soc_hold_probe_rsz_debug.log"]
    assert "mock: write" not in r.stderr


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_run_tmp_log_deleted_and_line_cap(tmp_path):
    r, wd, before = _run_mock(tmp_path, SHP_MAXLINES="20")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    body = [l for l in r.stdout.splitlines() if l and not l.startswith("########")]
    assert len(body) <= 21  # 20 + MOCK_EXIT
    assert re.search(r"DONE \(read-only; db not written; [1-9][0-9]* lines cut", r.stdout)
    logs = re.findall(r"log (/tmp/soc_hold_probe_rsz_\d+\.log)", r.stdout)
    for lg in logs:
        assert not os.path.exists(lg)
    assert sorted(p.name for p in wd.iterdir()) == before


@pytest.mark.skipif(not TCLSH, reason="tclsh not installed")
def test_mock_run_skip_repair(tmp_path):
    r, _, _ = _run_mock(tmp_path, SHP_REPAIR="0")
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "SHP1 skipped (SHP_REPAIR=0)" in r.stdout
    assert "repair_timing rc=" not in r.stdout
