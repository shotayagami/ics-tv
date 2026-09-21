# SPDX-FileCopyrightText: 2026 アイシーエス
# SPDX-License-Identifier: AGPL-3.0-or-later
"""API 共有スキーマ。ninja.Schema = Pydantic。OpenAPI に型として出力され、
フロントの TS クライアント生成元になる。"""

from ninja import Schema


class HealthOut(Schema):
    status: str
    service: str
    version: str


class ErrorOut(Schema):
    detail: str


class OkOut(Schema):
    ok: bool


# --- 公開プレイヤー島 (#Phase1) ---


class ChannelTab(Schema):
    slug: str
    name: str
    short: str
    tint: str
    live: bool
    pinned: bool


class ProgramNow(Schema):
    id: int
    title: str
    start_ts: int  # epoch 秒 (進行バー/カウントダウンをクライアントが毎秒算出)
    end_ts: int
    genre: str
    is_rerun: bool = False  # 再放送フィラー (編成外だが rerun_eligible 素材を「再放送」露出)
    # 番組がシリーズに紐付いていればその公開URL (#27 Phase B)。ファンクラブ限定ゲートの
    # 遷移先に使う。番組詳細ページ (/program/<id>/) は public_visible のみでゲート対象の
    # あらすじ/出演者/サムネを晒すため、ゲート済みの Series カード側へ誘導する。
    series_url: str | None = None


class ScheduleItem(Schema):
    id: int
    time: str
    title: str
    genre: str
    color: str
    is_now: bool
    is_rerun: bool = False  # フィラー由来の再放送行 (番組詳細リンク無し)
    row_bg: str
    time_color: str
    title_color: str


class ChannelDetail(Schema):
    slug: str
    name: str
    short: str
    tint: str
    live: bool
    # 意図的な放送休止中 (broadcast_windows 窓外)。公開プレイヤーはこのとき HLS を張らず休止表示。
    paused: bool = False
    # 休止中の「次回 ◯時 放送再開」ラベル (JST)。放送中/未設定は None。
    next_on_air: str | None = None
    is_pinned: bool
    hls_url: str | None = None
    # exposure_policy のサイト会員限定 / ファンクラブ ティア限定 (#27 Phase B) のゲート理由
    # ('' | 'login' | 'subscribe' | 'fc_join' | 'fc_unavailable')。hls_url が None のとき paused
    # 以外の理由で非表示なら埋まる (scheduling.vod.gate_reason / fanclub.services.fc_gate_reason と同語彙)。
    gate_reason: str = ""
    youtube_broadcast_id: str | None = None
    current: ProgramNow | None = None
    upcoming: ProgramNow | None = None
    day_list: list[ScheduleItem]


class CommentItem(Schema):
    id: int
    member_id: int
    nickname: str
    badge: bool
    created_ts: int
    time: str
    program_title: str
    body: str


class CommentListOut(Schema):
    count: int
    can_post: bool
    gate: str  # "ok" | "verify" | "login"
    nickname: str
    me_member_id: int | None = None
    items: list[CommentItem]


class CommentCreateIn(Schema):
    body: str


class PinOut(Schema):
    pinned: bool


# --- 番組表グリッド (#Phase2 guide 島) ---


class GuideBlock(Schema):
    id: int
    title: str
    genre: str
    time: str
    color: str
    is_live: bool
    time_color: str
    title_color: str
    top: float
    height: float
    # 当日窓クランプ済みの生の分オフセット/尺。client ズームが任意 px/分で top/height を再計算する。
    start_min: float = 0.0
    dur_min: float = 0.0
    bg: str
    border: str
    shadow: str
    is_rerun: bool = False  # フィラー由来の再放送ブロック (番組詳細リンク無し)


class GuideCol(Schema):
    slug: str
    name: str
    short: str
    tint: str
    blocks: list[GuideBlock]


class GuideHour(Schema):
    label: str
    top: int


class GuideOut(Schema):
    base_date: str
    label: str
    is_today: bool
    prev_date: str
    next_date: str
    today: str
    height: float
    px_per_min: float = 2.2  # client ズームの初期密度 / リセット先 (core.epg.PX_PER_MIN)
    now_top: float | None = None
    now_min: float | None = None  # client がズームで now ライン/自動スクロールを再計算
    now_label: str
    show_now: bool
    hours: list[GuideHour]
    cols: list[GuideCol]


# --- 公開トップ ライブカード (#Phase2b home 島。ヒーロー + チャンネルカード) ---


class HomeCard(Schema):
    slug: str
    name: str
    short: str
    tint: str
    online: bool
    live: bool
    nowtitle: str
    is_rerun: bool = False  # 再放送フィラー (編成外だが rerun_eligible 素材を「再放送」露出)
    genre: str
    genre_color: str
    time_range: str
    cur_start_ts: int | None = None
    cur_end_ts: int | None = None
    next_title: str
    next_time: str
    poster: str
    # exposure_policy/ファンクラブ ティア軸でゲートされている (または channel 未設定) なら空文字。
    # HLS 未設定と区別しないが、素通しの再生 URL を渡さないことが本フィールドの目的。
    hls_url: str
    # ゲート理由 ('' | 'login' | 'subscribe' | 'fc_join' | 'fc_unavailable')。ChannelDetail と同語彙 (#27 Phase B)。
    gate_reason: str = ""


class HomeOut(Schema):
    featured: HomeCard | None = None
    cards: list[HomeCard]


# --- ディスカバリ (#Phase2c search / browse / vod 島) ---


class SearchRow(Schema):
    id: int
    title: str
    channel_name: str
    genre: str
    start_display: str


class SearchChannel(Schema):
    slug: str
    name: str
    tint: str


class SearchOut(Schema):
    q: str
    programs: list[SearchRow]
    channels: list[SearchChannel]


class CardItem(Schema):
    id: int
    title: str
    channel_name: str
    genre: str
    start_display: str
    thumb_url: str
    vod_visibility: str = ""  # "" | "members" | "subscribers"
    can_watch: bool = True
    duration: str = ""  # asset.duration_display
    badge: str = ""  # "" | "yt"
    series_id: int | None = None  # セットされている場合は /series/<series_id>/ へリンク


class BrowseOut(Schema):
    genres: list[str]
    genre: str
    programs: list[CardItem]


class VodOut(Schema):
    items: list[CardItem]
    live_archives: list[CardItem]
    featured: CardItem | None = None  # 見逃しトップの featured Hero(編集選択・無ければ None)


