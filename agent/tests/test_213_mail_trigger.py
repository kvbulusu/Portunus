"""§213 mail_trigger: parsing, command validation, poll loop on a fake IMAP."""
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import mail_trigger as T  # noqa: E402

CMD = ("asic_flow/agent/janus_agent.py --blocks janus_soc_top --stop-flow-at cts "
       "--num-cores 14 --gdrive-upload --notify-email owner@example.com")


def mk(subject, body, html=False, mid="<a@x>", frm="someone@else.com", hdr=None):
    m = MIMEMultipart("alternative")
    m["Subject"], m["From"], m["Message-ID"] = subject, frm, mid
    for k, v in (hdr or {}).items():
        m[k] = v
    m.attach(MIMEText(body, "html" if html else "plain"))
    return m


# ------------------------------------------------------------- parsing ---
def test_extract_cmd_basic_and_case():
    assert T.extract_cmd(f"hi\nCMD :  {CMD}\n\nthanks") == CMD


def test_extract_cmd_joins_soft_wrap_and_stops_at_signature():
    body = ("cmd : asic_flow/agent/janus_agent.py --blocks janus_soc_top\n"
            "--stop-flow-at\ncts --num-cores 14\n-- \nKiran")
    assert T.extract_cmd(body) == ("asic_flow/agent/janus_agent.py --blocks "
                                   "janus_soc_top --stop-flow-at cts --num-cores 14")


def test_body_html_fallback_and_nbsp():
    m = mk("x", f"<div>cmd&nbsp;:&nbsp;{CMD}</div><div>bye</div>", html=True)
    assert T.extract_cmd(T.body_text(m)) == CMD


def test_no_cmd_line():
    assert T.validate_cmd(T.extract_cmd("hello"))[1].startswith("no 'cmd :'")


# ---------------------------------------------------------- validation ---
def test_valid_cmd_real_parser():
    args, why = T.validate_cmd(CMD)
    assert why is None and args[:2] == ["--blocks", "janus_soc_top"]


def test_python3_prefix_ok():
    assert T.validate_cmd("python3 ./" + CMD)[1] is None


@pytest.mark.parametrize("bad,frag", [
    (CMD + "; rm -rf ~", "metacharacters"),
    (CMD + " $(id)", "metacharacters"),
    (CMD + " `id`", "metacharacters"),
    ("bash -c ls", "must start with"),
    ("asic_flow/agent/other.py --blocks noc", "must start with"),
    (CMD + " --smtp-host evil.com", "not allowed"),
    (CMD + " --interactive", "not allowed"),
    (CMD + " --run-dir /etc", "absolute"),
    (CMD + " --run-dir ../../x", "parent"),
    (CMD + " --no-such-flag", "rejects"),
    (CMD.replace("janus_soc_top", "not_a_block"), "unknown block"),
    ("asic_flow/agent/janus_agent.py", "no agent options"),
])
def test_rejects(bad, frag):
    args, why = T.validate_cmd(bad)
    assert args is None and frag in why


def test_fallback_regex_when_agent_not_importable():
    assert T.validate_cmd(CMD, parser=None)[1] is None
    assert "unknown" in T.validate_cmd(CMD + " --bogus", parser=None)[1]


def test_sender_allow():
    assert T.sender_ok("X <a@b.com>", "")
    assert T.sender_ok("X <A@B.com>", "a@b.com, c@d.com")
    assert not T.sender_ok("e@f.com", "a@b.com")


def test_launch_argv_modes():
    a = ["--blocks", "noc"]
    assert T.launch_argv(a, "ec2")[-4:] == ["autorun", "--", "--blocks", "noc"]
    assert T.launch_argv(a, "local")[1].endswith("asic_flow/agent/janus_agent.py")


