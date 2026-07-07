import hashlib
import hmac
import importlib
import json
import sys

import pytest
from sqlalchemy import text


def load_app(monkeypatch):
    for name in ["app", "messenger_assistant"]:
        sys.modules.pop(name, None)
    monkeypatch.setenv("DISABLE_SCHEDULER", "true")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("META_APP_SECRET", "test-secret")
    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", "verify-token")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    module = importlib.import_module("app")
    module.app.config["TESTING"] = True
    return module


def signed(body: bytes) -> str:
    digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def payload(mid="mid-1", text="Bonjour"):
    return {
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": "user-1"},
                        "recipient": {"id": "page-1"},
                        "timestamp": 1,
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ]
    }


def post_signed(client, data):
    body = json.dumps(data).encode()
    return client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": signed(body)},
    )


def test_webhook_validation_and_bad_token(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()

    ok = client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=abc")
    bad = client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=abc")

    assert ok.status_code == 200
    assert ok.get_data(as_text=True) == "abc"
    assert bad.status_code == 403


def test_signature_rejection_and_queue_dedup(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    body = json.dumps(payload()).encode()

    bad = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )
    good = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": signed(body)},
    )
    duplicate = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": signed(body)},
    )

    assert bad.status_code == 403
    assert good.status_code == 200
    assert duplicate.status_code == 200
    with module.app.app_context():
        total = module.db.session.execute(text("select count(*) from messenger_messages")).scalar()
        assert total == 1


def test_echo_ignored_and_attachment_queued(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    data = {
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {"sender": {"id": "user-1"}, "timestamp": 1, "message": {"mid": "echo", "is_echo": True, "text": "x"}},
                    {"sender": {"id": "user-1"}, "timestamp": 2, "message": {"mid": "att", "attachments": [{"type": "image"}]}},
                ],
            }
        ]
    }

    assert post_signed(client, data).status_code == 200
    with module.app.app_context():
        rows = module.db.session.execute(text("select message_type, content from messenger_messages")).all()
        assert rows == [("attachment", "image")]


def test_admin_is_protected_and_webhook_is_not(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()

    assert client.get("/messenger").status_code == 401
    assert client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=abc").status_code == 200


def test_auto_disabled_does_not_send(monkeypatch):
    monkeypatch.setenv("MESSENGER_AUTO_REPLY_ENABLED", "false")
    module = load_app(monkeypatch)
    client = module.app.test_client()

    assert post_signed(client, payload()).status_code == 200
    module.messenger_assistant["process_pending"]()
    with module.app.app_context():
        outbound = module.db.session.execute(text("select count(*) from messenger_messages where direction='outbound'")).scalar()
        inbound_status = module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar()
        assert outbound == 0
        assert inbound_status == "completed"


def test_human_transfer_sends_ack(monkeypatch):
    module = load_app(monkeypatch)
    sent = {}

    def fake_send(self, page_id, psid, text_value):
        sent["message"] = text_value
        return [{"message_id": "out-1"}]

    monkeypatch.setattr("services.MetaClient.send_text_message", fake_send)
    client = module.app.test_client()
    with module.app.app_context():
        module.db.session.add(
            module.Connection(
                platform="meta",
                account_name="M2Malin",
                access_token_encrypted=module.encrypt_secret("page-token"),
                page_id="page-1",
            )
        )
        module.db.session.commit()

    assert post_signed(client, payload(text="Je veux parler a un conseiller")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        needs_human = module.db.session.execute(text("select needs_human from messenger_conversations")).scalar()
        assert needs_human == 1
        assert "conseiller" in sent["message"]
