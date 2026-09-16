# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""通知の抽象レイヤ (docs/operations.md 決定 O3)。

notify() が core.Notification を 1 件 INSERT し、settings 登録のバックエンド列へ配送する。
配送失敗は通知自体を壊さない (DB 行は残す。best-effort)。同一 (kind, channel) はクールダウン
(既定 5 分) 内なら DB 記録のみ行い配送を抑止する (フラップ時の通知氾濫防止)。Zabbix sender 等は
同一インタフェース NotifyBackend で後から追加できる。

settings:
- ICSTV_NOTIFY_BACKENDS: dotted path のリスト (既定 ["core.notify.WebhookBackend"])
- ICSTV_NOTIFY_WEBHOOK_URL: Discord/Slack 互換 webhook URL (未設定なら WebhookBackend は no-op)
- ICSTV_NOTIFY_WEBHOOK_MIN_SEVERITY: webhook へ流す最低 severity (既定 "crit")。
  これ未満は DB 行だけ残して配送しない。通知パネル・傾向分析は DB を見るので影響しない。
"""

from __future__ import annotations

import logging
from datetime import timedelta

import httpx
from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

from core.models import Channel, Notification, NotificationSeverity

logger = logging.getLogger(__name__)

_COOLDOWN = timedelta(minutes=5)
_DEFAULT_BACKENDS = ["core.notify.WebhookBackend"]

# severity の強さ。webhook の下限判定に使う。
# Notification.severity は str で入ってくるので、キーの型も str に寄せる
# (NotificationSeverity は TextChoices = str のサブクラスなので両方引ける)。
_SEVERITY_RANK: dict[str, int] = {
    NotificationSeverity.INFO: 0,
    NotificationSeverity.WARN: 1,
    NotificationSeverity.CRIT: 2,
}
_DEFAULT_WEBHOOK_MIN_SEVERITY = NotificationSeverity.CRIT


class NotifyBackend:
    """配送バックエンドのインタフェース。"""

    def send(self, n: Notification) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class WebhookBackend(NotifyBackend):
    """Discord/Slack 互換 webhook へ POST する。失敗はログのみ (通知 DB 行は壊さない)。

    ICSTV_NOTIFY_WEBHOOK_MIN_SEVERITY 未満は配送しない。既定は crit で、休止帯の WARN と
    復帰の INFO は DB にだけ残る。

    根拠: 2026-08-01 以降 34 日間 1 件も ack されておらず、直近 47 件の CRIT が無視されている。
    通知量を絞らないと、実際に効く放送中の断が同じ流れの中に埋もれる。2026-09-03..04 の
    30 時間では CRIT 4 + INFO 4 = 8 通が流れたが、この既定なら放送中の 1 通だけが立つ。
    休止帯も Discord で見たい場合は "warn" に下げる。
    """

    def send(self, n: Notification) -> None:
        url = getattr(settings, "ICSTV_NOTIFY_WEBHOOK_URL", "") or ""
        if not url:
            return
        floor = getattr(
            settings, "ICSTV_NOTIFY_WEBHOOK_MIN_SEVERITY", _DEFAULT_WEBHOOK_MIN_SEVERITY
        )
        # 未知の severity は握り潰さず配送する。アラート系は迷ったら出す側に倒す
        # (rank 0 = INFO 扱いにすると、新しい severity を足した日に無言で消える)。
        # 下限側が未知の場合は既定 (crit) に落とす。
        top = _SEVERITY_RANK[NotificationSeverity.CRIT]
        if _SEVERITY_RANK.get(n.severity, top) < _SEVERITY_RANK.get(
            floor, _SEVERITY_RANK[_DEFAULT_WEBHOOK_MIN_SEVERITY]
        ):
            return
        text = f"[{n.severity.upper()}] {n.kind}: {n.message}"
        try:
            httpx.post(url, json={"content": text}, timeout=5.0)
        except Exception:
            logger.warning("notify webhook 配送失敗 kind=%s", n.kind, exc_info=True)


def _backends() -> list[NotifyBackend]:
    paths = getattr(settings, "ICSTV_NOTIFY_BACKENDS", _DEFAULT_BACKENDS)
    out: list[NotifyBackend] = []
    for p in paths:
        try:
            out.append(import_string(p)())
        except Exception:
            logger.warning("notify backend のロード失敗: %s", p, exc_info=True)
    return out


def _recently_notified(kind: str, channel_id: int | None) -> bool:
    since = timezone.now() - _COOLDOWN
    qs = Notification.objects.filter(kind=kind, created_at__gte=since)
    qs = qs.filter(channel__isnull=True) if channel_id is None else qs.filter(channel_id=channel_id)
    return qs.exists()


def notify(
    severity: str,
    kind: str,
    message: str,
    *,
    channel=None,
    link: str | None = None,
    throttle: bool = True,
) -> Notification:
    """Notification を 1 件 INSERT し、バックエンドへ配送する。

    throttle=True のとき、同一 (kind, channel) がクールダウン (5 分) 内に既にあれば DB 記録のみ
    行い配送をスキップする。記録自体は常に残す (一覧の網羅性を保つ)。
    """
    channel_id = channel.id if isinstance(channel, Channel) else channel
    skip_send = throttle and _recently_notified(kind, channel_id)
    n = Notification.objects.create(
        channel_id=channel_id,
        severity=severity,
        kind=kind,
        message=message,
        link_url=link,
    )
    if not skip_send:
        for b in _backends():
            try:
                b.send(n)
            except Exception:
                logger.warning(
                    "notify backend 送信失敗 backend=%s", type(b).__name__, exc_info=True
                )
    return n
