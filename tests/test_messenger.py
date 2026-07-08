import base64
import hashlib
import hmac
import importlib
import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

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
    monkeypatch.setattr(sys.modules["messenger_assistant"], "_utc_now", lambda: datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc))
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
    return signed_with(body, b"test-secret")


def signed_with(body: bytes, secret: bytes) -> str:
    digest = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    if secret != b"test-secret":
        digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def signed_sha1(body: bytes, secret: bytes = b"test-secret") -> str:
    return f"sha1={hmac.new(secret, body, hashlib.sha1).hexdigest()}"


def load_meta_check_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_meta_configuration.py"
    spec = importlib.util.spec_from_file_location("check_meta_configuration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


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


def paris_utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Paris")).astimezone(timezone.utc)


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


def fake_meta_conversations(monkeypatch, conversations):
    def fake_get(self, page_id, limit=10):
        return conversations

    monkeypatch.setattr("services.MetaClient.get_messenger_conversations", fake_get)


def test_webhook_validation_and_bad_token(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()

    ok = client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=verify-token&hub.challenge=abc")
    bad = client.get("/webhooks/meta?hub.mode=subscribe&hub.verify_token=bad&hub.challenge=abc")

    assert ok.status_code == 200
    assert ok.get_data(as_text=True) == "abc"
    assert bad.status_code == 403


def test_health_exposes_render_commit_when_available(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc123")
    module = load_app(monkeypatch)
    client = module.app.test_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "commit": "abc123"}


def test_site_knowledge_refresh_job_is_separate_from_messenger_worker(monkeypatch):
    module = load_app(monkeypatch)

    refresh_job = module.scheduler.get_job("messenger-refresh-site-knowledge")
    process_job = module.scheduler.get_job("messenger-pending-messages")
    sync_job = module.scheduler.get_job("messenger-sync-inbox")

    assert refresh_job is not None
    assert process_job is not None
    assert sync_job is not None
    assert refresh_job.trigger.interval.total_seconds() == 21600
    assert process_job.trigger.interval.total_seconds() == 15
    assert sync_job.trigger.interval.total_seconds() == 60
    assert refresh_job.max_instances == 1
    assert process_job.max_instances == 1
    assert sync_job.max_instances == 1


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


def test_inbox_sync_queues_graph_message_without_raw_psid(monkeypatch):
    module = load_app(monkeypatch)
    fake_meta_conversations(
        monkeypatch,
        [
            {
                "id": "thread-1",
                "messages": {
                    "data": [
                        {
                            "id": "graph-mid-1",
                            "message": "Bonjour, quels sont vos delais de livraison ?",
                            "created_time": "2026-07-08T06:50:00+0000",
                            "from": {"id": "user-1", "name": "Client"},
                        }
                    ]
                },
            }
        ],
    )
    with module.app.app_context():
        add_meta_connection(module)

    assert module.messenger_assistant["sync_messenger_inbox"]() == 1

    with module.app.app_context():
        event_payload = module.db.session.execute(text("select payload from messenger_events")).scalar()
        message = module.db.session.execute(text("select content from messenger_messages")).scalar()
        assert message == "Bonjour, quels sont vos delais de livraison ?"
        assert "user-1" not in event_payload
        data = json.loads(event_payload)
        assert set(data) == {"page_id", "sender_hash", "message_id", "message_type", "timestamp", "has_attachment", "status"}
        assert data["sender_hash"] == hashlib.sha256(b"user-1").hexdigest()


def test_inbox_sync_ignores_page_messages_and_duplicates(monkeypatch):
    module = load_app(monkeypatch)
    fake_meta_conversations(
        monkeypatch,
        [
            {
                "messages": {
                    "data": [
                        {"id": "page-mid", "message": "Reponse page", "from": {"id": "page-1"}},
                        {"id": "graph-mid-2", "message": "Bonjour", "from": {"id": "user-1"}},
                    ]
                }
            }
        ],
    )
    with module.app.app_context():
        add_meta_connection(module)

    assert module.messenger_assistant["sync_messenger_inbox"]() == 1
    assert module.messenger_assistant["sync_messenger_inbox"]() == 0

    with module.app.app_context():
        assert module.db.session.execute(text("select count(*) from messenger_messages")).scalar() == 1
        assert module.db.session.execute(text("select content from messenger_messages")).scalar() == "Bonjour"


def test_dashboard_sync_button_uses_csrf_and_queues_inbox(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    fake_meta_conversations(
        monkeypatch,
        [{"messages": {"data": [{"id": "graph-mid-3", "message": "Bonjour", "from": {"id": "user-1"}}]}}],
    )
    with module.app.app_context():
        add_meta_connection(module)
    token = csrf_token(client)

    response = client.post("/messenger/sync-inbox", headers=auth_headers(), data={"csrf_token": token})

    assert response.status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select count(*) from messenger_messages")).scalar() == 1
        assert module.db.session.execute(text("select value from app_settings where key='messenger_last_inbox_sync_at'")).scalar()


def test_inbox_sync_then_process_sends_reply(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_meta_conversations(
        monkeypatch,
        [
            {
                "messages": {
                    "data": [
                        {
                            "id": "graph-mid-4",
                            "message": "Bonjour, quels sont vos delais de livraison ?",
                            "from": {"id": "user-1"},
                        }
                    ]
                }
            }
        ],
    )
    fake_openai(monkeypatch, output_text="Livraison suivie en France sous 5 a 8 jours ouvres.")
    fake_meta_send(monkeypatch, sent)
    with module.app.app_context():
        add_meta_connection(module)

    assert module.messenger_assistant["sync_messenger_inbox"]() == 1
    module.messenger_assistant["process_pending"]()

    assert sent == [{"page_id": "page-1", "psid": "user-1", "text": "Livraison suivie en France sous 5 a 8 jours ouvres."}]


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


def test_dashboard_schedule_settings(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    token = csrf_token(client)

    response = client.post(
        "/messenger/settings",
        headers=auth_headers(),
        data={"csrf_token": token, "start_time": "19:30", "end_time": "08:15", "timezone": "Europe/Paris"},
    )

    assert response.status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_schedule_start_time'")).scalar() == "19:30"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_schedule_end_time'")).scalar() == "08:15"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_schedule_timezone'")).scalar() == "Europe/Paris"


def test_dashboard_manual_modes(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()

    token = csrf_token(client)
    assert client.post("/messenger/settings", headers=auth_headers(), data={"csrf_token": token, "mode": "force_on"}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_auto_reply_mode'")).scalar() == "force_on"
        assert module.messenger_assistant["schedule_status"](paris_utc(2026, 1, 5, 12, 0))["active"] is True

    token = csrf_token(client)
    assert client.post("/messenger/settings", headers=auth_headers(), data={"csrf_token": token, "mode": "force_off"}).status_code == 302
    with module.app.app_context():
        assert module.messenger_assistant["schedule_status"](paris_utc(2026, 1, 5, 20, 0))["active"] is False

    token = csrf_token(client)
    assert client.post("/messenger/settings", headers=auth_headers(), data={"csrf_token": token, "mode": "schedule"}).status_code == 302
    with module.app.app_context():
        assert module.messenger_assistant["schedule_status"](paris_utc(2026, 1, 5, 12, 0))["active"] is False


def test_messenger_schedule_boundaries(monkeypatch):
    module = load_app(monkeypatch)
    cases = [
        (paris_utc(2026, 1, 5, 8, 59), True),
        (paris_utc(2026, 1, 5, 9, 0), False),
        (paris_utc(2026, 1, 5, 17, 59), False),
        (paris_utc(2026, 1, 5, 18, 0), True),
        (paris_utc(2026, 1, 6, 0, 0), True),
    ]

    with module.app.app_context():
        for now_utc, expected in cases:
            assert module.messenger_assistant["schedule_status"](now_utc)["active"] is expected


def test_messenger_schedule_weekend(monkeypatch):
    module = load_app(monkeypatch)

    with module.app.app_context():
        assert module.messenger_assistant["schedule_status"](paris_utc(2026, 1, 10, 8, 59))["active"] is True
        assert module.messenger_assistant["schedule_status"](paris_utc(2026, 1, 10, 12, 0))["active"] is False
        assert module.messenger_assistant["schedule_status"](paris_utc(2026, 1, 11, 18, 0))["active"] is True


def test_messenger_schedule_dst_transitions_europe_paris(monkeypatch):
    module = load_app(monkeypatch)

    with module.app.app_context():
        summer_start = module.messenger_assistant["schedule_status"](paris_utc(2026, 3, 29, 8, 59))
        summer_day = module.messenger_assistant["schedule_status"](paris_utc(2026, 3, 29, 9, 0))
        winter_start = module.messenger_assistant["schedule_status"](paris_utc(2026, 10, 25, 8, 59))
        winter_day = module.messenger_assistant["schedule_status"](paris_utc(2026, 10, 25, 9, 0))

    assert summer_start["active"] is True
    assert summer_day["active"] is False
    assert winter_start["active"] is True
    assert winter_day["active"] is False
    assert summer_start["timezone"] == "Europe/Paris"
    assert winter_start["timezone"] == "Europe/Paris"


def test_daytime_message_is_kept_for_human_without_auto_reply(monkeypatch):
    module = load_app(monkeypatch)
    monkeypatch.setattr(sys.modules["messenger_assistant"], "_utc_now", lambda: paris_utc(2026, 1, 5, 12, 0))
    sent = []
    fake_openai(monkeypatch, output_text="Reponse IA")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="bonjour")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert sent == []
        assert module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar() == "human_required"
        assert module.db.session.execute(text("select needs_human from messenger_conversations")).scalar() == 1


def test_force_on_answers_during_daytime(monkeypatch):
    module = load_app(monkeypatch)
    monkeypatch.setattr(sys.modules["messenger_assistant"], "_utc_now", lambda: paris_utc(2026, 1, 5, 12, 0))
    sent = []
    fake_openai(monkeypatch, output_text="Bonjour, je peux vous aider.")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)
        module.db.session.execute(text("insert into app_settings(key, value, updated_at) values('messenger_auto_reply_mode', 'force_on', CURRENT_TIMESTAMP)"))
        module.db.session.commit()

    assert post_signed(client, payload(text="bonjour")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert sent[0]["text"] == "Bonjour, je peux vous aider."
        assert module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar() == "completed"


def test_force_on_answers_paused_conversation(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, output_text="Nous vendons des produits pratiques pour la maison et le rangement.")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)
        module.db.session.execute(text("insert into app_settings(key, value, updated_at) values('messenger_auto_reply_mode', 'force_on', CURRENT_TIMESTAMP)"))
        module.db.session.commit()

    assert post_signed(client, payload(mid="mid-1", text="Question a verifier")).status_code == 200
    with module.app.app_context():
        module.db.session.execute(text("update messenger_messages set status='human_required' where meta_message_id='mid-1'"))
        module.db.session.execute(text("update messenger_conversations set needs_human=1, bot_paused=1"))
        module.db.session.commit()
    assert post_signed(client, payload(mid="mid-2", text="Bonjour, vous vendez quoi ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert sent[0]["text"] == "Nous vendons des produits pratiques pour la maison et le rangement."
        assert module.db.session.execute(text("select status from messenger_messages where meta_message_id='mid-2'")).scalar() == "completed"
        assert module.db.session.execute(text("select needs_human, bot_paused from messenger_conversations")).first() == (0, 0)


def test_dashboard_meta_configuration_check_valid(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "1551714796659004")
    module = load_app(monkeypatch)
    client = module.app.test_client()

    class FakeResponse:
        ok = True

        def json(self):
            return {"id": "1551714796659004"}

    monkeypatch.setattr("messenger_assistant.requests.get", lambda *args, **kwargs: FakeResponse())
    token = csrf_token(client)

    assert client.post("/messenger/check-meta-config", headers=auth_headers(), data={"csrf_token": token}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_app_id_valid'")).scalar() == "true"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_app_secret_valid'")).scalar() == "true"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_app_id_detected'")).scalar() == "1551714796659004"


def test_dashboard_meta_configuration_check_detects_wrong_app(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "4419342638395501")
    module = load_app(monkeypatch)
    client = module.app.test_client()

    class FakeResponse:
        ok = True

        def json(self):
            return {"id": "4419342638395501"}

    monkeypatch.setattr("messenger_assistant.requests.get", lambda *args, **kwargs: FakeResponse())
    token = csrf_token(client)

    assert client.post("/messenger/check-meta-config", headers=auth_headers(), data={"csrf_token": token}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_app_id_valid'")).scalar() == "false"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_app_secret_valid'")).scalar() == "true"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_expected_app_id'")).scalar() == "1551714796659004"


def test_dashboard_meta_token_check_validates_page_token_and_subscription(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "1551714796659004")
    module = load_app(monkeypatch)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    class FakeResponse:
        ok = True

        def __init__(self, payload_value):
            self.payload_value = payload_value

        def json(self):
            return self.payload_value

    def fake_get(url, **kwargs):
        if url.endswith("/debug_token"):
            return FakeResponse(
                {
                    "data": {
                        "app_id": "1551714796659004",
                        "profile_id": "1163222070213376",
                        "expires_at": 0,
                        "scopes": "pages_messaging,pages_manage_metadata,pages_show_list,pages_read_engagement",
                    }
                }
            )
        return FakeResponse(
            {
                "data": [
                    {
                        "id": "1551714796659004",
                        "subscribed_fields": ["messages", "messaging_postbacks"],
                    }
                ]
            }
        )

    monkeypatch.setattr("messenger_assistant.requests.get", fake_get)
    token = csrf_token(client)

    assert client.post("/messenger/check-meta-token", headers=auth_headers(), data={"csrf_token": token}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_token_app_valid'")).scalar() == "true"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_page_id_valid'")).scalar() == "true"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_permissions_valid'")).scalar() == "true"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_subscription_valid'")).scalar() == "true"


def test_dashboard_meta_token_check_accepts_list_scopes(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "1551714796659004")
    module = load_app(monkeypatch)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    class FakeResponse:
        ok = True

        def __init__(self, payload_value):
            self.payload_value = payload_value

        def json(self):
            return self.payload_value

    def fake_get(url, **kwargs):
        if url.endswith("/debug_token"):
            return FakeResponse(
                {
                    "data": {
                        "app_id": "1551714796659004",
                        "profile_id": "1163222070213376",
                        "scopes": [
                            "pages_messaging",
                            "pages_manage_metadata",
                            "pages_show_list",
                            "pages_read_engagement",
                        ],
                    }
                }
            )
        return FakeResponse(
            {
                "data": [
                    {
                        "id": "1551714796659004",
                        "subscribed_fields": ["messages", "messaging_postbacks"],
                    }
                ]
            }
        )

    monkeypatch.setattr("messenger_assistant.requests.get", fake_get)
    token = csrf_token(client)

    assert client.post("/messenger/check-meta-token", headers=auth_headers(), data={"csrf_token": token}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_missing_permissions'")).scalar() == ""
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_permissions_valid'")).scalar() == "true"


def test_dashboard_meta_token_check_detects_old_app_and_missing_subscription(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "1551714796659004")
    module = load_app(monkeypatch)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    class FakeResponse:
        ok = True

        def __init__(self, payload_value):
            self.payload_value = payload_value

        def json(self):
            return self.payload_value

    def fake_get(url, **kwargs):
        if url.endswith("/debug_token"):
            return FakeResponse(
                {
                    "data": {
                        "app_id": "4419342638395501",
                        "profile_id": "1163222070213376",
                        "expires_at": 123,
                        "scopes": "pages_messaging,pages_show_list",
                    }
                }
            )
        return FakeResponse(
            {
                "data": [
                    {
                        "id": "1551714796659004",
                        "subscribed_fields": ["messages"],
                    }
                ]
            }
        )

    monkeypatch.setattr("messenger_assistant.requests.get", fake_get)
    token = csrf_token(client)

    assert client.post("/messenger/check-meta-token", headers=auth_headers(), data={"csrf_token": token}).status_code == 302
    with module.app.app_context():
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_token_app_valid'")).scalar() == "false"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_page_id_valid'")).scalar() == "true"
        missing = module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_missing_permissions'")).scalar()
        assert "pages_manage_metadata" in missing
        assert "pages_read_engagement" in missing
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_subscription_postbacks'")).scalar() == "false"
        assert module.db.session.execute(text("select value from app_settings where key='messenger_meta_token_subscription_valid'")).scalar() == "false"


def test_auto_disabled_does_not_send(monkeypatch):
    monkeypatch.setenv("MESSENGER_AUTO_REPLY_ENABLED", "false")
    module = load_app(monkeypatch)
    client = module.app.test_client()

    assert post_signed(client, payload(text="Pouvez-vous verifier mon dossier client ?")).status_code == 200
    module.messenger_assistant["process_pending"]()
    with module.app.app_context():
        outbound = module.db.session.execute(text("select count(*) from messenger_messages where direction='outbound'")).scalar()
        inbound_status = module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar()
        assert outbound == 0
        assert inbound_status == "human_required"


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

    assert post_signed(client, payload(text="Pouvez-vous verifier mon dossier client ?")).status_code == 200
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

    assert post_signed(client, payload(text="Pouvez-vous verifier mon dossier client ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert module.db.session.execute(text("select needs_human from messenger_conversations")).scalar() == 1
        assert "difficulte" in sent[0]["text"]


def test_openai_failure_delivery_question_gets_useful_fallback(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="Quels sont vos delais de livraison ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        row = module.db.session.execute(text("select needs_human, bot_paused from messenger_conversations")).first()
        status = module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar()
        assert row == (0, 0)
        assert status == "completed"
        assert "delais de livraison" in sent[0]["text"]


def test_openai_failure_location_question_gets_useful_fallback(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="Ou vous trouvez-vous ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert module.db.session.execute(text("select needs_human from messenger_conversations")).scalar() == 0
        assert sent[0]["text"] == "M2 Malin est une boutique francaise basee a Aix-en-Provence. Vous pouvez decouvrir la boutique ici : https://m2malin.fr"


def test_openai_failure_greeting_gets_useful_fallback(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="bonjour")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar() == "completed"
        assert sent[0]["text"] == "Bonjour. Merci d'avoir contacte M2 Malin. Comment puis-je vous aider aujourd'hui ? Vous pouvez me poser une question sur un produit, la livraison, une commande ou un retour."


def test_paused_conversation_still_answers_safe_faq(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(mid="mid-1", text="Question inconnue")).status_code == 200
    module.messenger_assistant["process_pending"]()
    assert post_signed(client, payload(mid="mid-2", text="Ou vous trouvez-vous ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        rows = module.db.session.execute(text("select status from messenger_messages where direction='inbound' order by id")).all()
        assert [row[0] for row in rows] == ["human_required", "completed"]
        assert sent[-1]["text"] == "M2 Malin est une boutique francaise basee a Aix-en-Provence. Vous pouvez decouvrir la boutique ici : https://m2malin.fr"


def test_paused_conversation_still_answers_greeting(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(mid="mid-1", text="Question inconnue")).status_code == 200
    module.messenger_assistant["process_pending"]()
    assert post_signed(client, payload(mid="mid-2", text="salut")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        rows = module.db.session.execute(text("select status from messenger_messages where direction='inbound' order by id")).all()
        assert [row[0] for row in rows] == ["human_required", "completed"]
        assert sent[-1]["text"] == "Bonjour. Merci d'avoir contacte M2 Malin. Comment puis-je vous aider aujourd'hui ? Vous pouvez me poser une question sur un produit, la livraison, une commande ou un retour."


def test_worker_recovers_safe_faq_already_marked_human_required(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="Ou vous trouvez-vous ?")).status_code == 200
    with module.app.app_context():
        module.db.session.execute(text("update messenger_messages set status='human_required', processed_at=null"))
        module.db.session.execute(text("update messenger_conversations set needs_human=1, bot_paused=1"))
        module.db.session.commit()
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar() == "completed"
        assert sent[0]["text"] == "M2 Malin est une boutique francaise basee a Aix-en-Provence. Vous pouvez decouvrir la boutique ici : https://m2malin.fr"


def test_worker_recovers_greeting_already_marked_human_required(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="bonjour")).status_code == 200
    with module.app.app_context():
        module.db.session.execute(text("update messenger_messages set status='human_required', processed_at=null"))
        module.db.session.execute(text("update messenger_conversations set needs_human=1, bot_paused=1"))
        module.db.session.commit()
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar() == "completed"
        assert sent[0]["text"] == "Bonjour. Merci d'avoir contacte M2 Malin. Comment puis-je vous aider aujourd'hui ? Vous pouvez me poser une question sur un produit, la livraison, une commande ou un retour."


def test_worker_recovers_only_latest_safe_faq_per_conversation(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=RuntimeError("openai down"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(mid="mid-old", text="Quels sont vos delais de livraison ?")).status_code == 200
    assert post_signed(client, payload(mid="mid-new", text="Ou vous trouvez-vous ?")).status_code == 200
    with module.app.app_context():
        module.db.session.execute(text("update messenger_messages set status='human_required', processed_at=null"))
        module.db.session.execute(text("update messenger_conversations set needs_human=1, bot_paused=1"))
        module.db.session.commit()
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        rows = module.db.session.execute(
            text("select meta_message_id, status from messenger_messages where direction='inbound' order by id")
        ).all()
        assert rows == [("mid-old", "completed"), ("mid-new", "completed")]
        assert len(sent) == 1
        assert sent[0]["text"] == "M2 Malin est une boutique francaise basee a Aix-en-Provence. Vous pouvez decouvrir la boutique ici : https://m2malin.fr"


def test_openai_handoff_marker_is_not_sent_to_client(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_shopify(monkeypatch)
    fake_openai(monkeypatch, output_text="[HUMAN_REQUIRED] Je préfère vérifier cette information plutôt que de vous donner une réponse incorrecte.")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="Pouvez-vous verifier mon dossier client ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        row = module.db.session.execute(text("select needs_human, bot_paused from messenger_conversations")).first()
        status = module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar()
        assert row == (1, 1)
        assert status == "human_required"
        assert "[HUMAN_REQUIRED]" not in sent[0]["text"]
        assert sent[0]["text"] == "Je prefere verifier cette information plutot que de vous donner une reponse incorrecte. Je transmets votre demande a un conseiller qui reprendra a partir de 9 h."


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

    assert post_signed(client, payload(text="Pouvez-vous verifier mon dossier client ?")).status_code == 200
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


def test_process_pending_always_finishes_and_removes_session(monkeypatch):
    module = load_app(monkeypatch)
    fake_openai(monkeypatch, output_text="Reponse test.")
    fake_meta_send(monkeypatch, fail=True)
    removed = []
    original_remove = module.db.session.remove

    def tracked_remove():
        removed.append(True)
        return original_remove()

    monkeypatch.setattr(module.db.session, "remove", tracked_remove)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload()).status_code == 200
    before_process = len(removed)
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        status = module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar()
        assert status == "pending"
        assert len(removed) > before_process


def test_shopify_slow_call_does_not_block_messenger_processing(monkeypatch):
    module = load_app(monkeypatch)
    sent = []

    def fail_if_called(*args, **kwargs):
        raise AssertionError("Shopify ne doit pas etre appele pendant process_pending")

    monkeypatch.setattr("messenger_assistant.requests.get", fail_if_called)
    fake_openai(monkeypatch, output_text="Reponse depuis OpenAI.")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload()).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        assert sent[0]["text"] == "Reponse depuis OpenAI."
        assert module.db.session.execute(text("select status from messenger_messages where direction='inbound'")).scalar() == "completed"


def test_openai_timeout_is_controlled_and_sends_fallback(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, error=TimeoutError("openai timeout"))
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="Pouvez-vous verifier mon dossier client ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        row = module.db.session.execute(text("select needs_human, bot_paused from messenger_conversations")).first()
        assert row == (1, 1)
        assert "difficulte" in sent[0]["text"]


def test_meta_timeout_is_controlled_and_retried(monkeypatch):
    module = load_app(monkeypatch)
    fake_openai(monkeypatch, output_text="Reponse test.")
    fake_meta_send(monkeypatch, fail=True)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload()).status_code == 200
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        row = module.db.session.execute(
            text("select status, retry_count, error_message from messenger_messages where direction='inbound'")
        ).first()
        assert row[0] == "pending"
        assert row[1] == 1
        assert row[2] == "RuntimeError: meta down"


def test_stuck_processing_message_is_reset(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()

    assert post_signed(client, payload()).status_code == 200
    with module.app.app_context():
        old = datetime.utcnow() - timedelta(minutes=3)
        module.db.session.execute(
            text("update messenger_messages set status='processing', created_at=:created_at"),
            {"created_at": old},
        )
        module.db.session.commit()
        count = module.messenger_assistant["reset_stuck_processing"]()
        module.db.session.commit()
        status = module.db.session.execute(text("select status from messenger_messages")).scalar()

    assert count == 1
    assert status == "pending"


def test_valid_webhook_is_logged(monkeypatch, caplog):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    caplog.set_level("WARNING")

    assert post_signed(client, payload()).status_code == 200

    assert "messenger.webhook.received" in caplog.text
    assert "messenger.webhook.queued count=1" in caplog.text


def test_invalid_signature_is_logged_and_counted(monkeypatch, caplog):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    caplog.set_level("WARNING")
    body = json.dumps(payload()).encode()

    response = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )

    assert response.status_code == 403
    assert "messenger.webhook.signature_mismatch" in caplog.text
    with module.app.app_context():
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_invalid_signature_count'")
        ).scalar() == "1"


def test_next_message_is_processed_after_previous_failure(monkeypatch):
    module = load_app(monkeypatch)
    fake_openai(monkeypatch, output_text="Reponse test.")
    sends = {"count": 0}

    def flaky_send(self, page_id, psid, text_value):
        sends["count"] += 1
        if sends["count"] == 1:
            raise RuntimeError("meta down")
        return [{"message_id": f"out-{sends['count']}"}]

    monkeypatch.setattr("services.MetaClient.send_text_message", flaky_send)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(mid="mid-1", sender="user-1")).status_code == 200
    assert post_signed(client, payload(mid="mid-2", sender="user-2")).status_code == 200
    module.messenger_assistant["process_pending"]()
    module.messenger_assistant["process_pending"]()

    with module.app.app_context():
        rows = module.db.session.execute(
            text("select status from messenger_messages where direction='inbound' order by id")
        ).all()
        assert [row[0] for row in rows] == ["pending", "completed"]


def test_meta_signature_valid_with_real_hmac(monkeypatch, caplog):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    caplog.set_level("WARNING")
    body = json.dumps(payload()).encode()

    response = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": signed_with(body, b"test-secret")},
    )

    assert response.status_code == 200
    assert "messenger.webhook.signature_valid" in caplog.text
    with module.app.app_context():
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_signature_present'")
        ).scalar() == "true"


def test_meta_legacy_sha1_signature_is_accepted_when_sha256_absent(monkeypatch, caplog):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    caplog.set_level("WARNING")
    body = json.dumps(payload()).encode()

    response = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature": signed_sha1(body)},
    )

    assert response.status_code == 200
    assert "messenger.webhook.signature_valid" in caplog.text
    with module.app.app_context():
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_header_signature_256_present'")
        ).scalar() == "false"
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_header_signature_sha1_present'")
        ).scalar() == "true"
        assert module.db.session.execute(text("select count(*) from messenger_messages")).scalar() == 1


