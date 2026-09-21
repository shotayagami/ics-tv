# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""youtube.api._credentials の単体テスト (2026-09-02 監査 決定#7②)。

expiry を Credentials へ渡さないと google-auth は「無期限」扱いで valid=True になり、
失効後は毎 API 呼び出しが 401→transport refresh で往復していた。expiry の受け渡し
(aware DB 値 → naive UTC) と、失効時のみ refresh + DB 書き戻しを検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from django.utils import timezone

from youtube.api import Credentials, _credentials
from youtube.models import YoutubeCredential

pytestmark = pytest.mark.django_db


@pytest.fixture
def yt_cred(channel):
    return YoutubeCredential.objects.create(
        channel=channel,
        client_id="cid",
        client_secret="csec",  # pragma: allowlist secret
        refresh_token="rtok",
        access_token="atok",
        token_expiry=timezone.now() + timedelta(hours=1),
    )


def test_valid_token_is_used_without_refresh(yt_cred, monkeypatch):
    """expiry が未来なら refresh せずそのまま使う (従来は expiry 未設定でも「常に valid」だった
    のではなく、失効後も valid 扱い → 実呼び出しが 401 になっていた)。"""
    monkeypatch.setattr(
        Credentials, "refresh", lambda self, req: pytest.fail("有効期限内は refresh しない")
    )

    google_cred = _credentials(yt_cred.channel)
    assert google_cred.token == "atok"
    assert google_cred.expiry is not None  # expiry が渡っている (無期限扱いにならない)
    assert google_cred.valid


def test_expired_token_refreshes_and_persists(yt_cred, monkeypatch):
    """expiry が過去なら事前 refresh し、新 token/expiry を DB へ書き戻す (aware UTC)。"""
    yt_cred.token_expiry = timezone.now() - timedelta(minutes=5)
    yt_cred.save(update_fields=["token_expiry"])
    # naive UTC (google-auth が refresh 後に持つ形)
    new_expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    def fake_refresh(self, req):
        self.token = "new-atok"
        self.expiry = new_expiry  # google-auth は naive UTC を持つ

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)

    google_cred = _credentials(yt_cred.channel)
    assert google_cred.token == "new-atok"
    yt_cred.refresh_from_db()
    assert yt_cred.access_token == "new-atok"
    assert yt_cred.token_expiry == new_expiry.replace(tzinfo=UTC)
