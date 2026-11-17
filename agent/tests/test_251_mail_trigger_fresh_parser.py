"""§251: the looping mail listener must validate against janus_agent.py as it is
on disk now (a git pull adds flags), not the copy imported at its first mail."""
import importlib.util
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "asic_flow" / "utils" / "mail_trigger.py"
AGENT = ROOT / "asic_flow" / "agent" / "janus_agent.py"
CMD = "asic_flow/agent/janus_agent.py --blocks janus_soc_top --resume --eco-hold-only --eco-from-preeco"


def _fake_repo(tmp_path, agent_text):
    ign = shutil.ignore_patterns("tests", "__pycache__", "*.log")
    for d in ("agent", "utils"):
        shutil.copytree(ROOT / "asic_flow" / d, tmp_path / "asic_flow" / d, ignore=ign)
    a = tmp_path / "asic_flow" / "agent" / "janus_agent.py"
    a.write_text(agent_text)
    spec = importlib.util.spec_from_file_location("mt251", tmp_path / "asic_flow" / "utils" / "mail_trigger.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, a


def test_pull_adds_flag_without_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("JANUS_REPO", raising=False)
    real = AGENT.read_text()
    old = real.replace('"--eco-from-preeco"', '"--eco-NOTYET-preeco"')
    assert old != real
    m, a = _fake_repo(tmp_path, old)
    assert m.REPO == tmp_path.resolve()
    args, why = m.validate_cmd(CMD)
    assert args is None and "--eco-from-preeco" in why
    assert "validated by:" in why and str(a) in why
    a.write_text(real)                                  # the `git pull`
    st = a.stat()
    os.utime(a, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    args, why = m.validate_cmd(CMD)
    assert why is None and "--eco-from-preeco" in args


def test_shared_janus_agent_module_untouched():
    sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
    import mail_trigger as T
    before = sys.modules.get("janus_agent")
    assert T._agent_module() is not None
    assert sys.modules.get("janus_agent") is before
    assert T._agent_module() is T._agent_module()       # cached while unchanged


def test_snapshot_reject_names_snapshot():
    sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
    import mail_trigger as T
    args, why = T.validate_cmd("asic_flow/agent/janus_agent.py --blocks janus_soc_top --bogus", parser=None)
    assert args is None and "built-in snapshot" in why
