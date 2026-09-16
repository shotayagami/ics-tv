# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""セキュリティイベントの構造化ログ (#sec §3)。

認証・認可・課金・内部連携の重要イベントを icstv.security ロガーへ JSON 1 行で出す。Loki は
{logger="icstv.security"} | json で msg 内フィールド (event/outcome/ip/...) を抽出し、総当たり・
偽 webhook・内部トークン失敗をアラートできる (現状これらは無ログで、攻撃に気付けなかった)。

PII は載せない: メールは HMAC(SECRET_KEY) でハッシュ (core.request_meta.hash_email)、パスワード/
コード平文/TOTP秘密/各種トークンは一切出さない。emit() が出す message は必ず妥当な JSON なので、
settings 側は icstv.security 専用の security_json フォーマッタ (%(message)s) で素通しする。
"""

from __future__ import annotations

import json
import logging

from core.request_meta import client_ip

logger = logging.getLogger("icstv.security")


def emit(
    event: str,
    *,
    outcome: str = "",
    request=None,
    level: int = logging.INFO,
    **fields,
) -> None:
    """1 セキュリティイベントを JSON で記録する。

    event: 例 "auth.member.login" / "internal.token" / "billing.stripe_webhook"。
    outcome: 例 "success" / "fail" / "blocked" / "signature_fail"。
    request: 与えると ip を付す (client_ip)。fields: 追加項目 (email_hash, token_kind, member_id 等)。
    None 値の fields は落とす。ログ出力自体で例外を起こして本処理を妨げない。
    """
    try:
        payload: dict = {"event": event}
        if outcome:
            payload["outcome"] = outcome
        if request is not None:
            payload["ip"] = client_ip(request)
        payload.update({k: v for k, v in fields.items() if v is not None})
        logger.log(level, json.dumps(payload, ensure_ascii=False))
    except Exception:  # ロギングで業務処理を落とさない
        logger.exception("security_log emit failed: event=%s", event)


def emit_audit(event: str, *, actor=None, **fields) -> None:
    """金額を確定する操作の監査証跡 (#sec L-4/L-5)。

    security ログと同じ経路に出す。点検の結論 3 点目が「認証・**課金**・内部トークンの
    security ログ」をひとまとめにしているため、別ロガーを立てずに名前空間で分ける
    (`billing.*`)。番組予算 (procurement) は追加提供側へ移したので、このツリーが出すのは
    `billing.*` だけである。Loki からは
    `{logger="icstv.security"} | json | msg_event=~\\`billing\\..*\\`` で抽出できる。

    actor は操作した User。**None を許すのは意図的**で、Celery / 管理コマンド等の
    人手を介さない経路が実在する。その場合は actor="system" として記録し、
    「記録し忘れ」と「そもそも人がいない」を後から区別できるようにする。

    なお**この監査ログは長期保存の手段ではない** (Loki の retention は 7 日)。
    会計帳簿として要る 10 年の証跡は各モデルの `*_by` / `*_at` 列が持つ。
    こちらは「前後値を含む操作の流れ」を短期間追うためのもので、両者は役割が違う。
    """
    emit(
        event,
        outcome="ok",
        actor=(getattr(actor, "username", None) or "system"),
        actor_id=getattr(actor, "pk", None),
        **fields,
    )