# --- シリーズ詳細 公開ページ ---


class SeriesEpisodeItem(Schema):
    id: int
    title: str
    start_display: str  # "n/j(D) H:MM"
    state: str  # "live" | "upcoming" | "aired"
    thumb_url: str
    vod_url: str  # "/vod/<id>/" or ""
    program_url: str  # "/program/<id>/"
    duration: str  # "" or "m分"


class SeriesPostItem(Schema):
    id: int
    kind: str  # "article" | "campaign"
    title: str
    published_at: str  # "n/j(D)" or ""
    campaign_end: str  # "n/j(D) H:MM" or ""
    # #27 ファンクラブ限定ゲート。一覧からは隠さず「ロック表示」する方針
    # (タイトルは常に見せて発見性/コンバージョン導線を保つ、body は元々含まれていないため漏えいなし)。
    locked: bool = False
    gate_reason: str = ""  # "" | "login" | "fc_join" | "fc_unavailable"


class AudienceFieldDef(Schema):
    key: str
    label: str
    type: str  # text|textarea|email|tel|select|date|checkbox
    required: bool = False
    options: list[str] = []
    help: str = ""


class AudienceFormPublic(Schema):
    id: int
    kind: str  # "message" | "campaign"
    title: str
    description: str
    requires_login: bool
    fields: list[AudienceFieldDef]
    success_message: str
    # campaign のみ
    prize: str = ""
    ends_display: str = ""  # "n/j(D) H:MM" or ""


class SeriesDetailOut(Schema):
    id: int
    title: str
    slug: str
    description: str
    genre: str
    cast: str
    thumbnail_url: str
    channel_name: str
    public_url: str
    x_handle_display: str
    x_url: str
    x_hashtag_display: str
    hashtag_url: str
    episodes: list[SeriesEpisodeItem]
    posts: list[SeriesPostItem]
    forms: list[AudienceFormPublic]


class AudienceSubmitIn(Schema):
    payload: dict


class AudienceSubmitOut(Schema):
    ok: bool
    gate: str = ""  # "login" | "verify" | "" (空=ok)
    message: str = ""


# studio admin: AudienceForm CRUD ---


class AudienceFormFieldIn(Schema):
    key: str
    label: str
    type: str
    required: bool = False
    options: list[str] = []
    help: str = ""


class AudienceFormIn(Schema):
    kind: str = "message"
    title: str
    description: str = ""
    enabled: bool = False
    requires_login: bool = False
    fields: list[AudienceFormFieldIn] = []
    success_message: str = "送信しました。ありがとうございました。"
    notify_email: str = ""
    starts_at: str = ""  # ISO8601 or ""
    ends_at: str = ""
    prize: str = ""


class AudienceFormAdminOut(Schema):
    id: int
    kind: str
    title: str
    description: str
    enabled: bool
    requires_login: bool
    fields: list[AudienceFieldDef]
    success_message: str
    notify_email: str
    starts_at: str
    ends_at: str
    prize: str
    submission_count: int
    new_count: int


class AudienceSubmissionAdminOut(Schema):
    id: int
    form_id: int
    form_title: str
    member_id: int | None
    submitter_name: str
    submitter_email: str
    status: str
    payload: dict
    created_at: str
    deleted: bool


# --- 番組詳細 アクションバー (#Phase2c program 島) ---


class ProgramCta(Schema):
    kind: str  # "live" | "vod" | "yt" | "none"
    url: str = ""
    label: str = ""
    external: bool = False  # yt は別タブ


class ProgramDetailOut(Schema):
    """番組詳細ページの動的アクション部 (CTA + お気に入り/リマインド/共有)。

    本文 (タイトル/あらすじ/出演者/今後の放送) は SEO のため SSR のまま。島はこの
    JSON だけで操作バーを描画する。お気に入り/リマインドのトグルは
    POST /program/{id}/{favorite,remind} (member_any_auth)。
    """

    id: int
    state: str  # "live" | "aired" | "upcoming"
    cta: ProgramCta
    is_member: bool
    is_favorited: bool
    is_reminded: bool
    can_remind: bool  # member かつ verified (リマインドは要認証)
    start_pill: str  # upcoming の "n/j(D) H:i 放送予定"
    share_url: str
    share_title: str
    # #MOBILE-02: series が有効な Creator に紐付くときだけ slug (ファンクラブ導線)。無ければ ""
    fc_creator_slug: str = ""


class FavoriteOut(Schema):
    favorited: bool


class ReminderOut(Schema):
    reminded: bool


class MediaUploadStartIn(Schema):
    kind: str
    title: str
    original_filename: str
    content_type: str
    expected_size_bytes: int
    expected_sha256: str


class MediaUploadPartIn(Schema):
    part_number: int
    etag: str


class MediaUploadCompleteIn(Schema):
    parts: list[MediaUploadPartIn]


class MediaUploadPartOut(Schema):
    part_number: int
    url: str


class MediaUploadOut(Schema):
    upload_uuid: str
    status: str
    part_size_bytes: int
    parts: list[MediaUploadPartOut]
    expires_at: str
    asset_id: int | None = None
    error_code: str = ""


# --- studio 管理 SPA (#Phase2d) ---


class StatRow(Schema):
    label: str
    count: int


class MemberStatsOut(Schema):
    total: int
    verified: int
    unverified: int
    gender: list[StatRow]
    tfa: list[StatRow]
    age: list[StatRow]
    country: list[StatRow]  # 居住国 (表示名) → 件数
    region: list[StatRow]  # 郵便番号 上3桁 → 件数 (上位)。**日本在住のみが母数**
    region_overseas_excluded: int  # region から外した海外在住の件数 (母数のズレを見せる)


class AccessStatsOut(Schema):
    """HTTP アクセス統計 (awstats 的な画面。studio/backoffice 共通)。"""

    host: str
    hosts: list[str]  # 選択可能なホスト一覧 (直近に記録があるもの)
    range_hours: int
    total_hits: int
    total_pages: int
    hourly: list[StatRow]
    top_paths: list[StatRow]
    status: list[StatRow]


class ExpiringRight(Schema):
    program_id: int
    program_title: str
    channel: str
    holder: str
    available_until: str  # "Y/n/j" 表示


class RightsDashboardOut(Schema):
    expiring: list[ExpiringRight]


# --- 共有: id と表示名の組 (各画面のドロップダウン) ---


