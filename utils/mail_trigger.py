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

  JANUS_MAIL_LAUNCH=wsl2  (default)    janus_ec2.sh autorun -- <args>
  JANUS_MAIL_LAUNCH=local (on the box) python3 asic_flow/agent/janus_agent.py <args>
  ("ec2" is still accepted as an alias of wsl2.)

§217 standalone: on WSL2 no repo checkout is needed. Copy this one file
anywhere (e.g. ~/utils/) and keep janus_ec2.sh where you already run it. The
script is looked up in this order: $JANUS_EC2_SH, next to this file, ~/janus_ec2.sh,
~/utils/janus_ec2.sh, the repo copy (if a repo is found), then $PATH. It runs
from the script's own directory. Without a repo, options are checked against
AGENT_FLAGS / AGENT_VALUE_OPTS / AGENT_BLOCKS below (a snapshot of
janus_agent.py's argparse; a pytest fails if it drifts). Cross-option checks
(_validate_args) then happen on the box. local mode needs the repo.

§237 kill / query lines (same tag, same sender check; no cooldown):

    kill : now            stop the running flow (tmux + process groups); the box
                          idle-stops ~IDLE_MIN later unless a cmd follows
    kill : now hold       ... and touch ~/.janus_no_autostop (box stays up)
    kill : dry            list what would be killed
    query : status        flow processes + autorun log tail
    query : hold 2        worst 2 pre-repair hold paths at 4_cts, condensed
    query : hist          endpoint/startpoint histogram (4_cts_pre_repair.rpt min)
    query : ends min 2    §238 histogram over EVERY violating hold endpoint at 4_cts
                          (4_cts_pre_repair_ends.rpt): end / start / start->end prefixes
    query : setup 1 janus_07_global_route_paths.rpt
    query : grep "RSZ-0032|JANUS_CTS" 4_1_cts.log 50
    query : hooks 3_            JANUS_* lines of logs 3_*.log
    query : ls / tail FILE N / paths FILE min|max N

Order in one mail: kill -> queries -> cmd (so "kill + cmd" = kill and relaunch;
the cooldown is skipped when a kill comes first). They run via
`janus_ec2.sh remote -- ...` (wsl2) or asic_flow/utils/janus_remote.py (local);
outputs come back in the reply mail. The box is never started for them.

Every matching mail is marked read first (a mail can never start two runs),
and a short accepted/rejected reply goes to JANUS_MAIL_REPLY_TO.

Env (only SMTP_USER + SMTP_PASS are required; same names notification.py uses):
  SMTP_USER, SMTP_PASS        Gmail address + 16-char app password (IMAP login too)
  SMTP_HOST, SMTP_PORT        reply mail server (default smtp.gmail.com / 587)
  IMAP_HOST, IMAP_PORT        default imap.gmail.com / 993
  IMAP_USER, IMAP_PASS        override if the inbox differs from SMTP_USER
  JANUS_MAIL_TAG              subject tag (default "[janus-lsf]")
  JANUS_MAIL_LAUNCH           wsl2 | local (default wsl2; ec2 = alias of wsl2)
  JANUS_EC2_SH                path to janus_ec2.sh (wsl2 mode; default: searched, see above)
  JANUS_MAIL_REPLY_TO         status replies (default NOTIFY_EMAIL, else SMTP_USER)
  JANUS_MAIL_ALLOW_FROM       optional comma list of allowed senders (empty = any)
  JANUS_MAIL_COOLDOWN_MIN     refuse a new trigger within N min of the last (default 10)
  JANUS_REPO                  repo root (optional; default: this file's ../.. if it is
                              a checkout, else standalone)

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
import shutil
import smtplib
import subprocess
import sys
import time
from email.header import decode_header, make_header
from email.mime.text import MIMEText
from pathlib import Path

AGENT_REL = "asic_flow/agent/janus_agent.py"
HERE = Path(__file__).resolve().parent


def find_repo():
    """Repo root containing janus_agent.py, or None (standalone copy)."""
    cands = [os.environ.get("JANUS_REPO", "").strip()]
    if len(HERE.parents) >= 2:
        cands.append(str(HERE.parents[1]))
    for c in cands:
        if c and (Path(c).expanduser() / AGENT_REL).is_file():
            return Path(c).expanduser().resolve()
    return None


REPO = find_repo()

# §217 snapshot of janus_agent.py build_parser() for standalone validation.
# test_217 fails if it drifts from the real parser: refresh it then.
AGENT_FLAGS = frozenset("""
--resume --clean --resume-preview --status --gc --gc-dry-run --yes
--skip-stamp-check --dry-run --triage-slew --signoff-only --post-triage-slew
--estimate --no-llm --interactive --no-git-commit --git-push --reason
--no-physical --no-sram-regen --no-abstract --no-post-route-opt --eco-drv
--eco-drv-only --eco-hold-only --eco-from-preeco --abstract-only --no-debug-dump --no-debug-images --no-golden
--golden-include-heavy --no-signoff --verbose -v --tmux --gdrive-upload
--dashboard --no-signoff-sta --post-custom-on-fail --debug-on-fail
""".split())
# option: (action, nargs, type, choices)
AGENT_VALUE_OPTS = {
    '--blocks': ('store', '+', None, None),
    '--parallel': ('store', None, 'int', None),
    '--num-cores': ('store', None, 'int', None),
    '--max-retries': ('store', None, 'int', None),
    '--orfs': ('store', None, None, None),
    '--rtl-root': ('store', None, None, None),
    '--run-dir': ('store', None, None, None),
    '--variant': ('store', None, None, None),
    '--extra-make': ('store', '*', None, None),
    '--variants': ('store', '+', None, None),
    '--variants-reserve-cores': ('store', None, 'int', None),
    '--manifest': ('store', None, None, None),
    '--force-blocks': ('store', '+', None, None),
    '--resume-from': ('store', None, None,
                      ('1_synth', '2_floorplan', '3_place', '4_cts', '5_route', '6_final')),
    '--git-sync-branches': ('store', '+', None, None),
    '--reports-dir': ('store', None, None, None),
    '--drc-handful': ('store', None, 'int', None),
    '--golden-dir': ('store', None, None, None),
    '--golden-wns-margin-ps': ('store', None, 'float', None),
    '--golden-hold-margin-ps': ('store', None, 'float', None),
    '--signoff-stage': ('store', None, None, ('rcx', 'lvs', 'both')),
    '--signoff-timeout': ('store', None, 'int', None),
    '--llm-interval': ('store', None, 'float', None),
    '--llm-retries': ('store', None, 'int', None),
    '--heartbeat': ('store', None, 'float', None),
    '--progress': ('store', None, None, ('auto', 'dashboard', 'plain', 'quiet')),
    '--notify-email': ('store', None, None, None),
    '--smtp-host': ('store', None, None, None),
    '--smtp-port': ('store', None, 'int', None),
    '--smtp-user': ('store', None, None, None),
    '--smtp-pass': ('store', None, None, None),
    '--gdrive-creds': ('store', None, None, None),
    '--dashboard-port': ('store', None, 'int', None),
    '--dashboard-host': ('store', None, 'str', None),
    '--skip': ('extend', '+', None,
               ('llm', 'git-commit', 'macro-reshape', 'physical', 'sram', 'abstract',
                'post-route-opt', 'debug-dump', 'debug-images', 'golden', 'signoff',
                'signoff-sta', 'stamp-check')),
    '--adaptive': ('store', None, None, ('off', 'advise', 'auto')),
    '--adaptive-max-passes': ('store', None, 'int', None),
    '--adaptive-thresholds': ('store', None, None, None),
    '--stop-flow-at': ('store', None, None,
                       ('synth', 'floorplan', 'place', 'cts', 'grt', 'route')),
    '--post-custom-script': ('store', None, None, None),
    '--post-custom-args': ('store', None, None, None),
    '--post-custom-env': ('append', None, None, None),
    '--post-custom-timeout': ('store', None, 'float', None),
    '--auto-stop-idle': ('store', None, 'int', None),
}
AGENT_BLOCKS = ('security', 'noc', 'ampo', 'agent_domain', 'user_core_cva6',
                'user_core_ara_lane', 'user_core_ara', 'user_core', 'user_domain',
                'janus_soc_top')


def spec_parser():
    """argparse rebuilt from the snapshot (same nargs/types/choices)."""
    p = argparse.ArgumentParser(prog="janus_agent.py", add_help=False,
                                allow_abbrev=False)
    for o in sorted(AGENT_FLAGS):
        if o != "-v":
            p.add_argument(o, *(["-v"] if o == "--verbose" else []), action="store_true")
    types = {"int": int, "float": float, "str": str, None: None}
    for o, (act, nargs, typ, ch) in AGENT_VALUE_OPTS.items():
        kw = {"action": act, "type": types[typ]}
        if nargs is not None:
            kw["nargs"] = nargs
        if ch:
            kw["choices"] = list(ch)
        p.add_argument(o, **kw)
    return p


def find_ec2_sh():
    """janus_ec2.sh for wsl2 mode, or None."""
    cands = [os.environ.get("JANUS_EC2_SH", "").strip(), str(HERE / "janus_ec2.sh"),
             "~/janus_ec2.sh", "~/utils/janus_ec2.sh"]
    if REPO:
        cands.append(str(REPO / "asic_flow/scripts/janus_ec2.sh"))
    cands.append(shutil.which("janus_ec2.sh") or "")
    for c in cands:
        if c and Path(c).expanduser().is_file():
            return Path(c).expanduser().resolve()
    return None


STATE = Path(os.environ.get("JANUS_MAIL_STATE",
                            Path.home() / ".janus_mail_trigger.json"))
LOG = Path(os.environ.get("JANUS_MAIL_LOG",
                          Path.home() / ".janus_mail_trigger.log"))

META = set(";|&$`<>(){}\\\n\r")
DENY_OPTS = {"--smtp-host", "--smtp-port", "--smtp-user", "--smtp-pass",
             "--gdrive-creds", "--interactive", "--dashboard-host", "--help", "-h"}
CMD_RE = re.compile(r"^\s*cmd\s*:\s*(.*)$", re.I)
KILL_RE = re.compile(r"^\s*kill\s*:\s*(.*)$", re.I)
QUERY_RE = re.compile(r"^\s*query\s*:\s*(.*)$", re.I)
# §237 snapshot of janus_remote.py ACTIONS (it re-validates everything)
QUERY_ACTIONS = ("status", "ls", "hold", "setup", "paths", "hist", "ends", "hooks", "grep",
                 "tail")
REMOTE_TIMEOUT_S = 240


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


def _quoted_or_sig(s):
    return s.startswith(">") or s == "--" or s.startswith("-- ") or bool(re.match(r"^On .* wrote:$", s))


def extract_actions(body):
    """§237 -> (kill words list or None, [query strings]) from the body,
    stopping at the first quoted / signature line (never re-runs a quoted mail)."""
    kill, queries = None, []
    for ln in body.splitlines():
        s = ln.strip()
        if _quoted_or_sig(s):
            break
        m = KILL_RE.match(ln)
        if m and kill is None:
            kill = m.group(1).lower().split()
            continue
        m = QUERY_RE.match(ln)
        if m and m.group(1).strip():
            queries.append(m.group(1).strip())
    return kill, queries[:10]


def validate_kill(words):
    """-> (janus_remote args, None) or (None, reason)."""
    w = set(words or [])
    if not w or not w <= {"now", "yes", "run", "hold", "dry"}:
        return None, "kill line must be 'kill : now' (optional 'hold') or 'kill : dry'"
    args = ["kill"]
    if "hold" in w:
        args.append("--hold")
    if "dry" in w:
        args.append("--dry-run")
    return args, None


def validate_query(q):
    """-> (janus_remote args, None) or (None, reason). Never shelled locally;
    janus_ec2.sh %q-quotes for the ssh hop and janus_remote.py re-validates."""
    if any(c in q for c in "\n\r\0`"):
        return None, f"bad characters in query {q!r}"
    try:
        tok = shlex.split(q)
    except ValueError as e:
        return None, f"cannot split query {q!r}: {e}"
    if not tok or tok[0].lower() not in QUERY_ACTIONS:
        return None, f"query must start with one of: {' '.join(QUERY_ACTIONS)}"
    tok[0] = tok[0].lower()
    files = tok[2:3] if tok[0] == "grep" else tok[1:]
    for t in files:
        if "/" in t or t.startswith((".", "~")):
            return None, f"query file/arg must be a plain basename: {t!r}"
    return tok, None


def _agent_module():
    if REPO is None:
        return None
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


def validate_cmd(cmd, parser="auto"):
    """-> (agent_args list, None) or (None, reason).
    parser: "auto" = janus_agent's own if a repo is found, else the snapshot;
    None = always the snapshot (standalone)."""
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
    ja = _agent_module() if parser is not None else None
    if parser is None:                       # standalone: snapshot of the parser
        parser = spec_parser()
    try:
        parser.exit = lambda status=0, message=None: (_ for _ in ()).throw(
            ValueError((message or "").strip() or f"exit {status}"))
        parser.error = lambda message: (_ for _ in ()).throw(ValueError(message))
        ns = parser.parse_args(args)
    except ValueError as e:
        msg = str(e)
        if msg.startswith("unrecognized arguments"):
            msg = "unknown option(s)/argument(s): " + msg.split(":", 1)[-1].strip()
        return None, f"janus_agent.py rejects the options: {msg}"
    graph = getattr(ja, "DEFAULT_GRAPH", None) if ja else AGENT_BLOCKS
    bad = [b for b in (getattr(ns, "blocks", None) or []) if graph and b not in graph]
    if bad:
        return None, f"unknown block(s): {', '.join(bad)}"
    if ja is not None:                       # cross-option checks (repo only)
        try:
            if hasattr(ja, "_apply_skips"):
                ja._apply_skips(ns)
            errs = ja._validate_args(ns)[0] if hasattr(ja, "_validate_args") else []
        except Exception:
            errs = []
        if errs:
            return None, "janus_agent.py rejects the options: " + "; ".join(errs)
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


MODES = {"wsl2": "wsl2", "ec2": "wsl2", "local": "local"}


def norm_mode(m):
    return MODES.get((m or "wsl2").strip().lower())


def launch_target(mode):
    """-> (script Path, cwd Path) or raises RuntimeError with a fix hint."""
    if norm_mode(mode) == "local":
        if REPO is None:
            raise RuntimeError("JANUS_MAIL_LAUNCH=local needs the repo: set JANUS_REPO "
                               f"to the checkout containing {AGENT_REL}")
        return REPO / AGENT_REL, REPO
    sh = find_ec2_sh()
    if sh is None:
        raise RuntimeError("janus_ec2.sh not found: set JANUS_EC2_SH=/path/to/janus_ec2.sh "
                           "(searched: next to this file, ~/janus_ec2.sh, ~/utils/, $PATH)")
    return sh, sh.parent


def launch_argv(args, mode):
    script, _ = launch_target(mode)
    if norm_mode(mode) == "local":
        return [sys.executable, str(script)] + args
    return ["bash", str(script), "autorun", "--"] + args


def launch(args, mode):
    """Detached; returns (ok, info)."""
    try:
        argv = launch_argv(args, mode)
        cwd = launch_target(mode)[1]
    except RuntimeError as e:
        return False, str(e)
    out = Path.home() / "janus_mail_runs"
    out.mkdir(exist_ok=True)
    lf = out / time.strftime("%Y%m%d_%H%M%S.log")
    with lf.open("w") as f:
        p = subprocess.Popen(argv, cwd=str(cwd), stdout=f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(5)
    rc = p.poll()
    if rc not in (None, 0):
        tail = lf.read_text(errors="replace")[-1500:]
        return False, f"launcher exited rc={rc} (log {lf}):\n{tail}"
    return True, f"pid {p.pid}, log {lf}"


def remote_argv(args, mode):
    """§237 argv running janus_remote.py <args> on the box."""
    if norm_mode(mode) == "local":
        if REPO is None:
            raise RuntimeError("local mode needs the repo (JANUS_REPO)")
        return [sys.executable, str(REPO / "asic_flow/utils/janus_remote.py")] + args
    return ["bash", str(launch_target(mode)[0]), "remote", "--"] + args


def remote(args, mode):
    """Synchronous; -> (ok, output)."""
    try:
        argv = remote_argv(args, mode)
        cwd = REPO if norm_mode(mode) == "local" else launch_target(mode)[1]
    except RuntimeError as e:
        return False, str(e)
    envd = dict(os.environ)
    if norm_mode(mode) == "local" and REPO is not None:
        envd["JANUS_REPO"] = str(REPO)
    try:
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                           stdin=subprocess.DEVNULL, timeout=REMOTE_TIMEOUT_S, env=envd)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {REMOTE_TIMEOUT_S}s"
    except OSError as e:
        return False, str(e)
    out = (r.stdout + (("\n[stderr]\n" + r.stderr[-3000:]) if r.stderr.strip() else "")).strip()
    return r.returncode == 0, out[-60000:] if len(out) > 60000 else out


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
    body = body_text(msg)
    kill, queries = extract_actions(body)
    cmd = extract_cmd(body)
    if kill is None and not queries:
        args, why = validate_cmd(cmd)
        if why:
            return "reject", why
        return _launch_step(args, cfg, st, dry, after_kill=False)
    # §237: validate everything first; nothing runs if any line is bad
    steps = []
    if kill is not None:
        a, why = validate_kill(kill)
        if why:
            return "reject", why
        steps.append(("kill", a))
    for q in queries:
        a, why = validate_query(q)
        if why:
            return "reject", why
        steps.append(("query", a))
    args = None
    if cmd:
        args, why = validate_cmd(cmd)
        if why:
            return "reject", why
    parts, ok_all, killed = [], True, False
    for kind, a in steps:
        try:
            shown = " ".join(shlex.quote(x) for x in remote_argv(a, cfg["mode"]))
        except RuntimeError as e:
            return "error", str(e)
        if dry:
            parts.append(f"[{kind}] would run: {shown}")
            continue
        ok, out = remote(a, cfg["mode"])
        ok_all &= ok
        killed |= kind == "kill" and ok and "--dry-run" not in a
        parts.append(f"===== {kind}: {' '.join(a)}  ({'ok' if ok else 'FAILED'})\n{out}")
    if kill is not None and not dry and not killed and args:
        parts.append("===== cmd NOT launched: the kill step failed")
        return "error", "\n\n".join(parts)
    if args:
        status, detail = _launch_step(args, cfg, st, dry, after_kill=killed or (dry and kill is not None))
        parts.append(f"===== cmd ({status})\n{detail}")
        ok_all &= status in ("launched", "dry")
    if dry:
        return "dry", "\n".join(parts)
    return ("done" if ok_all else "error"), "\n\n".join(parts)


def _launch_step(args, cfg, st, dry, after_kill):
    last = st.get("last_launch", 0)
    if not dry and not after_kill and time.time() - last < cfg["cooldown"] * 60:
        return "reject", (f"cooldown: last run launched "
                          f"{int((time.time() - last) / 60)} min ago (< {cfg['cooldown']})")
    try:
        shown = " ".join(shlex.quote(a) for a in launch_argv(args, cfg["mode"]))
    except RuntimeError as e:
        return "error", str(e)
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
            word2 = {"launched": "STARTED", "reject": "REJECTED",
                     "done": "DONE"}.get(status, "FAILED")
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
        "mode": norm_mode(env("JANUS_MAIL_LAUNCH", "wsl2")),
        "reply_to": env("JANUS_MAIL_REPLY_TO", env("NOTIFY_EMAIL", env("SMTP_USER"))),
        "allow": env("JANUS_MAIL_ALLOW_FROM"),
        "cooldown": float(env("JANUS_MAIL_COOLDOWN_MIN", "10")),
    }


