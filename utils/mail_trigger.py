#!/usr/bin/env python3
"""§213 e-mail run trigger (stdlib only). Polls a Gmail inbox over IMAP and
launches a JANUS run from a mail whose subject contains the tag (default
"[janus-lsf]", case-insensitive) and whose body has one line

    cmd : asic_flow/agent/janus_agent.py --blocks janus_soc_top --stop-flow-at cts ...

The command is NEVER handed to a shell. It is shlex-split, must start with
asic_flow/agent/janus_agent.py (optionally "python3 "), and every option must
be one janus_agent.py really accepts (checked with its own argparse). Shell
metacharacters, absolute/~/.. paths and the SMTP/creds/interactive options
are refused. Then it is launched detached:

  JANUS_MAIL_LAUNCH=ec2   (default, for WSL) asic_flow/scripts/janus_ec2.sh autorun -- <args>
  JANUS_MAIL_LAUNCH=local (on the box)       nohup python3 asic_flow/agent/janus_agent.py <args>

Every matching mail is marked read first (a mail can never start two runs),
and a short accepted/rejected reply goes to JANUS_MAIL_REPLY_TO.

Env (only SMTP_USER + SMTP_PASS are required; same names notification.py uses):
  SMTP_USER, SMTP_PASS        Gmail address + 16-char app password (IMAP login too)
  SMTP_HOST, SMTP_PORT        reply mail server (default smtp.gmail.com / 587)
  IMAP_HOST, IMAP_PORT        default imap.gmail.com / 993
  IMAP_USER, IMAP_PASS        override if the inbox differs from SMTP_USER
  JANUS_MAIL_TAG              subject tag (default "[janus-lsf]")
  JANUS_MAIL_LAUNCH           ec2 | local (default ec2)
  JANUS_MAIL_REPLY_TO         status replies (default NOTIFY_EMAIL, else SMTP_USER)
  JANUS_MAIL_ALLOW_FROM       optional comma list of allowed senders (empty = any)
  JANUS_MAIL_COOLDOWN_MIN     refuse a new trigger within N min of the last (default 10)
  JANUS_REPO                  repo root (default: this file's ../..)

CLI: mail_trigger.py [--once | --interval SEC] [--dry-run] [--check]

Start it from the shell that HAS the env vars (cron does not inherit them):
  python3 asic_flow/utils/mail_trigger.py --check          # logins OK?
  python3 asic_flow/utils/mail_trigger.py --dry-run        # what would launch
  nohup python3 asic_flow/utils/mail_trigger.py >/dev/null 2>&1 &   # poll every 60 s
Log ~/.janus_mail_trigger.log, launcher output ~/janus_mail_runs/, state
~/.janus_mail_trigger.json (delete it to re-baseline). The first poll only
records mails already in the inbox; nothing launches from them.
"""
import argparse
import email
import email.utils
import imaplib
import json
import os
import re
import shlex
import smtplib
import subprocess
import sys
import time
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from pathlib import Path

REPO = Path(os.environ.get("JANUS_REPO") or Path(__file__).resolve().parents[2])
AGENT_REL = "asic_flow/agent/janus_agent.py"
STATE = Path(os.environ.get("JANUS_MAIL_STATE",
                            Path.home() / ".janus_mail_trigger.json"))
LOG = Path(os.environ.get("JANUS_MAIL_LOG",
                          Path.home() / ".janus_mail_trigger.log"))

META = set(";|&$`<>(){}\\\n\r")
DENY_OPTS = {"--smtp-host", "--smtp-port", "--smtp-user", "--smtp-pass",
             "--gdrive-creds", "--interactive", "--dashboard-host", "--help", "-h"}
CMD_RE = re.compile(r"^\s*cmd\s*:\s*(.*)$", re.I)


def env(k, d=""):
    return (os.environ.get(k) or d).strip()


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        with LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- parsing ---
def subject_of(msg):
    try:
        return str(make_header(decode_header(msg.get("Subject", ""))))
    except Exception:
        return msg.get("Subject", "")