class IdName(Schema):
    id: int
    name: str


# --- studio 管理 SPA: 請求 billing (#Phase2d-2 状態遷移) ---


class BillingPeriodRow(Schema):
    year: int
    month: int
    closed_at: str  # "" = 未締め, else "Y-m-d H:i"


class InvoiceRow(Schema):
    id: int
    invoice_number: str
    period: str  # "2026-06"
    advertiser: str
    subtotal: int
    commission_amount: int
    tax_amount: int
    total: int
    status: str  # draft | issued | paid | void


class BillingOut(Schema):
    periods: list[BillingPeriodRow]
    invoices: list[InvoiceRow]


class CloseMonthIn(Schema):
    year: int
    month: int
    force: bool = False


class CloseMonthOut(Schema):
    closed: bool
    warnings: list[str] = []
    message: str = ""


class InvoicePayIn(Schema):
    paid_amount: int
    method: str = ""


# --- studio 管理 SPA: 編成タイムライン (#Phase2d-3) ---


class AdBreakGeo(Schema):
    id: int
    offset_ms: int
    grid: str
    duration_ms: int


class TimelineProgram(Schema):
    id: int
    type: str  # recorded | live
    title: str
    start_at: str  # iso
    end_at: str  # iso
    public_visible: bool
    source: str
    thumb: str = ""
    asset_duration_ms: int | None = None
    has_cuesheet: bool = False
    breaks: list[AdBreakGeo] = []


class SchedChannel(Schema):
    slug: str
    name: str


class TimelineOut(Schema):
    channel: SchedChannel
    now: str  # iso (グリッド原点)
    horizon: str  # iso (now+24h)
    programs: list[TimelineProgram]
    channels: list[SchedChannel]  # ch 切替タブ


# --- studio 管理 SPA: 週間グリッド編成 (曜日×タイムライン D&D) ---


class WeekProgram(TimelineProgram):
    """週グリッドの実 Program。TimelineProgram のジオメトリ + 曜日列 (localtime 基準)。"""

    dow: int  # 0=月..6=日


class WeekSlotOccurrence(Schema):
    """週間基本編成 SeriesSlot を当週の各日付へ投影した1件 (下地レイヤ)。"""

    slot_id: int
    series_id: int
    series_title: str
    dow: int  # 列 0..6
    date: str  # "YYYY-MM-DD" (投影先の具体日)
    start_time: str  # "HH:MM"
    duration_ms: int
    program_type: str  # recorded | live
    source: str  # default_asset.title or live_source.name
    recurrence_kind: str
    recurrence_label: str
    covered: bool  # 同 series の実 Program が同日同時刻を既に占有 (二重表示防止)


class WeekOut(Schema):
    channel: SchedChannel
    week_start: str  # "YYYY-MM-DD" 月曜 (local)
    days: list[str]  # 7 日 月..日 ("YYYY-MM-DD")
    programs: list[WeekProgram]  # 当週の実 Program (差分レイヤ)
    slots: list[WeekSlotOccurrence]  # 基本編成スロットの投影 (下地レイヤ)
    channels: list[SchedChannel]  # ch 切替タブ


class SeriesCreateIn(Schema):
    title: str


class SeriesCreated(Schema):
    id: int
    title: str


# --- studio 管理 SPA: チャンネル管理 (#Phase2d-5) ---


class AdminChannel(Schema):
    id: int
    slug: str
    name: str
    short: str  # 略称 (raw, 未設定は "")
    tint: str  # 識別色 (raw, 未設定は "")
    tint_color: str  # 解決済み (property・パレット自動割当含む)
    enabled: bool
    settings_url: str  # 既存の詳細設定 (YouTube/CF/メディア割当) へのリンク


class ChannelUpdateIn(Schema):
    name: str
    slug: str
    short: str = ""
    tint: str = ""
    enabled: bool = True


# --- studio 管理 SPA: チャンネル詳細設定 (#Phase2e-4) ---


class ChannelYoutube(Schema):
    connected: bool  # refresh_token あり (書き込み可)
    oauth_configured: bool  # ICSTV_OAUTH_CLIENT_ID/SECRET 設定済 (connect 可能)
    has_config: bool  # YoutubeConfig (枠テンプレ) あり
    connect_url: str  # サーバ描画 OAuth 開始 (SPA からは <a> でのみ・JWT 化しない)


class ChannelCloudflare(Schema):
    live_input_id: str  # "" = 未作成
    playback_hls_url: str  # 視聴者向け HLS (空可)
    set_playback_url: str  # playback URL 保存 (form-POST)


class ChimeLibraryItem(Schema):
    id: int
    name: str  # 表示ラベル
    filename: str  # アップロード元ファイル名
    preview_url: str  # 試聴 (302 → presigned)
    created_at: str  # ISO8601


class ChannelChimeRow(Schema):
    category: str  # eew | weather | general
    label: str  # 表示名 (EEW (緊急) 等)
    selected_sound_id: int | None = None  # ライブラリ選択 (None = 既定フォールバック)


class ClockStyleOut(Schema):
    """時計エディタ設定 (スタイル + 表示設定 + 時間帯)。"""

    font_family: str
    time_size: int
    font_weight: int
    time_color: str
    date_color: str
    text_effect: str
    stroke_width: str
    stroke_color: str
    bg_preset: str
    bg_opacity: float
    box_shadow: str
    border_radius: str
    entrance_anim: str
    show_seconds: bool
    show_date: bool
    position: str
    clock_overlay_enabled: bool
    # [{"start":"HH:MM","end":"HH:MM","style":{...}|null}] の生 JSON
    clock_windows: list
    save_url: str  # /admin-ui/ch/<slug>/clock-style/


class ClockPresetOut(Schema):
    """時計スタイルプリセット (一覧・詳細)。"""

    id: int
    name: str
    style: dict
    created_at: str
    updated_at: str


class ClockPresetIn(Schema):
    """時計スタイルプリセット登録/更新。"""

    name: str
    style: dict


