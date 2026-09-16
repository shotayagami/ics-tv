# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#3 DB at-rest 暗号化 (core.fields.EncryptedTextField)。

at-rest で暗号文・読込で透過復号・鍵なしは平文 (dev)・legacy 平文も読める・auth 経路の
.only() 復号、を検証する。
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from django.db import connection

from core.models import Channel
from youtube.models import YoutubeCredential

KEY = Fernet.generate_key().decode()


@pytest.fixture
def enc_key(settings):
    settings.ICSTV_FIELD_ENCRYPTION_KEY = KEY
    return KEY


def _raw(table: str, col: str, where_col: str, pk) -> str:
    with connection.cursor() as cur:
        cur.execute(f"SELECT {col} FROM {table} WHERE {where_col} = %s", [pk])
        return cur.fetchone()[0]


def test_agent_token_encrypted_at_rest(db, enc_key):
    ch = Channel.objects.create(name="c", slug="enc1", agent_token="secret-token-xyz")
    raw = _raw("channel", "agent_token", "id", ch.id)
    assert raw.startswith("enc:v1:")
    assert "secret-token-xyz" not in raw  # 平文は DB に残らない
    assert Channel.objects.get(pk=ch.id).agent_token == "secret-token-xyz"  # 透過復号


def test_auth_lookup_path_decrypts(db, enc_key):
    # _verify_token と同じ .only("agent_token") 経路で復号されること
    Channel.objects.create(name="c", slug="enc2", agent_token="tok-abc", enabled=True)
    ch = Channel.objects.filter(slug="enc2", enabled=True).only("agent_token").first()
    assert ch.agent_token == "tok-abc"


def test_plaintext_fallback_without_key(db, settings):
    settings.ICSTV_FIELD_ENCRYPTION_KEY = ""
    ch = Channel.objects.create(name="c", slug="enc3", agent_token="plain-tok")
    assert _raw("channel", "agent_token", "id", ch.id) == "plain-tok"  # 鍵なし=平文
    assert Channel.objects.get(pk=ch.id).agent_token == "plain-tok"


def test_legacy_plaintext_readable_with_key(db, enc_key):
    # 鍵導入前に書かれた平文 (marker 無し) も読める (移行・混在を許容)
    ch = Channel.objects.create(name="c", slug="enc4")
    with connection.cursor() as cur:
        cur.execute("UPDATE channel SET agent_token = %s WHERE id = %s", ["legacy-plain", ch.id])
    assert Channel.objects.get(pk=ch.id).agent_token == "legacy-plain"


def test_youtube_credentials_encrypted(db, enc_key):
    ch = Channel.objects.create(name="c", slug="enc5")
    YoutubeCredential.objects.create(
        channel=ch,
        client_id="cid",
        client_secret="csecret",  # pragma: allowlist secret - test only
        refresh_token="rtoken",  # pragma: allowlist secret - test only
        scopes="x",
    )
    raw_cs = _raw("youtube_credential", "client_secret", "channel_id", ch.id)
    raw_rt = _raw("youtube_credential", "refresh_token", "channel_id", ch.id)
    assert raw_cs.startswith("enc:v1:") and "csecret" not in raw_cs
    assert raw_rt.startswith("enc:v1:") and "rtoken" not in raw_rt
    got = YoutubeCredential.objects.get(pk=ch.id)
    assert got.client_secret == "csecret"  # pragma: allowlist secret - test only
    assert got.refresh_token == "rtoken"  # pragma: allowlist secret - test only
