"""§221: NoC-slave-side cuts u_ucut_s1 (u_periph) / u_ucut_s2 (u_ddr).
Run 20261107_104724 4_cts worst setup -228.2 u_ucut_m -> u_ddr/bank_row[33]
and u_ucut_m -> u_ucut_m enables through u_noc + slave comb ready.
Run: python3 -m pytest asic_flow/agent/tests/test_221_noc_slave_cuts.py -q"""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SV = open(os.path.join(ROOT, "rtl", "top", "janus_soc_top.sv")).read()


def _block():
    m = re.search(r"`ifndef JANUS_SOC_NO_UCUT\n(\s*axi4_link_slice u_ucut_s1 \(.*?)`else\n(.*?)`endif",
                  SV, re.S)
    assert m, "u_ucut_s1/_s2 must be under `ifndef JANUS_SOC_NO_UCUT with a direct-assign `else"
    return m.group(1), m.group(2)


def test_slices_wired_noc_side():
    cut, _ = _block()
    for inst, k, req in (("u_ucut_s1", 1, "periph_s"), ("u_ucut_s2", 2, "ddr_s")):
        m = re.search(r"axi4_link_slice %s \((.*?)\);" % inst, cut, re.S)
        assert m, inst
        p = re.sub(r"\s+", "", m.group(1))
        assert ".clk(user_clk)" in p and ".rst_n(user_rst_n)" in p
        assert ".s_m2s_i(noc_s_req[%d])" % k in p and ".s_s2m_o(noc_s_rsp[%d])" % k in p
        assert ".m_m2s_o(%s_req)" % req in p and ".m_s2m_i(%s_rsp)" % req in p


def test_bypass_assigns():
    _, byp = _block()
    b = re.sub(r"\s+", "", byp)
    for s in ("assignperiph_s_req=noc_s_req[1];", "assignnoc_s_rsp[1]=periph_s_rsp;",
              "assignddr_s_req=noc_s_req[2];", "assignnoc_s_rsp[2]=ddr_s_rsp;"):
        assert s in b, s


def test_slaves_use_cut_nets_only():
    flat = re.sub(r"\s+", "", SV)
    assert ".s_m2s_i(periph_s_req)" in flat and ".s_s2m_o(periph_s_rsp)" in flat
    assert ".s_m2s_i(ddr_s_req)" in flat and ".s_s2m_o(ddr_s_rsp)" in flat
    code = "\n".join(l.split("//")[0] for l in SV.splitlines())
    # noc_s_req/rsp[1]/[2] appear only in the slice ports and the bypass
    for k in (1, 2):
        assert len(re.findall(r"noc_s_req\[%d\]" % k, code)) == 2, k
        assert len(re.findall(r"noc_s_rsp\[%d\]" % k, code)) == 2, k


def test_decls_before_use():
    assert SV.index("axi_m2s_t periph_s_req, ddr_s_req;") < SV.index("u_ucut_s1 (") < SV.index(") u_periph (")
    assert SV.index("u_ucut_s2 (") < SV.index(") u_ddr (")
