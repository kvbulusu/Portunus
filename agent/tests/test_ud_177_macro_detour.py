"""§177: user_domain macro detour enabled + revised CTS kill rule."""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path[:0] = [os.path.join(HERE, ".."), os.path.join(ROOT, "asic_flow", "utils")]

import stage_report as sr  # noqa: E402


def _read(*p):
    return open(os.path.join(ROOT, *p)).read()


def _active(txt):
    return "\n".join(l for l in txt.splitlines() if not l.lstrip().startswith("#"))


def test_user_domain_detour_knobs():
    mk = _active(_read("asic_flow", "configs", "user_domain.mk"))
    assert re.search(r"^export JANUS_MACRO_DETOUR_BLOCKS\s*=\s*user_domain\s*$", mk, re.M)
    assert re.search(r"^export JANUS_MACRO_DETOUR_MASTERS\s*=\s*ara_integration_wrapper\s*$", mk, re.M)
    assert re.search(r"^export JANUS_MACRO_DETOUR_MAX_NETS\s*=\s*300\s*$", mk, re.M)
    assert re.search(r"^export JANUS_MACRO_DETOUR_SKIP_CONST\s*\??=\s*1", mk, re.M)


def test_user_core_untouched():
    assert "MAX_NETS" not in _read("asic_flow", "configs", "user_core.mk")


def test_hook_max_nets_guard_default_off():
    t = _read("asic_flow", "scripts", "hooks", "janus_macro_detour.tcl")
    assert "env JANUS_MACRO_DETOUR_MAX_NETS 0" in t
    i_guard = t.index("JANUS_MACRO_DETOUR_MAX_NETS=$maxn")
    assert i_guard < t.index("if {$dry || ![llength $plan]} { return }")
    assert t.count("{") == t.count("}")


def test_flow_state_maps_detour_to_route():
    import flow_state as fs
    src = open(fs.__file__).read()
    assert "JANUS_MACRO_DETOUR" in src
    st = None
    for name in ("stage_for_var", "var_stage", "stage_of_var", "first_stage_for"):
        if hasattr(fs, name):
            st = getattr(fs, name)("JANUS_MACRO_DETOUR_MAX_NETS")
            break
    if st is not None:
        assert "route" in str(st), st


def test_red_flags_revised_rule():
    # run 20261030_172824: 5329 bufs, pre -314, closed +20 -> no KILL
    row = {"key": "4_cts", "status": "DONE", "hold_bufs": 5329,
           "hold_pre_wns": -314.0, "hold_ws": 20.0}
    f = sr.red_flags([row])
    assert f and all(lv == "RED" for lv, _ in f), f
    assert not sr.subject_line("user_domain", "stage", "4_cts", [row], f).endswith("KILL RUN?")
    # hold not closed after repair -> KILL
    assert ("KILL" in [lv for lv, _ in sr.red_flags([dict(row, hold_ws=-3.0)])])
    # > 10000 buffers -> KILL
    assert ("KILL" in [lv for lv, _ in sr.red_flags([dict(row, hold_bufs=10001)])])
    # under 5000 bufs and pre >= -300: no flags
    assert sr.red_flags([dict(row, hold_bufs=4000, hold_pre_wns=-250.0)]) == []
