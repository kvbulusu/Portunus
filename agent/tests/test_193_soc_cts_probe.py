"""§193 read-only soc_top CTS probe script: parses, never writes the DB."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TCL = ROOT / "asic_flow/scripts/debug/soc_cts_probe.tcl"


def test_probe_is_read_only_193():
    txt = TCL.read_text()
    assert not re.search(r"^\s*(write_db|orfs_write_db|write_def|write_verilog)\b", txt, re.M)
    assert "load_design $stage.odb $stage.sdc" in txt
    for sec in ("P2 u_clkgen", "P3 clock(s)", "P5 single-fanout", "P7a hold", "P8c netlist"):
        assert sec in txt


@pytest.mark.skipif(not shutil.which("tclsh"), reason="tclsh not installed")
def test_probe_parses_and_hier_193(tmp_path):
    drv = tmp_path / "d.tcl"
    drv.write_text(f"""
set f [open {{{TCL}}}]; set s [read $f]; close $f
if {{![info complete $s]}} {{ puts INCOMPLETE; exit 1 }}
regexp {{proc hier \\{{ name \\}} \\{{(.*?)\\n\\}}}} $s -> body
proc hier {{ name }} $body
puts [hier clone_4_u_clkgen/_147_],[hier u_periph/u_sysctrl.sys_state_o],[hier u_cdc_agent.u_aw.rd_ptr_bin]
""")
    out = subprocess.run(["tclsh", str(drv)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.strip() == "u_clkgen,u_periph,u_cdc_agent"
