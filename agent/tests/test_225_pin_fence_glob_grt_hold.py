"""§225 (run 20261108_150122):
  * janus_pin_fence.tcl matched 'u_ucut_mn/*' against 0 insts (names are
    u_ucut_mn.u_aw/...) -> u_ucut_mn never fenced -> -99/-69/-52 setup family.
    Fix: janus_pf::name_match also tries the name with '.' read as '/'.
  * a pin fence matching 0 insts is now a RED flag in the run mail.
  * post-GRT hold cap off: 400 passes would have stopped at TNS -22249 vs
    -4009 at pass 1000 (5_1_grt.log hold table).
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import stage_report as sr  # noqa: E402

PF = ROOT / "asic_flow/scripts/hooks/janus_pin_fence.tcl"
MK = (ROOT / "asic_flow/configs/janus_soc_top.mk").read_text()


def _tcl(body):
    if not shutil.which("tclsh"):
        pytest.skip("tclsh missing")
    src = PF.read_text()
    # load only the pure procs (everything before the hook's top-level run)
    procs = re.findall(r"(?ms)^proc janus_pf::(?:name_match|env) .*?^\}", src)
    t = "namespace eval janus_pf {}\n" + "\n".join(procs) + "\n" + body
    r = subprocess.run(["tclsh"], input=t, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_name_match_hier_forms():
    out = _tcl(r"""
foreach {p n} {
  u_ucut_mn/*   {u_ucut_mn.u_aw/g_ingress.in_d_q[67]$_DFF_PN0_}
  u_ucut_mn/*   {u_ucut_mn/u_ar/skid_data_q[3]$_DFFE_PN0P_}
  u_ucut_mn/*   {u_ucut_mn.u_r.out_data_q[1]$_DFFE_PN0P_}
  u_ucut_s3n/*  {u_ucut_s3n.u_r/g_ingress.in_d_q[5]$_DFF_PN0_}
  u_ucut_s3n/*  {u_ucut_s3/u_r.m_data_o[101]$_DFFE_PN0P_}
  u_ucut_mn/*   {u_ucut_m.u_aw/x}
  upin_*        {upin_dbg_req_q$_DFF_PN0_}
  clint_msip_q* {clint_msip_q[1]$_DFF_PN0_}
} { puts "[janus_pf::name_match $p $n]" }
""")
    assert out.split() == ["1", "1", "1", "1", "0", "0", "1", "1"]


def test_hook_uses_name_match_everywhere():
    s = PF.read_text()
    assert s.count("janus_pf::name_match $pat [$i getName]") == 1
    assert s.count("janus_pf::name_match $ag [$i getName]") == 1
    assert "string match $pat [$i getName]" not in s


def test_fence_specs_present():
    # §226 narrowed the globs to the g_ingress pin-capture flops
    m = re.search(r"^export JANUS_PIN_FENCE\s+=\s*(.*)$", MK, re.M)
    assert "ucut_mn:u_user:u_ucut_mn/u_*/g_ingress/in_*" in m.group(1).split()
    assert "ucut_s3n:u_user:u_ucut_s3n/u_*/g_ingress/in_*" in m.group(1).split()


def test_fence_zero_red_flag():
    log = ("JANUS_PIN_FENCE: WARN 'u_ucut_mn/*' matched 0 insts; skip ucut_mn\n"
           "JANUS_PIN_FENCE: upin members 7 area 1.3 um2 util 0.00 prelocked 3\n")
    rep = sr.parse_repair(log)
    assert rep.get("fence_zero") == ["ucut_mn"]
    flags = sr.red_flags([{"key": "3_place", "status": "DONE", "fence_zero": ["ucut_mn"]}])
    assert ("RED", "3_place: pin fence matched 0 insts: ucut_mn") in flags
    assert not sr.red_flags([{"key": "3_place", "status": "DONE"}])


def test_grt_hold_cap_off():
    assert re.search(r"^export JANUS_GRT_HOLD_MAX_ITERS\s+=\s*0\s*$", MK, re.M)
