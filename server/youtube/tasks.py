# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""youtube の非同期タスク。2h×12枠 rolling 生成・境界 transition。詳細は docs/youtube.md。

beat schedule (config/settings.py):
- generate_slots:  10 分周期で rolling_hours 先まで slot を埋める
- rotate_slots:    1 分周期で 2h 境界を検出し transition(live/complete)
- nudge_next_slot: 1 分周期で、終了 nudge_lead_minutes 分前の live 枠から次枠 watch URL へ誘導
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from celery import shared_task
from django.utils import timezone
from googleapiclient.errors import HttpError

from core.models import Channel, Notification, NotificationSeverity
from core.notify import notify
from youtube.api import (
    apply_broadcast_preset,
    broadcast_lifecycle,
    create_dedicated_stream,
    insert_broadcast,
    livestream_active,
    post_live_chat_message,
    transition_broadcast,
    update_broadcast,
    watch_url,
)
from youtube.models import ProgramBroadcast, YoutubeSlot, YtSlotStatus

logger = logging.getLogger(__name__)


def _is_transient(e: HttpError) -> bool:
    """YouTube 側の一過性障害 (5xx)。ERROR 確定にせず翌分の rotate に再試行させる。"""
    return getattr(e.resp, "status", 0) >= 500


# rotate 系 beat は 1 分周期で回るため、core.notify の既定クールダウン (5 分・配送のみ抑止) では
# 通知一覧が同一異常の行で氾濫する。同一 (kind, channel) は 30 分に 1 回だけ記録+配送する。
_ROTATE_NOTIFY_COOLDOWN = timedelta(minutes=30)


def _notify_rotate_anomaly(channel: Channel, kind: str, message: str) -> None:
    """rotate 系 beat の blocked/missed を運用通知する (2026-09-02 監査 決定#1)。

    missed=窓を落とした (無配信確定)、blocked=live 化できない (放置すると missed へ進む) は
    どちらも視聴者影響に直結するため CRIT。dedupe は Notification の既存行で判定する
    (rotate は毎分走るので、直近 _ROTATE_NOTIFY_COOLDOWN 内に同一 (kind, channel) が
    あれば何もしない)。
    """
    since = timezone.now() - _ROTATE_NOTIFY_COOLDOWN
    if Notification.objects.filter(kind=kind, channel=channel, created_at__gte=since).exists():
        return
    notify(NotificationSeverity.CRIT, kind, message, channel=channel)


def _reconciled_status(channel: Channel, broadcast_id: str, target: str) -> str | None:
    """transition が 4xx (invalidTransition 等) で拒否されたとき、実状態から追認できる遷移先を返す。

    - target=live: 既に live 系なら LIVE (YT Studio での手動 go-live や、前回呼び出しの
      応答喪失後に実は成功していたケース)
    - target=complete: 既に complete/revoked または削除済み (None) なら COMPLETE
    追認できない状態 (本当に異常) なら None。
    """
    try:
        life = broadcast_lifecycle(channel, broadcast_id)
    except HttpError:
        return None
    if target == "live" and life in ("live", "liveStarting"):
        return YtSlotStatus.LIVE
    if target == "complete" and life in (None, "complete", "revoked"):
        return YtSlotStatus.COMPLETE
    return None


# #23 番組専用枠を何時間先まで先回り生成するか。直前に作ると番組メタ (タイトル/時刻) 変更に
# 追従しやすい一方、transition には scheduledStart 前の bind 済み broadcast が要るので余裕を持つ。
DEDICATED_LOOKAHEAD_HOURS = 6


def _render_title(
    template: str, window_start: datetime, window_end: datetime, channel_name: str = ""
) -> str:
    # window_* は UTC-aware (USE_TZ=True)。日本向けチャンネルなのでタイトルの日付/時刻は
    # localtime(Asia/Tokyo) に変換してから整形する (説明欄の番組行と同じ JST 基準)。
    # {channel} はチャンネル名 (Channel.name)。マルチch で枠タイトルにch名を入れるため。
    start_local = timezone.localtime(window_start)
    end_local = timezone.localtime(window_end)
    return template.format(
        date=start_local.strftime("%Y-%m-%d"),
        start=start_local.strftime("%H:%M"),
        end=end_local.strftime("%H:%M"),
        channel=channel_name,
    )


