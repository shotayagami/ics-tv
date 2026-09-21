# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB at-rest 暗号化フィールド (#3 / datamodel.md「シークレット保管」の決定)。

決定: ランタイム生成の機密 (channel.agent_token は ch ごと生成、YouTube creds は OAuth フロー生成)
は deploy 時に配る Secret では管理できないため、アプリ層 Fernet 対称暗号で DB at-rest 暗号化する。
暗号鍵は settings.ICSTV_FIELD_ENCRYPTION_KEY (env 経由で配備基盤の Secret から渡す) に置き、DB とは
別の信頼境界に保つ (DB ダンプ単体では機密が漏れない)。外部シークレットストア (Vault 等) は
小〜中規模の自前運用では過剰なため採らない。

挙動:
- 保存時: 鍵があれば Fernet 暗号化し marker を前置 (enc:v1:)。鍵が無い (dev) なら平文のまま。
- 読込時: marker 付きは復号、marker 無し (既存平文/dev) はそのまま返す → 移行・混在を許容。
- 注意: 暗号文は非決定的なので、このフィールドを WHERE 句で照合してはいけない
  (例: agent_token の認証は slug で引いてから復号比較する。grpc_service._verify_token)。
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

_MARKER = "enc:v1:"


def _fernet():
    key = getattr(settings, "ICSTV_FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        return None
    from cryptography.fernet import Fernet

    return Fernet(key.encode() if isinstance(key, str) else key)


class EncryptedTextField(models.TextField):
    """保存時に Fernet 暗号化し読込時に復号する TextField。鍵未設定なら透過 (平文)。"""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value or value.startswith(_MARKER):
            return value  # 空 or 既に暗号化済はそのまま
        f = _fernet()
        if f is None:
            return value  # 鍵なし (dev) = 平文保管
        return _MARKER + f.encrypt(value.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or not value.startswith(_MARKER):
            return value  # 平文 / legacy はそのまま
        f = _fernet()
        if f is None:
            return value  # 鍵が無いと復号不可 (異常系) → 生値
        from cryptography.fernet import InvalidToken

        try:
            return f.decrypt(value[len(_MARKER) :].encode()).decode()
        except InvalidToken:
            return value