class ChannelSettingsOut(Schema):
    slug: str
    name: str
    youtube: ChannelYoutube
    cloudflare: ChannelCloudflare
    default_filler_id: int | None = None
    slate_asset_id: int | None = None
    # exposure_policy (#27) の案内フィラー2種。候補は slate_options と同一クエリ (非番組/非CM・正規化済)。
    site_only_filler_id: int | None = None
    members_filler_id: int | None = None
    filler_options: list[IdName]  # FillerPlaylist
    slate_options: list[IdName]  # 正規化済の非番組/非CM 素材 (name=title)
    media_post_url: str  # 既定フィラー/スレート/exposure_policy 案内素材2種 保存 (form-POST)
    chime_library: list[ChimeLibraryItem]  # 速報チャイム音源ライブラリ (局共通)
    chimes: list[ChannelChimeRow]  # カテゴリ別の選択 (eew/weather/general・per-channel)
    chime_sound_post_url: str  # ライブラリ音源アップロード (multipart POST: name + file)
    chime_select_url: str  # カテゴリ選択保存 (form-POST: category + sound)
    advanced_url: str  # 旧詳細画面 (CF/YT 作成等の重い外部API操作)


# --- studio 管理 SPA: 生入力 LiveSource (OBS ingest 接続情報の閲覧) ---


class LiveSourceRow(Schema):
    id: int
    name: str
    mediamtx_path: str  # "<rtmp_app>/<rtmp_key>"
    srt_latency_ms: int
    has_passphrase: bool  # passphrase 設定の有無 (実値は srt_url に含む)
    srt_url: str  # 現場 OBS へ渡す SRT push URL (host 込み・passphrase 込み)
    rtmp_url: str  # LAN/WG 内サブ卓向け RTMP push URL
    note: str = ""


class LiveSourcesOut(Schema):
    ingest_host: str  # 送出ノードの private IP/host (未設定はプレースホルダ)
    host_configured: bool  # ICSTV_INGEST_NODE_HOST 設定済か (false=URL は雛形のまま)
    admin_url: str  # 作成/編集は Django admin に据え置き (CRUD は据え置き方針)
    sources: list[LiveSourceRow]


# --- studio 管理 SPA: 素材ライブラリ medialib (#Phase2d-6) ---


class MlAsset(Schema):
    id: int
    title: str
    kind: str
    duration_display: str
    thumbnail_url: str
    normalize_status: str  # pending | processing | ready | failed
    has_cuesheet: bool
    is_program: bool
    edit_url: str
    cuesheet_url: str
    group_key: str  # グループ識別子 (series_{id} | cm_{advertiser} | ch_{slug} | other)
    group_label: str  # 表示ラベル


class MlCm(Schema):
    asset_id: int
    advertiser: str
    grid: str  # 表示名
    campaign: str  # "y/m/d–y/m/d" or "—"
    aired: str  # "n/max" or "n/∞"
    stock_label: str
    stock_css: str  # warn | live | ok | ""
    screening_status: str  # approved | rejected | pending
    edit_url: str


class MlNamed(Schema):
    id: int
    name: str
    count: int
    edit_url: str


class MlFilter(Schema):
    value: str
    label: str


class MedialibOut(Schema):
    assets: list[MlAsset]
    cms: list[MlCm]
    bundles: list[MlNamed]
    fillers: list[MlNamed]
    kinds: list[MlFilter]
    statuses: list[MlFilter]
    sel_kind: str
    sel_status: str
    n_screening_pending: int


# --- studio 管理 SPA: キューシート エディタ (#Phase2d-10) ---


class CuePointRow(Schema):
    id: int
    kind: str  # content | ad_break
    kind_label: str  # 本編 | CM枠
    duration: str  # "m:ss.mmm"
    offset: str  # ad_break の素材内オフセット "m:ss" (content は "")
    grid: str  # ad_break の grid or ""


class CueSheetOut(Schema):
    asset_id: int
    asset_title: str
    asset_duration: str
    points: list[CuePointRow]
    content_total: str
    remaining_ms: int  # 残り本編尺 (素材尺 − 本編合計)
    remaining_disp: str
    error: str  # 検証エラー or ""


# --- studio 管理 SPA: 生キューシート エディタ (LiveRundown/LiveCue、タイムキープ Phase 1) ---


class LiveCueRow(Schema):
    id: int
    seq: int
    kind: str
    kind_label: str
    label: str = ""
    planned_duration_ms: int
    planned_at: int
    cm_bundle_id: int | None = None
    cm_bundle_name: str = ""
    grid: str = ""
    asset_id: int | None = None
    asset_title: str = ""
    state: str
    state_label: str
    auto_fire: bool
    auto_offset_ms: int | None = None
    auto_anchor: str = "start"  # start | wallclock (D1 §11)
    auto_wall_time: str = ""  # "HH:MM" (auto_anchor=wallclock 時)


class LiveRundownOut(Schema):
    program_id: int
    program_title: str
    channel: SchedChannel
    program_start_at: int
    program_end_at: int
    cues: list[LiveCueRow]
    planned_total_ms: int
    over_under_ms: int
    bundles: list[IdName]
    assets: list[IdName]


class RundownTemplateOut(Schema):
    """SeriesSlot の定番進行表(雛形)。LiveRundownOut と同じ行型 (LiveCueRow) を返し、
    studio の共有進行表エディタで program/template を同一 UI で扱えるようにする (#25 Phase3 C)。"""

    slot_id: int
    slot_label: str
    channel: SchedChannel
    slot_duration_ms: int  # 枠 (over_under 基準・LiveRundownOut の program_end-start に相当)
    cues: list[LiveCueRow]
    planned_total_ms: int
    over_under_ms: int
    bundles: list[IdName]
    assets: list[IdName]


# --- studio 管理 SPA: 週間編成 series (#Phase2d-7) ---


class SeriesSlotRow(Schema):
    id: int
    recurrence: str  # "毎週月" 等
    start_time: str  # "HH:MM"
    duration: str  # "60分"
    program_type: str  # recorded | live
    source: str


class SeriesRow(Schema):
    id: int
    title: str
    genre: str
    is_active: bool
    n_slots: int
    slug: str  # 空文字=スラッグ未設定
    edit_url: str  # 旧画面 (フォーム編集・スロット追加)
    slots: list[SeriesSlotRow]


class SeriesOut(Schema):
    channel: SchedChannel
    series: list[SeriesRow]
    new_url: str  # series 新規作成フォーム (旧画面・フォールバック)
    channels: list[SchedChannel]


# --- studio 管理 SPA: series 作成/編集フォーム + スロット + 回 (旧画面の SPA 化) ---


class Choice(Schema):
    """value/label の選択肢 (TextChoices 由来。空 value も可)。"""

    value: str
    label: str


