import asyncio
import pytest
from fastapi import HTTPException

from uuid import UUID

from gateway.integrations import HandlerRequest, HandlerResult
from gateway.templates import (
    BaseBot,
    EchoBot,
    command,
    get_template,
    register_template,
    template_catalog,
)


def request(text: str, command_name: str | None = None) -> HandlerRequest:
    return HandlerRequest(
        bot_id="123e4567-e89b-12d3-a456-426655440000",
        chat_id="123e4567-e89b-12d3-a456-426655440001",
        chat_type="group",
        user_id="123e4567-e89b-12d3-a456-426655440002",
        message_id="message-1",
        text=text,
        command=command_name,
    )


def test_echo_template_catalog_and_commands():
    entry = get_template("echo")
    assert entry is not None
    assert {item.name for item in entry.bot_class.command_catalog()} == {"/help", "/ping"}
    assert {item["id"] for item in template_catalog()} == {"echo"}

    assert asyncio.run(EchoBot().dispatch(request("/ping", "/ping"))).text == "pong"
    assert "Доступные команды" in asyncio.run(EchoBot().dispatch(request("/help", "/help"))).text
    assert asyncio.run(EchoBot().dispatch(request("hello"))).text is None


def test_command_inheritance_and_override_by_command_name():
    class ParentBot(BaseBot):
        @command("/shared", "Inherited command")
        async def shared(self, _request):
            return HandlerResult(text="parent")

        @command("/parent", "Inherited command")
        async def parent(self, _request):
            return HandlerResult(text="parent-only")

    class ChildBot(ParentBot):
        @command("/shared", "Child override")
        async def child_shared(self, _request):
            return "child"

    entries = {item.name: item.description for item in ChildBot.command_catalog()}
    assert entries == {"/parent": "Inherited command", "/shared": "Child override"}
    assert asyncio.run(ChildBot().dispatch(request("/shared", "/shared"))).text == "child"
    assert asyncio.run(ChildBot().dispatch(request("/parent", "/parent"))).text == "parent-only"


def test_undecorated_method_override_keeps_inherited_command_binding():
    class ParentBot(BaseBot):
        @command("/shared")
        async def shared(self, _request):
            return "parent"

    class ChildBot(ParentBot):
        async def shared(self, _request):
            return "child implementation"

    assert asyncio.run(ChildBot().dispatch(request("/shared", "/shared"))).text == "child implementation"


def test_registered_template_is_explicit_and_catalog_visible():
    @register_template("unit-test-template", name="Unit Test", description="test")
    class UnitTemplate(BaseBot):
        @command("/run", "Run test")
        async def run(self, _request):
            return "ok"

    try:
        catalog_entry = next(item for item in template_catalog() if item["id"] == "unit-test-template")
        assert catalog_entry == {
            "id": "unit-test-template",
            "name": "Unit Test",
            "description": "test",
            "commands": [{"name": "/run", "description": "Run test"}],
        }
        assert asyncio.run(UnitTemplate().dispatch(request("/run", "/run"))).text == "ok"
    finally:
        from gateway.templates import TEMPLATE_REGISTRY
        del TEMPLATE_REGISTRY["unit-test-template"]


