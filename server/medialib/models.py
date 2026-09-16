# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
import uuid

from django.conf import settings
from django.db import models


class AssetKind(models.TextChoices):
    PROGRAM = "program", "番組"
    CM = "cm", "CM"
    FILLER = "filler", "フィラー"
    BUMPER = "bumper", "バンパー"
    SLATE = "slate", "スレート"


class NormalizeStatus(models.TextChoices):
    PENDING = "pending", "待機"
    PROCESSING = "processing", "正規化中"
    READY = "ready", "送出可"
    FAILED = "failed", "失敗"


class AssetUploadStatus(models.TextChoices):
    PENDING = "pending", "署名待ち"
    UPLOADING = "uploading", "アップロード中"
    VERIFYING = "verifying", "検証中"
    COMPLETED = "completed", "完了"
    ABORTED = "aborted", "中断"
    EXPIRED = "expired", "期限切れ"
    FAILED = "failed", "失敗"


class CaptionStatus(models.TextChoices):
    """字幕(WebVTT)自動生成の状態 (#ADMIN-04 / PLAYER-04・決定#24)。既定 NONE=未生成。"""

    NONE = "none", "未生成"
    PROCESSING = "processing", "生成中"
    READY = "ready", "生成済"
    FAILED = "failed", "失敗"


class ScreeningStatus(models.TextChoices):
    """表現考査 (素材属性。#6 S8)。業態考査は sales.advertiser 側。"""

    PENDING = "pending", "未考査"
    APPROVED = "approved", "考査OK"
    REJECTED = "rejected", "考査NG"


class CmGrid(models.TextChoices):
    G15 = "15s", "15秒"
    G20 = "20s", "20秒"