def test_meta_sha256_has_priority_over_legacy_sha1(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    body = json.dumps(payload()).encode()

    response = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={
            "X-Hub-Signature-256": "sha256=bad",
            "X-Hub-Signature": signed_sha1(body),
        },
    )

    assert response.status_code == 403
    with module.app.app_context():
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_signature_reject_reason'")
        ).scalar() == "signature_format_invalid"
        assert module.db.session.execute(text("select count(*) from messenger_messages")).scalar() == 0


def test_webhook_header_presence_diagnostics_are_recorded_without_values(monkeypatch, caplog):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    caplog.set_level("WARNING")
    body = json.dumps(payload()).encode()

    response = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature": signed_sha1(body), "User-Agent": "Meta-Test-Agent"},
    )

    assert response.status_code == 200
    assert "messenger.webhook.headers sig256=absent sigsha1=present content_type=present user_agent=present" in caplog.text
    assert signed_sha1(body) not in caplog.text
    assert "Meta-Test-Agent" not in caplog.text
    with module.app.app_context():
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_header_content_type_present'")
        ).scalar() == "true"
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_header_user_agent_present'")
        ).scalar() == "true"


def test_meta_signature_with_wrong_secret_is_rejected(monkeypatch, caplog):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    caplog.set_level("WARNING")
    body = json.dumps(payload()).encode()

    response = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": signed_with(body, b"wrong-secret")},
    )

    assert response.status_code == 403
    assert "messenger.webhook.signature_mismatch" in caplog.text
    with module.app.app_context():
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_signature_reject_reason'")
        ).scalar() == "signature_mismatch"


