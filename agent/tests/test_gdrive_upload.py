"""§138: direct Drive upload to janus_ec2_runs/<block>/ (Drive API mocked)."""
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
UTILS = os.path.abspath(os.path.join(HERE, "..", "..", "utils"))
sys.path.insert(0, UTILS)

import notification as nt  # noqa: E402

pytest.importorskip("googleapiclient")

FOLDER = "application/vnd.google-apps.folder"


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeDrive:
    """In-memory Drive: {id: {name, mimeType, parents}}; records call kwargs."""

    def __init__(self, items=None, fail_upload=None):
        self.items = dict(items or {})
        self.calls = []
        self.fail_upload = fail_upload
        self._n = 0

    def files(self):
        return self

    def list(self, **kw):
        self.calls.append(("list", kw))
        q = kw["q"]

        def run():
            hits = []
            for i, it in self.items.items():
                if f"name='{it['name']}'" not in q:
                    continue
                if FOLDER in q and it["mimeType"] != FOLDER:
                    continue
                if " in parents" in q and f"'{it['parents'][0] if it['parents'] else ''}' in parents" not in q:
                    continue
                hits.append({"id": i, "name": it["name"]})
            return {"files": hits}
        return _Req(run)

    def create(self, body, fields, media_body=None, supportsAllDrives=False):
        self.calls.append(("create", {"body": body, "supportsAllDrives": supportsAllDrives,
                                      "media": media_body is not None}))

        def run():
            if media_body is not None and self.fail_upload:
                raise RuntimeError(self.fail_upload)
            self._n += 1
            fid = f"new{self._n}"
            self.items[fid] = {"name": body["name"], "parents": body.get("parents", []),
                               "mimeType": body.get("mimeType", "file")}
            return {"id": fid, "webViewLink": f"https://drive/{fid}"}
        return _Req(run)


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("JANUS_GDRIVE_ROOT", "JANUS_GDRIVE_ROOT_ID", "NOTIFY_EMAIL",
              "SMTP_HOST", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(k, raising=False)
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"type": "service_account",
                                 "client_email": "sa@proj.iam.gserviceaccount.com"}))
    bundle = tmp_path / "noc_finish_20261015_120000.tar.gz"
    bundle.write_bytes(b"x" * 1024)
    logs = []
    return tmp_path, str(creds), str(bundle), logs


def _mgr(creds, logs, drive, monkeypatch):
    m = nt.NotificationManager(gdrive_enabled=True, gdrive_creds=creds,
                               logger=logs.append)

    def svc(build):
        m.gdrive_identity = "sa@proj.iam.gserviceaccount.com"
        return drive
    monkeypatch.setattr(m, "_gdrive_service", svc)
    return m


def test_uploads_into_existing_block_folder(env, monkeypatch):
    _, creds, bundle, logs = env
    d = FakeDrive({"R": {"name": "janus_ec2_runs", "parents": [], "mimeType": FOLDER},
                   "B": {"name": "noc", "parents": ["R"], "mimeType": FOLDER}})
    m = _mgr(creds, logs, d, monkeypatch)
    ids = m.upload_to_drive([bundle], "noc")
    assert ids == ["new1"]
    up = [c for c in d.calls if c[0] == "create"]
    assert len(up) == 1 and up[0][1]["body"]["parents"] == ["B"]  # no new folders
    assert up[0][1]["supportsAllDrives"]
    assert all(c[1].get("supportsAllDrives") for c in d.calls if c[0] == "list")
    assert m.last_drive_links == ["https://drive/new1"]
    assert "janus_ec2_runs/noc/" in m.last_drive_outcome
    assert not any("JANUS_runs" in str(c) for c in d.calls)


def test_creates_missing_block_folder_no_timestamp(env, monkeypatch):
    _, creds, bundle, logs = env
    d = FakeDrive({"R": {"name": "janus_ec2_runs", "parents": [], "mimeType": FOLDER}})
    m = _mgr(creds, logs, d, monkeypatch)
    assert m.upload_to_drive([bundle], "cva6")
    folders = [it for it in d.items.values() if it["mimeType"] == FOLDER]
    assert sorted(f["name"] for f in folders) == ["cva6", "janus_ec2_runs"]
    f = next(i for i, it in d.items.items() if it["name"] == "cva6")
    assert d.items["new2"]["parents"] == [f]


