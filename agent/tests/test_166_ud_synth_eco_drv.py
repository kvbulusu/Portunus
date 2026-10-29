"""§166 (run 20261028_235932): user_domain synth fix (no parameter override on
the blackboxed user_core macro) + post-route DRV repair never on SPEF."""
import os
import re

ASIC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO = os.path.dirname(ASIC)
UD_MK = os.path.join(ASIC, "configs", "user_domain.mk")
TOP = os.path.join(REPO, "rtl", "user", "user_cluster_top.sv")
WRAP = os.path.join(REPO, "rtl", "user", "ara_integration_wrapper.sv")
TCL = os.path.join(ASIC, "scripts", "post_route_opt.tcl")


def _preprocess(text, defines):
    """Minimal `ifdef/`else/`endif evaluator (no nesting needed here)."""
    out, stack = [], []
    for line in text.splitlines():
        t = line.strip()
        m = re.match(r"`(ifdef|ifndef)\s+(\w+)", t)
        if m:
            on = (m.group(2) in defines) == (m.group(1) == "ifdef")
            stack.append(on)
            continue
        if t.startswith("`else"):
            stack[-1] = not stack[-1]
            continue
        if t.startswith("`endif"):
            stack.pop()
            continue
        if all(stack):
            out.append(line)
    return "\n".join(out)


def _core_inst(text):
    m = re.search(r"ara_integration_wrapper\s*(#\s*\(.*?\))?\s*u_core\s*\(", text, re.S)
    assert m, "u_core instance not found"
    return m


def test_user_domain_defines_hard_core():
    assert re.search(r"^export VERILOG_DEFINES \+= -D JANUS_UD_HARD_CORE$",
                     open(UD_MK).read(), re.M)


def test_hard_core_instance_has_no_parameter_override():
    rtl = open(TOP).read()
    hard = _preprocess(rtl, {"JANUS_UD_HARD_CORE", "JANUS_UD_BOUNDARY_CUT"})
    assert _core_inst(hard).group(1) is None
    assert "NrLanes" not in hard[_core_inst(hard).start():_core_inst(hard).end()]
    # sim / FPGA (no define) keep the explicit override
    soft = _preprocess(rtl, set())
    assert ".NrLanes" in (_core_inst(soft).group(1) or "")


def test_wrapper_defaults_match_asic_package():
    """The macro is hardened with the wrapper's DEFAULT parameters, so dropping
    the override in user_domain must not change the core's configuration."""
    w = open(WRAP).read()
    assert re.search(r"parameter\s+int\s+unsigned\s+NrLanes\s*=\s*4\b", w)
    assert re.search(r"parameter\s+int\s+unsigned\s+VLEN\s*=\s*512\b", w)


def test_drv_repair_skipped_on_spef():
    s = open(TCL).read()
    skip = s.index('[_env JANUS_ECO_SETUP_DRV 0] ne "0" && $have_spef}')
    run = s.index("repair_design -slew_margin $sm -cap_margin $cm")
    assert skip < run
    assert "JANUS_PR_WARN drv_repair skipped" in s[skip:run]
