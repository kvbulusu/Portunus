"""§231 Drive OAuth token-age alert (utils/gdrive_token_age.py + call sites)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "asic_flow" / "utils"))
import gdrive_token_age as gta  # noqa: E402

DAY = 86400.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("JANUS_GDRIVE_TOKEN_WARN_DAYS", "JANUS_GDRIVE_AGE_ALERT_EVERY_H",
              "JANUS_GDRIVE_CREDENTIALS", "JANUS_GDRIVE_AGE_STAMP"):
        monkeypatch.delenv(k, raising=False)
    creds = tmp_path / "janus_gdrive_credentials.json"
    creds.write_text(json.dumps({"type": "authorized_user", "refresh_token": "x"}))
    stamp = tmp_path / "stamp.json"
    sent = []

    def send(subj, body):
        sent.append((subj, body))
        return True
    return creds, stamp, sent, send


def _age(p, days, now):
    os.utime(p, (now - days * DAY, now - days * DAY))


def test_default_paths_match_notification():
    import notification
    assert gta.DEFAULT_CREDS == notification._DEFAULT_GDRIVE_CREDS
    assert gta.DEFAULT_CREDS.endswith("/.config/janus_gdrive_credentials.json")
    assert gta.DEFAULT_STAMP.endswith("/.config/janus_gdrive_age_alert.stamp")


def test_new_file_no_alert(env):
    creds, stamp, sent, send = env
    now = time.time()
    _age(creds, 2.0, now)
    assert gta.check(str(creds), now=now, send=send, stamp=str(stamp)) == "ok"
    assert not sent and not stamp.exists()


def test_old_file_alerts_then_dedupes_then_repeats_daily(env):
    creds, stamp, sent, send = env
    now = time.time()
    _age(creds, 6.2, now)
    assert gta.check(str(creds), now=now, send=send, stamp=str(stamp)) == "alerted"
    assert len(sent) == 1 and "6.2 days old" in sent[0][0]
    assert "gdrive_auth.py" in sent[0][1] and "scp" in sent[0][1]
    assert gta.check(str(creds), now=now + 3600, send=send, stamp=str(stamp)) == "deduped"
    assert gta.check(str(creds), now=now + 23 * 3600, send=send, stamp=str(stamp)) == "deduped"
    assert gta.check(str(creds), now=now + 25 * 3600, send=send, stamp=str(stamp)) == "alerted"
    assert len(sent) == 2


def test_reauth_resets(env):
    creds, stamp, sent, send = env
    now = time.time()
    _age(creds, 6.5, now)
    assert gta.check(str(creds), now=now, send=send, stamp=str(stamp)) == "alerted"
    _age(creds, 0.0, now + 60)      # gdrive_auth.py + scp rewrote it
    assert gta.check(str(creds), now=now + 120, send=send, stamp=str(stamp)) == "ok"
    _age(creds, 6.1, now + 120)     # a different old file -> new alert, not deduped
    assert gta.check(str(creds), now=now + 120, send=send, stamp=str(stamp)) == "alerted"
    assert len(sent) == 2


def test_missing_service_account_off_and_send_failure(env, monkeypatch):
    creds, stamp, sent, send = env
    now = time.time()
    assert gta.check(str(creds) + ".nope", now=now, send=send, stamp=str(stamp)) == "missing"
    sa = creds.parent / "sa.json"
    sa.write_text(json.dumps({"type": "service_account"}))
    _age(sa, 30, now)
    assert gta.check(str(sa), now=now, send=send, stamp=str(stamp)) == "service_account"
    _age(creds, 7.5, now)
    assert gta.check(str(creds), now=now, send=lambda s, b: False,
                     stamp=str(stamp)) == "send_failed"
    assert not stamp.exists()      # retried next tick, not silently stamped
    monkeypatch.setenv("JANUS_GDRIVE_TOKEN_WARN_DAYS", "0")
    assert gta.check(str(creds), now=now, send=send, stamp=str(stamp)) == "off"
    monkeypatch.setenv("JANUS_GDRIVE_TOKEN_WARN_DAYS", "8")
    assert gta.check(str(creds), now=now, send=send, stamp=str(stamp)) == "ok"
    assert not sent


def test_env_creds_path(env, monkeypatch):
    creds, stamp, sent, send = env
    monkeypatch.setenv("JANUS_GDRIVE_CREDENTIALS", str(creds))
    monkeypatch.setenv("JANUS_GDRIVE_AGE_STAMP", str(stamp))
    now = time.time()
    _age(creds, 6.0, now)
    assert gta.check(now=now, send=send) == "alerted" and stamp.exists()


def test_call_sites_wired():
    a = (ROOT / "asic_flow/agent/janus_agent.py").read_text()
    assert 'self._gdrive_age_tick("startup")' in a
    assert 'self._gdrive_age_tick(f"{block}:{stg}")' in a
    assert 'threading.Thread(target=_run, name="gdrive-age", daemon=True)' in a
    assert "gdrive_age_tick(c)" in (ROOT / "asic_flow/utils/idle_stop.py").read_text()
    assert "_gdrive_age_tick(a.dry_run)" in (ROOT / "asic_flow/utils/mail_trigger.py").read_text()
    # the flow never rewrites the creds file (its mtime == last re-auth/copy)
    n = (ROOT / "asic_flow/utils/notification.py").read_text()
    assert "to_json" not in n
    assert "creds.to_json()" in (ROOT / "asic_flow/utils/gdrive_auth.py").read_text()


def test_agent_tick_never_raises_and_threads(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "asic_flow" / "agent"))
    import janus_agent as ja
    creds = tmp_path / "c.json"
    creds.write_text("{}")
    now = time.time()
    _age(creds, 6.3, now)
    monkeypatch.setenv("JANUS_GDRIVE_AGE_STAMP", str(tmp_path / "st"))
    sent, logs = [], []

    class NT:
        gdrive_creds = str(creds)
        enabled = True
        email_configured = True

        def send_email(self, s, b):
            sent.append(s)
            return True

    class A:
        notifier = NT()
        _notify_log = staticmethod(logs.append)
    a = A()
    ja.JANUSAutonomousAgent._gdrive_age_tick(a, "startup")
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    assert sent and "6.3 days old" in sent[0]
    ja.JANUSAutonomousAgent._gdrive_age_tick(a, "x:4_cts")   # rate-limited, no 2nd thread
    time.sleep(0.1)
    assert len(sent) == 1
