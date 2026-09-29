from __future__ import annotations

import json

import pytest

import interlock.connections.connectors as connector_module
from interlock.connections.connectors import (
    CONNECTOR_DEFINITIONS,
    GoogleWorkspaceAdapter,
    GwsRunner,
    role_templates_for_connector,
    sanitize_config,
)


class FakeGwsRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict]] = []

    async def run_json(
        self,
        connection_config,
        command,
        *,
        params=None,
        body=None,
        timeout_seconds=None,
        page_all=False,
    ):
        self.calls.append((command, params or {}))
        if command == ("drive", "about", "get"):
            return {"user": {"emailAddress": "agent@example.com"}}
        if command == ("drive", "files", "list"):
            return {
                "files": [
                    {
                        "id": "doc-1",
                        "name": "Runbook",
                        "mimeType": "application/vnd.google-apps.document",
                        "modifiedTime": "2026-06-01T00:00:00Z",
                        "parents": ["folder-1"],
                    },
                    {
                        "id": "pdf-1",
                        "name": "Policy.pdf",
                        "mimeType": "application/pdf",
                    },
                ]
            }
        if command == ("gmail", "users", "messages", "list"):
            return {"messages": [{"id": "msg-1", "threadId": "thread-1", "snippet": "hello"}]}
        if command == ("calendar", "events", "list"):
            return {
                "items": [
                    {
                        "id": "evt-1",
                        "summary": "Claims review",
                        "updated": "2026-06-02T00:00:00Z",
                    }
                ]
            }
        if command == ("docs", "documents", "get"):
            return {"documentId": params["documentId"], "title": "Runbook"}
        if command == ("gmail", "users", "messages", "get"):
            return {"id": params["id"], "payload": {"headers": []}}
        if command == ("calendar", "events", "get"):
            return {"id": params["eventId"], "summary": "Claims review"}
        raise AssertionError(f"unexpected gws command: {command}")


class FakeGoogleCall:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        return self.payload


class FakeGoogleService:
    def about(self):
        return self

    def files(self):
        return self

    def users(self):
        return self

    def messages(self):
        return self

    def events(self):
        return self

    def documents(self):
        return self

    def spreadsheets(self):
        return self

    def presentations(self):
        return self

    def get(self, **kwargs):
        if "fields" in kwargs:
            return FakeGoogleCall({"user": {"emailAddress": "agent@example.com"}})
        if "documentId" in kwargs:
            return FakeGoogleCall({"documentId": kwargs["documentId"], "title": "Runbook"})
        if "userId" in kwargs and "id" in kwargs:
            return FakeGoogleCall({"id": kwargs["id"], "payload": {"headers": []}})
        if "calendarId" in kwargs and "eventId" in kwargs:
            return FakeGoogleCall({"id": kwargs["eventId"], "summary": "Claims review"})
        return FakeGoogleCall({"id": kwargs.get("fileId"), "name": "Policy.pdf"})

    def list(self, **kwargs):
        if "q" in kwargs:
            return FakeGoogleCall(
                {
                    "files": [
                        {
                            "id": "doc-1",
                            "name": "Runbook",
                            "mimeType": "application/vnd.google-apps.document",
                        }
                    ]
                }
            )
        if "userId" in kwargs:
            return FakeGoogleCall({"messages": [{"id": "msg-1", "threadId": "thread-1"}]})
        if "calendarId" in kwargs:
            return FakeGoogleCall({"items": [{"id": "evt-1", "summary": "Claims review"}]})
        return FakeGoogleCall({})


def _adapter(runner: FakeGwsRunner | None = None) -> GoogleWorkspaceAdapter:
    return GoogleWorkspaceAdapter(CONNECTOR_DEFINITIONS["google_workspace"], runner=runner)


def test_google_workspace_registry_is_native_hybrid_connector() -> None:
    definition = CONNECTOR_DEFINITIONS["google_workspace"]

    assert definition.status == "native"
    assert definition.family == "workspace"
    assert "@googleworkspace/cli" in definition.oss_libraries
    assert definition.capabilities.supports_discovery is True
    assert definition.capabilities.supports_write is False