def test_meta_signature_rejects_modified_body(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    original = json.dumps(payload(text="Bonjour")).encode()
    modified = json.dumps(payload(text="Bonjour modifie")).encode()

    response = client.post(
        "/webhooks/meta",
        data=modified,
        content_type="application/json",
        headers={"X-Hub-Signature-256": signed_with(original, b"test-secret")},
    )

    assert response.status_code == 403
    with module.app.app_context():
        assert module.db.session.execute(
            text("select count(*) from messenger_messages")
        ).scalar() == 0


def test_meta_signature_missing_and_bad_format_are_diagnosed(monkeypatch, caplog):
    module = load_app(monkeypatch)
    client = module.app.test_client()
    body = json.dumps(payload()).encode()
    caplog.set_level("WARNING")

    missing = client.post("/webhooks/meta", data=body, content_type="application/json")
    malformed = client.post(
        "/webhooks/meta",
        data=body,
        content_type="application/json",
        headers={"X-Hub-Signature-256": "sha1=bad"},
    )

    assert missing.status_code == 403
    assert malformed.status_code == 403
    assert "messenger.webhook.signature_missing" in caplog.text
    assert "messenger.webhook.signature_mismatch" in caplog.text
    with module.app.app_context():
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_signature_reject_reason'")
        ).scalar() == "signature_format_invalid"