def body_text(msg):
    """text/plain part (HTML stripped as a fallback), NBSP -> space."""
    html = None
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        ct = part.get_content_type()
        try:
            txt = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if ct == "text/plain":
            return txt.replace("\xa0", " ")
        if ct == "text/html" and html is None:
            html = txt
    if html is None:
        return ""
    html = re.sub(r"(?i)<br\s*/?>|</(p|div)>", "\n", html)
    html = re.sub(r"<[^>]+>", "", html)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&#39;", "'"), ("&amp;", "&")):
        html = html.replace(a, b)
    return html.replace("\xa0", " ")


def extract_cmd(body):
    """The 'cmd :' line plus soft-wrapped continuation lines (mail clients
    wrap at ~76 chars) up to a blank line / quote / signature."""
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        m = CMD_RE.match(ln)
        if not m:
            continue
        parts = [m.group(1).strip()]
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if not s or s.startswith(">") or s == "--" or s.startswith("-- ") \
                    or re.match(r"^On .* wrote:$", s) or CMD_RE.match(s):
                break
            # continuation only if it is an option, or a value whose option
            # was the last token before the client's soft wrap
            last = " ".join(parts).split()[-1:] or [""]
            if not (s.startswith("-") or last[0].startswith("-")):
                break
            parts.append(s)
        return " ".join(p for p in parts if p)
    return None


def _agent_module():
    try:
        sys.path.insert(0, str(REPO / "asic_flow" / "agent"))
        import janus_agent  # noqa: E402
        return janus_agent
    except Exception:
        return None


def _agent_parser():
    """janus_agent's own argparse (exact validation); None if not importable."""
    ja = _agent_module()
    try:
        return ja.build_parser() if ja else None
    except Exception:
        return None


def _agent_opts():
    src = (REPO / AGENT_REL).read_text(errors="replace")
    return set(re.findall(r"""add_argument\(\s*['"](--[a-z0-9-]+)['"]""", src))


def validate_cmd(cmd, parser="auto"):
    """-> (agent_args list, None) or (None, reason)."""
    if not cmd:
        return None, "no 'cmd :' line in the body"
    bad = sorted(set(cmd) & META)
    if bad:
        return None, f"shell metacharacters not allowed: {' '.join(map(repr, bad))}"
    try:
        tok = shlex.split(cmd)
    except ValueError as e:
        return None, f"cannot split command: {e}"
    if tok and re.fullmatch(r"python3?", tok[0]):
        tok = tok[1:]
    if not tok or tok[0].lstrip("./") != AGENT_REL:
        return None, f"command must start with {AGENT_REL}"
    args = tok[1:]
    if not args:
        return None, "no agent options given"
    for a in args:
        opt = a.split("=", 1)[0]
        if opt in DENY_OPTS:
            return None, f"option {opt} is not allowed by mail"
        val = a.split("=", 1)[1] if a.startswith("-") and "=" in a else \
            ("" if a.startswith("-") else a)
        if val.startswith(("/", "~")) or ".." in val.split("/"):
            return None, f"absolute/home/parent paths not allowed: {val}"
    if parser == "auto":
        parser = _agent_parser()
    if parser is not None:
        try:
            parser.exit = lambda status=0, message=None: (_ for _ in ()).throw(
                ValueError((message or "").strip() or f"exit {status}"))
            parser.error = lambda message: (_ for _ in ()).throw(ValueError(message))
            ns = parser.parse_args(args)
        except ValueError as e:
            return None, f"janus_agent.py rejects the options: {e}"
        ja = _agent_module()
        if ja is not None:
            graph = getattr(ja, "DEFAULT_GRAPH", None)
            bad = [b for b in (getattr(ns, "blocks", None) or []) if graph and b not in graph]
            if bad:
                return None, f"unknown block(s): {', '.join(bad)}"
            try:
                if hasattr(ja, "_apply_skips"):
                    ja._apply_skips(ns)
                errs = ja._validate_args(ns)[0] if hasattr(ja, "_validate_args") else []
            except Exception:
                errs = []
            if errs:
                return None, "janus_agent.py rejects the options: " + "; ".join(errs)
    else:
        known = _agent_opts()
        for a in args:
            if a.startswith("--") and a.split("=", 1)[0] not in known:
                return None, f"unknown janus_agent.py option {a.split('=', 1)[0]}"
    return args, None


