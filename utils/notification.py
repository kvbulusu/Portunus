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
Pass --gdrive-upload to also push the tarball to Drive. This needs the Google
client libraries and a credentials file; if either is missing the upload is
skipped with a warning (the email still goes out):

    pip install google-api-python-client google-auth
    # place a SERVICE-ACCOUNT key json (preferred for headless EC2 runs) at:
    ~/.config/janus_gdrive_credentials.json
    # (override the path with --gdrive-creds or $JANUS_GDRIVE_CREDENTIALS)

A service account is preferred for headless runs (no browser OAuth consent).
Share a Drive folder with the service account's email if you want the uploads
to appear in a human's "Shared with me". The tarball is placed in a folder
named  JANUS_runs/<block>_<timestamp>.
"""

from __future__ import annotations

import glob
import os
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
_HEAVY_EXTS = (".odb", ".def", ".gds", ".gds.gz", ".spef", ".spef.gz",
               ".lef", ".lib", ".v.gz", ".cdl")
_DEFAULT_GDRIVE_CREDS = os.path.expanduser("~/.config/janus_gdrive_credentials.json")


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
        self.max_tar_bytes = int(max_tar_bytes)

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
    ) -> Optional[str]:
        """Tar+gzip the run logs and key reports for *block*.

        Includes:
          * every ``*.log`` under ``run_dir`` (the agent + ORFS console logs)
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
                if not root or not os.path.isdir(root):
                    return
                for pat in patterns:
                    for fp in glob.glob(os.path.join(root, "**", pat),
                                        recursive=True):
                        if not os.path.isfile(fp):
                            continue
                        low = fp.lower()
                        if any(low.endswith(ext) for ext in _HEAVY_EXTS):
                            continue
                        arc = os.path.join(block + safe_stage, sub,
                                           os.path.relpath(fp, root))
                        members.append((fp, arc))

            _add_glob(run_dir, ("*.log", "*.txt", "*.json", "manifest*"), "logs")
            _add_glob(reports_dir, ("*.rpt", "*.json", "*.txt"), "reports")
            # results dir: text artifacts only (skip .odb/.def/... via _HEAVY_EXTS)
            _add_glob(results_dir, ("*.rpt", "*.log", "*.json", "*.txt"), "results")

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
                        self.log(f"notification: tar size budget "
                                 f"({self.max_tar_bytes} B) reached; "
                                 f"{added} file(s) included, skipping the rest")
                        break
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
    ) -> bool:
        """Send an SMTP email with optional file attachments.

        Returns True on success, False otherwise (never raises).
        """
        attachments = attachments or []
        if not self.recipient:
            self.log("notification: no recipient set; skipping email")
            return False
        if not self.email_configured:
            self.log("notification: SMTP not fully configured "
                     "(need host/user/pass); skipping email. See "
                     "notification.py header for setup.")
            return False
        try:
            msg = MIMEMultipart()
            msg["From"] = self.smtp_user
            msg["To"] = self.recipient
            msg["Subject"] = subject
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
            self.log(f"notification: email sent to {self.recipient} "
                     f"({len(attachments)} attachment(s))")
            return True
        except Exception as e:  # never propagate
            self.log(f"notification: send_email failed: {e}")
            return False

    # ------------------------------------------------------------------ #
    # google drive (optional)
    # ------------------------------------------------------------------ #
    def upload_to_drive(
        self,
        files: List[str],
        folder_name: str,
    ) -> List[str]:
        """Upload *files* to Google Drive under ``JANUS_runs/<folder_name>``.

        Prefers a service account (headless). Gracefully skips (returns []) if
        the flag is off, the client libraries are absent, or the credentials
        file is missing. Never raises.
        """
        if not self.gdrive_enabled:
            return []
        files = [f for f in (files or []) if f and os.path.isfile(f)]
        if not files:
            self.log("notification: gdrive upload requested but no files")
            return []
        if not os.path.isfile(self.gdrive_creds):
            self.log(f"notification: gdrive credentials not found at "
                     f"{self.gdrive_creds}; skipping upload (see "
                     f"notification.py header)")
            return []
        try:
            from googleapiclient.discovery import build  # type: ignore
            from googleapiclient.http import MediaFileUpload  # type: ignore
        except Exception as e:
            self.log("notification: google-api-python-client not installed "
                     f"({e}); skipping gdrive upload "
                     "(pip install google-api-python-client google-auth)")
            return []
        try:
            service = self._gdrive_service(build)
            if service is None:
                return []
            parent = self._gdrive_folder(service, "JANUS_runs", None)
            target = self._gdrive_folder(service, folder_name, parent)
            uploaded: List[str] = []
            for fp in files:
                meta = {"name": os.path.basename(fp)}
                if target:
                    meta["parents"] = [target]
                media = MediaFileUpload(fp, resumable=False)
                created = service.files().create(
                    body=meta, media_body=media, fields="id").execute()
                fid = created.get("id", "")
                uploaded.append(fid)
                self.log(f"notification: uploaded {os.path.basename(fp)} "
                         f"to Drive (id={fid})")
            return uploaded
        except Exception as e:  # never propagate
            self.log(f"notification: upload_to_drive failed: {e}")
            return []

    def _gdrive_service(self, build):
        """Build a Drive API client from a service account or OAuth token json."""
        scopes = ["https://www.googleapis.com/auth/drive.file"]
        # 1) service account (preferred for headless)
        try:
            from google.oauth2 import service_account  # type: ignore
            creds = service_account.Credentials.from_service_account_file(
                self.gdrive_creds, scopes=scopes)
            return build("drive", "v3", credentials=creds,
                         cache_discovery=False)
        except Exception as e_sa:
            self.log(f"notification: service-account auth failed "
                     f"({e_sa}); trying OAuth token")
        # 2) OAuth2 authorized-user token json
        try:
            from google.oauth2.credentials import Credentials  # type: ignore
            creds = Credentials.from_authorized_user_file(
                self.gdrive_creds, scopes)
            return build("drive", "v3", credentials=creds,
                         cache_discovery=False)
        except Exception as e_oauth:
            self.log(f"notification: OAuth auth also failed ({e_oauth}); "
                     "skipping gdrive upload")
            return None

    @staticmethod
    def _gdrive_folder(service, name: str, parent: Optional[str]) -> Optional[str]:
        """Return the id of folder *name* (under *parent*), creating it if
        needed. Returns None on any error so uploads fall back to Drive root."""
        try:
            q = ("mimeType='application/vnd.google-apps.folder' "
                 f"and name='{name}' and trashed=false")
            if parent:
                q += f" and '{parent}' in parents"
            resp = service.files().list(
                q=q, spaces="drive", fields="files(id,name)").execute()
            hits = resp.get("files", [])
            if hits:
                return hits[0]["id"]
            meta = {"name": name,
                    "mimeType": "application/vnd.google-apps.folder"}
            if parent:
                meta["parents"] = [parent]
            created = service.files().create(body=meta, fields="id").execute()
            return created.get("id")
        except Exception:
            return None

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
    ) -> None:
        """Build the tarball, email it, and (optionally) upload to Drive.

        Fully best-effort: swallows every error. Does nothing if no channel is
        enabled. *status* is the block's settled status (e.g. DONE / FAILED /
        NEEDS_MANUAL); *stage* is the failing stage for failures.
        """
        if not self.enabled:
            return
        try:
            is_ok = str(status).upper() == "DONE"
            if is_ok:
                subject = f"JANUS {block} completed"
            else:
                where = f" at {stage}" if stage else ""
                subject = f"JANUS {block} {status}{where}"

            lines = [
                f"Block   : {block}",
                f"Status  : {status}",
            ]
            if stage:
                lines.append(f"Stage   : {stage}")
            lines.append(f"Time    : {datetime.now().isoformat(timespec='seconds')}")
            if run_dir:
                lines.append(f"Run dir : {run_dir}")
            if summary:
                lines += ["", summary]
            body = "\n".join(lines)

            tarball = self.collect_logs_and_reports(
                run_dir=run_dir, results_dir=results_dir,
                reports_dir=reports_dir, block=block,
                stage=("" if is_ok else stage),
            )
            attachments = [tarball] if tarball else []
            # If we somehow have no tarball but do have a single log, attach it
            # (bounded — only if it is under the size budget).
            if not attachments and log_path and os.path.isfile(log_path):
                try:
                    if os.path.getsize(log_path) <= self.max_tar_bytes:
                        attachments = [log_path]
                except OSError:
                    pass

            self.send_email(subject, body, attachments)

            if self.gdrive_enabled and tarball:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                self.upload_to_drive([tarball], f"{block}_{ts}")
        except Exception as e:  # never propagate
            self.log(f"notification: notify() failed for {block}: {e}")
