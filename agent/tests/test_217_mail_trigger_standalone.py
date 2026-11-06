"""§217 mail_trigger: wsl2 default mode, standalone copy (no repo), parser snapshot."""
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "asic_flow" / "utils" / "mail_trigger.py"
sys.path.insert(0, str(ROOT / "asic_flow" / "agent"))
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import janus_agent  # noqa: E402
import mail_trigger as T  # noqa: E402

CMD = ("asic_flow/agent/janus_agent.py --blocks janus_soc_top --stop-flow-at cts "
       "--num-cores 14 --gdrive-upload --notify-email owner@example.com "
       "--post-custom-script asic_flow/scripts/debug/soc_cts_probe.tcl "
       "--post-custom-env SCP_N=5 --post-custom-on-fail")


def test_snapshot_matches_real_parser():
    """If this fails, janus_agent.py options changed: refresh AGENT_FLAGS /
    AGENT_VALUE_OPTS / AGENT_BLOCKS in mail_trigger.py."""
    flags, vals = set(), {}
    for a in janus_agent.build_parser()._actions:
        kind = type(a).__name__.strip("_").replace("Action", "").lower()
        if kind == "help":
            continue
        for o in a.option_strings:
            if kind == "storetrue":
                flags.add(o)
            else:
                vals[o] = (kind, a.nargs, a.type.__name__ if a.type else None,
                           tuple(a.choices) if a.choices else None)
    assert flags == set(T.AGENT_FLAGS)
    assert vals == T.AGENT_VALUE_OPTS
    assert tuple(janus_agent.DEFAULT_GRAPH) == T.AGENT_BLOCKS


def test_modes_default_wsl2_and_alias(monkeypatch):
    monkeypatch.delenv("JANUS_MAIL_LAUNCH", raising=False)
    assert T.config()["mode"] == "wsl2"
    assert T.norm_mode("EC2") == "wsl2" and T.norm_mode("local") == "local"
    assert T.norm_mode("bogus") is None


@pytest.fixture
def standalone(tmp_path, monkeypatch):
    """mail_trigger.py copied to ~/utils with no repo around it."""
    home = tmp_path / "home"
    (home / "utils").mkdir(parents=True)
    dst = home / "utils" / "mail_trigger.py"
    shutil.copy(SRC, dst)
    monkeypatch.setenv("HOME", str(home))
    for k in ("JANUS_REPO", "JANUS_EC2_SH", "JANUS_MAIL_LAUNCH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    def load():
        spec = importlib.util.spec_from_file_location("mt_standalone", dst)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m
    return home, load


def test_standalone_validation_without_repo(standalone):
    _, load = standalone
    M = load()
    assert M.REPO is None and M._agent_parser() is None
    args, why = M.validate_cmd(CMD)
    assert why is None and args[-1] == "--post-custom-on-fail"
    assert "unknown option" in M.validate_cmd(CMD + " --bogus")[1]
    assert "unknown block" in M.validate_cmd(CMD.replace("janus_soc_top", "foo"))[1]
    assert "invalid choice" in M.validate_cmd(CMD.replace("cts", "ctx", 1))[1]
    assert "invalid int" in M.validate_cmd(CMD.replace("14", "x"))[1]
    assert "not allowed" in M.validate_cmd(CMD + " --smtp-pass p")[1]


def test_users_cmd_eq_form_rejected(standalone):
    _, load = standalone
    M = load()
    body = ("cmd=./janus_ec2.sh autorun -- --blocks janus_soc_top --notify-email "
            "$SMTP_USER --post-custom-on-fail")
    assert M.validate_cmd(M.extract_cmd(body))[1].startswith("no 'cmd :'")


def test_standalone_ec2_sh_lookup(standalone, monkeypatch):
    home, load = standalone
    M = load()
    with pytest.raises(RuntimeError, match="JANUS_EC2_SH"):
        M.launch_target("wsl2")
    ok, info = M.launch(["--blocks", "noc"], "wsl2")
    assert not ok and "janus_ec2.sh not found" in info
    with pytest.raises(RuntimeError, match="JANUS_REPO"):
        M.launch_target("local")
    # ~/janus_ec2.sh (where kiran runs ./janus_ec2.sh) is found; cwd = its dir
    (home / "janus_ec2.sh").write_text("#!/bin/bash\n")
    sh, cwd = M.launch_target("wsl2")
    assert sh == (home / "janus_ec2.sh").resolve() and cwd == home.resolve()
    assert M.launch_argv(["--blocks", "noc"], "ec2") == \
        ["bash", str(sh), "autorun", "--", "--blocks", "noc"]
    # next to the copied file wins over ~
    (home / "utils" / "janus_ec2.sh").write_text("#!/bin/bash\n")
    assert M.launch_target("wsl2")[0] == (home / "utils" / "janus_ec2.sh").resolve()
    # explicit env wins over everything
    other = home / "bin" / "janus_ec2.sh"
    other.parent.mkdir()
    other.write_text("#!/bin/bash\n")
    monkeypatch.setenv("JANUS_EC2_SH", str(other))
    assert M.launch_target("wsl2") == (other.resolve(), other.parent.resolve())


def test_standalone_main_fails_fast_without_ec2_sh(standalone, monkeypatch):
    _, load = standalone
    M = load()
    monkeypatch.setenv("SMTP_USER", "a@b.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    with pytest.raises(SystemExit, match="janus_ec2.sh not found"):
        M.main(["--once"])
    monkeypatch.setenv("JANUS_MAIL_LAUNCH", "nope")
    with pytest.raises(SystemExit, match="wsl2"):
        M.main(["--once"])


def test_standalone_dry_run_handle(standalone, monkeypatch):
    home, load = standalone
    M = load()
    (home / "janus_ec2.sh").write_text("#!/bin/bash\n")
    from email.mime.text import MIMEText
    m = MIMEText(f"cmd : {CMD}")
    m["Subject"], m["From"], m["Message-ID"] = "[janus-lsf] run", "x@y.com", "<s1>"
    status, shown = M.handle(m, M.config(), {}, dry=True)
    assert status == "dry" and "autorun -- --blocks janus_soc_top" in shown


def test_repo_mode_still_uses_real_parser():
    assert T.REPO == ROOT.resolve() and T._agent_parser() is not None
    assert T.launch_target("local") == (ROOT.resolve() / T.AGENT_REL, ROOT.resolve())