class SeriesInitial(Schema):
    id: int | None = None
    title: str = ""
    slug: str = ""
    genre: str = ""
    rating: str = ""
    # 配信ポリシー既定値 (#27)。展開 Program の既定になる (genre/rating と同じ継承の流儀)。
    exposure_policy_default: str = "public"
    description: str = ""
    cast: str = ""
    thumbnail_url: str = ""
    x_handle: str = ""
    x_hashtag: str = ""
    is_active: bool = True
    clock_hidden: bool = False
    lbar_hidden: bool = False
    clock_style_override: dict | None = None
    youtube_dedicated: bool = False
    youtube_preset_id: int | None = None


class SeriesFormOut(Schema):
    channel: SchedChannel
    initial: SeriesInitial
    genre_choices: list[Choice]
    rating_choices: list[Choice]
    exposure_policy_choices: list[Choice]
    youtube_presets: list[IdName]  # ch 専用 + 全 ch 共通
    thumbnail_post_url: str = ""  # 編集時のみ (multipart アップロード先)
    delete_url: str = ""  # 編集時のみ (旧 delete エンドポイント)


class SeriesFullIn(Schema):
    title: str
    slug: str = ""
    genre: str = ""
    rating: str = ""
    exposure_policy_default: str = "public"
    description: str = ""
    cast: str = ""
    thumbnail_url: str = ""
    x_handle: str = ""
    x_hashtag: str = ""
    is_active: bool = True
    clock_hidden: bool = False
    lbar_hidden: bool = False
    clock_style_override: dict | None = None
    youtube_dedicated: bool = False
    youtube_preset_id: int | None = None


class SlotInitial(Schema):
    id: int | None = None
    dow: int = 0
    start_time: str = ""  # "HH:MM"
    duration_min: int = 30
    program_type: str = "recorded"
    default_asset_id: int | None = None
    live_source_id: int | None = None
    effective_from: str = ""  # "YYYY-MM-DD"
    effective_to: str = ""
    recurrence_kind: str = "weekly"
    weeks_csv: str = ""
    days_csv: str = ""
    ending_csv: str = ""


class SlotFormOut(Schema):
    initial: SlotInitial
    assets: list[IdName]  # default_asset 候補 (READY 録画素材)
    live_sources: list[IdName]
    dow_choices: list[Choice]
    recurrence_choices: list[Choice]


class ThumbnailOut(Schema):
    thumbnail_url: str  # 保存後の配信パス (/t/<key>)


class SlotIn(Schema):
    dow: int
    start_time: str  # "HH:MM"
    duration_min: int
    program_type: str  # recorded | live
    default_asset_id: int | None = None
    live_source_id: int | None = None
    effective_from: str  # "YYYY-MM-DD"
    effective_to: str = ""
    recurrence_kind: str = "weekly"
    weeks_csv: str = ""
    days_csv: str = ""
    ending_csv: str = ""


class EpisodeOut(Schema):
    id: int
    episode_no: int | None = None
    air_date: str = ""  # "YYYY-MM-DD" or 空
    title: str = ""
    status: str  # planned | confirmed | aired
    status_label: str
    asset_id: int | None = None
    asset_title: str = ""  # 納品で確定した本編素材 (読み取り専用)


class EpisodeIn(Schema):
    episode_no: int | None = None
    air_date: str = ""  # "YYYY-MM-DD" or 空
    title: str = ""
    status: str = "planned"  # asset は受け付けない (delivery seam が確定)


class YtPresetIn(Schema):
    """YouTube 配信プリセットのインライン作成 (シリーズ編集フォームから)。"""

    name: str
    title_template: str = "{channel} {date} {start}-{end}"
    description_template: str = ""
    privacy: str = "public"  # public | unlisted | private


class YtDescTemplateOut(Schema):
    id: int
    name: str
    title_template: str
    description_template: str


class YtDescTemplateIn(Schema):
    name: str
    title_template: str = "{program} | {channel}"
    description_template: str = ""


# --- studio 管理 SPA: YoutubeConfig (rolling 枠生成テンプレ・per-channel) ---


class YtRollingConfigOut(Schema):
    """rolling 枠を駆動する per-channel テンプレ/設定。exists=False なら未設定 (既定値を提示)。"""

    exists: bool
    title_template: str
    description_template: str
    privacy: str
    enable_monitor: bool
    rolling_hours: int
    slot_minutes: int
    nudge_lead_minutes: int
    nudge_template: str
    nudge_ended_template: str
    privacy_choices: list[str]


class YtRollingConfigIn(Schema):
    title_template: str = "{channel} {date} {start}-{end}"
    description_template: str = ""
    privacy: str = "public"
    enable_monitor: bool = False
    rolling_hours: int = 24
    slot_minutes: int = 240
    nudge_lead_minutes: int = 10
    nudge_template: str = ""
    nudge_ended_template: str = ""


class YtResyncOut(Schema):
    ok: bool
    stats: dict[str, int]


# --- studio 管理 SPA: 運用 ops ダッシュボード (#Phase2d-8) ---


class OpsLayer(Schema):
    layer: int | None = None
    role: str = ""
    content: str = ""


class OpsHealthOut(Schema):
    online: bool
    last_heartbeat: str
    caspar_health: str
    feed_state: str
    slate_active: bool
    auto_return: bool
    auto_return_suspended: bool
    queue_depth: int | None = None
    last_seq: int | None = None
    layers: list[OpsLayer]
    yt_slot_status: str  # 現在の YouTube スロット status


class OpsEventRow(Schema):
    time: str
    action: str
    status: str
    title: str


class OpsOnAirOut(Schema):
    program: str
    status: str
    action: str
    note: str
    time: str


class OpsNotif(Schema):
    id: int
    created: str
    severity: str
    kind: str
    message: str


class OpsChimeChoice(Schema):
    value: str  # "cat:<category>" (カテゴリ既定) | "snd:<id>" (ライブラリ直接)
    label: str  # 表示名 (カテゴリ既定: EEW … / ライブラリ: …)
    group: str  # "category" | "library" (UI のグルーピング用)


class OpsStatusOut(Schema):
    channel: SchedChannel
    channels: list[SchedChannel]
    chime_choices: list[OpsChimeChoice]  # 手動速報の発火時チャイム選択肢
    on_air: OpsOnAirOut | None = None
    live_program: str
    live_program_id: int | None = (
        None  # 押え/巻き (extend/shorten) 用の生番組 id (放送コンソール P1.3)
    )
    upcoming: list[OpsEventRow]
    health: OpsHealthOut
    asrun: list[OpsEventRow]
    notifications: list[OpsNotif]
    notifications_count: int
    bundles: list[IdName]  # cm-in 用
    analytics_url: str  # 視聴計測 (旧画面・同時接続/番組別視聴ランキング)