def test_meta_test_sample_without_signature_is_not_queued(monkeypatch):
    module = load_app(monkeypatch)
    client = module.app.test_client()

    response = client.post("/webhooks/meta", json=payload())

    assert response.status_code == 403
    with module.app.app_context():
        assert module.db.session.execute(text("select count(*) from messenger_messages")).scalar() == 0
        assert module.db.session.execute(
            text("select value from app_settings where key='messenger_last_signature_present'")
        ).scalar() == "false"


def test_check_meta_configuration_accepts_expected_app_id():
    script = load_meta_check_script()

    class FakeResponse:
        ok = True

        def json(self):
            return {"id": "1551714796659004"}

    result = script.check_meta_app_credentials(
        "1551714796659004",
        "secret",
        get=lambda *args, **kwargs: FakeResponse(),
    )

    assert result == {
        "app_id_valid": True,
        "app_id_detected": "1551714796659004",
        "expected_app_id": "1551714796659004",
        "app_secret_valid": True,
    }


def test_check_meta_configuration_rejects_wrong_app_id():
    script = load_meta_check_script()

    class FakeResponse:
        ok = True

        def json(self):
            return {"id": "4419342638395501"}

    result = script.check_meta_app_credentials(
        "4419342638395501",
        "secret",
        get=lambda *args, **kwargs: FakeResponse(),
    )

    assert result["app_id_valid"] is False
    assert result["app_id_detected"] == "4419342638395501"
    assert result["app_secret_valid"] is True