def test_root_is_never_created(env, monkeypatch):
    _, creds, bundle, logs = env
    d = FakeDrive({})
    m = _mgr(creds, logs, d, monkeypatch)
    assert m.upload_to_drive([bundle], "noc") == []
    assert not [c for c in d.calls if c[0] == "create"]
    assert "not visible" in m.last_drive_outcome
    assert any("share it" in l for l in logs)


def test_root_id_and_name_overrides(env, monkeypatch):
    _, creds, bundle, logs = env
    monkeypatch.setenv("JANUS_GDRIVE_ROOT_ID", "PINNED")
    d = FakeDrive({"B": {"name": "noc", "parents": ["PINNED"], "mimeType": FOLDER}})
    m = _mgr(creds, logs, d, monkeypatch)
    assert m.upload_to_drive([bundle], "noc") == ["new1"]
    assert d.items["new1"]["parents"] == ["B"]
    monkeypatch.delenv("JANUS_GDRIVE_ROOT_ID")
    monkeypatch.setenv("JANUS_GDRIVE_ROOT", "other_root")
    assert nt.NotificationManager(gdrive_enabled=True).gdrive_root == "other_root"


def test_quota_error_gets_actionable_hint(env, monkeypatch):
    _, creds, bundle, logs = env
    d = FakeDrive({"R": {"name": "janus_ec2_runs", "parents": [], "mimeType": FOLDER}},
                  fail_upload="HttpError 403 storageQuotaExceeded")
    m = _mgr(creds, logs, d, monkeypatch)
    assert m.upload_to_drive([bundle], "noc") == []
    assert "Shared Drive" in m.last_drive_outcome


def test_problem_probe(tmp_path, monkeypatch):
    assert nt.NotificationManager().gdrive_problem().startswith("off")
    m = nt.NotificationManager(gdrive_enabled=True, gdrive_creds=str(tmp_path / "nope"))
    assert "not found" in m.gdrive_problem()
    c = tmp_path / "c.json"
    c.write_text("{}")
    assert nt.NotificationManager(gdrive_enabled=True, gdrive_creds=str(c)).gdrive_problem() == ""


def test_real_service_builds_with_drive_scope(env):
    """Real googleapiclient build from a syntactically valid SA key (no network)."""
    tmp, _, _, logs = env
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    key = tmp / "sa.json"
    key.write_text(json.dumps({
        "type": "service_account", "project_id": "p", "private_key_id": "k",
        "private_key": pem, "client_email": "sa@p.iam.gserviceaccount.com",
        "client_id": "1", "token_uri": "https://oauth2.googleapis.com/token"}))
    m = nt.NotificationManager(gdrive_enabled=True, gdrive_creds=str(key), logger=logs.append)
    from googleapiclient.discovery import build
    svc = m._gdrive_service(build)
    assert svc is not None, logs
    assert m.gdrive_identity == "sa@p.iam.gserviceaccount.com"
    assert svc._http.credentials._scopes == ["https://www.googleapis.com/auth/drive"]


def test_notify_uploads_before_email_and_links_it(env, monkeypatch):
    tmp, creds, bundle, logs = env
    d = FakeDrive({"R": {"name": "janus_ec2_runs", "parents": [], "mimeType": FOLDER}})
    m = _mgr(creds, logs, d, monkeypatch)
    m.recipient, m.smtp_host, m.smtp_user, m.smtp_pass = "a@b", "h", "u", "p"
    sent = {}

    def fake_send(subject, body, att):
        sent["body"] = body
        m.last_outcome = "email sent"
        return True
    monkeypatch.setattr(m, "send_email", fake_send)
    m.notify("noc", "DONE", run_dir=str(tmp), bundle_dir=str(tmp / "bundles"))
    assert "https://drive/" in sent["body"]
    assert "janus_ec2_runs/noc/" in sent["body"]
    assert "[drive: uploaded 1 file(s)" in m.last_outcome
    noc = next(i for i, it in d.items.items() if it["name"] == "noc")
    assert any(it["parents"] == [noc] and it["mimeType"] == "file" for it in d.items.values())
