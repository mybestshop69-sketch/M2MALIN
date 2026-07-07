import base64
import hashlib
import hmac
import importlib
import json
import sys
import types

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


def auth_headers():
    token = base64.b64encode(b"admin:test-password").decode("ascii")
    return {"Authorization": f"Basic {token}"}


def csrf_token(client):
    response = client.get("/messenger", headers=auth_headers())
    body = response.get_data(as_text=True)
    marker = 'name="csrf_token" value="'
    start = body.index(marker) + len(marker)
    return body[start:body.index('"', start)]


def signed(body: bytes) -> str:
    digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def payload(mid="mid-1", text="Bonjour", sender="user-1"):
    return {
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": "page-1"},
                        "timestamp": 1,
                        "message": {"mid": mid, "text": text},
                    }
                ],
            }
        ]
    }


def postback_payload(sender="user-1", timestamp=1, button_payload="BTN"):
    return {
        "entry": [
            {
                "id": "page-1",
                "messaging": [
                    {
                        "sender": {"id": sender},
                        "recipient": {"id": "page-1"},
                        "timestamp": timestamp,
                        "postback": {"payload": button_payload, "title": "Oui"},
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


def add_meta_connection(module):
    module.db.session.add(
        module.Connection(
            platform="meta",
            account_name="M2Malin",
            access_token_encrypted=module.encrypt_secret("page-token"),
            page_id="page-1",
        )
    )
    module.db.session.commit()


def fake_openai(monkeypatch, output_text=None, error=None):
    class FakeResponses:
        def create(self, **kwargs):
            if error:
                raise error
            return types.SimpleNamespace(output_text=output_text)

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))


def fake_shopify(monkeypatch):
    class FakeResponse:
        ok = True
        text = "<html><body><h1>Livraison</h1><p>Livraison suivie en France.</p></body></html>"

        def json(self):
            return {
                "products": [
                    {
                        "title": "Boite rangement",
                        "handle": "boite-rangement",
                        "variants": [{"price": "12.90", "title": "Default", "available": True}],
                    }
                ]
            }

    monkeypatch.setattr("messenger_assistant.requests.get", lambda *args, **kwargs: FakeResponse())


def fake_meta_send(monkeypatch, sent=None, fail=False):
    def fake_send(self, page_id, psid, text_value):
        if fail:
            raise RuntimeError("meta down")
        if sent is not None:
            sent.append({"page_id": page_id, "psid": psid, "text": text_value})
        return [{"message_id": f"out-{len(sent or [])}"}]

    monkeypatch.setattr("services.MetaClient.send_text_message", fake_send)


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

    bad = client.post("/webhooks/meta", data=body, content_type="application/json", headers={"X-Hub-Signature-256": "sha256=bad"})
    good = client.post("/webhooks/meta", data=body, content_type="application/json", headers={"X-Hub-Signature-256": signed(body)})
    duplicate = client.post("/webhooks/meta", data=body, content_type="application/json", headers={"X-Hub-Signature-256": signed(body)})

    assert bad.status_code == 403
    assert good.status_code == 200
    assert duplicate.status_code == 200
    with module.app.app_context():
        assert module.db.session.execute(text("select count(*) from messenger_messages")).scalar() == 1


def test_raw_psid_never_stored_in_messenger_event(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()

    assert post_signed(client, payload(sender="user-1")).status_code == 200

    with module.app.app_context():
        event_payload = module.db.session.execute(text("select payload from messenger_events")).scalar()
        assert "user-1" not in event_payload
        data = json.loads(event_payload)
        assert set(data) == {"page_id", "sender_hash", "message_id", "message_type", "timestamp", "has_attachment", "status"}
        assert data["sender_hash"] == hashlib.sha256(b"user-1").hexdigest()


def test_two_users_same_postback_create_distinct_events(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()

    assert post_signed(client, postback_payload(sender="user-1", button_payload="SAME")).status_code == 200
    assert post_signed(client, postback_payload(sender="user-2", button_payload="SAME")).status_code == 200

    with module.app.app_context():
        rows = module.db.session.execute(text("select event_id, payload from messenger_events order by id")).all()
        assert len(rows) == 2
        assert rows[0][0] != rows[1][0]
        assert "user-1" not in rows[0][1]
        assert "user-2" not in rows[1][1]


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


def test_dashboard_activation_and_deactivation(monkeypatch):
    monkeypatch.setenv("MESSENGER_AUTO_REPLY_ENABLED", "false")
    module = load_app(monkeypatch)
    client = module.app.test_client()
    token = csrf_token(client)

    assert client.post("/messenger/settings", headers=auth_headers(), data={"csrf_token": token, "enabled": "true"}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_auto_reply_enabled'")).scalar() == "true"

    token = csrf_token(client)
    assert client.post("/messenger/settings", headers=auth_headers(), data={"csrf_token": token, "enabled": "false"}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_auto_reply_enabled'")).scalar() == "false"


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


def test_human_transfer_detects_accented_keywords(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="J'ai une réclamation urgente, commande non reçue")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert module.db.session.execute(text("select needs_human from messenger_conversations")).scalar() == 1
        assert "conseiller" in sent[0]["text"]


def test_openai_normal_response_and_meta_send(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_shopify(monkeypatch)
    fake_openai(monkeypatch, output_text="Bonjour, je peux vous aider.")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload()).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar() == "completed"
        assert sent == [{"page_id": "page-1", "psid": "user-1", "text": "Bonjour, je peux vous aider."}]


def test_openai_failure_sends_fallback(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_shopify(monkeypatch)
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload()).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert module.db.session.execute(text("select needs_human from messenger_conversations")).scalar() == 1
        assert "difficulte" in sent[0]["text"]


def test_openai_handoff_marker_is_not_sent_to_client(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_shopify(monkeypatch)
    fake_openai(monkeypatch, output_text="[HUMAN_REQUIRED] Je préfère vérifier cette information plutôt que de vous donner une réponse incorrecte.")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload()).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        row = module.db.session.execute(text("select needs_human, bot_paused from messenger_conversations")).first()
        status = module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar()
        assert row == (1, 1)
        assert status == "human_required"
        assert "[HUMAN_REQUIRED]" not in sent[0]["text"]
        assert sent[0]["text"] == "Je préfère vérifier cette information plutôt que de vous donner une réponse incorrecte. Je transmets votre demande à un conseiller."


def test_policy_fetch_and_cache(monkeypatch):
    module = load_app(monkeypatch)
    calls = []

    class FakeResponse:
        ok = True
        text = "<html><body><script>secret()</script><p>Retour sous conditions.</p></body></html>"

        def json(self):
            return {"products": []}

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse()

    monkeypatch.setattr("messenger_assistant.requests.get", fake_get)
    with module.app.app_context():
        knowledge_1 = module.messenger_assistant["site_knowledge"]()
        knowledge_2 = module.messenger_assistant["site_knowledge"]()

    assert len(calls) == 6
    assert knowledge_1 == knowledge_2
    assert len(knowledge_1["policies"]) == 5
    assert "script" not in knowledge_1["policies"][0]["text"].lower()


def test_retry_after_failed_message(monkeypatch):
    module = load_app(monkeypatch)
    fake_shopify(monkeypatch)
    fake_openai(monkeypatch, output_text="Reponse test.")
    fake_meta_send(monkeypatch, fail=True)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload()).status_code == 200
    for _ in range(3):
        with module.app.app_context():
            module.db.session.execute(text("update messenger_messages set next_attempt_at = null"))
            module.db.session.commit()
        module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        message_id = module.db.session.execute(text("select id from messenger_messages where direction='inbound'")).scalar()
        assert module.db.session.execute(text("select status from messenger_messages where id=:id"), {"id": message_id}).scalar() == "failed"

    token = csrf_token(client)
    assert client.post(f"/messenger/messages/{message_id}/retry", headers=auth_headers(), data={"csrf_token": token}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select status from messenger_messages where id=:id"), {"id": message_id}).scalar() == "pending"
