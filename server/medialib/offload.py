# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""正規化の Windows オフロード・ブリッジ (R2 疎結合 + 常時クラスタフォールバック)。

設計正本: docs/normalize-offload.md。Windows レンダ機に mezzanine エンコードを opportunistic に
振り、機が不在/失敗/停滞なら即クラスタ内正規化へフォールバックする。DB/Celery broker には
外部機から一切到達させず、連携面は R2 prefix のみ (slidecast/weather のオフロードと同型)。

本モジュールは純粋なオーケストレーション (判定/R2 入出力/検証/昇格/掃除)。Celery タスクの薄い
ラッパは medialib.tasks (dispatch_normalize / reconcile_normalize_offload)。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core import r2
from medialib import mezz
from medialib.models import Asset, NormalizeStatus

logger = logging.getLogger(__name__)

# --- R2 prefix / キー (バケットは既存 R2_BUCKET を共用、prefix で分離) ---
OFFLOAD_PREFIX = "normalize/"
HEARTBEAT_KEY = "normalize/watch/heartbeat.json"


def _in_prefix(asset_id: int) -> str:
    return f"normalize/in/{asset_id}/"


def _out_prefix(asset_id: int) -> str:
    return f"normalize/out/{asset_id}/"


def request_key(asset_id: int) -> str:
    return f"{_in_prefix(asset_id)}request.json"


def status_key(asset_id: int) -> str:
    return f"{_out_prefix(asset_id)}status.json"


def result_key(asset_id: int) -> str:
    return f"{_out_prefix(asset_id)}result.json"


def mezz_out_key(asset_id: int) -> str:
    return f"{_out_prefix(asset_id)}mezz.mp4"


# --- env (killswitch 流儀 = os.environ 直読み、既定は完全无害) ---
def _enabled() -> bool:
    return os.environ.get("NORMALIZE_OFFLOAD_ENABLED", "false").lower() == "true"


def _min_source_sec() -> int:
    return int(os.environ.get("NORMALIZE_OFFLOAD_MIN_SOURCE_SEC", "300"))


def _heartbeat_fresh_sec() -> int:
    return int(os.environ.get("NORMALIZE_OFFLOAD_HEARTBEAT_FRESH_SEC", "180"))


def _claim_sec() -> int:
    return int(os.environ.get("NORMALIZE_OFFLOAD_CLAIM_SEC", "300"))


def _progress_stale_sec() -> int:
    return int(os.environ.get("NORMALIZE_OFFLOAD_PROGRESS_STALE_SEC", "900"))


def _max_wait_sec() -> int:
    return int(os.environ.get("NORMALIZE_OFFLOAD_MAX_WAIT_SEC", "14400"))


def _orphan_ttl_sec() -> int:
    return int(os.environ.get("NORMALIZE_OFFLOAD_ORPHAN_TTL_SEC", "86400"))


def _probe_timeout_sec() -> int:
    # dispatch の尺ゲート用 ffprobe (presigned GET 越し) の上限。既定 20s (短めにして、判定を積む
    # queue を長時間占有しない。網障害時は subprocess timeout で打ち切りローカルへ倒す)。
    return int(os.environ.get("NORMALIZE_OFFLOAD_PROBE_TIMEOUT_SEC", "20"))


def _max_source_sec() -> int:
    # ローカル passthrough 閾値 (MEZZ_MAX_SOURCE_SEC) と同値。これを超える長尺はオフロードせず
    # ローカルへ倒す = ローカルは即 passthrough (映像コピー) で数分で READY 化できるため、
    # オフロードのフルエンコード (数時間) で VOD 即時性を退行させない (docs §3.2 / レビュー #5)。
    # 0 (passthrough 無効) のときは上限なし = 長尺もフルエンコードでオフロードする。
    return int(os.environ.get("MEZZ_MAX_SOURCE_SEC", "0"))


def heartbeat_fresh() -> bool:
    """watcher の heartbeat が新しいか (Windows 機が稼働中か)。

    heartbeat.json 本体の updatedAt ではなく R2 の LastModified (サーバ時刻) で判定し、
    外部機の時計ずれに依存しない (docs/normalize-offload.md §3.2)。
    """
    meta = r2.head_object(HEARTBEAT_KEY)
    if not meta or not meta.get("last_modified"):
        return False
    age = (timezone.now() - meta["last_modified"]).total_seconds()
    # 微小な時計ずれ (R2 サーバ時刻 vs クラスタ NTP) で直近 PUT が僅かに未来に見えても fresh 扱い。
    # 逆に大きく未来 (fresh_sec 超) は異常 → 安全側で not-fresh (= ローカル処理)。
    fresh = _heartbeat_fresh_sec()
    return -fresh <= age <= fresh


