"""§238 per-stage debug digest + every-violating-endpoint histogram (ends)."""
import importlib.util
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


jr = _load("janus_remote_238", "asic_flow/utils/janus_remote.py")
mt = _load("mail_trigger_238", "asic_flow/utils/mail_trigger.py")

ENDS = """## min
Startpoint                               Endpoint                                 Slack
--------------------------------------------------------------------------------------------
u_jobsched/l_job_q_47_/CLK               u_user/job_i[47]                         -325.20
u_jobsched/l_job_q_3_/CLK                u_user/job_i[3]                          -300.00
u_noc/a_reg/CLK                          u_user/noc_i[1]                          -100.00
rst_ni (input)                           u_periph/r_reg/D                         -5.00
## max
Startpoint                               Endpoint                                 Slack
--------------------------------------------------------------------------------------------
u_user/mr_q_reg/CLK                      u_noc/mr_d_reg/D                         -40.50
"""


def _dirs(tmp_path):
    rep, logs = tmp_path / "rep", tmp_path / "logs"
    rep.mkdir()
    logs.mkdir()
    (rep / "4_cts_pre_repair_ends.rpt").write_text(ENDS)
    (logs / "4_1_cts.log").write_text("JANUS_CTS_DIAG: wrote x\nother\n")
    return rep, logs


def test_parse_ends():
    e = jr.parse_ends(ENDS)
    assert len(e["min"]) == 4 and len(e["max"]) == 1
    assert e["min"][0] == ("u_jobsched/l_job_q_47_/CLK", "u_user/job_i[47]", -325.2)
    assert e["min"][3][0] == "rst_ni"


def test_q_ends_family(tmp_path):
    rep, logs = _dirs(tmp_path)
    out = jr.run(["ends", "min", "1"], jr.Ctx("janus_soc_top", str(rep), str(logs)))
    assert "4 violating min endpoint(s), TNS -730.2 ps, worst -325.2" in out
    assert "      3     -325.2      -725.2  u_user" in out
    assert "u_jobsched  ->  u_user" in out.split("-- start -> end --")[1]
    assert "1 violating max" in jr.run(["ends", "max"], jr.Ctx("x", str(rep), str(logs)))


def test_stale_skipped(tmp_path):
    rep, logs = _dirs(tmp_path)
    old = time.time() - 3600
    os.utime(rep / "4_cts_pre_repair_ends.rpt", (old, old))
    d = jr.stage_digest("4_cts", str(rep), str(logs), since=time.time() - 60)
    assert "stale" in d and "TNS" not in d and "JANUS_CTS_DIAG: wrote x" in d


def test_digest_defaults_override_off(tmp_path, monkeypatch):
    rep, logs = _dirs(tmp_path)
    d = jr.stage_digest("4_cts", str(rep), str(logs), since=time.time() - 60)
    assert "########## ends min 1" in d and "  u_jobsched  ->  u_user" in d
    assert "not found" in d                      # hold 2: 4_cts_pre_repair.rpt absent -> skipped
    monkeypatch.setenv("JANUS_STAGE_DIGEST_4_CTS", "ends min 1; kill ; hooks 4_")
    assert jr.stage_queries("4_cts") == ["ends min 1", "hooks 4_"]
    monkeypatch.setenv("JANUS_STAGE_DIGEST", "0")
    assert jr.stage_digest("4_cts", str(rep), str(logs)) == ""
    monkeypatch.delenv("JANUS_STAGE_DIGEST")
    assert jr.stage_queries("9_unknown") == []


def test_agent_stage_digest_writes_file(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT / "asic_flow/agent"))
    import janus_agent as ja
    rep, logs = _dirs(tmp_path)

    class S:
        logs_ = []

        def _notify_log(self, m):
            self.logs_.append(m)
    d = ja.JANUSAutonomousAgent._stage_digest(S(), "janus_soc_top", "4_cts", (str(logs), str(rep), None),
                                    time.time() - 60)
    assert "violating min endpoint" in d
    assert (rep / "janus_digest_4_cts.txt").read_text().startswith("########## ends min 1")
    assert ja.JANUSAutonomousAgent._stage_digest(S(), "b", "4_cts", (None, None, None), None).count("skipped") >= 1


def test_mail_ends_query_and_tcl_hook():
    assert mt.validate_query("ends min 3")[0] == ["ends", "min", "3"]
    assert mt.validate_query("ends min 2 ../x")[0] is None
    t = (ROOT / "asic_flow/scripts/hooks/janus_cts_balance.tcl").read_text()
    assert "4_cts_pre_repair_ends.rpt" in t and "-format summary" in t and "JANUS_CTS_DIAG_ENDS" in t