def _gdrive_age_tick(dry=False):
    """§231: daily Drive token-age alert when a creds copy lives on this host
    (gdrive_token_age.py next to this file; silent if absent). Never raises."""
    if dry:
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import gdrive_token_age as gta
        st = gta.check()
        if st in ("alerted", "send_failed"):
            log(f"gdrive token age alert: {st}")
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--once", action="store_true", help="poll once and exit (cron)")
    ap.add_argument("--interval", type=int, default=60, help="loop poll period, s")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would launch; do not mark read, launch or reply")
    ap.add_argument("--check", action="store_true", help="test IMAP+SMTP login and exit")
    a = ap.parse_args(argv)
    cfg = config()
    if cfg["mode"] is None:
        sys.exit("JANUS_MAIL_LAUNCH must be wsl2 (default) or local")
    try:
        script, cwd = launch_target(cfg["mode"])
    except RuntimeError as e:
        sys.exit(str(e))
    where = (f"launcher={script} cwd={cwd} | validation="
             f"{'janus_agent.py argparse (' + str(REPO) + ')' if REPO else 'built-in snapshot (standalone)'}")
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
              f" allow={cfg['allow'] or 'ANY'}\n{where}")
        return 0
    log(f"mail_trigger up: tag={cfg['tag']} mode={cfg['mode']} allow={cfg['allow'] or 'ANY'}"
        f"{' DRY-RUN' if a.dry_run else ''} | {where}")
    while True:
        _gdrive_age_tick(a.dry_run)
        try:
            poll(cfg, a.dry_run)
        except Exception as e:      # network blips: keep the loop alive
            log(f"WARN poll failed: {type(e).__name__}: {e}")
        if a.once or a.dry_run:
            return 0
        time.sleep(max(15, a.interval))


if __name__ == "__main__":
    sys.exit(main())
