"""§226 (run 20261109_143449, 4_cts JANUS_CTS_HOLD_ABORT -789.6):
the §225 whole-slice globs fenced u_ucut_mn (11292 insts) incl. the MODE 4
egress mr_q (r_ready pin flop + R read-mux select) -> mr_q -> u_user/m_s2m_i
-539.1, MACRO_ALIGN setup_in 3224.8 -> 2158.2. Fence only g_ingress in_* flops.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PF = ROOT / "asic_flow/scripts/hooks/janus_pin_fence.tcl"
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()


def _specs():
    m = re.search(r"^export JANUS_PIN_FENCE\s+=\s*(.*)$", MK, re.M)
    return dict(s.split(":", 1)[0:1] + [s.split(":", 2)[2]] for s in m.group(1).split()
                if s.count(":") == 2) if m else {}


def test_specs_ingress_only():
    sp = _specs()
    assert sp["ucut_mn"] == "u_ucut_mn/u_*/g_ingress/in_*"
    assert sp["ucut_s3n"] == "u_ucut_s3n/u_*/g_ingress/in_*"
    assert sp["upin"] == "upin_*" and sp["msip_q"] == "clint_msip_q*"


def test_glob_on_run_143449_names():
    if not shutil.which("tclsh"):
        pytest.skip("tclsh missing")
    src = PF.read_text()
    procs = re.findall(r"(?ms)^proc janus_pf::(?:name_match|env) .*?^\}", src)
    sp = _specs()
    cases = [
        (sp["ucut_mn"], "u_ucut_mn.u_aw/g_ingress.in_d_q[67]$_DFF_PN0_", 1),
        (sp["ucut_mn"], "u_ucut_mn.u_ar/g_ingress.in_v_q$_DFF_PN0_", 1),
        (sp["ucut_mn"], "u_ucut_mn.u_w/g_ingress.in_d_q[3]$_DFF_PN0_", 1),
        (sp["ucut_mn"], "u_ucut_mn.u_r/g_egress.mr_q$_DFF_PN0_", 0),
        (sp["ucut_mn"], "u_ucut_mn.u_r/g_egress.g_e[1].ent_q[37]$_DFFE_PN0P_", 0),
        (sp["ucut_mn"], "u_ucut_mn.u_aw/g_ingress.g_e[0].ent_q[5]$_DFFE_PN0P_", 0),
        (sp["ucut_mn"], "u_ucut_mn.u_r/_3071_", 0),
        (sp["ucut_mn"], "u_ucut_m.u_aw/g_ingress.in_d_q[1]$_DFF_PN0_", 0),
        (sp["ucut_s3n"], "u_ucut_s3n.u_r/g_ingress.in_d_q[5]$_DFF_PN0_", 1),
        (sp["ucut_s3n"], "u_ucut_s3n.u_r/s_ready_o$_DFF_PN0_", 0),
        (sp["ucut_s3n"], "u_ucut_s3/u_w.m_data_o[128]$_DFFE_PN0P_", 0),
    ]
    body = "\n".join(f"puts [janus_pf::name_match {{{p}}} {{{n}}}]" for p, n, _ in cases)
    t = "namespace eval janus_pf {}\n" + "\n".join(procs) + "\n" + body
    r = subprocess.run(["tclsh"], input=t, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert [int(x) for x in r.stdout.split()] == [e for _, _, e in cases]