def test_token_debug_detects_wrong_meta_application():
    script = load_meta_check_script()

    summary = script.summarize_token_debug(
        {
            "data": {
                "app_id": "4419342638395501",
                "profile_id": "1163222070213376",
                "scopes": "pages_messaging,pages_show_list",
                "expires_at": 123,
            }
        }
    )

    assert summary["token_app_valid"] is False
    assert summary["token_app_id_detected"] == "4419342638395501"
    assert summary["page_id"] == "1163222070213376"
    assert "pages_manage_metadata" in summary["missing_scopes"]


def test_token_debug_accepts_list_scopes():
    script = load_meta_check_script()

    summary = script.summarize_token_debug(
        {
            "data": {
                "app_id": "1551714796659004",
                "profile_id": "1163222070213376",
                "scopes": [
                    "pages_messaging",
                    "pages_manage_metadata",
                    "pages_show_list",
                    "pages_read_engagement",
                ],
            }
        }
    )

    assert summary["missing_scopes"] == []


def test_real_messenger_message_flow_with_secure_logs(monkeypatch, caplog):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, output_text="Bonjour, votre commande est suivie.")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    caplog.set_level("WARNING")
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="Bonjour, pouvez-vous aider ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    logs = caplog.text
    assert "messenger.message.queued" in logs
    assert "messenger.message.processing" in logs
    assert "messenger.openai.completed" in logs
    assert "messenger.reply.sent" in logs
    assert "messenger.message.completed" in logs
    assert "user-1" not in logs
    assert "page-token" not in logs
    assert sent[0]["text"] == "Bonjour, votre commande est suivie."


