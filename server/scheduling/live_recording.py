# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""生放送録画クリップの取り込みロジック (#VOD 生放送録画・resolver.emit_live の record_live 連携)。

送出 agent 側が record_live=True の生放送を録画し、録画サブシステムが完成クリップを R2 の
`ingest/live_recording/<channel_slug>/<program_id>.mp4` に置く。ここでは:
  scan() … 新規キーを Asset(kind=program) 化 (→ post_save signal が正規化を投入) し
           LiveRecordingIngest に記録
  bind() … 正規化 READY 後、対応する Program.recording_asset を差し替える (初回のみ)
source_key 一意で二重取り込みを防ぐ。program_id はキーに直接含まれるため、weather.py と異なり
時刻ウィンドウ照合は不要で Program を一意に特定できる。該当 Program が無ければ Asset は作らず
LiveRecordingIngest のみ NO_PROGRAM で記録し、運用者が後で調査できる孤児レコードとして残す。
"""

from __future__ import annotations

import logging
import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from core import r2
from core.models import Channel
from medialib.models import Asset, AssetKind, NormalizeStatus
from scheduling.models import LiveRecordingIngest, LiveRecordingIngestState, Program

logger = logging.getLogger(__name__)

INGEST_PREFIX = "ingest/live_recording/"
# ingest/live_recording/<channel_slug>/<program_id>.mp4 のみ対象
_KEY_RE = re.compile(r"^ingest/live_recording/([^/]+)/(\d+)\.mp4$")


def parse_key(key: str) -> tuple[str, int] | None:
    """投入キー → (channel_slug, program_id)。不正/対象外なら None。"""
    m = _KEY_RE.match(key)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def scan(limit: int = 200) -> int:
    """ingest/live_recording/ の新規キーを Asset 化 + LiveRecordingIngest 記録。戻り = 新規件数。"""
    created = 0
    for o in r2.list_objects(INGEST_PREFIX):
        if created >= limit:
            logger.warning("live_recording scan: limit %d reached, deferring rest", limit)
            break
        key = o["key"]
        if LiveRecordingIngest.objects.filter(source_key=key).exists():
            continue  # 取り込み済み (冪等)
        parsed = parse_key(key)
        if parsed is None:
            continue  # processed/ や不正キー
        channel_slug, program_id = parsed
        channel = Channel.objects.filter(slug=channel_slug).first()
        if channel is None:
            logger.warning("live_recording scan: unknown channel slug %s in %s", channel_slug, key)
            continue
        prog = Program.objects.filter(pk=program_id).first()
        try:
            with transaction.atomic():
                if prog is None:
                    # 対象番組なし: Asset は作らず孤児レコードとして記録 (運用者が後で調査)。
                    LiveRecordingIngest.objects.create(
                        source_key=key,
                        channel=channel,
                        state=LiveRecordingIngestState.NO_PROGRAM,
                    )
                else:
                    asset = Asset.objects.create(
                        kind=AssetKind.PROGRAM,
                        title=f"{prog.title} (録画) {timezone.localtime(prog.start_at):%Y-%m-%d %H:%M}",
                        source_path=f"r2://{key}",
                        normalize_status=NormalizeStatus.PENDING,
                    )  # post_save signal が on_commit で normalize_asset.delay
                    LiveRecordingIngest.objects.create(
                        source_key=key,
                        channel=channel,
                        program=prog,
                        asset=asset,
                        state=LiveRecordingIngestState.NORMALIZING,
                    )
            created += 1
        except IntegrityError:
            # 並走で同 key を先に作られた (source_key unique) → rollback されスキップ
            logger.info("live_recording scan: duplicate key %s skipped", key)
    return created


def bind() -> dict[str, int]:
    """正規化 READY の在庫を該当 Program.recording_asset へ差し替え。初回のみ (冪等)。"""
    stats = {"bound": 0, "failed": 0, "waiting": 0}
    qs = LiveRecordingIngest.objects.filter(
        state=LiveRecordingIngestState.NORMALIZING, program__isnull=False
    ).select_related("asset", "program")
    for ri in qs:
        asset = ri.asset
        if asset is None:
            _fail(ri, "asset が消失")
            stats["failed"] += 1
            continue
        if asset.normalize_status == NormalizeStatus.FAILED:
            _fail(ri, asset.normalize_error or "正規化失敗")
            stats["failed"] += 1
            continue
        if asset.normalize_status != NormalizeStatus.READY:
            stats["waiting"] += 1
            continue
        prog = ri.program
        if prog is None:
            _fail(ri, "program が消失")
            stats["failed"] += 1
            continue
        if prog.recording_asset_id is None:
            prog.recording_asset = asset
            prog.save(update_fields=["recording_asset"])
        ri.state = LiveRecordingIngestState.BOUND
        ri.save(update_fields=["state"])
        stats["bound"] += 1
    return stats


def _fail(ri: LiveRecordingIngest, msg: str) -> None:
    ri.state = LiveRecordingIngestState.FAILED
    ri.error = msg
    ri.save(update_fields=["state", "error"])