# --- タイムキーパー (docs/timekeeper-live.md Phase 0・#25) ---


class TkBroadcastOut(Schema):
    state: str  # "onair" | "pre" | "post"
    program_id: int | None = None
    program_type: str = ""  # "live" | "recorded" | ""
    title: str = ""
    start_at: int | None = None  # epoch 秒。枠は押え/巻きで動くので毎回読む
    end_at: int | None = None
    next_program_at: int | None = None
    next_on_air: int | None = None


class TkNowOut(Schema):
    kind: str  # "line" | "cm" | "filler" | "slate" | "none"
    label: str = ""
    segment_end_at: int | None = None


class TkNextOut(Schema):
    kind: str
    label: str = ""
    at: int | None = None


class TkCmRemainingOut(Schema):
    count: int
    seconds: int
    tracked: bool  # False=生番組でCM義務台帳が無い(Phase1で解消)。「残り0本」と区別する


class TkCueOut(Schema):
    id: int
    seq: int
    kind: str
    label: str = ""
    planned_at: int
    planned_dur: int
    state: str
    auto_fire: bool


class TkRundownOut(Schema):
    program_id: int
    cues: list[TkCueOut]
    planned_total_ms: int
    over_under_ms: int


class TimekeeperOut(Schema):
    server_now: int  # クライアント時計ズレ補正の基準
    broadcast: TkBroadcastOut
    now: TkNowOut
    next: TkNextOut
    cm_remaining: TkCmRemainingOut
    vt_remaining: TkCmRemainingOut
    next_cm_at: int | None = None
    next_section_at: int | None = None
    rundown: TkRundownOut | None = None
    health: OpsHealthOut  # 既存スキーマをそのまま再利用
    channel: SchedChannel
    channels: list[SchedChannel]
    bundles: list[IdName]
    live_program_id: int | None = None


# --- studio 管理 SPA: 営業 CM割付 sales (#Phase2d-9) ---


class AllocItem(Schema):
    item_id: int
    advertiser: str  # "" = 空き枠
    match: str  # placement match_kind or ""
    editable: bool


class AllocBreak(Schema):
    break_id: int  # AdBreak pk (item 追加/削除の対象)
    offset: str  # 番組内オフセット "mm:ss"
    grid: str  # CM枠 grid (入替候補の絞り込みキー)
    items: list[AllocItem]
    remaining: str  # 残り尺 "mm:ss" (この枠にあと入る量)
    full: bool  # 残り尺 < 1 grid = これ以上追加不可


class AllocProgram(Schema):
    title: str
    time: str  # 開始 "HH:MM"
    breaks: list[AllocBreak]


class CmOption(Schema):
    id: int  # cm_asset_id (swap の差替先)
    name: str  # advertiser
    grid: str  # 一致する枠のみ差替可 (op_swap_item で 400)


class AllocationOut(Schema):
    channels: list[IdName]
    channel_id: int | None = None
    channel_name: str
    day: str  # "YYYY-MM-DD"
    rows: list[AllocProgram]
    cm_options: list[CmOption]  # 入替候補 (考査OK の CM・id=cm_asset_id)
    edit_url: str  # 旧 allocation 画面 (band 線引き編集)


# --- studio 管理 SPA: YouTube スロット dashboard (#Phase2d-12) ---


class YtSlotRow(Schema):
    id: int
    window: str  # "m/d HH:MM–HH:MM"
    status: str  # created/ready/testing/live/complete/error
    broadcast_id: str
    title: str
    description: str
    manual: bool
    error: str


class SlotListOut(Schema):
    channel: SchedChannel
    channels: list[SchedChannel]
    slots: list[YtSlotRow]
    settings_url: str  # チャンネル設定 (YouTube OAuth/CF) 旧画面


# --- studio 管理 SPA: 配信プリセット CRUD (#Phase2e-YTpreset) ---


class PresetListItem(Schema):
    id: int
    name: str
    channel_name: str | None  # null = 全ch共通
    privacy: str
    latency: str
    category_id: int | None


class CategoryChoice(Schema):
    value: int | None
    label: str


class ChannelOption(Schema):
    id: int
    slug: str
    name: str


class PresetFormMeta(Schema):
    channels: list[ChannelOption]
    category_choices: list[CategoryChoice]


class ChecklistItemDef(Schema):
    key: str
    label: str
    default: bool


class PresetDetail(Schema):
    id: int
    name: str
    channel_id: int | None
    title_template: str
    description_template: str
    category_id: int | None
    tags: list[str]
    privacy: str
    made_for_kids: bool
    default_language: str
    default_audio_language: str
    latency: str
    enable_dvr: bool
    enable_embed: bool
    enable_auto_start: bool
    enable_auto_stop: bool
    record_from_start: bool
    license: str
    public_stats_viewable: bool
    thumbnail_id: int | None
    playlist_id: str
    manual_checklist: list[ChecklistItemDef]


class PresetIn(Schema):
    name: str
    channel_id: int | None = None
    title_template: str = ""
    description_template: str = ""
    category_id: int | None = None
    tags: list[str] = []
    privacy: str = "public"
    made_for_kids: bool = False
    default_language: str = ""
    default_audio_language: str = ""
    latency: str = "low"
    enable_dvr: bool = True
    enable_embed: bool = True
    enable_auto_start: bool = False
    enable_auto_stop: bool = False
    record_from_start: bool = True
    license: str = "youtube"
    public_stats_viewable: bool = True
    thumbnail_id: int | None = None
    playlist_id: str = ""
    manual_checklist: list[ChecklistItemDef] = []


# --- studio 管理 SPA: 番組専用枠ダッシュボード (#Phase2e-dedicated) ---


class DedicatedCandidate(Schema):
    program_id: int
    title: str
    start_at: str  # JST "YYYY-MM-DD HH:MM"
    preset_id: int
    preset_name: str


class ChecklistItemState(Schema):
    key: str
    label: str
    checked: bool


class DedicatedBroadcastRow(Schema):
    id: int
    program_id: int
    program_title: str
    start_at: str
    end_at: str
    status: str
    manual: bool
    broadcast_id: str
    watch_url: str
    preset_name: str
    checklist: list[ChecklistItemState]