def _source_key(asset: Asset) -> str | None:
    """r2:// source_path から R2 キーを取り出す。r2:// でなければ None (オフロード対象外)。"""
    sp = asset.source_path or ""
    if sp.startswith("r2://"):
        return sp[len("r2://") :]
    return None


def should_offload(asset: Asset) -> tuple[bool, str, float | None]:
    """オフロードすべきか判定。戻り値 (offload?, reason, source_sec)。

    上から順に不成立で False (= クラスタ内正規化)。判定は数秒 (R2 HEAD + ffprobe ヘッダ読み)。
    source_sec は成功時のみ実測値、それ以外 None。
    """
    if not _enabled():
        return False, "disabled", None
    key = _source_key(asset)
    if key is None:
        # ローカルパス素材 (admin 投入) は watcher が取得できない → ローカル。
        return False, "not-r2-source", None
    if not heartbeat_fresh():
        return False, "no-heartbeat", None
    # 尺ゲート: presigned GET 越しに ffprobe (moov のみ取得、DL しない)。測れなければ安全側で
    # ローカルに倒す (誤オフロードより確実なローカル処理)。
    try:
        url = r2.presign_get(key, expires=max(600, _probe_timeout_sec() + 60))
        src_sec = mezz.probe_duration_sec(url, timeout=_probe_timeout_sec())
    except Exception as e:  # R2/ffprobe 障害時はローカルに倒す
        logger.warning("offload probe 失敗 asset=%s: %s — ローカルへ", asset.id, e)
        return False, "probe-failed", None
    if src_sec is None:
        return False, "probe-none", None
    if src_sec < _min_source_sec():
        # 短尺 (weather 180s 等) は往復コストが勝つ + 時刻直結が多い → ローカル。
        return False, "too-short", src_sec
    max_sec = _max_source_sec()
    if 0 < max_sec < src_sec:
        # ローカル passthrough で即 READY にできる長尺は、オフロードのフルエンコードで即時性を
        # 落とさないためローカルへ (VOD 退行防止・レビュー #5)。
        return False, "too-long-passthrough", src_sec
    return True, "offload", src_sec


def submit(asset: Asset, source_key: str, source_sec: float) -> str:
    """request.json を R2 に PUT してオフロード依頼を発行。新しい requestId を返す。

    bundle は無く原本は既に R2 にあるので、request.json 単体が「レンダ待ち」マーカー。
    """
    request_id = str(uuid.uuid4())
    spec = mezz.spec_from_env()
    body = {
        "assetId": asset.id,
        "requestId": request_id,
        "sourceKey": source_key,
        "sourceDurationSec": source_sec,
        "resultPrefix": _out_prefix(asset.id),
        "spec": spec,
        "submittedAt": timezone.now().isoformat(),
    }
    r2.put_object(
        request_key(asset.id),
        json.dumps(body).encode("utf-8"),
        "application/json",
    )
    return request_id


def _read_json(key: str) -> dict | None:
    try:
        body, _ = r2.get_object(key)
    except Exception:  # 404 含め「まだ無い」として扱う
        return None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


# これらの reason は「オフロードしない=クラスタ内正規化へ」を意味する (tasks が normalize を投入)。
LOCAL_REASONS = frozenset(
    {
        "disabled",
        "not-r2-source",
        "no-heartbeat",
        "too-short",
        "too-long-passthrough",
        "probe-failed",
        "probe-none",
    }
)


def dispatch(asset_id: int) -> str:
    """1 asset をオフロードするか判定し、する場合は request 発行 + Asset をオフロード中に。

    戻り値 reason: "offload"=依頼済 / LOCAL_REASONS の各値=ローカル正規化へ / "not-pending"|"gone"=何もしない。
    尺ゲートの ffprobe (最大 60s) はロック外で行い、状態遷移だけを select_for_update で確保する
    (行ロックを長時間保持しない)。
    """
    asset = Asset.objects.filter(pk=asset_id).first()
    if asset is None:
        return "gone"
    if asset.normalize_status != NormalizeStatus.PENDING:
        return "not-pending"  # 既に処理中/完了 → 二重投入しない
    ok, reason, src_sec = should_offload(asset)  # 網越し ffprobe を含む (ロック外)
    source_key = _source_key(asset)
    if not ok or source_key is None or src_sec is None:
        # ok=True なら source_key/src_sec は非 None だが、型の絞り込み + 念のための安全側 (ローカル)。
        return reason
    with transaction.atomic():
        locked = (
            Asset.objects.select_for_update(skip_locked=True)
            .filter(pk=asset_id, normalize_status=NormalizeStatus.PENDING)
            .first()
        )
        if locked is None:
            return "not-pending"  # 判定中に別処理が確保 → 何もしない
        request_id = submit(locked, source_key, src_sec)  # PUT request.json (sub-second)
        locked.normalize_status = NormalizeStatus.PROCESSING
        locked.normalize_error = None
        locked.normalize_started_at = timezone.now()
        locked.offload_request_id = request_id
        locked.offload_dispatched_at = timezone.now()
        locked.save(
            update_fields=[
                "normalize_status",
                "normalize_error",
                "normalize_started_at",
                "offload_request_id",
                "offload_dispatched_at",
            ]
        )
    logger.info("offload dispatched asset=%s request=%s dur=%.0fs", asset_id, request_id, src_sec)
    return "offload"