def test_delivery_question_does_not_get_generic_welcome(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, output_text="Bonjour, bienvenue chez M2 Malin. Comment puis-je vous aider ?")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)
        module.db.session.execute(
            text(
                "insert into site_knowledge_cache (cache_key, payload, expires_at, updated_at) "
                "values ('public_site', :payload, :expires_at, :updated_at)"
            ),
            {
                "payload": json.dumps(
                    {
                        "site": "https://m2malin.fr",
                        "products": [],
                        "policies": [
                            {
                                "name": "livraison",
                                "url": "https://m2malin.fr/policies/shipping-policy",
                                "text": "Livraison suivie en France sous 5 a 8 jours ouvres.",
                            }
                        ],
                    }
                ),
                "expires_at": datetime.utcnow() + timedelta(hours=1),
                "updated_at": datetime.utcnow(),
            },
        )
        module.db.session.commit()

    assert post_signed(client, payload(text="Bonjour, quels sont vos delais de livraison ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    assert "Livraison suivie en France sous 5 a 8 jours ouvres." in sent[0]["text"]
    assert "Comment puis-je vous aider" not in sent[0]["text"]


def test_delivery_question_without_policy_uses_required_fallback(monkeypatch):
    module = load_app(monkeypatch)
    sent = []
    fake_openai(monkeypatch, output_text="Bonjour, bienvenue chez M2 Malin. Comment puis-je vous aider ?")
    fake_meta_send(monkeypatch, sent)
    client = module.app.test_client()
    with module.app.app_context():
        add_meta_connection(module)

    assert post_signed(client, payload(text="Quels sont vos delais de livraison ?")).status_code == 200
    module.messenger_assistant["process_pending"]()

    assert sent[0]["text"] == "Les delais de livraison peuvent varier selon le produit. Ils sont indiques sur la fiche du produit et lors de la validation de la commande. Envoyez-moi le nom ou le lien du produit concerne afin que je verifie le delai correspondant."