def _render_description(
    template: str,
    window_start: datetime,
    window_end: datetime,
    programs: list,
    channel_name: str = "",
) -> str:
    """説明欄テンプレートを JST 日時 + 番組リスト ({programs}) で埋めて返す。

    番組が無ければ {programs} は空文字。末尾に残る余分な空行は strip で落とす。
    {channel} はチャンネル名 (title と同じく使用可)。
    """
    start_local = timezone.localtime(window_start)
    end_local = timezone.localtime(window_end)
    program_lines = "\n".join(
        f"{timezone.localtime(p.start_at).strftime('%H:%M')} {p.title}" for p in programs
    )
    return template.format(
        date=start_local.strftime("%Y-%m-%d"),
        start=start_local.strftime("%H:%M"),
        end=end_local.strftime("%H:%M"),
        programs=program_lines,
        channel=channel_name,
    ).strip()


def _render_nudge(template: str, url: str, w_start: datetime, w_end: datetime) -> str:
    """次枠誘導文を {url}/{start}/{end} (次枠 watch URL・JST 開始/終了) で埋めて返す。"""
    start_local = timezone.localtime(w_start)
    end_local = timezone.localtime(w_end)
    return template.format(
        url=url,
        start=start_local.strftime("%H:%M"),
        end=end_local.strftime("%H:%M"),
    )


def _compose_slot_meta(
    channel: Channel,
    w_start: datetime,
    w_end: datetime,
    title_template: str,
    description_template: str,
) -> tuple[str, str]:
    """その 2h 窓の (title, description) を合成。

    - title: title_template (JST) に、窓に入る公開番組があれば "| 先頭番組 ほかN件" を足し 100 字に丸める。
    - description: description_template を JST 日時 + 番組リスト ({programs}) で埋める。番組が無くても
      テンプレート本文は必ず書き込む (YouTube デフォルト説明に頼らない / #7)。
    """
    from scheduling.models import Program

    progs = list(
        Program.objects.filter(
            channel=channel,
            public_visible=True,
            start_at__lt=w_end,
            end_at__gt=w_start,
        ).order_by("start_at")[:20]
    )
    base = _render_title(title_template, w_start, w_end, channel.name)
    if progs:
        lead = progs[0].title
        extra = f" ほか{len(progs) - 1}件" if len(progs) > 1 else ""
        title = f"{base} | {lead}{extra}"[:100]
    else:
        title = base
    description = _render_description(description_template, w_start, w_end, progs, channel.name)
    return title, description


