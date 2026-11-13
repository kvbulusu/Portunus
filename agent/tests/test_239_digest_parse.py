"""§239 digest parse: cell-annotated summary pins + worst-first path order."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location("janus_remote_239", ROOT / "asic_flow/utils/janus_remote.py")
jr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jr)

# verbatim shape from run 20261112_125523 4_cts_pre_repair_ends.rpt
ENDS = """## min
Startpoint                           Endpoint                               Slack
--------------------------------------------------------------------------------
u_npu_s_job_cdc.sync_q0[30]$_DFF_PN0_/QN (DFFASRHQNx1_ASAP7_75t_SL) u_npu_s_job_cdc.data_o[30]$_DFF_PN0_/D (DFFASRHQNx1_ASAP7_75t_SL) -404.81
rst_ni (input) u_periph/r_reg/D (DFFASRHQNx1_ASAP7_75t_SL) -5.00
u_jobsched/l_job_q_3_/CLK u_user/job_i[3] -300.00
## max
Startpoint                           Endpoint                               Slack
--------------------------------------------------------------------------------
u_ucut_mn.u_ar/g_ingress.in_v_q$_DFF_PN0_/QN (DFFASRHQNx1_ASAP7_75t_SL) u_ucut_mn.u_ar/s_ready_o$_DFF_PN0_/D (DFFASRHQNx1_ASAP7_75t_SL) -609.56
"""


def test_cell_annotated_pins_parse():
    r = jr.parse_ends(ENDS)
    assert len(r["min"]) == 3 and len(r["max"]) == 1
    assert r["min"][0] == ("u_npu_s_job_cdc.sync_q0[30]$_DFF_PN0_/QN",
                           "u_npu_s_job_cdc.data_o[30]$_DFF_PN0_/D", -404.81)
    assert r["min"][1][0] == "rst_ni"
    assert r["max"][0][2] == -609.56


def _path(group, slack):
    return (f"Startpoint: s_{group}\nEndpoint: e_{group}\nPath Group: {group}\n"
            f"Path Type: min\n\n  {slack:.2f}   slack (VIOLATED)\n\n")


def test_paths_worst_first(tmp_path, monkeypatch):
    f = tmp_path / "4_cts_pre_repair.rpt"
    f.write_text(_path("in2reg", 707.0) + _path("reg2reg", -405.0) + _path("reg2out", 10.0))
    monkeypatch.setattr(jr, "find_file", lambda fname, block: f)
    out = jr.q_paths(None, f.name, "min", 2)
    assert out.index("e_reg2reg") < out.index("e_reg2out")
    assert "e_in2reg" not in out
