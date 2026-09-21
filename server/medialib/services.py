# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""素材・CM の運用ロジック (admin アクションと専用画面の双方から使う共有処理)。

normalize 再投入・考査ステータス更新・CM 在庫状態の算出を一元化する。
在庫状態の区分は docs/ui.md「CM 在庫状態」に対応づける。
"""

from __future__ import annotations

from datetime import date

from django.db import transaction

from medialib.models import (
    Asset,
    CmCreative,
    CmGrid,
    CueKind,
    CueSheet,
    NormalizeStatus,
    ScreeningStatus,
)

# 残尺がこの比率を下回ったら「残少」警告 (max_airings に対する残回数比)。
LOW_STOCK_RATIO = 0.2

_SCREENING_DECISIONS = {
    "approve": ScreeningStatus.APPROVED,
    "reject": ScreeningStatus.REJECTED,
    "reset": ScreeningStatus.PENDING,
}


def renormalize(asset: Asset) -> None:
    """素材を pending に戻し正規化を投入 (失敗素材の再正規化・再取り込み)。

    入口は dispatch_normalize (Windows オフロード可否を判定してから振り分ける)。オフロード追跡も
    クリアして、前回のオフロード残骸に引きずられず新規 requestId でやり直せるようにする。
    """
    from medialib.tasks import dispatch_normalize

    asset.normalize_status = NormalizeStatus.PENDING
    asset.normalize_error = None
    asset.offload_request_id = None
    asset.offload_dispatched_at = None
    asset.save(
        update_fields=[
            "normalize_status",
            "normalize_error",
            "offload_request_id",
            "offload_dispatched_at",
        ]
    )
    transaction.on_commit(lambda: dispatch_normalize.delay(asset.pk))


def set_screening(cm: CmCreative, decision: str) -> ScreeningStatus:
    """表現考査ステータスを更新 (approve / reject / reset)。"""
    if decision not in _SCREENING_DECISIONS:
        raise ValueError(f"未知の考査操作: {decision!r}")
    cm.screening_status = _SCREENING_DECISIONS[decision]
    cm.save(update_fields=["screening_status"])
    return cm.screening_status


def cm_stock_state(cm: CmCreative, today: date | None = None) -> dict[str, str]:
    """CM 在庫状態を docs/ui.md の区分で算出。{"label", "css"} を返す。

    優先順位: 要設定 (日付欠落) → 終了 → 予定 → 上限到達 → 残少 → 配信中。
    """
    today = today or date.today()
    if cm.campaign_start is None or cm.campaign_end is None:
        return {"label": "△要設定", "css": "warn"}
    if cm.campaign_end < today:
        return {"label": "✓終了", "css": "muted"}
    if cm.campaign_start > today:
        return {"label": "○予定", "css": "muted"}
    if cm.max_airings is not None and cm.aired_count >= cm.max_airings:
        return {"label": "上限到達", "css": "warn"}
    if cm.max_airings and (cm.max_airings - cm.aired_count) / cm.max_airings < LOW_STOCK_RATIO:
        return {"label": "⚠残少", "css": "warn"}
    return {"label": "●配信中", "css": "live"}


# ---- キューシート (素材内部ランダウン) ----


class CueSheetError(ValueError):
    """キューシートの整合性エラー (UI に表示する想定)。"""


def _grid_unit(grid: str | None) -> int:
    return 20000 if grid == CmGrid.G20 else 15000


def cue_content_total_ms(cue_sheet: CueSheet) -> int:
    return sum(p.duration_ms for p in cue_sheet.points.all() if p.kind == CueKind.CONTENT)


def validate_cue_sheet(cue_sheet: CueSheet) -> None:
    """整合性検証。本編で素材尺を過不足なく分割し、CM枠は grid 整数倍・素材尺内に収まること。

    挿入モデル (resolver) は CM を本編の途中に差し込むため、offset は [0, 素材尺) に限る
    (末尾の post-roll は非対応)。
    """
    asset = cue_sheet.asset
    if asset.duration_ms is None:
        raise CueSheetError("素材尺が未取得です (正規化後に設定可能)")
    points = list(cue_sheet.points.all())
    content_head = 0
    for p in points:
        if p.duration_ms <= 0:
            raise CueSheetError("各行の尺は正の値が必要です")
        if p.kind == CueKind.CONTENT:
            content_head += p.duration_ms
            continue
        # ad_break
        unit = _grid_unit(p.grid)
        if p.grid not in CmGrid.values:
            raise CueSheetError("CM枠には grid (15s/20s) が必要です")
        if p.duration_ms % unit != 0:
            raise CueSheetError(f"CM枠尺は {unit // 1000}秒 の整数倍が必要です")
        if content_head <= 0 or content_head >= asset.duration_ms:
            raise CueSheetError("CM枠は本編の途中 (素材尺の範囲内) に挿入してください")
    total = sum(p.duration_ms for p in points if p.kind == CueKind.CONTENT)
    if total != asset.duration_ms:
        raise CueSheetError(
            f"本編尺の合計 ({total}ms) が素材尺 ({asset.duration_ms}ms) と一致しません"
        )


def derive_ad_breaks(cue_sheet: CueSheet) -> list[dict]:
    """キューシートから ad_break 仕様 [{offset_ms, grid, duration_ms}] を導出 (検証込み)。

    offset_ms = その CM枠より前に流れる本編の累積尺 (= 純コンテンツ列での挿入位置)。
    resolver.emit_recorded が期待する形。
    """
    validate_cue_sheet(cue_sheet)
    specs: list[dict] = []
    content_head = 0
    for p in cue_sheet.points.all():
        if p.kind == CueKind.CONTENT:
            content_head += p.duration_ms
        else:
            specs.append({"offset_ms": content_head, "grid": p.grid, "duration_ms": p.duration_ms})
    return specs


def cue_total_airtime_ms(cue_sheet: CueSheet) -> int:
    """配置時の総尺 = 素材尺 + Σ(CM枠尺)。end_at 算出用。"""
    return sum(p.duration_ms for p in cue_sheet.points.all())


def program_airtime_ms(asset: Asset) -> int | None:
    """素材を番組として配置したときの総尺(ms)。end_at の自動算出に使う。

    キューシートがあれば 素材尺 + Σ(CM枠尺) (cue_total_airtime_ms)、
    なければ素材尺 (asset.duration_ms)。尺未取得 (正規化前など) は None。
    """
    try:
        cue = asset.cue_sheet
    except CueSheet.DoesNotExist:
        cue = None
    if cue is not None:
        return cue_total_airtime_ms(cue)
    return asset.duration_ms