def _delete_prefixes(asset_id: int) -> None:
    """in/ と out/ の全オブジェクトを削除 (finalize/fallback 後の掃除)。冪等。"""
    for prefix in (_in_prefix(asset_id), _out_prefix(asset_id)):
        for o in r2.list_objects(prefix):
            with _suppress():
                r2.delete_object(o["key"])


@contextlib.contextmanager
def _suppress():
    """R2 掃除の削除失敗を握り潰して継続する (ログのみ・冪等な best-effort 削除用)。"""
    try:
        yield
    except Exception as e:
        logger.warning("offload cleanup 削除失敗 (無視して継続): %s", e)


def _validate_output(asset_id: int, probe: dict, spec: dict, src_sec: float) -> bool:
    """watcher 成果物の構造検証 (mezzanine 均一性)。NG は False。

    解像度/コーデック/fps が規格 spec と一致し、尺が原本と整合するか。watcher が別 spec で焼いた
    もの・破損・トリムを弾く (内容の正しさは検証できない = レビュー #16 の残存リスクは doc 記載)。
    """
    v = mezz.stream(probe, "video")
    if v is None:
        logger.warning("offload finalize 検証NG asset=%s: video stream 無し", asset_id)
        return False
    if v.get("width") != spec["width"] or v.get("height") != spec["height"]:
        logger.warning(
            "offload finalize 検証NG asset=%s: 解像度 %sx%s != 規格 %sx%s",
            asset_id,
            v.get("width"),
            v.get("height"),
            spec["width"],
            spec["height"],
        )
        return False
    if (v.get("codec_name") or "").lower() != "h264":
        logger.warning("offload finalize 検証NG asset=%s: vcodec=%s", asset_id, v.get("codec_name"))
        return False
    # fps 検証: 規格 fps ±1 (watcher が -r で規格 fps に固定しているはず。VFR/別 fps を弾く)。
    fps = mezz.fps_from_str(v.get("avg_frame_rate"))
    if fps is None or abs(fps - spec["fps"]) > 1.0:
        logger.warning(
            "offload finalize 検証NG asset=%s: fps=%s != 規格 %s", asset_id, fps, spec["fps"]
        )
        return False
    # 尺整合: 原本尺と mezzanine 尺が ±max(2s, 2%) で一致 (トリム/欠落の検出)。
    try:
        out_sec = float(probe["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        logger.warning("offload finalize 検証NG asset=%s: 尺取得不可", asset_id)
        return False
    if src_sec > 0:
        tol = max(2.0, src_sec * 0.02)
        if abs(out_sec - src_sec) > tol:
            logger.warning(
                "offload finalize 検証NG asset=%s: 尺 %.1fs vs 原本 %.1fs (許容±%.1fs)",
                asset_id,
                out_sec,
                src_sec,
                tol,
            )
            return False
    return True


def produce_mezzanine(asset_id: int, kind: str, result: dict) -> dict | None:
    """watcher 成果物を ffprobe 検証 → mezzanine/ へサーバサイド昇格。probe dict / 検証NG は None。

    **DB もロックも触らない** (レビュー #11: 数分かかりうる R2 copy を行ロック外で行う)。呼び出し側は
    戻り値の probe で短いトランザクション内に Asset を READY 確定する。mezzanine キーは決定的
    (`mezzanine/<kind>/<id>.<container>`) なので、並行/再実行で上書きしても最後の書き手が勝つだけ。
    """
    out_key = mezz_out_key(asset_id)
    spec = mezz.spec_from_env()
    # クラスタ自身が計測 (watcher の自己申告 result.json は信用しない)。output は +faststart なので
    # moov が先頭 → presigned GET 越しの ffprobe は安価。
    try:
        probe = mezz.ffprobe(r2.presign_get(out_key, expires=3600))
    except Exception as e:
        logger.warning("offload finalize probe 失敗 asset=%s: %s", asset_id, e)
        return None

    if not _validate_output(asset_id, probe, spec, float(result.get("sourceDurationSec") or 0)):
        return None

    # 検証OK → 最終 mezzanine キーへサーバサイド copy (mezzanine/ に書くのはクラスタだけ)。
    # copy 失敗 (R2 一時障害・大容量 multipart copy 非対応等) は None にして fallback へ (伝播させると
    # reconcile が同 asset を毎周 retry する poison loop になる)。
    final_key = f"mezzanine/{kind}/{asset_id}.{spec['container']}"
    try:
        r2.copy_object(out_key, final_key, content_type=f"video/{spec['container']}")
    except Exception as e:
        logger.warning("offload finalize copy 失敗 asset=%s: %s → fallback", asset_id, e)
        return None
    logger.info("offload mezzanine 昇格 asset=%s r2=%s", asset_id, final_key)
    return probe


def mezzanine_key(kind: str, asset_id: int) -> str:
    return f"mezzanine/{kind}/{asset_id}.{mezz.spec_from_env()['container']}"


def cleanup_after_finalize(asset_id: int) -> None:
    """finalize 成功後の R2 掃除 (in/out 全削除)。on_commit から呼ぶ想定。"""
    _delete_prefixes(asset_id)


def clear_offload_fields(asset: Asset, *, reset_started_at: bool = False) -> None:
    """Asset のオフロード追跡フィールドをクリア (finalize 済/フォールバック時)。

    reset_started_at=True でフォールバック時に normalize_started_at も now に更新する
    (ローカル正規化がキュー滞留で遅れても 8h stale 回収に誤検出されないため・レビュー #2/#12)。
    """
    asset.offload_request_id = None
    asset.offload_dispatched_at = None
    fields = ["offload_request_id", "offload_dispatched_at"]
    if reset_started_at:
        asset.normalize_started_at = timezone.now()
        fields.append("normalize_started_at")
    asset.save(update_fields=fields)


def scan_orphans() -> int:
    """オフロード中でない asset の out/ 成果物で古いものを削除。削除件数を返す。

    フォールバック後に遅れて届いた GB 級成果物を放置しないための掃除。out/<id>/ 直下の
    最新更新が TTL より古く、かつ該当 asset が今オフロード中でない (request_id null / 別 id) ものを消す。
    """
    ttl = _orphan_ttl_sec()
    cutoff = timezone.now() - timedelta(seconds=ttl)
    # out/<id>/ ごとにグルーピング。
    by_asset: dict[int, list[dict]] = {}
    for o in r2.list_objects("normalize/out/"):
        parts = o["key"].split("/")
        if len(parts) < 4:
            continue
        try:
            aid = int(parts[2])
        except ValueError:
            continue
        by_asset.setdefault(aid, []).append(o)

    if not by_asset:
        return 0
    # 今オフロード中の asset は触らない。
    active = set(
        Asset.objects.filter(pk__in=list(by_asset), offload_request_id__isnull=False).values_list(
            "pk", flat=True
        )
    )
    deleted = 0
    for aid, objs in by_asset.items():
        if aid in active:
            continue
        newest = max(
            (o["last_modified"] for o in objs if o.get("last_modified")),
            default=None,
        )
        if newest is None or newest >= cutoff:
            continue  # まだ新しい (処理中かもしれない) → 次回
        for o in objs:
            _delete_if_still_stale(o["key"], cutoff)
            deleted += 1
        # in/ 側も掃除 (削除直前に再 HEAD して stale を再確認)。
        for o in r2.list_objects(_in_prefix(aid)):
            _delete_if_still_stale(o["key"], cutoff)
    if deleted:
        logger.info("offload orphan 掃除: %d オブジェクト削除試行", deleted)
    return deleted


def _delete_if_still_stale(key: str, cutoff) -> None:
    """削除直前に再 HEAD し、list スナップショット後に上書き (再ディスパッチ) されていたら消さない。

    list→delete の TOCTOU で、掃除判定後に運用者の再正規化 + watcher claim が同キー
    (status.json 等) を新しく PUT した場合に、その新規オブジェクトを誤削除しないための再確認
    (レビュー #1)。冪等な best-effort。
    """
    with _suppress():
        meta = r2.head_object(key)
        if meta is None:
            return  # 既に無い
        lm = meta.get("last_modified")
        if lm is not None and lm >= cutoff:
            logger.info("offload orphan: %s が再更新済 → 削除スキップ", key)
            return
        r2.delete_object(key)