def sender_ok(from_hdr, allow):
    if not allow:
        return True
    addr = email.utils.parseaddr(from_hdr or "")[1].lower()
    return addr in {a.strip().lower() for a in allow.split(",") if a.strip()}


# ---------------------------------------------------------------- actions ---
def load_state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st):
    try:
        STATE.write_text(json.dumps(st, indent=1))
    except OSError as e:
        log(f"WARN cannot write {STATE}: {e}")


def launch_argv(args, mode):
    if mode == "local":
        return [sys.executable, str(REPO / AGENT_REL)] + args
    return ["bash", str(REPO / "asic_flow/scripts/janus_ec2.sh"), "autorun", "--"] + args


def launch(args, mode):
    """Detached; returns (ok, info)."""
    argv = launch_argv(args, mode)
    out = Path.home() / "janus_mail_runs"
    out.mkdir(exist_ok=True)
    lf = out / time.strftime("%Y%m%d_%H%M%S.log")
    with lf.open("w") as f:
        p = subprocess.Popen(argv, cwd=str(REPO), stdout=f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(5)
    rc = p.poll()
    if rc not in (None, 0):
        tail = lf.read_text(errors="replace")[-1500:]
        return False, f"launcher exited rc={rc} (log {lf}):\n{tail}"
    return True, f"pid {p.pid}, log {lf}"


def reply(to, subject, text):
    host, user, pw = env("SMTP_HOST", "smtp.gmail.com"), env("SMTP_USER"), env("SMTP_PASS")
    if not (to and user and pw):
        return
    m = MIMEText(text)
    m["Subject"], m["From"], m["To"] = subject, user, to
    m["X-Janus-Mail-Trigger"] = "reply"     # never re-parsed as a trigger
    try:
        with smtplib.SMTP(host, int(env("SMTP_PORT", "587")), timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(m)
    except Exception as e:
        log(f"WARN reply mail failed: {e}")


def handle(msg, cfg, st, dry=False):
    """Decide + act on one message. Returns (status, detail)."""
    subj, frm = subject_of(msg), msg.get("From", "")
    if cfg["tag"].lower() not in subj.lower():
        return "skip", "tag not in subject"
    if msg.get("X-Janus-Mail-Trigger"):
        return "skip", "own status reply"
    if not sender_ok(frm, cfg["allow"]):
        return "reject", f"sender {frm} not in JANUS_MAIL_ALLOW_FROM"
    args, why = validate_cmd(extract_cmd(body_text(msg)))
    if why:
        return "reject", why
    last = st.get("last_launch", 0)
    if not dry and time.time() - last < cfg["cooldown"] * 60:
        return "reject", (f"cooldown: last run launched "
                          f"{int((time.time() - last) / 60)} min ago (< {cfg['cooldown']})")
    shown = " ".join(shlex.quote(a) for a in launch_argv(args, cfg["mode"]))
    if dry:
        return "dry", shown
    ok, info = launch(args, cfg["mode"])
    if ok:
        st["last_launch"] = time.time()
        return "launched", f"{shown}\n{info}"
    return "error", info


def poll(cfg, dry=False):
    st = load_state()
    user, pw = env("IMAP_USER", env("SMTP_USER")), env("IMAP_PASS", env("SMTP_PASS"))
    M = imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.gmail.com"), int(env("IMAP_PORT", "993")))
    try:
        M.login(user, pw)
        M.select("INBOX")
        # Read/unread is NOT used to select (Gmail marks a mail read when you open
        # it, or self-sent mail may arrive read) -> last 2 days by subject, dedup by
        # Message-ID in STATE. First ever poll only baselines what is already there.
        word = cfg["tag"].strip("[]")
        since = time.strftime("%d-%b-%Y", time.localtime(time.time() - 2 * 86400))
        typ, data = M.search(None, "SINCE", since, "SUBJECT", f'"{word}"')
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        baseline = "seen" not in st
        for i in ids:
            typ, d = M.fetch(i, "(BODY.PEEK[])")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            msg = email.message_from_bytes(d[0][1])
            mid = msg.get("Message-ID", "") or \
                f"{msg.get('From', '')}|{msg.get('Date', '')}|{subject_of(msg)}"
            if mid in st.get("seen", []):
                continue
            if baseline and not dry:
                st.setdefault("seen", []).append(mid)
                log(f"BASELINE (existing mail, not launched) subj={subject_of(msg)!r}")
                continue
            if not dry:            # record + mark read BEFORE acting: never a double launch
                st.setdefault("seen", []).append(mid)
                st["seen"] = st["seen"][-200:]
                save_state(st)
                M.store(i, "+FLAGS", "\\Seen")
            status, detail = handle(msg, cfg, st, dry)
            subj = subject_of(msg)
            log(f"{status.upper():8s} from={msg.get('From', '')!r} subj={subj!r}: {detail}")
            if status in ("skip", "dup") or dry:
                continue
            save_state(st)
            word2 = {"launched": "STARTED", "reject": "REJECTED"}.get(status, "FAILED")
            plain = re.sub(re.escape(cfg["tag"]), "", subj, flags=re.I).strip()
            reply(cfg["reply_to"], f"JANUS mail-trigger {word2}: {plain}",
                  f"{word2}\nfrom: {msg.get('From', '')}\nmode: {cfg['mode']}\n\n{detail}\n")
        if baseline and not dry:
            st.setdefault("seen", [])
            save_state(st)
    finally:
        try:
            M.logout()
        except Exception:
            pass


def config():
    return {
        "tag": env("JANUS_MAIL_TAG", "[janus-lsf]"),
        "mode": env("JANUS_MAIL_LAUNCH", "ec2").lower(),
        "reply_to": env("JANUS_MAIL_REPLY_TO", env("NOTIFY_EMAIL", env("SMTP_USER"))),
        "allow": env("JANUS_MAIL_ALLOW_FROM"),
        "cooldown": float(env("JANUS_MAIL_COOLDOWN_MIN", "10")),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="poll once and exit (cron)")
    ap.add_argument("--interval", type=int, default=60, help="loop poll period, s")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would launch; do not mark read, launch or reply")
    ap.add_argument("--check", action="store_true", help="test IMAP+SMTP login and exit")
    a = ap.parse_args(argv)
    cfg = config()
    if cfg["mode"] not in ("ec2", "local"):
        sys.exit("JANUS_MAIL_LAUNCH must be ec2 or local")
    if not (env("IMAP_USER", env("SMTP_USER")) and env("IMAP_PASS", env("SMTP_PASS"))):
        sys.exit("set SMTP_USER and SMTP_PASS (Gmail address + app password)")
    if a.check:
        M = imaplib.IMAP4_SSL(env("IMAP_HOST", "imap.gmail.com"), int(env("IMAP_PORT", "993")))
        M.login(env("IMAP_USER", env("SMTP_USER")), env("IMAP_PASS", env("SMTP_PASS")))
        M.logout()
        print("IMAP login OK")
        with smtplib.SMTP(env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "587")),
                          timeout=30) as s:
            s.starttls()
            s.login(env("SMTP_USER"), env("SMTP_PASS"))
        print(f"SMTP login OK  | tag={cfg['tag']} mode={cfg['mode']} reply_to={cfg['reply_to']}"
              f" allow={cfg['allow'] or 'ANY'} repo={REPO}")
        return 0
    log(f"mail_trigger up: tag={cfg['tag']} mode={cfg['mode']} allow={cfg['allow'] or 'ANY'}"
        f"{' DRY-RUN' if a.dry_run else ''}")
    while True:
        try:
            poll(cfg, a.dry_run)
        except Exception as e:      # network blips: keep the loop alive
            log(f"WARN poll failed: {type(e).__name__}: {e}")
        if a.once or a.dry_run:
            return 0
        time.sleep(max(15, a.interval))


if __name__ == "__main__":
    sys.exit(main())