class DedicatedDashboardOut(Schema):
    channel: SchedChannel
    channels: list[SchedChannel]
    candidates: list[DedicatedCandidate]
    broadcasts: list[DedicatedBroadcastRow]
    settings_url: str


class TransitionIn(Schema):
    action: str  # "go_live" | "complete"


class ChecklistStateIn(Schema):
    state: dict[str, bool]


# --- studio 管理 SPA: 自動グラフィック graphic_cues (#Phase2e-1) ---


class GraphicCueRow(Schema):
    id: int
    layer: int
    kind: str  # graphic | video | text
    summary: str
    show_s: float
    hide_s: float | None = None
    seq: int


class GraphicCuesOut(Schema):
    title: str
    subtitle: str
    owner: str  # series | program | filler
    owner_id: int
    cues: list[GraphicCueRow]
    back_url: str  # 旧 graphic_cues 画面 (画像/動画アップロード等のリッチ編集用)


# --- studio 管理 SPA: 番組 作成/編集フォーム (#Phase2e-2) ---


class ProgramInitial(Schema):
    id: int | None = None
    title: str = ""
    type: str = "recorded"
    genre: str = ""
    # 配信ポリシー個別上書き (#27)。空 = シリーズ既定 (exposure_policy_default) を継承。
    exposure_policy: str = ""
    start_at: str = ""  # datetime-local "YYYY-MM-DDTHH:MM:SS"
    end_at: str = ""
    asset_id: int | None = None
    live_source_id: int | None = None
    cast: str = ""
    description: str = ""
    public_visible: bool = True
    vod_visibility: str = "off"  # off|public|members|subscribers
    clock_style_override: dict | None = None
    record_live: bool = False  # type=live のときのみ意味を持つ (自動録画→見逃し配信化)


class ProgramFormOut(Schema):
    channel: SchedChannel
    assets: list[IdName]  # READY 録画素材
    live_sources: list[IdName]
    initial: ProgramInitial
    exposure_policy_choices: list[Choice]
    delete_url: str  # 編集時のみ (旧 delete エンドポイント)


class ProgramIn(Schema):
    title: str
    type: str  # recorded | live
    genre: str = ""
    exposure_policy: str = ""
    start_at: str
    end_at: str
    asset_id: int | None = None
    live_source_id: int | None = None
    cast: str = ""
    description: str = ""
    public_visible: bool = True
    vod_visibility: str = "off"
    clock_style_override: dict | None = None
    record_live: bool = False  # type=live のときのみ意味を持つ (自動録画→見逃し配信化)


# --- ファンクラブ (#27 studio admin) ---


class CreatorAdminOut(Schema):
    id: int
    name: str
    slug: str
    description: str
    status: str  # active | suspended
    created_at: str
    series_count: int
    member_count: int
    active_contract_count: int
    onboarding_status: str  # pending | approved | rejected
    identity_verified_at: str = ""
    legal_name: str
    is_individual: bool
    representative_name: str
    address: str
    phone: str
    hide_contact_details: bool
    contact_email: str
    invoice_registration_number: str


class CreatorIn(Schema):
    name: str
    slug: str
    description: str = ""
    status: str = "active"
    onboarding_status: str = "pending"
    legal_name: str = ""
    is_individual: bool = True
    representative_name: str = ""
    address: str = ""
    phone: str = ""
    hide_contact_details: bool = True
    contact_email: str = ""
    invoice_registration_number: str = ""


class SeriesOptionOut(Schema):
    id: int
    title: str
    channel_name: str
    linked_creator_name: str = ""  # 他 creator に既リンク済みなら名前を添える。空=未紐付け


class SeriesCreatorLinkOut(Schema):
    creator_id: int | None = None


class CreatorSeriesOut(Schema):
    series_id: int
    series_title: str
    channel_name: str


class CreatorTierOut(Schema):
    id: int
    level: int
    name: str
    description: str
    price_jpy: int | None = None  # 後方互換 (配布済みアプリが参照)。JPY 以外は None
    price_minor: int | None = None
    currency: str = "jpy"
    is_active: bool
    stripe_price_id: str = ""
    joinable: (
        bool  # fanclub.services.tier_is_joinable() (Stripe Price + Connect オンボーディング済み)
    )


class CreatorTierIn(Schema):
    level: int
    name: str
    description: str = ""
    price_minor: int | None = None
    is_active: bool = True
    stripe_price_id: str = ""


class CreatorInvitationOut(Schema):
    id: int
    email: str
    expires_at: str
    accepted_at: str = ""
    created_at: str
    invite_url: str


class CreatorInvitationIn(Schema):
    email: str


class SlotContractOut(Schema):
    id: int
    title: str
    monthly_fee_minor: int
    currency: str = "jpy"  # 金額は minor unit。表示側は通貨とセットで扱う
    starts_on: str
    ends_on: str = ""
    status: str
    youtube_destination: str
    notes: str = ""
    # Stripe Billing 連携済み(stripe_subscription_id 設定済み)なら true。true の間 status は
    # Webhook のみが更新するため、studio からの手動 status 変更は拒否される (409)。
    stripe_active: bool = False


class SlotContractIn(Schema):
    title: str
    monthly_fee_minor: int
    starts_on: str
    ends_on: str = ""
    status: str = "draft"
    youtube_destination: str = "none"
    notes: str = ""


class CreatorMembersSummaryOut(Schema):
    total: int
    by_level: dict[str, int]  # level(文字列key) -> 会員数


class CreatorRevenueMonthOut(Schema):
    month: str  # "YYYY-MM" (ローカル月、当月は月初〜現在時刻の途中経過)
    joined: int
    left: int


class CreatorRevenueSummaryOut(Schema):
    slot_mrr_jpy: int  # 有効な枠契約 (SlotContract, B2B) の月額合計。**JPY 分のみ**
    # 通貨ごとの内訳。通貨をまたいだ合算は誤りなので、外貨の契約が出たらこちらを見る。
    slot_mrr_by_currency: list[dict] = []
    active_slot_contracts: int
    fc_active_members: int
    monthly: list[CreatorRevenueMonthOut]  # 直近6ヶ月、古い順
    churn_rate: float | None  # 直近の完了月 (退会数 / 月初在籍数)。母数0/データ不足はNone


