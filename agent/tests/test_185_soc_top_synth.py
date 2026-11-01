"""§185: janus_soc_top hierarchical synth — macro instances must not carry
parameter overrides when JANUS_SOC_HARD_MACROS is set (liberty cells have no
parameters), and the unknown slang warning flag must be gone."""
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MACROS = {
    "rtl/top/janus_soc_top.sv": ["axi4_crossbar", "user_cluster_top",
                                 "agent_cluster_top", "janus_kv_secure_subsystem"],
    "rtl/periph/periph_subsystem.sv": ["ampo_top"],
}


def _read(rel):
    with open(os.path.join(ROOT, rel)) as f:
        return f.read()


def _hard_branch(src):
    """Text with JANUS_SOC_HARD_MACROS defined (keep ifdef-branch, drop else)."""
    return re.sub(r"`ifdef JANUS_SOC_HARD_MACROS\n(.*?)`else\n.*?`endif\n",
                  r"\1", src, flags=re.S)


def test_mk_defines_hard_macros_and_drops_unknown_flag():
    mk = _read("asic_flow/configs/janus_soc_top.mk")
    assert "-D JANUS_SOC_HARD_MACROS" in mk
    assert not re.search(r"^\s*export SYNTH_SLANG_ARGS \+= -Wno-undefined-param-override",
                         mk, re.M)


def test_macro_instances_param_free_under_define():
    for rel, mods in MACROS.items():
        hard = _hard_branch(_read(rel))
        for m in mods:
            assert re.search(rf"^\s*{m}\s+u_\w+\s*\(", hard, re.M), (rel, m)
            assert not re.search(rf"^\s*{m}\s*#\s*\(", hard, re.M), (rel, m)


def test_hart_map_declared_before_use():
    top = _read("rtl/top/janus_soc_top.sv")
    decl = top.index("localparam int USER_HART_LO")
    assert decl < top.index("USER_HART_LO +:")
    assert top.index("logic [NR_HARTS-1:0] dbg_debug_req;") < top.index("dbg_debug_req[")


def test_job_scheduler_ports_do_not_use_body_localparams():
    js = _read("rtl/periph/job_scheduler.sv")
    header = js[js.index("module job_scheduler"):js.index(");")]
    assert "CIDX_W" not in header.split(") (", 1)[1].split("//")[0] or \
        not re.search(r"\[(CIDX_W|AIDX_W)-1:0\]", header)


def test_bootrom_no_long_initial_loop():
    br = _read("rtl/periph/bootrom_controller.sv")
    assert not re.search(r"for\s*\(\s*w\s*=\s*0\s*;\s*w\s*<\s*NUM_WORDS", br)
