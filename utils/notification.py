"""
notification.py — run-completion notifications for the JANUS ASIC flow.

Sends an email (with a compressed tarball of logs + key reports) and,
optionally, uploads that tarball to Google Drive when a block finishes or
fails. Written so kiran does not have to poll the EC2 box for hours-long runs.

Everything here is BEST-EFFORT and FAIL-SAFE: any error (missing SMTP config,
missing Google API libs, network failure, oversized attachment) is logged and
swallowed. A notification failure must NEVER crash or alter a real ORFS run.

------------------------------------------------------------------------------
CONFIGURING EMAIL (SMTP)
------------------------------------------------------------------------------
Notifications are OFF by default. Turn them on by giving a recipient and SMTP
credentials, either on the command line or via environment variables (env vars
are the safe choice for the password so it never lands in shell history / ps):

    export SMTP_HOST=smtp.gmail.com
    export SMTP_PORT=587
    export SMTP_USER=you@gmail.com
    export SMTP_PASS=<16-char Gmail App Password>     # NOT your login password
    python3 janus_agent.py --blocks user_core_ara --notify-email you@gmail.com

CLI equivalents (--smtp-host/--smtp-port/--smtp-user/--smtp-pass) override the
env vars when both are present. Gmail requires an "App Password" (Google
Account -> Security -> 2-Step Verification -> App passwords); a normal password
will be rejected. Port 587 uses STARTTLS (default); port 465 uses implicit SSL
and is auto-detected.

------------------------------------------------------------------------------
CONFIGURING GOOGLE DRIVE (optional)
------------------------------------------------------------------------------
Pass --gdrive-upload to also push each block's bundle straight to Drive
(no mail->Drive Apps Script needed). The email is still sent, and now carries
the Drive link. If anything is missing the upload is skipped with a logged
reason in <run_dir>/notify.log; the email still goes out.

    pip install google-api-python-client google-auth
    # credentials json at ~/.config/janus_gdrive_credentials.json
    # (override with --gdrive-creds or $JANUS_GDRIVE_CREDENTIALS)

Destination (§138):  janus_ec2_runs/<block>/<block>_<stage>_<ts>.tar.gz
  * janus_ec2_runs must ALREADY exist and be shared (Editor) with the
    credential's identity; it is never created. Override the name with
    $JANUS_GDRIVE_ROOT or pin the exact folder with $JANUS_GDRIVE_ROOT_ID
    (the id is the last segment of the folder URL).
  * <block>/ is reused if present, otherwise created.

Credential options:
  a) Service account key (type=service_account). Share janus_ec2_runs with
     its client_email. NOTE: service accounts have no storage quota, so this
     only works when janus_ec2_runs lives in a SHARED DRIVE (add the SA as
     Content manager). In a plain My-Drive folder uploads fail with
     storageQuotaExceeded.
  b) OAuth user token (type=authorized_user) — works with a plain My-Drive
     folder; files are owned by you. Generate it once on a machine with a
     browser:  python3 asic_flow/utils/gdrive_auth.py client_secret.json
     then copy the output json to the EC2 box at the path above.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import smtplib
import ssl
import tarfile
import time
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable, List, Optional


# Default cap for the emailed tarball. Gmail rejects attachments > 25 MB; most
# providers cap ~25-50 MB. We aim for 50 MB of compressed logs/reports max and
# never tar heavy PD binaries (.odb/.def/.gds/.spef).
_DEFAULT_MAX_TAR_BYTES = 50 * 1024 * 1024
# §136: the 50 MB above is the UNCOMPRESSED bundle budget. What the mail server
# sees is the compressed tar after base64 (+33%), and Gmail/most relays reject
# a message > 25 MB with a 552 that used to be swallowed silently. Attach only
# if the .tar.gz is <= this; otherwise email without it and name the path.
_DEFAULT_ATTACH_MAX_BYTES = 18 * 1024 * 1024
_HEAVY_EXTS = (".odb", ".def", ".gds", ".gds.gz", ".spef", ".spef.gz",
               ".lef", ".lib", ".v.gz", ".cdl")
_UTILS_DIR = os.path.dirname(os.path.abspath(__file__))
if _UTILS_DIR not in sys.path:
    sys.path.insert(0, _UTILS_DIR)
_DEFAULT_GDRIVE_CREDS = os.path.expanduser("~/.config/janus_gdrive_credentials.json")
_DEFAULT_GDRIVE_ROOT = "janus_ec2_runs"


def _fmt(v, nd: int = 2) -> str:
    if v is None or v == "":
        return "-"
    try:
        return f"{float(v):+.{nd}f}" if nd else str(int(v))
    except (TypeError, ValueError):
        return str(v)


_TABLE_COLS = (("Block", "block"), ("Status", "status"), ("Setup WS (ps)", "setup_ps"),
               ("Hold WS (ps)", "hold_ps"), ("DRC", "drc"), ("Max-slew viol", "slew"),
               ("Note", "note"))


def _cells(r: dict) -> List[str]:
    return [str(r.get("block") or ""), str(r.get("status") or "PENDING"),
            _fmt(r.get("setup_ps")), _fmt(r.get("hold_ps")),
            _fmt(r.get("drc"), 0), _fmt(r.get("slew"), 0), str(r.get("note") or "")]


def render_run_table_text(rows: List[dict]) -> str:
    """§145: fixed-width run-wide block table (plain-text email part)."""
    if not rows:
        return ""
    head = [h for h, _ in _TABLE_COLS]
    body = [_cells(r) for r in rows]
    w = [max(len(x[i]) for x in [head] + body) for i in range(len(head))]
    line = lambda c: "  ".join(c[i].ljust(w[i]) for i in range(len(c))).rstrip()
    return "\n".join([line(head), line(["-" * n for n in w])] + [line(c) for c in body])


def render_run_table_html(rows: List[dict], title: str = "All blocks (latest)") -> str:
    """§145: same table as HTML (status-coloured)."""
    import html as _h
    if not rows:
        return ""
    col = {"DONE": "#d4edda", "FAILED": "#f8d7da", "BLOCKED": "#f8d7da",
           "NEEDS_MANUAL": "#fff3cd", "ABORTED": "#f8d7da"}
    th = "".join(f'<th style="border:1px solid #999;padding:3px 8px;background:#eee">'
                 f"{_h.escape(h)}</th>" for h, _ in _TABLE_COLS)
    trs = []
    for r in rows:
        c = _cells(r)
        st = c[1]
        bg = col.get(st, "#e2e3e5" if st == "PENDING" else "#cce5ff")
        tds = "".join(
            f'<td style="border:1px solid #999;padding:3px 8px;'
            f'{"text-align:right;" if 2 <= i <= 5 else ""}'
            f'{"background:" + bg + ";" if i == 1 else ""}'
            f'{"color:#b00;" if 2 <= i <= 3 and v.startswith("-") and v != "-" else ""}">'
            f"{_h.escape(v)}</td>" for i, v in enumerate(c))
        trs.append(f"<tr>{tds}</tr>")
    return (f"<p><b>{_h.escape(title)}</b></p>"
            '<table style="border-collapse:collapse;font-family:monospace;font-size:13px">'
            f"<tr>{th}</tr>{''.join(trs)}</table>")


def _noop(_msg: str) -> None:
    pass


class NotificationManager:
    """Email + optional Google-Drive notifications for finished/failed blocks.

    Construct once per run; call :meth:`notify` after a block settles. All
    public methods are wrapped so they never raise into the caller.
    """

    def __init__(
        self,
        recipient: Optional[str] = None,
        smtp_host: Optional[str] = None,
        smtp_port: Optional[int] = None,
        smtp_user: Optional[str] = None,
        smtp_pass: Optional[str] = None,
        gdrive_enabled: bool = False,
        gdrive_creds: Optional[str] = None,
        max_tar_bytes: int = _DEFAULT_MAX_TAR_BYTES,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.log = logger or _noop
        # Recipient + SMTP: CLI value wins, else fall back to environment.
        self.recipient = recipient or os.environ.get("NOTIFY_EMAIL") or ""
        self.smtp_host = smtp_host or os.environ.get("SMTP_HOST") or ""
        try:
            self.smtp_port = int(smtp_port or os.environ.get("SMTP_PORT") or 587)
        except (TypeError, ValueError):
            self.smtp_port = 587
        self.smtp_user = smtp_user or os.environ.get("SMTP_USER") or ""
        self.smtp_pass = smtp_pass or os.environ.get("SMTP_PASS") or ""
        self.gdrive_enabled = bool(gdrive_enabled)
        self.gdrive_creds = (gdrive_creds
                             or os.environ.get("JANUS_GDRIVE_CREDENTIALS")
                             or _DEFAULT_GDRIVE_CREDS)
        # §138: bundles go to <root>/<block>/ inside an existing, shared folder.
        self.gdrive_root = (os.environ.get("JANUS_GDRIVE_ROOT", "").strip()
                            or _DEFAULT_GDRIVE_ROOT)
        self.gdrive_root_id = os.environ.get("JANUS_GDRIVE_ROOT_ID", "").strip()
        self.gdrive_identity = ""
        self.last_drive_links: List[str] = []
        self.last_drive_outcome = ""
        self.last_drive_error = ""  # §142: raw API error behind a FAILED outcome
        self.max_tar_bytes = int(max_tar_bytes)
        # §137: downstream consumers (e.g. a Google Apps Script mail->Drive
        # extractor with a 6-min execution limit) need smaller / zip bundles.
        # Env only (no new CLI flags): JANUS_NOTIFY_BUNDLE_MAX_MB caps the
        # uncompressed bundle; JANUS_NOTIFY_ARCHIVE=zip|tar.gz picks the format.
        try:
            _bmb = float(os.environ.get("JANUS_NOTIFY_BUNDLE_MAX_MB", ""))
            if _bmb > 0:
                self.max_tar_bytes = int(_bmb * 1024 * 1024)
        except ValueError:
            pass
        self.archive_fmt = ("zip" if os.environ.get("JANUS_NOTIFY_ARCHIVE", "")
                            .strip().lower() == "zip" else "tar.gz")
        try:
            self.attach_max_bytes = int(float(
                os.environ.get("JANUS_NOTIFY_ATTACH_MAX_MB", "")) * 1024 * 1024)
        except ValueError:
            self.attach_max_bytes = _DEFAULT_ATTACH_MAX_BYTES
        if self.attach_max_bytes <= 0:
            self.attach_max_bytes = _DEFAULT_ATTACH_MAX_BYTES
        # §136: one-line outcome of the last notify()/send_email(), so the
        # agent can surface it (it used to exist only at --verbose debug level).
        self.last_outcome = ""

    def config_problem(self) -> str:
        """Why email cannot be sent ('' if it can). Names missing pieces only,
        never their values."""
        if not self.recipient:
            return "no recipient (--notify-email / $NOTIFY_EMAIL)"
        missing = [n for n, v in (("SMTP_HOST", self.smtp_host),
                                  ("SMTP_USER", self.smtp_user),
                                  ("SMTP_PASS", self.smtp_pass)) if not v]
        return ("SMTP not configured, missing: " + ", ".join(missing)) if missing else ""

    # ------------------------------------------------------------------ #
    # capability probes
    # ------------------------------------------------------------------ #
    @property
    def email_configured(self) -> bool:
        """True only if we have enough to actually send an email."""
        return bool(self.recipient and self.smtp_host and self.smtp_user
                    and self.smtp_pass)

    @property
    def enabled(self) -> bool:
        """True if the user asked for ANY notification channel."""
        return bool(self.recipient or self.gdrive_enabled)

    # ------------------------------------------------------------------ #
    # tarball creation
    # ------------------------------------------------------------------ #
    def collect_logs_and_reports(
        self,
        run_dir: Optional[str],
        results_dir: Optional[str],
        block: str,
        stage: str = "",
        reports_dir: Optional[str] = None,
        out_dir: Optional[str] = None,
        logs_dir: Optional[str] = None,
        block_log: Optional[str] = None,
    ) -> Optional[str]:
        """LEGACY fallback tarball (used only if report_bundle cannot import).

        Per-block since §120: only *block_log* (or ``run_dir/<block>.log``),
        the ORFS ``logs_dir`` / ``reports_dir`` / ``results_dir`` leaf dirs of
        THIS block, non-recursive, critical reports first, and an oversize file
        is skipped instead of ending collection.

        Tar+gzip the run logs and key reports for *block*.

        Includes:
          * the block's own agent log (never other blocks' logs)
          * ORFS ``*.log``/``*.json`` under ``logs_dir``
          * every ``*.rpt`` / ``*.json`` / ``*.txt`` under ``reports_dir``
            (2_floorplan ... 6_finish stage reports, incl. 6_finish.rpt)
          * lightweight text results (``*.rpt``/``*.log``/``*.json``) under
            ``results_dir`` — heavy PD binaries (.odb/.def/.gds/.spef/...) are
            EXCLUDED to keep the attachment small.

        Returns the path to the created ``.tar.gz`` (under ``out_dir`` or
        ``run_dir``), or ``None`` if nothing could be collected.
        """
        try:
            dest_dir = out_dir or run_dir or os.getcwd()
            os.makedirs(dest_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_stage = ("_" + stage) if stage else ""
            tar_path = os.path.join(dest_dir, f"{block}{safe_stage}_{ts}.tar.gz")

            members: List[tuple] = []  # (abs_path, arcname)

            def _add_glob(root: Optional[str], patterns, sub: str) -> None:
                # NON-recursive (§120): reports/results/logs leaf dirs are
                # flat; recursing into the shared agent run dir is what pulled
                # every parallel block's log + signoff/* into one tarball.
                if not root or not os.path.isdir(root):
                    return
                for pat in patterns:
                    for fp in sorted(glob.glob(os.path.join(root, pat))):
                        if not os.path.isfile(fp):
                            continue
                        low = fp.lower()
                        if any(low.endswith(ext) for ext in _HEAVY_EXTS):
                            continue
                        arc = os.path.join(block + safe_stage, sub,
                                           os.path.relpath(fp, root))
                        members.append((fp, arc))

            # Only THIS block's agent log from the (shared) run dir.
            blog = block_log or (os.path.join(run_dir, f"{block}.log")
                                 if run_dir else None)
            if blog and os.path.isfile(blog):
                members.append((blog, os.path.join(block + safe_stage, "logs",
                                                   os.path.basename(blog))))
            _add_glob(logs_dir, ("*.log", "*.json"), "orfs_logs")
            # final/critical reports first so the cap can never starve them
            _add_glob(reports_dir, ("6_finish.rpt", "5_route_drc.rpt",
                                    "4_cts_final.rpt", "*.rpt", "*.log",
                                    "*.json", "*.txt"), "reports")
            # results dir: text artifacts only (skip .odb/.def/... via _HEAVY_EXTS)
            _add_glob(results_dir, ("*.sdc", "*.rpt", "*.log", "*.json",
                                    "*.txt", "*.tcl"), "results")

            if not members:
                self.log(f"notification: nothing to tar for {block} "
                         f"(run_dir={run_dir!r})")
                return None

            # de-dup by arcname, then add under a running size budget
            seen = set()
            total = 0
            added = 0
            with tarfile.open(tar_path, "w:gz") as tar:
                for fp, arc in members:
                    if arc in seen:
                        continue
                    seen.add(arc)
                    try:
                        sz = os.path.getsize(fp)
                    except OSError:
                        continue
                    if total + sz > self.max_tar_bytes:
                        # skip THIS file and keep going (§120): the old
                        # `break` dropped everything after the first big file
                        # (results/ and the final reports were lost to ~45 MB
                        # of janus_*_paths.rpt).
                        self.log(f"notification: skipping {os.path.basename(fp)} "
                                 f"({sz} B) -- tar size budget "
                                 f"({self.max_tar_bytes} B) would be exceeded")
                        continue
                    try:
                        tar.add(fp, arcname=arc)
                        total += sz
                        added += 1
                    except OSError as e:
                        self.log(f"notification: could not add {fp}: {e}")

            if added == 0:
                self.log(f"notification: tarball empty for {block}; removing")
                try:
                    os.remove(tar_path)
                except OSError:
                    pass
                return None
            self.log(f"notification: created {tar_path} "
                     f"({added} files, {total} B uncompressed)")
            return tar_path
        except Exception as e:  # never propagate
            self.log(f"notification: collect_logs_and_reports failed: {e}")
            return None

    # ------------------------------------------------------------------ #
    # email
    # ------------------------------------------------------------------ #
    def send_email(
        self,
        subject: str,
        body: str,
        attachments: Optional[List[str]] = None,
        html: Optional[str] = None,
    ) -> bool:
        """Send an SMTP email with optional file attachments. *html* (§145)
        adds a text/html alternative to the plain body.

        Returns True on success, False otherwise (never raises).
        """
        attachments = attachments or []
        problem = self.config_problem()
        if problem:
            self.last_outcome = f"email SKIPPED: {problem}"
            self.log(f"notification: {self.last_outcome}")
            return False
        try:
            msg = MIMEMultipart()
            msg["From"] = self.smtp_user
            msg["To"] = self.recipient
            msg["Subject"] = subject
            if html:
                alt = MIMEMultipart("alternative")
                alt.attach(MIMEText(body, "plain"))
                alt.attach(MIMEText(html, "html"))
                msg.attach(alt)
            else:
                msg.attach(MIMEText(body, "plain"))

            for path in attachments:
                if not path or not os.path.isfile(path):
                    continue
                try:
                    with open(path, "rb") as fh:
                        part = MIMEApplication(fh.read(),
                                               Name=os.path.basename(path))
                    part["Content-Disposition"] = (
                        f'attachment; filename="{os.path.basename(path)}"')
                    msg.attach(part)
                except OSError as e:
                    self.log(f"notification: could not attach {path}: {e}")

            # port 465 => implicit SSL; everything else => STARTTLS
            if self.smtp_port == 465:
                ctx = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port,
                                      timeout=60, context=ctx) as srv:
                    srv.login(self.smtp_user, self.smtp_pass)
                    srv.sendmail(self.smtp_user, [self.recipient],
                                 msg.as_string())
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port,
                                  timeout=60) as srv:
                    srv.ehlo()
                    try:
                        srv.starttls(context=ssl.create_default_context())
                        srv.ehlo()
                    except smtplib.SMTPException:
                        pass  # server without STARTTLS; try to send anyway
                    srv.login(self.smtp_user, self.smtp_pass)
                    srv.sendmail(self.smtp_user, [self.recipient],
                                 msg.as_string())
            sizes = []
            for p in attachments:
                try:
                    sizes.append(f"{os.path.basename(p)} {os.path.getsize(p) / 1048576:.1f} MB")
                except OSError:
                    pass
            self.last_outcome = (f"email SENT to {self.recipient} "
                                 f"({len(attachments)} attachment(s)"
                                 + (": " + ", ".join(sizes) if sizes else "") + ")")
            self.log(f"notification: {self.last_outcome}")
            return True
        except Exception as e:  # never propagate
            self.last_outcome = (f"email FAILED via {self.smtp_host}:{self.smtp_port}: "
                                 f"{type(e).__name__}: {e}")[:300]
            self.log(f"notification: {self.last_outcome}")
            return False

    # ------------------------------------------------------------------ #
    # google drive (optional)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _q(name: str) -> str:
        """Escape a value for a Drive v3 query string literal."""
        return name.replace("\\", "\\\\").replace("'", "\\'")

    def gdrive_problem(self) -> str:
        """Why a Drive upload cannot happen ('' if it looks possible). Cheap,
        no network: checks the flag, the creds file and the client library."""
        if not self.gdrive_enabled:
            return "off (no --gdrive-upload)"
        if not os.path.isfile(self.gdrive_creds):
            return f"credentials file not found: {self.gdrive_creds}"
        try:
            import googleapiclient.discovery  # type: ignore  # noqa: F401
        except Exception as e:
            return (f"google-api-python-client not importable ({e}); "
                    "pip install google-api-python-client google-auth")
        return ""

    def gdrive_target_desc(self, block: str = "<block>") -> str:
        root = (f"id={self.gdrive_root_id}" if self.gdrive_root_id
                else self.gdrive_root)
        return f"{root}/{block}/"

    def upload_to_drive(
        self,
        files: List[str],
        block_name: str,
        subfolder: Optional[str] = None,
    ) -> List[str]:
        """§138: upload *files* to ``<gdrive_root>/<block_name>/`` on Drive.
        §145: with *subfolder* (the run timestamp) they go to
        ``<gdrive_root>/<block_name>/<subfolder>/`` instead.

        ``gdrive_root`` is an EXISTING folder the user owns and has shared with
        the credential's identity (default name ``janus_ec2_runs``, override
        with $JANUS_GDRIVE_ROOT, or pin it by id with $JANUS_GDRIVE_ROOT_ID).
        The root is never created (a service account would create it in its
        own invisible drive); the per-block subfolder is reused if present,
        else created. Bundle filenames already carry a timestamp, so repeated
        runs accumulate side by side in the block folder.

        Shared Drives and plain shared My-Drive folders are both handled
        (supportsAllDrives). Records webViewLinks in ``self.last_drive_links``.
        Returns uploaded file ids; [] on any skip/failure. Never raises.
        """
        self.last_drive_links = []
        self.last_drive_error = ""
        if not self.gdrive_enabled:
            return []
        files = [f for f in (files or []) if f and os.path.isfile(f)]
        if not files:
            self.log("notification: gdrive upload requested but no files")
            return []
        prob = self.gdrive_problem()
        if prob:
            self.log(f"notification: gdrive upload SKIPPED: {prob}")
            self.last_drive_outcome = f"skipped: {prob}"
            return []
        try:
            from googleapiclient.discovery import build  # type: ignore
            from googleapiclient.http import MediaFileUpload  # type: ignore
            service = self._gdrive_service(build)
            if service is None:
                self.last_drive_outcome = "skipped: authentication failed"
                return []
            root = self._gdrive_root(service)
            if not root:
                self.last_drive_outcome = (
                    f"FAILED: root folder '{self.gdrive_target_desc('')}' not "
                    f"visible to {self.gdrive_identity or 'the credential'}"
                    + self._drive_err_suffix())
                return []
            target = self._gdrive_folder(service, block_name, root)
            if not target:
                self.last_drive_outcome = (f"FAILED: could not find/create "
                                           f"{block_name}/ under the root"
                                           + self._drive_err_suffix())
                return []
            dest = block_name
            if subfolder:
                target = self._gdrive_folder(service, subfolder, target)
                dest = f"{block_name}/{subfolder}"
                if not target:
                    self.last_drive_outcome = (f"FAILED: could not find/create "
                                               f"{dest}/ under the root"
                                               + self._drive_err_suffix())
                    return []
            uploaded: List[str] = []
            for fp in files:
                media = MediaFileUpload(fp, resumable=True,
                                        chunksize=8 * 1024 * 1024)
                created = service.files().create(
                    body={"name": os.path.basename(fp), "parents": [target]},
                    media_body=media, fields="id,webViewLink",
                    supportsAllDrives=True).execute()
                fid = created.get("id", "")
                uploaded.append(fid)
                link = created.get("webViewLink", "")
                if link:
                    self.last_drive_links.append(link)
                self.log(f"notification: uploaded {os.path.basename(fp)} to "
                         f"Drive {self.gdrive_target_desc(dest)} "
                         f"(id={fid}) {link}")
            self.last_drive_outcome = (f"uploaded {len(uploaded)} file(s) to "
                                       f"{self.gdrive_target_desc(dest)}")
            return uploaded
        except Exception as e:  # never propagate
            msg = str(e)
            hint = ""
            if "storageQuotaExceeded" in msg or "do not have storage quota" in msg:
                hint = (" -- service accounts have NO Drive storage of their "
                        "own: put janus_ec2_runs in a Shared Drive (add the "
                        "service account as Content manager) or use an OAuth "
                        "user token instead (asic_flow/utils/gdrive_auth.py)")
            elif "insufficientPermissions" in msg or "403" in msg:
                hint = (f" -- share {self.gdrive_root} with "
                        f"{self.gdrive_identity or 'the credential'} as Editor")
            self.last_drive_outcome = f"FAILED: {msg[:200]}{hint}"
            self.log(f"notification: upload_to_drive failed: {msg}{hint}")
            return []

    def _drive_err_suffix(self) -> str:
        """§142: append the Google API error (trimmed) + a hint for common
        causes, so the email/dashboard line is diagnosable on its own."""
        e = self.last_drive_error
        if not e:
            return ""
        hint = ""
        if "File not found" in e or "notFound" in e or "404" in e:
            hint = (" -- root id not found/visible: check $JANUS_GDRIVE_ROOT_ID "
                    "is just the id from the folder URL (after /folders/, no "
                    "?usp=...)")
        elif ("insufficientFilePermissions" in e or "insufficientPermissions" in e
              or "403" in e):
            hint = (" -- the logged-in account can see the folder but cannot "
                    "write to it (needs Editor), or the Drive scope was not "
                    "granted at consent (re-run gdrive_auth.py and tick Drive)")
        elif "invalid_grant" in e or "invalid_client" in e:
            hint = " -- token revoked/expired: re-run gdrive_auth.py"
        return f" [{e[:240]}]{hint}"

    def _gdrive_service(self, build):
        """Build a Drive API client from a service account or OAuth token json.

        Full ``drive`` scope is required: ``drive.file`` only sees files this
        app created, so it could never find the user's existing
        janus_ec2_runs/<block> folders."""
        scopes = ["https://www.googleapis.com/auth/drive"]
        try:
            with open(self.gdrive_creds) as fh:
                info = json.load(fh)
        except Exception as e:
            self.log(f"notification: cannot read {self.gdrive_creds}: {e}")
            return None
        try:
            if info.get("type") == "service_account":
                from google.oauth2 import service_account  # type: ignore
                creds = service_account.Credentials.from_service_account_info(
                    info, scopes=scopes)
                self.gdrive_identity = info.get("client_email", "")
            else:  # OAuth2 authorized-user token (gdrive_auth.py output)
                from google.oauth2.credentials import Credentials  # type: ignore
                creds = Credentials.from_authorized_user_info(info, scopes)
                self.gdrive_identity = "OAuth user token"
            return build("drive", "v3", credentials=creds,
                         cache_discovery=False)
        except Exception as e:
            self.log(f"notification: Drive auth failed ({e}); skipping upload")
            return None

    def _gdrive_root(self, service) -> Optional[str]:
        """Resolve the EXISTING root folder id (never created)."""
        if self.gdrive_root_id:
            return self.gdrive_root_id
        try:
            resp = service.files().list(
                q=("mimeType='application/vnd.google-apps.folder' and "
                   f"name='{self._q(self.gdrive_root)}' and trashed=false"),
                corpora="allDrives", includeItemsFromAllDrives=True,
                supportsAllDrives=True, fields="files(id,name)").execute()
            hits = resp.get("files", [])
        except Exception as e:
            self.last_drive_error = str(e)
            self.log(f"notification: Drive root lookup failed: {e}")
            return None
        if not hits:
            self.log(f"notification: Drive folder '{self.gdrive_root}' is not "
                     f"visible to {self.gdrive_identity or 'the credential'} -- "
                     "share it with that identity as Editor (or set "
                     "$JANUS_GDRIVE_ROOT_ID)")
            return None
        if len(hits) > 1:
            self.log(f"notification: {len(hits)} folders named "
                     f"'{self.gdrive_root}' visible; using id={hits[0]['id']} "
                     "(set $JANUS_GDRIVE_ROOT_ID to pin one)")
        return hits[0]["id"]

    def _gdrive_folder(self, service, name: str, parent: str) -> Optional[str]:
        """Return the id of folder *name* under *parent*, creating it if
        needed. None on error."""
        try:
            resp = service.files().list(
                q=("mimeType='application/vnd.google-apps.folder' and "
                   f"name='{self._q(name)}' and '{parent}' in parents and "
                   "trashed=false"),
                corpora="allDrives", includeItemsFromAllDrives=True,
                supportsAllDrives=True, fields="files(id,name)").execute()
            hits = resp.get("files", [])
            if hits:
                return hits[0]["id"]
            created = service.files().create(
                body={"name": name, "parents": [parent],
                      "mimeType": "application/vnd.google-apps.folder"},
                fields="id", supportsAllDrives=True).execute()
            self.log(f"notification: created Drive folder {name}/ under root")
            return created.get("id")
        except Exception as e:
            self.last_drive_error = str(e)
            self.log(f"notification: Drive folder '{name}' lookup/create "
                     f"failed: {e}")
            return None

    def send_summary(self, subject: str, rows: List[dict],
                     run_dir: Optional[str] = None) -> bool:
        """§145: run-end email with just the all-blocks table."""
        txt = "ALL BLOCKS (final)\n" + render_run_table_text(rows)
        if run_dir:
            txt += f"\n\nRun dir: {run_dir}"
        html = render_run_table_html(rows, "All blocks (final)")
        return self.send_email(subject, txt, [], html=html)

    # ------------------------------------------------------------------ #
    # orchestrator
    # ------------------------------------------------------------------ #
    def notify(
        self,
        block: str,
        status: str,
        stage: str = "",
        run_dir: Optional[str] = None,
        results_dir: Optional[str] = None,
        reports_dir: Optional[str] = None,
        log_path: Optional[str] = None,
        summary: str = "",
        logs_dir: Optional[str] = None,
        signoff_dir: Optional[str] = None,
        tries: Optional[int] = None,
        note: str = "",
        qor_out_dir: Optional[str] = None,
        bundle_dir: Optional[str] = None,
        run_table: Optional[List[dict]] = None,
        drive_subfolder: Optional[str] = None,
    ) -> Optional[dict]:
        """Build the per-block bundle, email it, and (optionally) upload to Drive.

        Fully best-effort: swallows every error. Does nothing if no channel is
        enabled. *status* is the block's settled status (e.g. DONE / FAILED /
        NEEDS_MANUAL); *stage* is the manifest's last completed stage (hint
        for the failing stage). *run_dir* is DISPLAY ONLY (§120): only
        *log_path* (this block's agent log) is read from it.

        §120/§121: uses report_bundle (stage-aware selection, INDEX.md,
        qor.json, dashboard-parser QoR + failure triage in the body). qor.json
        is also written to *qor_out_dir* (the block's ORFS reports dir) for
        the adaptive flow. Falls back to the legacy tarball if report_bundle
        cannot be imported. Returns the qor dict (or None).
        """
        if not self.enabled:
            return None
        qor = None
        self.last_outcome = ""
        try:
            tarball = None
            body = ""
            failed_stage = stage
            try:
                import report_bundle as rb  # same dir (utils/)
            except Exception as e:  # pragma: no cover
                rb = None
                self.log(f"notification: report_bundle unavailable ({e}); legacy tarball")
            if rb is not None:
                out = bundle_dir or os.path.join(
                    run_dir or os.getcwd(), "bundles", block)
                res = rb.build_bundle(
                    block, status, out, logs_dir=logs_dir,
                    reports_dir=reports_dir, results_dir=results_dir,
                    agent_log=log_path, signoff_dir=signoff_dir,
                    manifest_stage=stage, cap_bytes=self.max_tar_bytes,
                    tries=tries, note=note, archive_fmt=self.archive_fmt)
                qor = res["qor"]
                tarball = res["tar"]
                body = res["body"]
                failed_stage = qor.get("failed_stage") or stage
                if qor_out_dir and os.path.isdir(qor_out_dir):
                    try:
                        with open(os.path.join(qor_out_dir, "qor.json"), "w") as fh:
                            json.dump(qor, fh, indent=2, default=str)
                    except OSError as e:
                        self.log(f"notification: cannot write qor.json to {qor_out_dir}: {e}")
                self.log(f"notification: bundle {tarball} "
                         f"({len(res['included'])} included, {len(res['skipped'])} skipped)")

            is_ok = str(status).upper() == "DONE"
            if is_ok:
                subject = f"JANUS {block} completed"
            else:
                where = f" at {failed_stage}" if failed_stage else ""
                subject = f"JANUS {block} {status}{where}"
            if qor:
                t = qor.get("timing") or {}
                ws = t.get("setup_worst_slack")
                if ws is not None:
                    subject += f" | setup WS {ws:.2f} ps"
                hs = t.get("hold_worst_slack")
                if hs is not None:
                    subject += f" hold WS {hs:.2f} ps"

            if not body:
                lines = [f"Block   : {block}", f"Status  : {status}"]
                if stage:
                    lines.append(f"Stage   : {stage}")
                lines.append(f"Time    : {datetime.now().isoformat(timespec='seconds')}")
                body = "\n".join(lines)
            extra = []
            if run_dir:
                extra.append(f"Run dir      : {run_dir}")
            if reports_dir:
                extra.append(f"Reports dir  : {reports_dir}")
            if summary and not qor:
                extra += ["", summary]
            # §139: with --gdrive-upload the email is a status/QoR summary only;
            # the bundle lives on Drive (link appended below), never attached.
            summary_only = self.gdrive_enabled
            if tarball and not summary_only:
                extra.append(f"Attachment   : {os.path.basename(tarball)} "
                             "(see INDEX.md inside for what was included/skipped and why)")
            body = body + ("\n\n" + "\n".join(extra) if extra else "")
            # §145: every email carries the run-wide table (latest state of
            # every block; not-yet-run ones PENDING) at the top.
            table_txt = render_run_table_text(run_table or [])
            if table_txt:
                body = ("ALL BLOCKS (latest)\n" + table_txt + "\n\n"
                        + "=" * 72 + f"\nTHIS BLOCK: {block}\n" + "=" * 72
                        + "\n" + body)

            if tarball is None:
                tarball = self.collect_logs_and_reports(
                    run_dir=run_dir, results_dir=results_dir,
                    reports_dir=reports_dir, block=block,
                    stage=("" if is_ok else stage), logs_dir=logs_dir,
                    block_log=log_path,
                )
            attachments = [tarball] if tarball and not summary_only else []
            # If we somehow have no tarball but do have a single log, attach it
            # (bounded — only if it is under the size budget).
            if (not summary_only and not attachments and log_path
                    and os.path.isfile(log_path)):
                try:
                    if os.path.getsize(log_path) <= self.max_tar_bytes:
                        attachments = [log_path]
                except OSError:
                    pass

            # §136: never let an oversize attachment cost the whole email.
            kept, withheld = [], []
            for p in attachments:
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                if sz > self.attach_max_bytes:
                    body += (f"\n\nNOT ATTACHED (too large for email, "
                             f"{sz / 1048576:.1f} MB > "
                             f"{self.attach_max_bytes / 1048576:.0f} MB): {p}")
                    self.log(f"notification: {p} is {sz} B > attach cap "
                             f"{self.attach_max_bytes} B; sending without it")
                    withheld.append(os.path.basename(p))
                else:
                    kept.append(p)
            attachments = kept

            # §138: Drive first, so the email can carry the link.
            if self.gdrive_enabled and tarball:
                # §145: bundle + its qor.json / INDEX.md / SUMMARY.txt (the
                # email body) into <block>/<run timestamp>/ -- readable on
                # Drive with no extraction (no Apps Script needed).
                up = [tarball]
                bdir = os.path.dirname(tarball)
                for nm in ("qor.json", "INDEX.md"):
                    if os.path.isfile(os.path.join(bdir, nm)):
                        up.append(os.path.join(bdir, nm))
                try:
                    sp = os.path.join(bdir, "SUMMARY.txt")
                    with open(sp, "w") as fh:
                        fh.write(f"{subject}\n\n{body}\n")
                    up.append(sp)
                except OSError:
                    pass
                self.upload_to_drive(up, block, **({"subfolder": drive_subfolder}
                                                   if drive_subfolder else {}))
                dest = f"{block}/{drive_subfolder}" if drive_subfolder else block
                if self.last_drive_links:
                    body += ("\n\nBundle on Google Drive ("
                             f"{self.gdrive_target_desc(dest)}):\n  "
                             + "\n  ".join(self.last_drive_links))
                elif self.last_drive_outcome:
                    body += (f"\n\nGoogle Drive upload: {self.last_drive_outcome}"
                             f"\nBundle on EC2 disk: {tarball}")

            self.last_subject, self.last_body = subject, body
            html = None
            if run_table:
                import html as _h
                html = (render_run_table_html(run_table)
                        + f"<hr><p><b>This block: {_h.escape(block)}</b></p>"
                        + "<pre style=\"font-size:12px\">"
                        + _h.escape(body.split("=" * 72 + "\n", 2)[-1]) + "</pre>")
            hk = {"html": html} if html else {}
            ok = self.send_email(subject, body, attachments, **hk)
            if not ok and attachments and self.email_configured:
                # Rejected WITH the attachment (size/policy 552, timeout on a
                # slow upload, ...): retry once text-only so a mail still lands.
                first = self.last_outcome
                body += ("\n\nThe attachment was dropped because the send with it "
                         f"failed ({first}). Bundle on disk: "
                         + ", ".join(attachments))
                self.last_body = body
                if self.send_email(subject, body, [], **hk):
                    self.last_outcome = (f"{self.last_outcome} [text-only retry; "
                                         f"with-attachment send failed: {first}]")[:400]
            elif ok and withheld:
                self.last_outcome += (f" [bundle NOT attached, over "
                                      f"{self.attach_max_bytes / 1048576:.0f} MB cap: "
                                      + ", ".join(withheld) + "]")
            if self.gdrive_enabled and self.last_drive_outcome:
                self.last_outcome = (f"{self.last_outcome} [drive: "
                                     f"{self.last_drive_outcome}]")[:500]
        except Exception as e:  # never propagate
            self.last_outcome = f"notify() FAILED before/while sending: {type(e).__name__}: {e}"[:300]
            self.log(f"notification: notify() failed for {block}: {e}")
        return qor
