"""§186: bootrom must not be an inferred memory (SYNTH_MEMORY_MAX_BITS gate)."""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))


def _src():
    with open(os.path.join(ROOT, "rtl/periph/bootrom_controller.sv")) as f:
        return re.sub(r"//[^\n]*", "", f.read())


def test_no_unpacked_rom_array():
    # any "logic [..] name [..];" unpacked array is what yosys infers as $mem
    assert not re.search(r"logic\s*\[[^\]]+\]\s*\w+\s*\[[^\]]+\]\s*;", _src())


def test_stub_contents_preserved():
    s = _src()
    assert "rom_word(word_idx)" in s
    for word in ("32'h1050_0073", "32'h0000_006F", "32'h0000_0013"):
        assert word in s
    assert re.search(r"WORD_AW'\(1\)\s*:\s*rom_word\s*=\s*DATA_W'\(32'h1050_0073\)", s)
    assert re.search(r"WORD_AW'\(2\)\s*:\s*rom_word\s*=\s*DATA_W'\(32'h0000_006F\)", s)
    assert re.search(r"default\s*:\s*rom_word\s*=\s*DATA_W'\(32'h0000_0013\)", s)