class Asset(models.Model):
    """正規化済みメディア(統一テーブル。kind で番組/CM/フィラー/バンパー/スレートを区別)。"""

    kind = models.CharField(max_length=16, choices=AssetKind.choices)
    title = models.CharField(max_length=300)
    # 固定サムネ画像 URL (任意。#7 Phase 2 ①。自動抽出はせず手動指定 / 未設定は既定プレースホルダ)
    thumbnail_url = models.CharField(max_length=500, blank=True, null=True)
    r2_key = models.CharField(max_length=500, blank=True, null=True)
    source_path = models.CharField(max_length=500, blank=True, null=True)
    checksum = models.CharField(max_length=128, blank=True, null=True)
    duration_ms = models.BigIntegerField(null=True, blank=True)  # フレーム精度の尺
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    fps = models.DecimalField(max_digits=6, decimal_places=3, null=True, blank=True)
    vcodec = models.CharField(max_length=64, blank=True, null=True)
    acodec = models.CharField(max_length=64, blank=True, null=True)
    normalize_status = models.CharField(
        max_length=16,
        choices=NormalizeStatus.choices,
        default=NormalizeStatus.PENDING,
    )
    normalize_error = models.TextField(blank=True, null=True)
    # processing へ遷移した時刻。stale 滞留の回収 (reconcile_stale_normalize) と監視に使う。
    normalize_started_at = models.DateTimeField(null=True, blank=True)
    # Windows オフロード中の requestId (null=オフロードしていない)。reconcile が result.json の
    # requestId と CAS 照合して finalize/fallback を確定する (docs/normalize-offload.md §3)。
    offload_request_id = models.CharField(max_length=36, blank=True, null=True)
    offload_dispatched_at = models.DateTimeField(null=True, blank=True)
    # 長尺で映像の再エンコードを諦め、原本(映像コピー+音声loudnorm)で素材化したフラグ。
    # status は READY だが mezzanine 規格(1080p60)ではないため UI に原本バッジを出す。
    passthrough = models.BooleanField(default=False)
    # 役割フラグ: kind は主分類(1値)のまま、1素材を複数用途で使えるようにする (「フィラーかつ番組」)。
    # 番組編成/フィラープレイリストの素材ピッカーは kind ではなくこのフラグで候補を絞る。
    # 既定は kind から導出 (新規作成時に save() で設定)・編集画面のチェックボックスで個別に上書きする。
    usable_as_program = models.BooleanField(default=False)
    usable_as_filler = models.BooleanField(default=False)
    # フィラーとして送出されたとき、視聴者向け面 (現在放送中カード / 番組表) に素材タイトルを
    # 「再放送」として露出してよいか。既定 False = 従来どおり露出しない (局ID/プロモ等の混在対策)。
    # kind からは導出せず、オペレータが再放送クリップだけを明示的に立てる (save() で自動設定しない)。
    rerun_eligible = models.BooleanField(default=False)
    # 字幕(WebVTT)の自動生成状態とサイドカー保存先 (#ADMIN-04 / PLAYER-04・決定#24)。
    # 正規化 READY 後、kind=program の素材だけ captions キューで faster-whisper→VTT を R2 保存する。
    # 既定 none = 未生成 (既存行/既存挙動は不変)。VOD プレイヤーが同一オリジン配信の <track> で消費。
    caption_status = models.CharField(
        max_length=16,
        choices=CaptionStatus.choices,
        default=CaptionStatus.NONE,
    )
    caption_r2_key = models.CharField(max_length=500, blank=True, null=True)
    caption_lang = models.CharField(max_length=8, default="ja")
    caption_error = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "asset"
        indexes = [
            models.Index(
                fields=["kind"],
                condition=models.Q(normalize_status="ready"),
                name="idx_asset_kind_ready",
            ),
        ]

    def __str__(self):
        return f"[{self.kind}] {self.title}"

    def save(self, *args, **kwargs):
        # 新規作成時のみ役割フラグを kind から導出 (番組→番組可 / 番組・CM 以外→フィラー可)。
        # 既存行は migration で backfill 済。編集画面はチェックボックス値で保存する (adding=False) ため
        # 上書きを尊重する。kind の事後変更でフラグを追従させたい場合は編集画面で明示する。
        if self._state.adding:
            self.usable_as_program = self.kind == AssetKind.PROGRAM
            self.usable_as_filler = self.kind not in (AssetKind.PROGRAM, AssetKind.CM)
        super().save(*args, **kwargs)

    @property
    def has_caption(self) -> bool:
        """再生に使える字幕 VTT があるか (READY かつ保存済)。"""
        return self.caption_status == CaptionStatus.READY and bool(self.caption_r2_key)

    @property
    def duration_display(self) -> str:
        """尺を人間可読 (h:mm:ss / m:ss) に。未取得は「—」。"""
        if self.duration_ms is None:
            return "—"
        total = self.duration_ms // 1000
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class AssetUpload(models.Model):
    """未完成objectの所有権と検証をAssetから分離したstaff upload session。"""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="asset_uploads",
    )
    status = models.CharField(
        max_length=16,
        choices=AssetUploadStatus.choices,
        default=AssetUploadStatus.PENDING,
    )
    kind = models.CharField(max_length=16, choices=AssetKind.choices)
    title = models.CharField(max_length=300)
    original_filename = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    expected_size_bytes = models.BigIntegerField()
    expected_sha256 = models.CharField(max_length=64)
    observed_size_bytes = models.BigIntegerField(null=True, blank=True)
    verified_sha256 = models.CharField(max_length=64, blank=True, null=True)
    staging_key = models.CharField(max_length=500, unique=True)
    canonical_key = models.CharField(max_length=500, unique=True, blank=True, null=True)
    multipart_upload_id = models.CharField(max_length=512, blank=True, null=True)
    object_etag_or_version = models.CharField(max_length=512, blank=True, null=True)
    idempotency_key_hash = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=64)
    # D016: 前回の期限延長判定時にproviderが報告していた完了パート数。再署名(start_upload)の
    # たびにこれと比べ、増えていれば「実際に進捗した」とみなしexpires_atを延ばす。同じ値のままなら
    # 延長しない (放棄されたsessionが延長だけ繰り返す経路を作らない)。
    last_progress_part_count = models.PositiveIntegerField(default=0)
    asset = models.OneToOneField(
        Asset,
        on_delete=models.PROTECT,
        related_name="upload_session",
        blank=True,
        null=True,
    )
    expires_at = models.DateTimeField()
    verify_started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    aborted_at = models.DateTimeField(blank=True, null=True)
    cleanup_after = models.DateTimeField(blank=True, null=True)
    object_deleted_at = models.DateTimeField(blank=True, null=True)
    cleanup_attempts = models.PositiveIntegerField(default=0)
    last_error_code = models.CharField(max_length=64, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "asset_upload"
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "idempotency_key_hash"],
                name="uq_asset_upload_owner_idem",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status=AssetUploadStatus.COMPLETED)
                    | (
                        models.Q(asset__isnull=False)
                        & models.Q(canonical_key__isnull=False)
                        & models.Q(observed_size_bytes__isnull=False)
                        & models.Q(verified_sha256__isnull=False)
                    )
                ),
                name="ck_upload_completed_fields",
            ),
            models.CheckConstraint(
                condition=models.Q(status=AssetUploadStatus.COMPLETED)
                | models.Q(asset__isnull=True),
                name="ck_upload_asset_only_complete",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="idx_upload_status_expiry"),
            models.Index(fields=["status", "cleanup_after"], name="idx_upload_status_cleanup"),
        ]


