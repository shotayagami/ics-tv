# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""playout の非同期タスク。

check_agent_liveness: agent_status.last_heartbeat_at の途絶を周期検出し、online↔offline の
**状態遷移時のみ**通知する (docs/operations.md 決定 O6/O7)。offline_notified フラグで二重通知を
防ぐ (遷移検出はこのタスクが一元管理。Heartbeat 側は触らない)。

check_stuck_slate: 放送中 (broadcast_windows 窓内) なのにスレートが出続けている状態を検出する。
スレート層 (layer 90) は本線 (layer 10) と独立なので、本線が正常でも画面はスレートのまま=停波
相当になるが、送出は「成功」し続けるためどの既存監視にも掛からない。実際 2026-07-21 06:00 の
休止明けに 2h21m 気付かれず、運用者の手動解除まで復旧しなかった。
"""

from __future__ import annotations

import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from core import notify as notify_mod
from core.models import Notification, NotificationSeverity
from playout.models import AgentStatus, PlayoutAction, PlayoutEvent, PlayoutStatus

logger = logging.getLogger(__name__)

# 心拍途絶とみなす閾値 (heartbeat 30s 周期の約 3 倍。docs/operations.md L36)
OFFLINE_THRESHOLD_SEC = 90

# 放送中スレートを「固着」とみなすまでの猶予。休止明けの CLEAR_SLATE 実行や resolver の自己修復
# (最大 1 beat = 5 分) が働く余地を残し、正常な遷移中の一過性状態で鳴らさない。
STUCK_SLATE_GRACE_SEC = 420


def _offline_message(st: AgentStatus, now, *, on_air: bool) -> str:
    """agent_offline の本文を組み立てる。

    文面が「agent 心拍途絶」だと agent が落ちたと読めるが、観測された 4 件すべてで agent は
    生きており、消えていたのは制御プレーンとの間の経路だった (2026-09-04 の 15 分は agent 側の
    gRPC が半開通を検知できず TCP 再送予算まで粘ったもので、PR #189 で解消済み)。責任の所在を
    誤らせない書き方にする。

    Discord に出る 1 通だけで切り分けが終わるよう、経過秒・放送中/休止中・直近 24h の同種発生
    回数を載せる。通知パネルの ack が 34 日動いていない以上、UI を開かせる前提の設計は機能して
    いないため、本文を自己完結させる方に倒す。
    """
    gap = int((now - st.last_heartbeat_at).total_seconds())
    recent = Notification.objects.filter(
        kind="agent_offline",
        channel=st.channel,
        created_at__gte=now - timedelta(hours=24),
    ).count()
    state = "放送中" if on_air else "休止中"
    # recent はこの通知を作る前に数えるので、今回分を足して「何件目か」にする。
    return (
        f"heartbeat 未達 ({gap}s / 閾値 {OFFLINE_THRESHOLD_SEC}s) — 制御プレーン↔agent 間の断。"
        f"送出は agent ローカルキューで継続中 ({state})。直近 24h で {recent + 1} 件目"
    )


@shared_task
def check_agent_liveness() -> dict[str, int]:
    """有効な channel の agent_status を走査し、offline 化/復帰の遷移を通知する。

    **enabled=False の channel は対象外。** 退役した channel の AgentStatus 行は最後の心拍で
    止まったまま残るため、除外しないと offline 判定が永久に真になり、offline_notified が
    latch されたきり誰も ack できない孤児行になる (ch2 が 2026-06-22 の退役から 74 日その状態
    だった)。scheduling/tasks.py が `Channel.objects.filter(enabled=True)` で回しているのと
    同じ規約に揃える。

    severity は放送中かどうかで分ける。休止帯の断は視聴影響が無く、同じ CRIT で鳴らすと
    実際に効く放送中の 1 件が埋もれる (2026-09-03..04 の 4 件は休止帯 3 + 放送中 1 で、
    最長の放送中 1 件が他 3 件と同じ扱いだった)。check_stuck_slate が既に is_on_air で
    ゲートしているので、非対称だったものを揃える形になる。
    """
    now = timezone.now()
    threshold = now - timedelta(seconds=OFFLINE_THRESHOLD_SEC)
    went_offline = 0
    recovered = 0
    for st in AgentStatus.objects.select_related("channel").filter(channel__enabled=True):
        offline = st.last_heartbeat_at < threshold
        if offline and not st.offline_notified:
            on_air = st.channel.is_on_air(now)
            notify_mod.notify(
                NotificationSeverity.CRIT if on_air else NotificationSeverity.WARN,
                "agent_offline",
                _offline_message(st, now, on_air=on_air),
                channel=st.channel,
            )
            AgentStatus.objects.filter(pk=st.pk).update(offline_notified=True)
            went_offline += 1
        elif not offline and st.offline_notified:
            notify_mod.notify(
                NotificationSeverity.INFO,
                "agent_recovered",
                "heartbeat 復帰",
                channel=st.channel,
            )
            AgentStatus.objects.filter(pk=st.pk).update(offline_notified=False)
            recovered += 1
    if went_offline or recovered:
        logger.info("check_agent_liveness offline=%d recovered=%d", went_offline, recovered)
    return {"offline": went_offline, "recovered": recovered}


def _stuck_slate_since(st: AgentStatus, now):
    """放送中スレート固着なら、その原因スレートの実行時刻を返す。該当しなければ None。

    「固着」= resolver 管轄の休止スレート (off_air) が放送中区間に残り続けている状態。
    以下は除外する:
      - 休止中 (broadcast_windows の窓外): スレートが出ているのが正常
      - feed 断由来 (feed_state="lost"): SLATE_ON / feed_lost で別途通知済み
      - 運用者の手動/緊急スレート (直近スレートに off_air param が無い): 運用意図なので鳴らさない
      - 猶予内 (STUCK_SLATE_GRACE_SEC): 休止明けの解除や resolver の自己修復が働く余地を残す
    """
    if not st.slate_active or st.feed_state == "lost":
        return None
    if not st.channel.is_on_air(now):
        return None
    last_slate = (
        PlayoutEvent.objects.filter(
            channel=st.channel,
            action=PlayoutAction.PLAY_SLATE,
            actual_at__isnull=False,
        )
        .exclude(status=PlayoutStatus.CANCELLED)
        .order_by("-actual_at")
        .first()
    )
    if last_slate is None or not (last_slate.params or {}).get("off_air"):
        return None
    if last_slate.actual_at > now - timedelta(seconds=STUCK_SLATE_GRACE_SEC):
        return None
    return last_slate.actual_at


@shared_task
def check_stuck_slate() -> dict[str, int]:
    """放送中なのにスレートが残り続けている channel を検出し、遷移時のみ通知する。

    本線 (layer 10) が正常でもスレート層 (layer 90) が被っていれば視聴者には停波と同じだが、
    送出イベントは成功し続けるため既存の死活/送出失敗監視には掛からない。2026-07-21 06:00 の
    休止明けでは 2h21m 誰も気付かず、運用者が手動解除するまで復旧しなかった。
    """
    now = timezone.now()
    stuck = 0
    cleared = 0
    for st in AgentStatus.objects.select_related("channel"):
        since = _stuck_slate_since(st, now)
        if since is not None and not st.slate_stuck_notified:
            minutes = int((now - since).total_seconds() // 60)
            notify_mod.notify(
                NotificationSeverity.CRIT,
                "slate_stuck",
                f"放送中にスレートが {minutes} 分継続。本線は流れているが画面はスレートの可能性",
                channel=st.channel,
            )
            AgentStatus.objects.filter(pk=st.pk).update(slate_stuck_notified=True)
            stuck += 1
        elif since is None and st.slate_stuck_notified:
            notify_mod.notify(
                NotificationSeverity.INFO,
                "slate_stuck_cleared",
                "放送中のスレート固着が解消",
                channel=st.channel,
            )
            AgentStatus.objects.filter(pk=st.pk).update(slate_stuck_notified=False)
            cleared += 1
    if stuck or cleared:
        logger.info("check_stuck_slate stuck=%d cleared=%d", stuck, cleared)
    return {"stuck": stuck, "cleared": cleared}
