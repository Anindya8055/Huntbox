"""Live API-key settings: atomic .env rewrite + the PATCH /api/settings endpoint."""

import os

import pytest
from fastapi.testclient import TestClient

from app.config import update_env_file


class TestUpdateEnvFile:
    def test_replaces_existing_key_in_place(self, tmp_path):
        (tmp_path / ".env").write_text(
            "# a comment\nPRODUCTHUNT_API_TOKEN=old-ph\nAPIFY_API_TOKEN=old-apify\nLOG_LEVEL=INFO\n",
            encoding="utf-8",
        )
        update_env_file(tmp_path, {"APIFY_API_TOKEN": "new-apify"})

        text = (tmp_path / ".env").read_text(encoding="utf-8")
        lines = text.splitlines()
        assert "# a comment" in lines
        assert "PRODUCTHUNT_API_TOKEN=old-ph" in lines
        assert "APIFY_API_TOKEN=new-apify" in lines
        assert "APIFY_API_TOKEN=old-apify" not in text
        assert "LOG_LEVEL=INFO" in lines
        # Order and line count preserved -- only the value changed.
        assert lines.index("APIFY_API_TOKEN=new-apify") == 2

    def test_appends_key_not_already_present(self, tmp_path):
        (tmp_path / ".env").write_text("PRODUCTHUNT_API_TOKEN=abc\n", encoding="utf-8")
        update_env_file(tmp_path, {"SERPER_API_KEY": "shiny"})

        lines = (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
        assert "PRODUCTHUNT_API_TOKEN=abc" in lines
        assert "SERPER_API_KEY=shiny" in lines

    def test_creates_file_if_missing(self, tmp_path):
        update_env_file(tmp_path, {"APIFY_API_TOKEN": "fresh"})
        assert (tmp_path / ".env").read_text(encoding="utf-8").strip() == "APIFY_API_TOKEN=fresh"

    def test_updates_os_environ_immediately(self, tmp_path, monkeypatch):
        monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
        update_env_file(tmp_path, {"APIFY_API_TOKEN": "live-value"})
        assert os.environ["APIFY_API_TOKEN"] == "live-value"

    def test_no_updates_is_a_noop(self, tmp_path):
        update_env_file(tmp_path, {})
        assert not (tmp_path / ".env").exists()


class TestSettingsEndpoint:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        import app.main as main

        # Isolate from whatever the developer's real .env has loaded into
        # os.environ for this process, so has_apify/has_serper reflect only
        # what this test itself writes.
        monkeypatch.delenv("APIFY_API_TOKEN", raising=False)
        monkeypatch.delenv("SERPER_API_KEY", raising=False)
        (tmp_path / ".env").write_text("PRODUCTHUNT_API_TOKEN=existing\n", encoding="utf-8")
        monkeypatch.setattr(main, "BASE_DIR", tmp_path)
        return TestClient(main.app)

    def test_updates_apify_token_and_flips_health(self, client):
        res = client.patch("/api/settings", json={"apify_api_token": "secret-token"})
        assert res.status_code == 200
        body = res.json()
        assert body == {"ok": True, "has_apify": True, "has_serper": False}
        # The token value must never appear in the response.
        assert "secret-token" not in res.text

        health = client.get("/api/health").json()
        assert health["apify_key"] is True

    def test_blank_body_is_rejected(self, client):
        res = client.patch("/api/settings", json={})
        assert res.status_code == 400

    def test_whitespace_only_field_is_treated_as_blank(self, client):
        res = client.patch("/api/settings", json={"apify_api_token": "   "})
        assert res.status_code == 400

    def test_env_file_preserves_existing_keys(self, client, tmp_path):
        client.patch("/api/settings", json={"serper_api_key": "new-serper"})
        text = (tmp_path / ".env").read_text(encoding="utf-8")
        assert "PRODUCTHUNT_API_TOKEN=existing" in text
        assert "SERPER_API_KEY=new-serper" in text
