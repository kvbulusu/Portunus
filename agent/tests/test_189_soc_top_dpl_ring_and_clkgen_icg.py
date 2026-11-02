"""§189 janus_soc_top 3_place regressions (EC2 run 20261101_123501).

1. DPL-0036: the 50%-density ring around the noc must be a SOFT blockage.
   DPL skips only isSoft() blockages; a non-soft one is a hard keep-out even
   with maxDensity 50, while GPL fills it to 50% -> unlegalizable cells.
2. Gated-clock hold -4937 ps: clock_gen must gate the PLL-placeholder clocks
   through the latch-based clock_gate_cell, not a bare AND with a flop enable,
   and the top must synthesize clock_gate_cell.sv.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MACROS = ROOT / "asic_flow/scripts/soc_top_macros.tcl"
CLKGEN = ROOT / "rtl/common/clock_gen.sv"
MK = ROOT / "asic_flow/configs/janus_soc_top.mk"


def _code(text, comment):
    return "\n".join(l.split(comment, 1)[0] for l in text.splitlines())


def test_noc_ring_blockage_is_soft_189():
    src = _code(MACROS.read_text(), "#")
    create = src.index("dbBlockage_create")
    tail = src[create:]
    assert "setMaxDensity" in tail
    assert re.search(r"\$bl\s+setSoft\b", tail), "noc ring must be soft (DPL hard-blocks non-soft)"
    assert "isSoft" in tail, "must verify the blockage became soft"
    assert "dbBlockage_destroy" in tail, "a failed soft conversion must not leave a hard ring"


def test_no_hard_blockages_anywhere_in_soc_top_macros_189():
    src = _code(MACROS.read_text(), "#")
    assert src.count("dbBlockage_create") == src.count("setSoft") == 1


def test_clock_gen_uses_icg_not_bare_and_189():
    src = _code(CLKGEN.read_text(), "//")
    assert not re.search(r"assign\s+clk_(base|user)_o\s*=.*&", src), "bare AND clock gate is back"
    for out in ("clk_base_o", "clk_user_o"):
        assert re.search(r"clock_gate_cell\s+\w+\s*\([^;]*\.clk_o\s*\(\s*%s\s*\)" % out, src, re.S), out


def test_soc_top_synthesizes_clock_gate_cell_189():
    mk = _code(MK.read_text(), "#")
    files = re.findall(r"\$\(RTL\)/(\S+\.sv)", mk)
    assert "common/clock_gate_cell.sv" in files
    assert files.index("common/clock_gate_cell.sv") < files.index("common/clock_gen.sv")