class FcSettlementOut(Schema):
    id: int
    member_email: str = ""  # 会員が削除済みなら空
    tier_name: str = ""  # ティアが削除済みなら空
    gross_amount_minor: int
    application_fee_minor: int
    net_amount_minor: int
    currency: str = "jpy"  # 台帳は「実際に何で決済されたか」を持つ。表示は通貨とセット
    disputed: bool
    period_start: str = ""
    period_end: str = ""
    created_at: str


class FcSettlementSummaryOut(Schema):
    total_gross_jpy: int  # 後方互換 (studio SPA が参照)。JPY 分のみ
    total_fee_jpy: int
    total_net_jpy: int
    # 通貨ごとの内訳。通貨をまたいだ合算は誤りなので、増えたらこちらを見る。
    totals_by_currency: list[dict] = []
    disputed_count: int
    items: list[FcSettlementOut]


class SeriesPostAdminOut(Schema):
    id: int
    kind: str
    title: str
    body: str
    media_url: str
    form_url: str
    campaign_start: str = ""
    campaign_end: str = ""
    is_published: bool
    published_at: str = ""
    fc_required_level: int | None = None


class SeriesPostIn(Schema):
    kind: str = "article"
    title: str
    body: str = ""
    media_url: str = ""
    form_url: str = ""
    campaign_start: str = ""
    campaign_end: str = ""
    is_published: bool = False
    fc_required_level: int | None = None


# --- ネイティブアプリ トークン認証 (#MOBILE-01) ---
class MobileLoginIn(Schema):
    email: str
    password: str
    device_label: str = ""  # 端末識別用の表示名。ログイン一覧/失効 UI 用


class MobileLogin2faIn(Schema):
    challenge_id: str
    code: str


class MobileResendIn(Schema):
    challenge_id: str


class MobileMemberOut(Schema):
    id: int
    nickname: str
    email: str
    is_verified: bool


class MobileLoginOut(Schema):
    """status="ok" ならトークン発行済み、"2fa_required" なら第2要素へ進む。

    2FA が要る場合も HTTP は 200 (第1要素は成功しているため)。クライアントは status で分岐する。
    """

    status: str
    token: str = ""
    member: MobileMemberOut | None = None
    method: str = ""  # "totp" / "email" (2fa_required のときのみ)
    challenge_id: str = ""


# --- VOD 再生 (#MOBILE-01) ---
class VodPlayOut(Schema):
    """再生可否と、可のときだけ R2 の期限付き署名 URL。

    url は progressive MP4 (HTTP range でシーク可)。短命なので保存せず、再生直前に取得する。
    """

    can_watch: bool
    gate: str  # "" (再生可) / "age" / "login" / "verify" / "subscribe" / fanclub 系
    url: str = ""
    expires_in: int = 0
    program_id: int
    title: str = ""
    duration_ms: int | None = None


# --- ファンクラブ アプリ向け (#MOBILE-02) ---
class FcTierItem(Schema):
    """ティアカード1行。action は services.tier_rows の語彙 + 無料ティア用の "free_join"/"current"。

    有料ティアへの加入/変更 (Stripe Checkout) はアプリ内では完結させず web (web_url) へ誘導する。
    """

    id: int
    level: int
    name: str
    description: str = ""
    price_jpy: int | None = None  # 後方互換 (配布済みアプリが参照)。JPY 以外は None
    price_minor: int | None = None
    currency: str = "jpy"
    action: str  # "free_join" | "join" | "current" | "upgrade" | "downgrade"


class FcMembershipOut(Schema):
    """在籍1件 (デジタル会員証の表示要素)。member_no は 5桁ゼロ詰めの表示文字列。"""

    creator_slug: str
    creator_name: str
    avatar_url: str = ""
    theme_color: str = ""
    tier_level: int
    tier_name: str
    member_no: str
    joined_display: str = ""
    enrolled_months: int = 0
    loyalty_badge: str = ""
    pending_tier_name: str = ""
    pending_effective_display: str = ""


class FcUpcomingItem(Schema):
    program_id: int
    title: str
    series_title: str
    channel_slug: str
    time: str  # "n/j(D) H:MM"


class FcSeriesItem(Schema):
    id: int
    title: str


class FcPostItem(Schema):
    """creator ページの投稿フィード1行。本文は含めない (詳細 API 側でゲート判定)。"""

    id: int
    series_id: int
    series_title: str
    kind: str  # "article" | "campaign"
    title: str
    published_at: str  # "n/j(D)" or ""
    locked: bool = False


class FcPageOut(Schema):
    """公開クリエイターページ /fc/<slug>/ のアプリ表現。無料公開レイヤーは匿名でも返す。

    viewer は Bearer/session で解決できた会員が在籍中のときのみ。決済を伴う操作
    (有料加入/変更/チップ/ギフト購入) は web_url へリンクアウトする。
    """

    slug: str
    name: str
    description: str = ""
    avatar_url: str = ""
    cover_url: str = ""
    theme_color: str = ""
    sns_x_url: str = ""
    sns_youtube_url: str = ""
    sns_instagram_url: str = ""
    website_url: str = ""
    tiers: list[FcTierItem]
    is_member: bool  # ログイン済みか (未ログインはログイン CTA を出す)
    viewer: FcMembershipOut | None = None
    chat_enabled: bool = False
    can_chat: bool = False
    chat_required_level: int | None = None
    upcoming: list[FcUpcomingItem]
    series: list[FcSeriesItem]
    posts: list[FcPostItem]
    web_url: str


class FcJoinFreeOut(Schema):
    ok: bool
    tier_name: str


class FcMyOut(Schema):
    memberships: list[FcMembershipOut]


class FcChatItem(Schema):
    id: int
    member_id: int
    nickname: str
    badge: str = ""  # 勤続バッジのラベル ("" = バッジなし)
    time: str
    body: str


class FcChatListOut(Schema):
    can_post: bool
    me_member_id: int | None = None
    items: list[FcChatItem]


class FcChatCreateIn(Schema):
    body: str


class SeriesPostDetailOut(Schema):
    """シリーズ投稿の本文。ロック中は body_html/media_url/form_url を空にして返す (漏えい防止)。"""

    id: int
    series_id: int
    series_title: str
    kind: str
    title: str
    published_at: str
    locked: bool
    gate: str  # "" | "login" | "fc_join" | "fc_unavailable"
    fc_creator_slug: str = ""
    body_html: str = ""
    media_url: str = ""
    form_url: str = ""
    campaign_end: str = ""
