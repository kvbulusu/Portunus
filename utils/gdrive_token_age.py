#!/usr/bin/env python3
"""§231 Google Drive OAuth token-age alert (stdlib only).

An OAuth app in "Testing" publishing status gets refresh tokens that expire 7
days after consent (run 20261109_210551: upload failed after 19h38m with
invalid_grant). The credentials file is written once by gdrive_auth.py
(laptop, browser consent) and copied to the box; nothing in the flow rewrites
it (notification.py only reads it and refreshes the access token in memory),
so its mtime == the last re-auth/copy time.

check() emails ONE alert per day once the file is >= JANUS_GDRIVE_TOKEN_WARN_DAYS
(default 6) days old, so there is ~1 day to re-run gdrive_auth.py before the
token dies. It never raises and never blocks for long (SMTP timeout 20 s; the
agent calls it from a daemon thread).

Paths: creds = arg, else $JANUS_GDRIVE_CREDENTIALS, else
~/.config/janus_gdrive_credentials.json (same lookup as notification.py).
Dedupe stamp: $JANUS_GDRIVE_AGE_STAMP, else ~/.config/janus_gdrive_age_alert.stamp
(holds the creds mtime it alerted for; a re-auth changes the mtime -> reset).

Knobs: JANUS_GDRIVE_TOKEN_WARN_DAYS (6; 0 = off), JANUS_GDRIVE_AGE_ALERT_EVERY_H (24).
Statuses: off | missing | service_account | ok | deduped | alerted | send_failed.

CLI (cron-able):  python3 gdrive_token_age.py [--creds F] [--dry-run] [--force]
"""
from __future__ import annotations

import argparse
import json
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from typing import Callable, Optional

DEFAULT_CREDS = os.path.expanduser("~/.config/janus_gdrive_credentials.json")
DEFAULT_STAMP = os.path.expanduser("~/.config/janus_gdrive_age_alert.stamp")
TESTING_TOKEN_DAYS = 7.0


def _env_f(name: str, dflt: float) -> float:
    try:
        return float(os.environ.get(name, "") or dflt)
    except ValueError:
        return dflt


def creds_path(path: Optional[str] = None) -> str:
    return path or os.environ.get("JANUS_GDRIVE_CREDENTIALS") or DEFAULT_CREDS


def stamp_path(path: Optional[str] = None) -> str:
    return path or os.environ.get("JANUS_GDRIVE_AGE_STAMP") or DEFAULT_STAMP


def token_age_days(path: str, now: Optional[float] = None) -> Optional[float]:
    try:
        return ((now or time.time()) - os.path.getmtime(path)) / 86400.0
    except OSError:
        return None


def smtp_send(subject: str, body: str, to: str = "") -> bool:
    """Plain SMTP from the same env names notification.py uses. Never raises."""
    host = os.environ.get("SMTP_HOST") or "smtp.gmail.com"
    user = os.environ.get("SMTP_USER", "")
    pw = os.environ.get("SMTP_PASS", "")
    to = (to or os.environ.get("NOTIFY_EMAIL") or os.environ.get("JANUS_MAIL_REPLY_TO")
          or user)
    if not (user and pw and to):
        return False
    try:
        port = int(os.environ.get("SMTP_PORT") or 587)
        msg = MIMEText(body)
        msg["Subject"], msg["From"], msg["To"] = subject, user, to
        s = (smtplib.SMTP_SSL(host, port, timeout=20) if port == 465
             else smtplib.SMTP(host, port, timeout=20))
        if port != 465:
            s.starttls()
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())
        s.quit()
        return True
    except Exception:
        return False


def message(path: str, age: float, mtime: float) -> tuple:
    exp = time.strftime("%Y-%m-%d %H:%M %Z",
                        time.localtime(mtime + TESTING_TOKEN_DAYS * 86400))
    subj = f"[JANUS] Google Drive token is {age:.1f} days old - refresh it"
    body = (
        f"{path} was written {age:.1f} days ago "
        f"({time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(mtime))}).\n"
        f"If the OAuth app is in 'Testing' status the refresh token dies "
        f"{TESTING_TOKEN_DAYS:.0f} days after consent, i.e. around {exp}; after that "
        "every Drive upload fails with invalid_grant.\n\n"
        "Refresh (laptop with a browser):\n"
        "  python3 asic_flow/utils/gdrive_auth.py client_secret.json\n"
        "  scp janus_gdrive_credentials.json janus:~/.config/janus_gdrive_credentials.json\n\n"
        "Permanent fix: Google Cloud Console > OAuth consent screen > PUBLISH APP "
        "(In production), or a service account + Shared Drive.\n"
        "This alert repeats at most once a day until the file is replaced "
        "(JANUS_GDRIVE_TOKEN_WARN_DAYS=0 turns it off).\n")
    return subj, body


def check(path: Optional[str] = None, *, now: Optional[float] = None,
          send: Optional[Callable[[str, str], bool]] = None,
          stamp: Optional[str] = None, force: bool = False,
          log: Optional[Callable[[str], None]] = None) -> str:
    """Never raises. Returns a status string (see module doc)."""
    log = log or (lambda _m: None)
    try:
        warn = _env_f("JANUS_GDRIVE_TOKEN_WARN_DAYS", 6.0)
        every_h = _env_f("JANUS_GDRIVE_AGE_ALERT_EVERY_H", 24.0)
        if warn <= 0:
            return "off"
        p = creds_path(path)
        now = now or time.time()
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            return "missing"
        try:
            with open(p) as fh:
                if json.load(fh).get("type") == "service_account":
                    return "service_account"  # no 7-day expiry
        except Exception:
            pass
        age = (now - mtime) / 86400.0
        if age < warn and not force:
            return "ok"
        sp = stamp_path(stamp)
        if not force:
            try:
                with open(sp) as fh:
                    st = json.load(fh)
                if (abs(float(st.get("creds_mtime", -1)) - mtime) < 1.0
                        and now - float(st.get("sent_at", 0)) < every_h * 3600):
                    return "deduped"
            except Exception:
                pass
        subj, body = message(p, age, mtime)
        ok = (send or smtp_send)(subj, body)
        if not ok:
            log(f"gdrive token age {age:.1f} d: alert email NOT sent")
            return "send_failed"
        try:
            os.makedirs(os.path.dirname(sp) or ".", exist_ok=True)
            with open(sp, "w") as fh:
                json.dump({"creds": p, "creds_mtime": mtime, "sent_at": now,
                           "age_days": round(age, 2)}, fh)
        except OSError as e:
            log(f"gdrive token age: stamp write failed: {e}")
        log(f"gdrive token age {age:.1f} d >= {warn:g}: alert sent")
        return "alerted"
    except Exception as e:  # pragma: no cover - belt and braces
        log(f"gdrive token age check error: {e}")
        return "error"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--creds", default=None)
    ap.add_argument("--dry-run", action="store_true", help="report only, no email/stamp")
    ap.add_argument("--force", action="store_true", help="send now (ignore age + dedupe)")
    a = ap.parse_args(argv)
    p = creds_path(a.creds)
    age = token_age_days(p)
    print(f"{p}: " + ("missing" if age is None else f"{age:.2f} days old"))
    if a.dry_run:
        return 0
    st = check(p, force=a.force, log=print)
    print(f"status: {st}")
    return 0 if st != "send_failed" else 1


if __name__ == "__main__":
    sys.exit(main())