def _windows(t0: datetime, t1: datetime, step: timedelta):
    """[t0, t1) を step ごとに区切ったタプル (start, end) を yield。

    step は YoutubeConfig.slot_minutes (既定 240) から渡される。
    """
    # t0 の壁時計 (= 呼び出し側が渡す tz) を基準に直近の step 境界へ揃える。
    # generate_slots は localtime(JST) を渡すので JST の 00:00/02:00/… に揃う。
    minutes = t0.hour * 60 + t0.minute
    step_min = int(step.total_seconds() // 60)
    aligned_min = (minutes // step_min) * step_min
    start = t0.replace(minute=0, second=0, microsecond=0) + timedelta(
        minutes=aligned_min - t0.hour * 60,
    )
    while start < t1:
        yield start, start + step
        start += step


def _channel_windows(channel: Channel, t0: datetime, t1: datetime, step: timedelta):
    """[t0, t1) を channel の枠境界で区切る。

    broadcast_windows 未設定 (24h 運用) なら従来通り t0 起点の step グリッド (_windows)。
    設定時は放送時間帯そのものを起点に step 刻みで分割する。固定グリッドのままだと放送
    時間帯の途中を境界が横切り、1 本のはずの放送が 2 本に分断されてしまう
    (例: 放送時間帯 06:00-10:00 に対し 00:00 起点 4h グリッドだと 04:00-08:00 と 08:00-12:00
    の 2 本が重なりありとして生成され、08:00 で配信が途切れる)。
    """
    intervals = channel.broadcast_intervals(t0, t1)
    if not intervals:
        yield from _windows(t0, t1, step)
        return
    for on_s, on_e in intervals:
        cur = on_s
        while cur < on_e:
            nxt = min(cur + step, on_e)
            if nxt > t0 and cur < t1:
                yield cur, nxt
            cur = nxt


@shared_task
def generate_slots(channel_id: int) -> dict[str, int]:
    """rolling_hours 先まで liveBroadcast を生成・bind し YoutubeSlot を upsert。

    既存 slot は unique(channel, window_start) で skip (冪等)。
    """
    channel = Channel.objects.select_related("youtube_config").get(pk=channel_id, enabled=True)
    cfg = channel.youtube_config  # OneToOneField (なければ DoesNotExist)
    if not channel.youtube_livestream_id:
        logger.warning("generate_slots channel=%s: 永続 liveStream 未作成", channel.slug)
        return {"created": 0, "skipped": 0}

    # 枠境界は JST の step 境界 (既定 4h → 00:00/04:00/…、broadcast_windows 設定時はその
    # 時間帯起点) に揃える。now を localtime(JST) 化してから渡す。yield される window は
    # tz-aware のまま (offset +09:00) なので、isoformat()/DB 保存時に正しい瞬間へ変換される。
    now = timezone.localtime(timezone.now())
    t1 = now + timedelta(hours=cfg.rolling_hours)
    step = timedelta(minutes=cfg.slot_minutes)

    stats = {"created": 0, "skipped": 0, "off_air": 0, "errors": 0}
    for w_start, w_end in _channel_windows(channel, now, t1, step):
        if YoutubeSlot.objects.filter(channel=channel, window_start=w_start).exists():
            stats["skipped"] += 1
            continue
        if not channel.slot_has_on_air(w_start, w_end):
            logger.debug(
                "generate_slots channel=%s skip off-air window %s-%s",
                channel.slug,
                w_start,
                w_end,
            )
            stats["off_air"] += 1
            continue
        title, description = _compose_slot_meta(
            channel, w_start, w_end, cfg.title_template, cfg.description_template
        )
        try:
            broadcast_id = insert_broadcast(
                channel,
                title=title,
                scheduled_start=w_start,
                privacy=cfg.privacy,
                enable_monitor=cfg.enable_monitor,
                description=description,
            )
        except HttpError as e:
            logger.exception("insert_broadcast failed channel=%s window=%s", channel.slug, w_start)
            YoutubeSlot.objects.update_or_create(
                channel=channel,
                window_start=w_start,
                defaults={
                    "window_end": w_end,
                    "title": title,
                    "description": description,
                    "status": YtSlotStatus.ERROR,
                    "error": str(e)[:1000],
                },
            )
            stats["errors"] += 1
            continue
        YoutubeSlot.objects.update_or_create(
            channel=channel,
            window_start=w_start,
            defaults={
                "window_end": w_end,
                "broadcast_id": broadcast_id,
                "title": title,
                "description": description,
                "status": YtSlotStatus.READY,
                "error": None,
            },
        )
        stats["created"] += 1
    return stats


@shared_task
def resync_slot_meta(channel_id: int, *, include_manual: bool = False) -> dict[str, int]:
    """未終了の既存枠のタイトル/説明を現在のテンプレートで再生成し YouTube へ反映。

    generate_slots は既存枠を skip するため、テンプレート (title_template/description_template)
    変更後に過去作成分へ内容を行き渡らせる用途。対象は window_end > now かつ COMPLETE/ERROR 以外。
    manual=True (人が編集) は include_manual=True のときのみ上書きする。
    """
    channel = Channel.objects.select_related("youtube_config").get(pk=channel_id, enabled=True)
    cfg = channel.youtube_config
    now = timezone.now()
    qs = YoutubeSlot.objects.filter(channel=channel, window_end__gt=now).exclude(
        status__in=[YtSlotStatus.COMPLETE, YtSlotStatus.ERROR],
    )
    if not include_manual:
        qs = qs.filter(manual=False)

    stats = {"updated": 0, "synced": 0, "errors": 0}
    for slot in qs:
        title, description = _compose_slot_meta(
            channel,
            slot.window_start,
            slot.window_end,
            cfg.title_template,
            cfg.description_template,
        )
        if slot.broadcast_id:
            try:
                update_broadcast(
                    channel,
                    slot.broadcast_id,
                    title=title,
                    scheduled_start=slot.window_start,
                    description=description,
                )
            except HttpError as e:
                logger.exception(
                    "resync update_broadcast failed channel=%s slot=%s", channel.slug, slot.id
                )
                slot.status = YtSlotStatus.ERROR
                slot.error = str(e)[:1000]
                slot.save(update_fields=["status", "error"])
                stats["errors"] += 1
                continue
            stats["synced"] += 1
        slot.title = title
        slot.description = description
        slot.save(update_fields=["title", "description"])
        stats["updated"] += 1
    return stats


@shared_task
def rotate_slots(channel_id: int) -> dict[str, int]:
    """壁時計を基準に live/complete 遷移を実行。

    - window_start <= now < window_end かつ status=ready → transition(live)
      事前条件: liveStream が active (RTMP 流入中)
    - window_end <= now かつ status=live → transition(complete)

    transition 失敗時の自己回復 (2026-07-06 の 503→4h 無配信の再発防止):
    - 5xx (一過性) は ERROR 確定にせず status を保ち、翌分の rotate が再試行 (deferred)
    - 4xx は実 lifeCycleStatus を確認し、既に目的状態なら追認 (invalidTransition ≒ 済み)
    - 窓を live 化されないまま過ぎた READY は ERROR に落として可視化 (missed)
    """
    channel = Channel.objects.get(pk=channel_id, enabled=True)
    now = timezone.now()
    stats = {"to_live": 0, "to_complete": 0, "blocked": 0, "deferred": 0, "missed": 0, "errors": 0}
    blocked_reason = ""

    # to_live: 今 window 内かつ ready のもの (放送時間帯外はスキップ)
    incoming = YoutubeSlot.objects.filter(
        channel=channel,
        window_start__lte=now,
        window_end__gt=now,
        status=YtSlotStatus.READY,
    )
    if incoming.exists():
        if not channel.is_on_air(now):
            logger.info(
                "rotate_slots channel=%s: off-air window, defer transition(live)",
                channel.slug,
            )
            stats["blocked"] = incoming.count()
            blocked_reason = "放送時間帯オフ"
        elif not livestream_active(channel):
            logger.warning(
                "rotate_slots channel=%s: liveStream not active, defer transition(live)",
                channel.slug,
            )
            stats["blocked"] = incoming.count()
            blocked_reason = "liveStream 非 active"
        else:
            for slot in incoming:
                if not slot.broadcast_id:
                    logger.warning(
                        "rotate_slots: slot=%s status=ready なのに broadcast_id=NULL",
                        slot.id,
                    )
                    stats["errors"] += 1
                    continue
                try:
                    transition_broadcast(channel, slot.broadcast_id, "live")
                    slot.status = YtSlotStatus.LIVE
                    slot.error = None
                    slot.save(update_fields=["status", "error"])
                    stats["to_live"] += 1
                except HttpError as e:
                    if _is_transient(e):
                        logger.warning(
                            "transition(live) transient failure channel=%s slot=%s: %s"
                            " (retry next tick)",
                            channel.slug,
                            slot.id,
                            e,
                        )
                        slot.error = str(e)[:1000]
                        slot.save(update_fields=["error"])
                        stats["deferred"] += 1
                        continue
                    reconciled = _reconciled_status(channel, slot.broadcast_id, "live")
                    if reconciled:
                        slot.status = reconciled
                        slot.error = None
                        slot.save(update_fields=["status", "error"])
                        stats["to_live"] += 1
                        continue
                    logger.exception(
                        "transition(live) failed channel=%s slot=%s",
                        channel.slug,
                        slot.id,
                    )
                    slot.status = YtSlotStatus.ERROR
                    slot.error = str(e)[:1000]
                    slot.save(update_fields=["status", "error"])
                    stats["errors"] += 1

    # to_complete: 終わった live を閉じる (window_end 到達、または放送時間帯外に入った場合)
    all_live = list(YoutubeSlot.objects.filter(channel=channel, status=YtSlotStatus.LIVE))
    now_off_air = bool(channel.effective_broadcast_windows) and not channel.is_on_air(now)
    finishing = [
        s
        for s in all_live
        if s.window_end <= now or (now_off_air and s.window_start <= now < s.window_end)
    ]
    for slot in finishing:
        if not slot.broadcast_id:
            logger.warning(
                "rotate_slots: slot=%s status=live なのに broadcast_id=NULL",
                slot.id,
            )
            stats["errors"] += 1
            continue
        try:
            transition_broadcast(channel, slot.broadcast_id, "complete")
            slot.status = YtSlotStatus.COMPLETE
            slot.save(update_fields=["status"])
            stats["to_complete"] += 1
        except HttpError as e:
            if _is_transient(e):
                logger.warning(
                    "transition(complete) transient failure channel=%s slot=%s: %s"
                    " (retry next tick)",
                    channel.slug,
                    slot.id,
                    e,
                )
                slot.error = str(e)[:1000]
                slot.save(update_fields=["error"])
                stats["deferred"] += 1
                continue
            reconciled = _reconciled_status(channel, slot.broadcast_id, "complete")
            if reconciled:
                slot.status = reconciled
                slot.error = None
                slot.save(update_fields=["status", "error"])
                stats["to_complete"] += 1
                continue
            logger.exception(
                "transition(complete) failed channel=%s slot=%s",
                channel.slug,
                slot.id,
            )
            slot.status = YtSlotStatus.ERROR
            slot.error = str(e)[:1000]
            slot.save(update_fields=["status", "error"])
            stats["errors"] += 1

    # 窓を live 化されないまま過ぎた READY (再試行が実らなかった / 放送時間帯オフのまま等) は
    # ERROR に落として studio コンソールで可視化する。放置すると再試行対象外のまま埋もれる。
    missed = YoutubeSlot.objects.filter(
        channel=channel, status=YtSlotStatus.READY, window_end__lte=now
    )
    missed_ids: list[int] = []
    for slot in missed:
        slot.status = YtSlotStatus.ERROR
        slot.error = ((slot.error or "") + " | 窓を通過 (live 化されず)").strip(" |")[:1000]
        slot.save(update_fields=["status", "error"])
        logger.warning(
            "rotate_slots: slot=%s missed its window without go-live channel=%s",
            slot.id,
            channel.slug,
        )
        missed_ids.append(slot.id)
        stats["missed"] += 1

    # 運用通知 (2026-09-02 監査 決定#1): blocked/missed は視聴者影響に直結するため webhook へ
    # 上げる。ログだけだと 2026-07-06 の「朝枠 4h 無配信」のように誰も気付かない。
    if stats["blocked"]:
        _notify_rotate_anomaly(
            channel,
            "yt_rotate_blocked",
            f"rolling枠 {stats['blocked']} 件が live 化できない ({blocked_reason})"
            f" channel={channel.slug}",
        )
    if stats["missed"]:
        _notify_rotate_anomaly(
            channel,
            "yt_rotate_missed",
            f"rolling枠 {stats['missed']} 件が窓を通過 (live 化されず ERROR 化)"
            f" slot={missed_ids} channel={channel.slug}",
        )

    return stats


def _next_broadcast_slot(channel: Channel, after: datetime) -> YoutubeSlot | None:
    """after 以降に始まる、broadcast_id を持つ最初の枠 (complete/error 除外)。次枠誘導の宛先。"""
    return (
        YoutubeSlot.objects.filter(
            channel=channel,
            window_start__gte=after,
            broadcast_id__isnull=False,
        )
        .exclude(status__in=[YtSlotStatus.COMPLETE, YtSlotStatus.ERROR])
        .order_by("window_start")
        .first()
    )


@shared_task
def nudge_next_slot(channel_id: int) -> dict[str, int]:
    """次枠誘導: 次枠 watch URL へ視聴者を誘導する。予告 (live) と移動後 (complete) の 2 段。

    2h 枠は watch URL が枠ごとに変わる (YouTube 仕様) ため、直接視聴している人は現枠の complete で
    取り残される。

    [予告] status=live かつ window_end - lead <= now < window_end かつ next_nudged=False の枠に、
    次枠 watch URL を nudge_template で整形し ① ライブチャットへ 1 回投稿 ② 説明欄先頭へ追記。
    成功/チャット未開設で next_nudged=True を立て重複投稿を防ぐ。説明欄は best-effort。lead=0 で無効。

    [移動後] status=complete かつ next_nudged=True かつ ended_nudged=False の枠 (= 予告済みで終了した
    枠) の説明文を nudge_ended_template (「終了しています」版) へ差し替え、ended_nudged=True を立てる。
    アーカイブに残る予告文 (「まもなく終了」) を終了後の文言へ更新する。ライブチャットは complete で
    閉じる (replay) ため対象外。次枠が無い / ended テンプレ空なら差し替えず flag だけ立てる。
    """
    channel = Channel.objects.select_related("youtube_config").get(pk=channel_id, enabled=True)
    cfg = getattr(channel, "youtube_config", None)
    stats = {"nudged": 0, "ended": 0, "skipped": 0, "errors": 0}
    if not cfg:
        return stats  # 設定なし
    now = timezone.now()

    # ---- 予告: live 枠の終了 lead 分前 ----
    lead = cfg.nudge_lead_minutes
    if lead > 0:
        due = YoutubeSlot.objects.filter(
            channel=channel,
            status=YtSlotStatus.LIVE,
            next_nudged=False,
            window_end__gt=now,
            window_end__lte=now + timedelta(minutes=lead),
        )
        for slot in due:
            nxt = _next_broadcast_slot(channel, slot.window_end)
            if nxt is None or not nxt.broadcast_id:
                logger.info(
                    "nudge_next_slot channel=%s slot=%s: 次枠なし、skip", channel.slug, slot.id
                )
                stats["skipped"] += 1
                continue

            text = _render_nudge(
                cfg.nudge_template, watch_url(nxt.broadcast_id), nxt.window_start, nxt.window_end
            )

            # 1) 説明欄に誘導文を prepend (冪等。best-effort で失敗しても投稿は続行)。
            if slot.broadcast_id:
                try:
                    update_broadcast(
                        channel,
                        slot.broadcast_id,
                        title=slot.title or "",
                        scheduled_start=slot.window_start,
                        description=(text + "\n\n" + (slot.description or "")).strip(),
                    )
                except HttpError:
                    logger.exception(
                        "nudge_next_slot desc update failed channel=%s slot=%s",
                        channel.slug,
                        slot.id,
                    )

            # 2) ライブチャットへ投稿 (非冪等)。成功/未開設で next_nudged を立て重複投稿を防ぐ。
            #    HttpError 時はフラグを立てず次 beat で window 内なら再試行。
            if slot.broadcast_id:
                try:
                    post_live_chat_message(channel, slot.broadcast_id, text)
                except HttpError:
                    logger.exception(
                        "nudge_next_slot chat post failed channel=%s slot=%s",
                        channel.slug,
                        slot.id,
                    )
                    stats["errors"] += 1
                    continue

            slot.next_nudged = True
            slot.save(update_fields=["next_nudged"])
            stats["nudged"] += 1

    # ---- 移動後: complete に遷移した「予告済み」枠の説明文を差し替え ----
    ended_due = YoutubeSlot.objects.filter(
        channel=channel,
        status=YtSlotStatus.COMPLETE,
        next_nudged=True,
        ended_nudged=False,
    )
    for slot in ended_due:
        nxt = _next_broadcast_slot(channel, slot.window_end)
        if nxt is None or not nxt.broadcast_id or not cfg.nudge_ended_template:
            # 差し替えるものが無い → flag だけ立て再走しない
            slot.ended_nudged = True
            slot.save(update_fields=["ended_nudged"])
            continue
        text = _render_nudge(
            cfg.nudge_ended_template, watch_url(nxt.broadcast_id), nxt.window_start, nxt.window_end
        )
        if slot.broadcast_id:
            try:
                update_broadcast(
                    channel,
                    slot.broadcast_id,
                    title=slot.title or "",
                    scheduled_start=slot.window_start,
                    description=(text + "\n\n" + (slot.description or "")).strip(),
                )
            except HttpError:
                logger.exception(
                    "nudge_next_slot ended update failed channel=%s slot=%s", channel.slug, slot.id
                )
                stats["errors"] += 1
                continue  # flag 立てず次 beat で再試行
        slot.ended_nudged = True
        slot.save(update_fields=["ended_nudged"])
        stats["ended"] += 1

    return stats


# ---- #23 番組専用枠 (rolling 枠と並行) ----


def _render_program_template(template: str, program, channel_name: str) -> str:
    """専用枠の title/description テンプレを {program}/{date}/{start}/{end}/{channel} で埋める。"""
    start_local = timezone.localtime(program.start_at)
    end_local = timezone.localtime(program.end_at)
    return template.format(
        program=program.title,
        date=start_local.strftime("%Y-%m-%d"),
        start=start_local.strftime("%H:%M"),
        end=end_local.strftime("%H:%M"),
        channel=channel_name,
    )


def _compose_program_meta(program, preset, channel_name: str) -> tuple[str, str]:
    """専用枠の (title, description)。テンプレ未設定/不正なら番組の素の値にフォールバック。"""
    title = program.title
    if preset.title_template:
        try:
            title = _render_program_template(preset.title_template, program, channel_name)[:100]
        except (KeyError, IndexError, ValueError):
            logger.warning("preset %s title_template 整形失敗、番組タイトルを使用", preset.id)
    description = program.description or ""
    if preset.description_template:
        try:
            description = _render_program_template(
                preset.description_template, program, channel_name
            )
        except (KeyError, IndexError, ValueError):
            logger.warning("preset %s description_template 整形失敗", preset.id)
    return title, description


def create_program_broadcast(channel: Channel, program, preset, *, manual: bool = False) -> str:
    """番組専用枠の liveBroadcast を 2 本目 liveStream へ insert→bind→preset 適用し ProgramBroadcast を upsert。

    手動ボタン (step ⑥) と beat (generate_dedicated_broadcasts) が共有する create primitive。
    2 本目 liveStream が未作成なら呼び出し側が事前に create_dedicated_stream すること。
    broadcast_id を返す。HttpError はそのまま投げる (呼び出し側で握る)。
    """
    title, description = _compose_program_meta(program, preset, channel.name)
    broadcast_id = insert_broadcast(
        channel,
        title=title,
        scheduled_start=program.start_at,
        privacy=preset.privacy,
        enable_monitor=False,
        description=description,
        made_for_kids=preset.made_for_kids,
        enable_dvr=preset.enable_dvr,
        enable_embed=preset.enable_embed,
        enable_auto_start=preset.enable_auto_start,
        enable_auto_stop=preset.enable_auto_stop,
        latency_preference=preset.latency,
        record_from_start=preset.record_from_start,
        scheduled_end=program.end_at,
        stream_id=channel.youtube_livestream_id_2,
    )
    apply_broadcast_preset(channel, broadcast_id, preset, title=title)
    ProgramBroadcast.objects.update_or_create(
        program=program,
        defaults={
            "preset": preset,
            "broadcast_id": broadcast_id,
            "status": YtSlotStatus.READY,
            "checklist_state": preset.initial_checklist_state(),
            "manual": manual,
            "error": None,
        },
    )
    return broadcast_id


@shared_task
def generate_dedicated_broadcasts(channel_id: int) -> dict[str, int]:
    """#23 直近 DEDICATED_LOOKAHEAD_HOURS の dedicated 番組へ専用枠を insert→preset適用→ready。

    2 本目 liveStream は対象番組がある時だけ自動プロビジョンする (無駄な stream を作らない)。
    preset 未解決 (番組/シリーズとも未指定) の番組は no_preset としてスキップ。冪等 (program OneToOne)。
    """
    from scheduling.models import Program

    channel = Channel.objects.get(pk=channel_id, enabled=True)
    now = timezone.now()
    horizon = now + timedelta(hours=DEDICATED_LOOKAHEAD_HOURS)
    stats = {"provisioned": 0, "created": 0, "skipped": 0, "no_preset": 0, "errors": 0}

    progs = Program.objects.select_related(
        "series", "youtube_preset", "series__youtube_preset"
    ).filter(channel=channel, end_at__gt=now, start_at__lt=horizon)
    todo = []
    for p in progs:
        if not p.wants_dedicated:
            continue
        if ProgramBroadcast.objects.filter(program=p).exists():
            stats["skipped"] += 1
            continue
        if p.resolved_youtube_preset is None:
            stats["no_preset"] += 1
            continue
        todo.append(p)

    if not todo:
        return stats  # 対象なし → 2 本目 stream のプロビジョンもしない

    if not channel.youtube_livestream_id_2:
        try:
            create_dedicated_stream(channel)
            stats["provisioned"] = 1
        except HttpError:
            logger.exception("create_dedicated_stream failed channel=%s", channel.slug)
            stats["errors"] += 1
            return stats

    for p in todo:
        preset = p.resolved_youtube_preset
        try:
            create_program_broadcast(channel, p, preset)
        except HttpError as e:
            logger.exception(
                "create_program_broadcast failed channel=%s program=%s", channel.slug, p.id
            )
            ProgramBroadcast.objects.update_or_create(
                program=p,
                defaults={"preset": preset, "status": YtSlotStatus.ERROR, "error": str(e)[:1000]},
            )
            stats["errors"] += 1
            continue
        stats["created"] += 1
    return stats


@shared_task
def rotate_dedicated(channel_id: int) -> dict[str, int]:
    """#23 番組専用枠の live/complete 遷移。rotate_slots と同型 (事前条件=2本目stream active)。

    - program.start_at <= now < program.end_at かつ status=ready → transition(live)
    - program.end_at <= now かつ status=live → transition(complete)
    失敗時の自己回復 (5xx=deferred 再試行 / 4xx=実状態追認 / 窓超過=missed) も rotate_slots と同じ。
    """
    channel = Channel.objects.get(pk=channel_id, enabled=True)
    now = timezone.now()
    stats = {"to_live": 0, "to_complete": 0, "blocked": 0, "deferred": 0, "missed": 0, "errors": 0}

    incoming = ProgramBroadcast.objects.select_related("program").filter(
        program__channel=channel,
        program__start_at__lte=now,
        program__end_at__gt=now,
        status=YtSlotStatus.READY,
    )
    if incoming.exists():
        if not livestream_active(channel, channel.youtube_livestream_id_2):
            logger.warning(
                "rotate_dedicated channel=%s: 2本目 liveStream not active, defer", channel.slug
            )
            stats["blocked"] = incoming.count()
        else:
            for pb in incoming:
                if not pb.broadcast_id:
                    stats["errors"] += 1
                    continue
                try:
                    transition_broadcast(channel, pb.broadcast_id, "live")
                    pb.status = YtSlotStatus.LIVE
                    pb.error = None
                    pb.save(update_fields=["status", "error", "updated_at"])
                    stats["to_live"] += 1
                except HttpError as e:
                    if _is_transient(e):
                        logger.warning(
                            "dedicated transition(live) transient failure channel=%s pb=%s: %s"
                            " (retry next tick)",
                            channel.slug,
                            pb.id,
                            e,
                        )
                        pb.error = str(e)[:1000]
                        pb.save(update_fields=["error", "updated_at"])
                        stats["deferred"] += 1
                        continue
                    reconciled = _reconciled_status(channel, pb.broadcast_id, "live")
                    if reconciled:
                        pb.status = reconciled
                        pb.error = None
                        pb.save(update_fields=["status", "error", "updated_at"])
                        stats["to_live"] += 1
                        continue
                    logger.exception(
                        "dedicated transition(live) failed channel=%s pb=%s", channel.slug, pb.id
                    )
                    pb.status = YtSlotStatus.ERROR
                    pb.error = str(e)[:1000]
                    pb.save(update_fields=["status", "error", "updated_at"])
                    stats["errors"] += 1

    finishing = ProgramBroadcast.objects.select_related("program").filter(
        program__channel=channel,
        program__end_at__lte=now,
        status=YtSlotStatus.LIVE,
    )
    for pb in finishing:
        if not pb.broadcast_id:
            stats["errors"] += 1
            continue
        try:
            transition_broadcast(channel, pb.broadcast_id, "complete")
            pb.status = YtSlotStatus.COMPLETE
            pb.save(update_fields=["status", "updated_at"])
            stats["to_complete"] += 1
        except HttpError as e:
            if _is_transient(e):
                logger.warning(
                    "dedicated transition(complete) transient failure channel=%s pb=%s: %s"
                    " (retry next tick)",
                    channel.slug,
                    pb.id,
                    e,
                )
                pb.error = str(e)[:1000]
                pb.save(update_fields=["error", "updated_at"])
                stats["deferred"] += 1
                continue
            reconciled = _reconciled_status(channel, pb.broadcast_id, "complete")
            if reconciled:
                pb.status = reconciled
                pb.error = None
                pb.save(update_fields=["status", "error", "updated_at"])
                stats["to_complete"] += 1
                continue
            logger.exception(
                "dedicated transition(complete) failed channel=%s pb=%s", channel.slug, pb.id
            )
            pb.status = YtSlotStatus.ERROR
            pb.error = str(e)[:1000]
            pb.save(update_fields=["status", "error", "updated_at"])
            stats["errors"] += 1

    # 番組終了まで live 化されなかった READY は ERROR で可視化 (rotate_slots の missed と同義)
    missed = ProgramBroadcast.objects.select_related("program").filter(
        program__channel=channel,
        program__end_at__lte=now,
        status=YtSlotStatus.READY,
    )
    missed_ids: list[int] = []
    for pb in missed:
        pb.status = YtSlotStatus.ERROR
        pb.error = ((pb.error or "") + " | 番組時間を通過 (live 化されず)").strip(" |")[:1000]
        pb.save(update_fields=["status", "error", "updated_at"])
        logger.warning(
            "rotate_dedicated: pb=%s missed program window without go-live channel=%s",
            pb.id,
            channel.slug,
        )
        missed_ids.append(pb.id)
        stats["missed"] += 1

    # 運用通知 (2026-09-02 監査 決定#1): rotate_slots と同じく blocked/missed を webhook へ上げる。
    if stats["blocked"]:
        _notify_rotate_anomaly(
            channel,
            "yt_dedicated_blocked",
            f"番組専用枠 {stats['blocked']} 件が live 化できない (2本目 liveStream 非 active)"
            f" channel={channel.slug}",
        )
    if stats["missed"]:
        _notify_rotate_anomaly(
            channel,
            "yt_dedicated_missed",
            f"番組専用枠 {stats['missed']} 件が番組時間を通過 (live 化されず ERROR 化)"
            f" pb={missed_ids} channel={channel.slug}",
        )

    return stats


@shared_task
def generate_dedicated_broadcasts_all() -> dict[int, dict[str, int]]:
    return {
        ch.id: generate_dedicated_broadcasts(ch.id)
        for ch in Channel.objects.filter(enabled=True, youtube_livestream_id__isnull=False)
    }


@shared_task
def rotate_dedicated_all() -> dict[int, dict[str, int]]:
    return {
        ch.id: rotate_dedicated(ch.id)
        for ch in Channel.objects.filter(enabled=True, youtube_livestream_id__isnull=False)
    }


@shared_task
def generate_slots_all() -> dict[int, dict[str, int]]:
    return {
        ch.id: generate_slots(ch.id)
        for ch in Channel.objects.filter(enabled=True, youtube_livestream_id__isnull=False)
    }


@shared_task
def rotate_slots_all() -> dict[int, dict[str, int]]:
    return {
        ch.id: rotate_slots(ch.id)
        for ch in Channel.objects.filter(enabled=True, youtube_livestream_id__isnull=False)
    }


@shared_task
def nudge_next_slot_all() -> dict[int, dict[str, int]]:
    return {
        ch.id: nudge_next_slot(ch.id)
        for ch in Channel.objects.filter(enabled=True, youtube_livestream_id__isnull=False)
    }