class CmCreative(models.Model):
    """CM 固有属性 (asset kind='cm' に 1:1)。"""

    asset = models.OneToOneField(
        Asset,
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="cm",
    )
    advertiser = models.CharField(max_length=200)
    grid = models.CharField(max_length=8, choices=CmGrid.choices)
    campaign_start = models.DateField(null=True, blank=True)
    campaign_end = models.DateField(null=True, blank=True)
    max_airings = models.IntegerField(null=True, blank=True)  # NULL=無制限
    aired_count = models.IntegerField(default=0)
    # 表現考査 (#6 S8)。既存行は data migration で approved にバックフィル (みなし考査済)。
    screening_status = models.CharField(
        max_length=10,
        choices=ScreeningStatus.choices,
        default=ScreeningStatus.PENDING,
    )

    class Meta:
        db_table = "cm_creative"

    def __str__(self):
        return f"CM {self.advertiser} ({self.grid})"


class CmBundle(models.Model):
    """生番組用に複数 CM を束ねたリール。"""

    name = models.CharField(max_length=200)
    note = models.TextField(blank=True, null=True)

    class Meta:
        db_table = "cm_bundle"

    def __str__(self):
        return self.name


class CmBundleItem(models.Model):
    cm_bundle = models.ForeignKey(CmBundle, on_delete=models.CASCADE, related_name="items")
    seq = models.IntegerField()
    cm_asset = models.ForeignKey(CmCreative, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "cm_bundle_item"
        constraints = [
            models.UniqueConstraint(fields=["cm_bundle", "seq"], name="uq_cm_bundle_item_seq"),
        ]
        ordering = ["cm_bundle", "seq"]


class CueKind(models.TextChoices):
    CONTENT = "content", "本編"
    AD_BREAK = "ad_break", "CM枠"


class CueSheet(models.Model):
    """録画素材の内部ランダウン (本編 ⊕ CM枠)。素材ごとに 1 枚。

    編成表に番組を配置するとき、この原本から Program.ad_break を展開する (テンプレ適用)。
    本編セグメントの累積尺が CM枠の挿入位置 (offset_ms) を与える。展開後は番組ごとに微調整可。
    不変条件: Σ(content.duration_ms) == asset.duration_ms (本編で素材尺を過不足なく分割)。
    """

    asset = models.OneToOneField(Asset, on_delete=models.CASCADE, related_name="cue_sheet")
    note = models.TextField(blank=True, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "cue_sheet"

    def __str__(self):
        return f"cuesheet({self.asset_id})"


class CuePoint(models.Model):
    """キューシートの 1 行 (順序付き)。本編セグメント or CM枠。"""

    cue_sheet = models.ForeignKey(CueSheet, on_delete=models.CASCADE, related_name="points")
    seq = models.IntegerField()
    kind = models.CharField(max_length=16, choices=CueKind.choices)
    duration_ms = models.BigIntegerField()  # content=本編尺 / ad_break=CM枠尺(grid 整数倍)
    grid = models.CharField(max_length=8, choices=CmGrid.choices, blank=True, null=True)  # CM枠のみ
    label = models.CharField(max_length=120, blank=True, null=True)

    class Meta:
        db_table = "cue_point"
        constraints = [
            models.UniqueConstraint(fields=["cue_sheet", "seq"], name="uq_cue_point_seq"),
        ]
        ordering = ["cue_sheet", "seq"]


class FillerPlaylist(models.Model):
    """隙間充填用の既定ループ(局ID/次番予告/提供)。"""

    name = models.CharField(max_length=200)

    class Meta:
        db_table = "filler_playlist"

    def __str__(self):
        return self.name


class FillerItem(models.Model):
    filler_playlist = models.ForeignKey(
        FillerPlaylist,
        on_delete=models.CASCADE,
        related_name="items",
    )
    seq = models.IntegerField()
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="+")

    class Meta:
        db_table = "filler_item"
        constraints = [
            models.UniqueConstraint(fields=["filler_playlist", "seq"], name="uq_filler_item_seq"),
        ]
        ordering = ["filler_playlist", "seq"]