def test_google_workspace_role_templates_are_read_only_and_service_scoped() -> None:
    templates = role_templates_for_connector("google_workspace")

    assert {
        "workspace_reader",
        "workspace_knowledge_reader",
        "drive_reader",
        "gmail_reader",
        "calendar_reader",
        "admin_auditor",
        "blocked",
    }.issubset(templates)
    assert any(
        p["action"] == "workspace.drive.file.read" and p["resource_pattern"] == "gdrive://*"
        for p in templates["drive_reader"]
    )
    assert any(p["action"] == "workspace.gmail.message.read" for p in templates["gmail_reader"])
    for role_key, permissions in templates.items():
        if role_key == "blocked":
            continue
        assert all(".write" not in p["action"] for p in permissions)
        assert all(".delete" not in p["action"] for p in permissions)
        assert all(".send" not in p["action"] for p in permissions)


def test_google_workspace_sanitizes_credentials_and_refs() -> None:
    safe = sanitize_config(
        {
            "workspace_domain": "example.com",
            "access_token": "ya29.secret",
            "access_token_ref": "env://GOOGLE_TOKEN",
            "credentials_file": "/tmp/creds.json",
            "service_account_json": '{"private_key":"secret"}',
        },
        CONNECTOR_DEFINITIONS["google_workspace"],
    )

    assert safe["workspace_domain"] == "example.com"
    assert safe["access_token"] == "<configured>"
    assert safe["credentials_file"] == "<configured>"
    assert safe["service_account_json"] == "<configured>"
    assert safe["access_token_ref"] == "env:GOOGLE_TOKEN"


def test_google_workspace_permission_requests_are_service_scoped() -> None:
    adapter = _adapter()

    drive = adapter.build_permission_request(
        source_id="gw",
        identity_id=7,
        operation="read",
        metadata={"asset_ref": "gdocs://document/doc-1"},
    )
    gmail = adapter.build_permission_request(
        source_id="gw",
        identity_id=7,
        operation="read",
        metadata={"asset_ref": "gmail://user/me/message/msg-1"},
    )
    calendar = adapter.build_permission_request(
        source_id="gw",
        identity_id=7,
        operation="read",
        metadata={"asset_ref": "gcal://calendar/primary/event/evt-1"},
    )

    assert drive.action == "workspace.docs.document.read"
    assert drive.resource_type == "workspace.docs.document"
    assert drive.resources == ["gdocs://document/doc-1"]
    assert gmail.action == "workspace.gmail.message.read"
    assert gmail.resource_type == "workspace.gmail.message"
    assert calendar.action == "workspace.calendar.event.read"
    assert calendar.resource_type == "workspace.calendar.event"


@pytest.mark.anyio
async def test_google_workspace_probe_lists_assets_and_fetches_with_gws_runner() -> None:
    runner = FakeGwsRunner()
    adapter = _adapter(runner)
    config = {
        "workspace_domain": "example.com",
        "access_token_ref": "env://TEST_GOOGLE_TOKEN",
        "enabled_services": "drive,gmail,calendar",
        "gmail_users": "me",
        "calendar_ids": "primary",
    }

    probe = await adapter.probe(config)
    assets = await adapter.list_assets(config)
    doc_payload = json.loads((await adapter.fetch_asset(config, "gdocs://document/doc-1")).decode())
    message_payload = json.loads(
        (await adapter.fetch_asset(config, "gmail://user/me/message/msg-1")).decode()
    )
    event_payload = json.loads(
        (await adapter.fetch_asset(config, "gcal://calendar/primary/event/evt-1")).decode()
    )

    assert probe.healthy is True
    assert {
        "gdocs://document/doc-1",
        "gdrive://file/pdf-1",
        "gmail://user/me/message/msg-1",
        "gcal://calendar/primary/event/evt-1",
    }.issubset({asset["asset_path"] for asset in assets})
    assert doc_payload["documentId"] == "doc-1"
    assert message_payload["id"] == "msg-1"
    assert event_payload["id"] == "evt-1"
    assert ("drive", "about", "get") in [call[0] for call in runner.calls]