# ----------------------------------------------------------- poll loop ---
class FakeIMAP:
    box = []

    def __init__(self, *a):
        self.stored = []

    def login(self, u, p):
        pass

    def select(self, b):
        pass

    def search(self, *crit):
        assert "SINCE" in crit and "SUBJECT" in crit
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.box)))]

    def fetch(self, i, what):
        assert "PEEK" in what
        return "OK", [(b"1", self.box[int(i) - 1].as_bytes())]

    def store(self, i, *a):
        self.stored.append(i)

    def logout(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "STATE", tmp_path / "st.json")
    monkeypatch.setattr(T, "LOG", tmp_path / "log")
    monkeypatch.setattr(T.imaplib, "IMAP4_SSL", FakeIMAP)
    monkeypatch.setenv("SMTP_USER", "owner@example.com")
    monkeypatch.setenv("SMTP_PASS", "x")
    for k in ("JANUS_MAIL_ALLOW_FROM", "JANUS_MAIL_LAUNCH", "JANUS_MAIL_TAG",
              "JANUS_MAIL_COOLDOWN_MIN"):
        monkeypatch.delenv(k, raising=False)
    calls = {"launch": [], "reply": []}
    monkeypatch.setattr(T, "launch", lambda a, m: (calls["launch"].append((a, m)) or (True, "pid 1")))
    monkeypatch.setattr(T, "reply", lambda to, s, t: calls["reply"].append((to, s, t)))
    FakeIMAP.box = []
    return calls


def test_first_poll_baselines_then_launches_new(env):
    FakeIMAP.box = [mk("[janus-lsf] old", f"cmd : {CMD}", mid="<old>")]
    T.poll(T.config())
    assert env["launch"] == []                       # existing mail never launches
    FakeIMAP.box.append(mk("[JANUS-LSF] go", f"cmd : {CMD}", mid="<new>"))
    T.poll(T.config())
    assert len(env["launch"]) == 1 and env["launch"][0][1] == "wsl2"
    to, subj, _ = env["reply"][0]
    assert to == "owner@example.com" and "STARTED" in subj and "janus-lsf" not in subj.lower()
    T.poll(T.config())                               # same mail again -> nothing
    assert len(env["launch"]) == 1


def test_reject_replies_and_no_launch(env):
    T.save_state({"seen": []})
    FakeIMAP.box = [mk("[janus-lsf] x", f"cmd : {CMD}; reboot", mid="<r>")]
    T.poll(T.config())
    assert env["launch"] == [] and "REJECTED" in env["reply"][0][1]


def test_own_reply_ignored(env):
    T.save_state({"seen": []})
    FakeIMAP.box = [mk("[janus-lsf] x", f"cmd : {CMD}", mid="<o>",
                       hdr={"X-Janus-Mail-Trigger": "reply"})]
    T.poll(T.config())
    assert env["launch"] == [] and env["reply"] == []


def test_cooldown(env, monkeypatch):
    T.save_state({"seen": []})
    FakeIMAP.box = [mk("[janus-lsf] a", f"cmd : {CMD}", mid="<1>"),
                    mk("[janus-lsf] b", f"cmd : {CMD}", mid="<2>")]
    T.poll(T.config())
    assert len(env["launch"]) == 1 and "cooldown" in env["reply"][1][2]


def test_dry_run_touches_nothing(env):
    T.save_state({"seen": []})
    FakeIMAP.box = [mk("[janus-lsf] a", f"cmd : {CMD}", mid="<d>")]
    T.poll(T.config(), dry=True)
    assert env["launch"] == [] and env["reply"] == [] and T.load_state() == {"seen": []}


def test_local_mode_and_allow_list(env, monkeypatch):
    monkeypatch.setenv("JANUS_MAIL_LAUNCH", "local")
    monkeypatch.setenv("JANUS_MAIL_ALLOW_FROM", "owner@example.com")
    T.save_state({"seen": []})
    FakeIMAP.box = [mk("[janus-lsf] a", f"cmd : {CMD}", mid="<l1>", frm="bad@x.com"),
                    mk("[janus-lsf] b", f"cmd : {CMD}", mid="<l2>", frm="K <owner@example.com>")]
    T.poll(T.config())
    assert [m for _, m in env["launch"]] == ["local"]
    assert "not in JANUS_MAIL_ALLOW_FROM" in env["reply"][0][2]


def test_launch_detached_ok_and_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(T.Path, "home", staticmethod(lambda: tmp_path))
    real_sleep = T.time.sleep
    monkeypatch.setattr(T.time, "sleep", lambda s: real_sleep(0.5))
    monkeypatch.setattr(T, "launch_argv", lambda a, m: ["sh", "-c", "sleep 3"])
    ok, info = T.launch([], "ec2")
    assert ok and "pid" in info
    monkeypatch.setattr(T, "launch_argv", lambda a, m: ["sh", "-c", "echo boom; exit 3"])
    ok, info = T.launch([], "ec2")
    assert not ok and "rc=3" in info and "boom" in info