def test_worker_dispatch_uses_selected_template_and_explicit_summary_compatibility(monkeypatch):
    import gateway.main as main

    record = main.Bot(
        id=UUID("123e4567-e89b-12d3-a456-426655440000"),
        name="test",
        template_id="echo",
        default_handler="echo",
    )
    output = asyncio.run(main.dispatch_template_message(record, request("/ping", "/ping")))
    assert output == "pong"

    calls = []

    async def legacy(*_args):
        calls.append("legacy")
        return "legacy summary"

    monkeypatch.setattr(main, "legacy_summary", legacy)
    summary_request = request("/summary 7d", "/summary")
    assert asyncio.run(main.dispatch_template_message(record, summary_request)) == "legacy summary"
    assert calls == ["legacy"]

    @register_template("unit-summary-template", name="Summary override")
    class SummaryTemplate(BaseBot):
        @command("/summary", "Template summary")
        async def summary(self, _request):
            return "template summary"

    try:
        record.template_id = "unit-summary-template"
        assert asyncio.run(main.dispatch_template_message(record, summary_request)) == "template summary"
        assert calls == ["legacy"]
        old_summary_job = {
            "kind": "summary",
            "text": "/summary 7d",
            "sender_id": "123e4567-e89b-12d3-a456-426655440002",
            "message_id": "old-summary-job",
            "is_group": True,
        }
        assert asyncio.run(main.dispatch_queued_message(
            record,
            record.id,
            UUID("123e4567-e89b-12d3-a456-426655440001"),
            old_summary_job,
        )) == "template summary"
        assert calls == ["legacy"]
    finally:
        from gateway.templates import TEMPLATE_REGISTRY
        del TEMPLATE_REGISTRY["unit-summary-template"]

    record.template_id = "removed-template"
    unavailable = asyncio.run(main.dispatch_template_message(record, request("/unknown", "/unknown")))
    assert "недоступен" in unavailable
    assert unavailable != "/unknown"


def test_template_routes_validate_and_update_binding(monkeypatch):
    import gateway.main as main

    catalog = asyncio.run(main.list_bot_templates())
    assert any(item["id"] == "echo" and {"name", "description"} <= item["commands"][0].keys() for item in catalog)
    unknown = main.bot_record_response(main.Bot(
        id=UUID("123e4567-e89b-12d3-a456-426655440000"),
        name="stale",
        template_id="removed-template",
    ))
    assert unknown["template_id"] == "removed-template"
    assert unknown["template_status"] == "unavailable"

    class FakeSession:
        def __init__(self, record):
            self.record = record

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _model, _bot_id):
            return self.record

        async def commit(self):
            return None

        async def refresh(self, _record):
            return None

    record = main.Bot(
        id=UUID("123e4567-e89b-12d3-a456-426655440000"),
        name="bound",
        template_id="echo",
    )
    monkeypatch.setattr(main, "Session", lambda: FakeSession(record))

    with pytest.raises(HTTPException) as invalid_create:
        asyncio.run(main.create_bot(main.BotCreateRequest(
            bot_id=record.id,
            cts_url="https://cts.example.org",
            secret_key="secret",
            name="Invalid",
            template_id="missing-template",
        )))
    assert invalid_create.value.status_code == 422
    with pytest.raises(HTTPException) as invalid_patch:
        asyncio.run(main.update_bot_template(
            record.id,
            main.BotTemplateUpdate(template_id="missing-template"),
        ))
    assert invalid_patch.value.status_code == 422

    result = asyncio.run(main.update_bot_template(
        record.id,
        main.BotTemplateUpdate(template_id="echo"),
    ))
    assert result["template_id"] == "echo"
    assert result["template_status"] == "available"


def test_create_bot_persists_selected_template_and_updates_runtime_provider(monkeypatch):
    import gateway.main as main
    from pybotx import BotAccountKey

    bot_id = UUID("123e4567-e89b-12d3-a456-426655440010")
    record = main.Bot(
        id=bot_id,
        name="pending",
        enabled=False,
        template_id="echo",
        cts_url=None,
        secret_key=None,
        credential_revision=1,
    )

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _model, _bot_id):
            return record

        async def commit(self):
            return None

        async def refresh(self, _record):
            return None

    invalidated = []
    monkeypatch.setattr(main, "Session", lambda: FakeSession())
    monkeypatch.setattr(
        main.account_provider,
        "upsert",
        lambda account: account.account_key,
    )
    monkeypatch.setattr(main.bot, "invalidate_account", invalidated.append)
    result = asyncio.run(main.create_bot(main.BotCreateRequest(
        bot_id=bot_id,
        cts_url="https://cts.example.org",
        secret_key="secret-value",
        name="Runtime Bot",
        template_id="echo",
    )))

    assert record.template_id == "echo"
    assert record.cts_url == "https://cts.example.org/"
    assert record.secret_key == "secret-value"
    assert result["template_status"] == "available"
    assert "secret_key" not in result
    assert invalidated == [BotAccountKey(server_id="cts.example.org", bot_id=bot_id)]