@pytest.mark.anyio
async def test_google_workspace_native_backend_uses_google_api_for_delegated_subject(
    monkeypatch,
) -> None:
    runner = FakeGwsRunner()
    service_calls = []

    def fake_google_service(config, service_name, version):
        service_calls.append((service_name, version, config["subject_user"]))
        return FakeGoogleService()

    monkeypatch.setattr(connector_module, "_google_service", fake_google_service)
    adapter = _adapter(runner)
    config = {
        "workspace_domain": "example.com",
        "credentials_file_ref": "file:///run/secrets/gws-credentials.json",
        "subject_user": "agent@example.com",
        "enabled_services": "drive,gmail,calendar",
        "drive_query": "trashed = false",
        "gmail_users": "me",
        "calendar_ids": "primary",
    }

    probe = await adapter.probe(config)
    assets = await adapter.list_assets(config)
    doc_payload = json.loads((await adapter.fetch_asset(config, "gdocs://document/doc-1")).decode())

    assert probe.healthy is True
    assert runner.calls == []
    assert {"gdocs://document/doc-1", "gmail://user/me/message/msg-1"}.issubset(
        {asset["asset_path"] for asset in assets}
    )
    assert doc_payload["documentId"] == "doc-1"
    assert ("drive", "v3", "agent@example.com") in service_calls
    assert ("gmail", "v1", "agent@example.com") in service_calls
    assert ("calendar", "v3", "agent@example.com") in service_calls


@pytest.mark.anyio
async def test_google_workspace_probe_requires_auth_reference() -> None:
    probe = await _adapter(FakeGwsRunner()).probe({"workspace_domain": "example.com"})

    assert probe.healthy is False
    assert "access_token_ref" in (probe.error or "")


@pytest.mark.anyio
async def test_google_workspace_writes_fail_closed() -> None:
    with pytest.raises(NotImplementedError):
        await _adapter(FakeGwsRunner()).execute_write({})


@pytest.mark.anyio
async def test_gws_runner_rejects_unallowlisted_commands() -> None:
    runner = GwsRunner(binary="gws")

    with pytest.raises(PermissionError):
        await runner.run_json(
            {"access_token": "test"},
            ("gmail", "users", "messages", "delete"),
            params={"userId": "me", "id": "msg-1"},
        )


@pytest.mark.anyio
async def test_gws_runner_accepts_mounted_credential_file_refs(monkeypatch) -> None:
    captured = {}
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-leak")

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b'{"user":{"emailAddress":"agent@example.com"}}', b""

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setenv("INTERLOCK_SECRET_FILE_ROOTS", "/run/secrets")

    payload = await GwsRunner(binary="gws").run_json(
        {"credentials_file_ref": "file:///run/secrets/gws-credentials.json"},
        ("drive", "about", "get"),
        params={"fields": "user"},
    )

    assert payload["user"]["emailAddress"] == "agent@example.com"
    assert captured["env"]["GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"] == (
        "/run/secrets/gws-credentials.json"
    )
    assert "AWS_SECRET_ACCESS_KEY" not in captured["env"]
    assert set(captured["env"]).issubset(
        {
            "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE",
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "SSL_CERT_DIR",
            "SSL_CERT_FILE",
        }
    )


@pytest.mark.anyio
async def test_gws_runner_scrubs_credentials_from_command_failures(monkeypatch) -> None:
    class FakeProcess:
        returncode = 1

        async def communicate(self):
            return (
                b"",
                b'failed token=ya29.secret private_key="-----BEGIN PRIVATE KEY-----abc"',
            )

    async def fake_create_subprocess_exec(*argv, **kwargs):
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(RuntimeError) as exc:
        await GwsRunner(binary="gws").run_json(
            {
                "access_token": "ya29.secret",
                "service_account_json": '{"private_key":"-----BEGIN PRIVATE KEY-----abc"}',
            },
            ("drive", "about", "get"),
        )

    message = str(exc.value)
    assert "ya29.secret" not in message
    assert "BEGIN PRIVATE KEY" not in message
    assert "[REDACTED]" in message
