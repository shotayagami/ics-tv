# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cloudflare Live Input/Output 設定 UI の HTTP smoke。実 CF API は httpx mock で。"""

from __future__ import annotations

import httpx
import pytest


def test_create_cf_live_input_requires_post(staff_client, channel):
    res = staff_client.get(f"/admin-ui/ch/{channel.slug}/cloudflare/live-input/create/")
    assert res.status_code == 405


def test_create_cf_live_input_unauthenticated_redirects(http_client, channel):
    res = http_client.post(f"/admin-ui/ch/{channel.slug}/cloudflare/live-input/create/")
    assert res.status_code == 302


def test_create_cf_live_input_without_env_returns_503(staff_client, channel, monkeypatch):
    monkeypatch.delenv("ICSTV_CF_API_TOKEN", raising=False)
    monkeypatch.delenv("ICSTV_CF_ACCOUNT_ID", raising=False)
    res = staff_client.post(f"/admin-ui/ch/{channel.slug}/cloudflare/live-input/create/")
    assert res.status_code == 503


def test_create_cf_live_input_existing_409(staff_client, channel):
    channel.cf_live_input_id = "existing-input-id"
    channel.save(update_fields=["cf_live_input_id"])
    res = staff_client.post(f"/admin-ui/ch/{channel.slug}/cloudflare/live-input/create/")
    assert res.status_code == 409


def test_create_cf_live_input_calls_api_and_saves(staff_client, channel, monkeypatch):
    monkeypatch.setenv("ICSTV_CF_API_TOKEN", "test-token")  # pragma: allowlist secret
    monkeypatch.setenv("ICSTV_CF_ACCOUNT_ID", "test-account")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/stream/live_inputs")
        assert request.method == "POST"
        return httpx.Response(200, json={"result": {"uid": "new-live-input-uid", "rtmps": {}}})

    transport = httpx.MockTransport(mock_handler)
    real_client = httpx.Client

    def patched_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched_client)

    res = staff_client.post(f"/admin-ui/ch/{channel.slug}/cloudflare/live-input/create/")
    assert res.status_code in (200, 302)
    channel.refresh_from_db()
    assert channel.cf_live_input_id == "new-live-input-uid"


def test_create_cf_live_output_requires_live_input_412(staff_client, channel):
    res = staff_client.post(f"/admin-ui/ch/{channel.slug}/cloudflare/live-output/create/")
    assert res.status_code == 412
    assert "Live Input" in res.content.decode("utf-8")


def test_create_cf_live_output_requires_youtube_key_412(staff_client, channel):
    channel.cf_live_input_id = "live-input-id"
    channel.youtube_stream_key = ""
    channel.save()
    res = staff_client.post(f"/admin-ui/ch/{channel.slug}/cloudflare/live-output/create/")
    assert res.status_code == 412
    assert "youtube_stream_key" in res.content.decode("utf-8")


@pytest.mark.parametrize("status_code", [400, 401, 500])
def test_create_cf_live_input_api_error_502(staff_client, channel, monkeypatch, status_code):
    monkeypatch.setenv("ICSTV_CF_API_TOKEN", "x")  # pragma: allowlist secret
    monkeypatch.setenv("ICSTV_CF_ACCOUNT_ID", "x")

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"errors": ["nope"]})

    transport = httpx.MockTransport(mock_handler)
    real_client = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)

    res = staff_client.post(f"/admin-ui/ch/{channel.slug}/cloudflare/live-input/create/")
    assert res.status_code == 502


def _mock_client(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def patched(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", patched)


def test_update_live_output_puts_enabled_only(channel, monkeypatch):
    """#27 Part B: enabled のみの部分更新 (url/streamKey は再送しない)。"""
    from core import cloudflare_api

    monkeypatch.setenv("ICSTV_CF_API_TOKEN", "x")  # pragma: allowlist secret
    monkeypatch.setenv("ICSTV_CF_ACCOUNT_ID", "acct")
    channel.cf_live_input_id = "live-input-id"
    channel.save(update_fields=["cf_live_input_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == (
            "/client/v4/accounts/acct/stream/live_inputs/live-input-id/outputs/out-1"
        )
        import json as _json

        assert _json.loads(request.content) == {"enabled": False}
        return httpx.Response(200, json={"result": {"uid": "out-1", "enabled": False}})

    _mock_client(monkeypatch, handler)
    result = cloudflare_api.update_live_output(channel, "out-1", enabled=False)
    assert result["enabled"] is False


def test_update_live_output_without_live_input_raises(channel):
    from core import cloudflare_api

    with pytest.raises(ValueError, match="cf_live_input_id"):
        cloudflare_api.update_live_output(channel, "out-1", enabled=True)


def test_delete_live_output_calls_api(channel, monkeypatch):
    from core import cloudflare_api

    monkeypatch.setenv("ICSTV_CF_API_TOKEN", "x")  # pragma: allowlist secret
    monkeypatch.setenv("ICSTV_CF_ACCOUNT_ID", "acct")
    channel.cf_live_input_id = "live-input-id"
    channel.save(update_fields=["cf_live_input_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == (
            "/client/v4/accounts/acct/stream/live_inputs/live-input-id/outputs/out-1"
        )
        return httpx.Response(200, json={"result": {}})

    _mock_client(monkeypatch, handler)
    cloudflare_api.delete_live_output(channel, "out-1")  # 例外なしで完了


def test_delete_live_output_without_live_input_raises(channel):
    from core import cloudflare_api

    with pytest.raises(ValueError, match="cf_live_input_id"):
        cloudflare_api.delete_live_output(channel, "out-1")


def test_update_live_output_api_error_propagates(channel, monkeypatch):
    from core import cloudflare_api

    monkeypatch.setenv("ICSTV_CF_API_TOKEN", "x")  # pragma: allowlist secret
    monkeypatch.setenv("ICSTV_CF_ACCOUNT_ID", "acct")
    channel.cf_live_input_id = "live-input-id"
    channel.save(update_fields=["cf_live_input_id"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": ["not found"]})

    _mock_client(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        cloudflare_api.update_live_output(channel, "out-1", enabled=True)
