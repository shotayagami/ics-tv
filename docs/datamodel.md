# #1 データモデル（PostgreSQL DDL）

> ステータス: **2026-09 全面更新**。本書の DDL は実装（`server/*/models.py` の Django モデル）を正として
> 書き直した。初期設計（PG ENUM / GENERATED IDENTITY 前提）から、実装は Django の流儀
> （`bigserial` PK・enum は varchar + TextChoices・FK の PROTECT/SET_NULL はアプリ層）に置き換わっている。

## スコープ（本書が扱う範囲と、扱わない範囲の正本）

本書が DDL を記載するのは初期設計の中核 **17 テーブル**（+付随 1）のみ:
チャンネル（`channel` / `live_source`）・素材（`asset` / `cm_creative` / `cm_bundle*` / `filler_*`）・
編成（`program` / `ad_break*`）・YouTube 連携（`youtube_*` / `program_broadcast`）・送出 as-run（`playout_event`）。

実装はこれを大きく超えて **96 テーブル**（Django モデル実測、2026-09）まで拡張されている。
本書に無いテーブルの **DDL 正本は `server/<app>/models.py`** である（series/編成拡張=`scheduling`、
会員=`members`、サブスク=`subscriptions`、ファンクラブ=`fanclub`、権利=`rights`、請求=`billing`、
視聴計測=`analytics` ほか）。番組予算 `procurement` は追加提供側の機能で、このツリーの
`server/procurement/models.py` はモデルを持たない。docs/ 内に CREATE TABLE を持つ文書は次の 3+1 本のみ:

- 本書（17 テーブル。以下）
- [sales.md](sales.md)（23 テーブル。`series` / `series_slot` と営放・請求系の設計正本。実装との差分に注意）
- [operations.md](operations.md)（2 テーブル。`agent_status` / `notification`）
- [delivery.md](delivery.md)（7 テーブル。**別リポ ICS-DELIVERY へ移管済みの歴史的記録**。ICS-TV の DB には無い）

時間は frame-accurate 重視で **`duration_ms`（整数ミリ秒）** を基本単位とする。

## 列挙（実装は varchar + Django TextChoices。PG ENUM は不使用）

| 論理名 | 値 | 定義箇所 |
|---|---|---|
| asset_kind | `program` `cm` `filler` `bumper` `slate` | `medialib.AssetKind` |
| normalize_status | `pending` `processing` `ready` `failed` | `medialib.NormalizeStatus` |
| caption_status | `none` `processing` `ready` `failed` | `medialib.CaptionStatus`（決定#24 VOD 字幕） |
| screening_status | `pending` `approved` `rejected` | `medialib.ScreeningStatus`（表現考査 #6 S8） |
| cm_grid | `15s` `20s` | `medialib.CmGrid` |
| program_type | `recorded` `live` | `scheduling.ProgramType` |
| playout_action | `play_asset` `play_cm` `play_cm_bundle` `cut_live` `play_filler` `play_slate` `yt_transition` `clear_slate` `overlay_op` `play_vt` `yt_mirror_filler` `yt_mirror_route` | `playout.PlayoutAction` |
| playout_status | `scheduled` `executing` `done` `skipped` `failed` `cancelled` | `playout.PlayoutStatus` |
| yt_slot_status | `created` `ready` `testing` `live` `complete` `error` | `youtube.YtSlotStatus` |

`vod_visibility` / `exposure_policy` / `genre` / `rating` 等の編成系列挙は `scheduling/models.py` を参照。

## DDL（実装からの転記）

以下は Django が生成する実スキーマの読み下し。`bigserial PRIMARY KEY` = Django `BigAutoField`。
FK の PROTECT / SET_NULL / CASCADE は Django `on_delete`（DB 制約は素の FK + アプリ層挙動）。
「暗号化」= `core.fields.EncryptedTextField`（#3 DB at-rest 暗号化。暗号文は非決定的で WHERE 照合不可）。

```sql
-- ---- チャンネル (server/core/models.py Channel) ----
CREATE TABLE channel (
    id                      bigserial PRIMARY KEY,
    name                    varchar(200) NOT NULL,
    slug                    varchar(100) NOT NULL UNIQUE,
    enabled                 boolean NOT NULL DEFAULT true,
    short                   varchar(20) NOT NULL DEFAULT '',   -- 略称(例「総合」#7 公開フロント)
    tint                    varchar(7) NOT NULL DEFAULT '',    -- 識別色 hex。未設定は slug ハッシュで自動
    cf_live_input_id        varchar(200),                      -- Cloudflare Live Input UID
    cf_playback_hls_url     varchar(500),                      -- CF Stream HLS 再生 URL (#7 Phase2)
    youtube_channel_id      varchar(200),
    youtube_stream_key      text,                              -- 暗号化 (永続 streamName)
    youtube_livestream_id   varchar(200),                      -- liveStreams.insert の id (isReusable)
    youtube_ingest_url      varchar(500),
    youtube_livestream_id_2 varchar(200),                      -- #23 番組専用枠用の 2 本目永続 liveStream
    youtube_ingest_url_2    varchar(500),
    youtube_stream_key_2    text,                              -- 暗号化
    agent_token             text UNIQUE,                       -- 暗号化 (agent gRPC Bearer。slug で引いて復号比較)
    default_filler_id       bigint REFERENCES filler_playlist(id),  -- SET_NULL
    slate_asset_id          bigint REFERENCES asset(id),       -- SET_NULL。ch 別スレート (layer90)
    site_only_filler_id     bigint REFERENCES asset(id),       -- #27 exposure_policy: 公開ミラー案内
    members_filler_id       bigint REFERENCES asset(id),       -- #27 exposure_policy: メンバーミラー待機画
    clock_overlay_enabled   boolean NOT NULL DEFAULT false,    -- 朝夕の左上時計 (opt-in)
    clock_windows           jsonb NOT NULL DEFAULT '[]',       -- [{"start":"HH:MM","end":"HH:MM"},…] JST
    clock_style             jsonb NOT NULL DEFAULT '{}',
    broadcast_windows       jsonb NOT NULL DEFAULT '[]',       -- 放送時間帯(休止)。[]=24h。end="24:00"可
    created_at              timestamptz NOT NULL
);

-- ---- メディアライブラリ (server/medialib/models.py Asset) ----
CREATE TABLE asset (
    id                    bigserial PRIMARY KEY,
    kind                  varchar(16) NOT NULL,                -- asset_kind
    title                 varchar(300) NOT NULL,
    thumbnail_url         varchar(500),                        -- 固定サムネ(手動指定)
    r2_key                varchar(500),                        -- R2 上の正規化済み mezzanine キー
    source_path           varchar(500),                        -- OMV 上のマスタ元
    checksum              varchar(128),
    duration_ms           bigint,                              -- フレーム精度の尺
    width                 int,
    height                int,
    fps                   numeric(6,3),
    vcodec                varchar(64),
    acodec                varchar(64),
    normalize_status      varchar(16) NOT NULL DEFAULT 'pending',
    normalize_error       text,
    normalize_started_at  timestamptz,                         -- stale 滞留回収・監視用
    offload_request_id    varchar(36),                         -- Windows オフロード requestId (normalize-offload.md)
    offload_dispatched_at timestamptz,
    passthrough           boolean NOT NULL DEFAULT false,      -- 長尺原本化(映像コピー+loudnorm)フラグ
    usable_as_program     boolean NOT NULL DEFAULT false,      -- 役割フラグ(1素材複数用途)
    usable_as_filler      boolean NOT NULL DEFAULT false,
    rerun_eligible        boolean NOT NULL DEFAULT false,      -- フィラー送出時に「再放送」として EPG 露出可
    caption_status        varchar(16) NOT NULL DEFAULT 'none', -- 決定#24 VOD 字幕
    caption_r2_key        varchar(500),
    caption_lang          varchar(8) NOT NULL DEFAULT 'ja',
    caption_error         text,
    created_at            timestamptz NOT NULL
);
CREATE INDEX idx_asset_kind_ready ON asset(kind) WHERE normalize_status = 'ready';

-- ---- CM固有属性 (asset kind='cm' に 1:1) ----
CREATE TABLE cm_creative (
    asset_id         bigint PRIMARY KEY REFERENCES asset(id) ON DELETE CASCADE,
    advertiser       varchar(200) NOT NULL,
    grid             varchar(8) NOT NULL,                 -- cm_grid: 15s / 20s
    campaign_start   date,
    campaign_end     date,
    max_airings      int,                                 -- 出稿上限(回数), NULL=無制限
    aired_count      int NOT NULL DEFAULT 0,              -- 実送出確定時に増分 (as-run 返送)
    screening_status varchar(10) NOT NULL DEFAULT 'pending'  -- 表現考査 (#6 S8)
);

-- ---- 生入力ソース (送出ノードの MediaMTX で SRT/RTMP 終端。決定#21) ----
CREATE TABLE live_source (
    id             bigserial PRIMARY KEY,
    name           varchar(200) NOT NULL,
    rtmp_app       varchar(200) NOT NULL,                 -- MediaMTX path = <rtmp_app>/<rtmp_key>
    rtmp_key       varchar(200) NOT NULL,
    srt_passphrase text,                                  -- 暗号化 (SRT ingest URL 用)
    srt_latency_ms int NOT NULL DEFAULT 2000,
    note           text
);

-- ---- 番組 (server/scheduling/models.py Program。人が組む / フリー編成) ----
CREATE TABLE program (
    id                   bigserial PRIMARY KEY,
    channel_id           bigint NOT NULL REFERENCES channel(id),        -- PROTECT
    series_id            bigint REFERENCES series(id),                  -- SET_NULL (#6。series は sales.md)
    episode_id           bigint REFERENCES episode(id),                 -- SET_NULL (#5 回。再放送は同一回を複数 program)
    type                 varchar(10) NOT NULL,                          -- program_type
    title                varchar(300) NOT NULL,
    genre                varchar(20) NOT NULL DEFAULT '',               -- 空= series.genre へフォールバック
    rating               varchar(8) NOT NULL DEFAULT '',                -- 視聴年齢制限 (#BILL-02)。空= series 継承
    start_at             timestamptz NOT NULL,
    end_at               timestamptz NOT NULL,   -- = start_at + asset尺 + Σ(ad_break尺) (アプリ算出)
    asset_id             bigint REFERENCES asset(id),                   -- PROTECT。recorded 時
    live_source_id       bigint REFERENCES live_source(id),             -- PROTECT。live 時
    recording_asset_id   bigint REFERENCES asset(id),                   -- 生放送の録画メザニン (VOD 見逃し用)
    record_live          boolean NOT NULL DEFAULT false,                -- この放送を自動録画する (live のみ)
    cm_bundle_id         bigint REFERENCES cm_bundle(id),               -- PROTECT。live の事前バンドル CM
    description          text,
    "cast"               text,                                          -- 出演者 (#EPG-01)。空= series 継承
    clock_hidden         boolean NOT NULL DEFAULT false,                -- 朝夕時計の番組単位 opt-out
    clock_style_override jsonb,                                         -- NULL=継承 (series→channel)
    lbar_hidden          boolean NOT NULL DEFAULT false,                -- L字の番組単位 opt-out
    public_visible       boolean NOT NULL DEFAULT true,
    vod_visibility       varchar(12) NOT NULL DEFAULT 'off',            -- 見逃し配信 (#VOD-01)
    vod_available_until  timestamptz,                                   -- NULL=無期限
    fc_required_level    smallint,             -- #27 FC 限定 (NULL=完全公開/0=無料会員以上/n=ティアn以上)
    exposure_policy      varchar(20) NOT NULL DEFAULT '',               -- #27 site-only-broadcast.md。空= series 継承
    is_featured          boolean NOT NULL DEFAULT false,                -- 見逃しトップ Hero 編集選択
    youtube_dedicated    boolean NOT NULL DEFAULT false,                -- #23 専用枠 (番組 OR シリーズ)
    youtube_preset_id    bigint REFERENCES youtube_broadcast_preset(id),-- SET_NULL
    created_at           timestamptz NOT NULL,
    CONSTRAINT chk_program_time CHECK (end_at > start_at),
    CONSTRAINT chk_program_source CHECK (
        (type='recorded' AND asset_id IS NOT NULL AND live_source_id IS NULL) OR
        (type='live'     AND live_source_id IS NOT NULL AND asset_id IS NULL)
    ),
    CONSTRAINT chk_program_fc_requires_level CHECK (      -- fanclub 限定を名乗るなら level 必須
        (vod_visibility='fanclub' AND fc_required_level IS NOT NULL) OR vod_visibility <> 'fanclub'
    ),
    CONSTRAINT program_no_overlap_per_channel EXCLUDE USING gist (  -- 同一chで時間的に重ならない
        channel_id WITH =,
        tstzrange(start_at, end_at) WITH &&
    )
);
CREATE INDEX idx_program_ch_time ON program(channel_id, start_at);
-- series / series_slot の DDL は sales.md「編成強化 (scheduling 拡張)」を参照。episode (#19 回) の
-- 実装正本は scheduling/models.py Episode (初期 DDL は delivery.md #19 にあるが同 doc は移管済み歴史記録)。

-- ---- CM枠 (録画番組内) ----
CREATE TABLE ad_break (
    id          bigserial PRIMARY KEY,
    program_id  bigint NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    offset_ms   bigint NOT NULL,                          -- 番組頭からの挿入位置
    grid        varchar(8) NOT NULL,                      -- cm_grid
    duration_ms bigint NOT NULL                           -- 枠尺 (grid 整数倍。chk_grid_multiple は
);                                                        --  scheduling migration 0002 で付与)

-- ---- CM枠の充填結果 (スケジューラが選定, 再現性/as-run計画用) ----
CREATE TABLE ad_break_item (
    id          bigserial PRIMARY KEY,
    ad_break_id bigint NOT NULL REFERENCES ad_break(id) ON DELETE CASCADE,
    seq         int NOT NULL,
    cm_asset_id bigint NOT NULL REFERENCES cm_creative(asset_id),  -- PROTECT
    CONSTRAINT uq_ad_break_item_seq UNIQUE (ad_break_id, seq)
);

-- ---- CMバンドル (生番組用リール) ----
CREATE TABLE cm_bundle (
    id   bigserial PRIMARY KEY,
    name varchar(200) NOT NULL,
    note text
);
CREATE TABLE cm_bundle_item (
    id           bigserial PRIMARY KEY,
    cm_bundle_id bigint NOT NULL REFERENCES cm_bundle(id) ON DELETE CASCADE,
    seq          int NOT NULL,
    cm_asset_id  bigint NOT NULL REFERENCES cm_creative(asset_id),  -- PROTECT
    CONSTRAINT uq_cm_bundle_item_seq UNIQUE (cm_bundle_id, seq)
);

-- ---- フィラープレイリスト (隙間充填ループ) ----
CREATE TABLE filler_playlist (
    id   bigserial PRIMARY KEY,
    name varchar(200) NOT NULL
);
CREATE TABLE filler_item (
    id                 bigserial PRIMARY KEY,
    filler_playlist_id bigint NOT NULL REFERENCES filler_playlist(id) ON DELETE CASCADE,
    seq                int NOT NULL,
    asset_id           bigint NOT NULL REFERENCES asset(id),  -- PROTECT
    CONSTRAINT uq_filler_item_seq UNIQUE (filler_playlist_id, seq)
);

-- ---- YouTube rolling 枠 (server/youtube/models.py YoutubeSlot。既定 4h 枠) ----
CREATE TABLE youtube_slot (
    id           bigserial PRIMARY KEY,
    channel_id   bigint NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    window_start timestamptz NOT NULL,
    window_end   timestamptz NOT NULL,                    -- = window_start + youtube_config.slot_minutes
    broadcast_id varchar(64),                             -- YouTube liveBroadcast id
    status       varchar(12) NOT NULL DEFAULT 'created',  -- yt_slot_status
    title        varchar(300),
    description  text,                                    -- YouTube キャプション (#7 枠メタ)
    manual       boolean NOT NULL DEFAULT false,          -- 人が編集した枠 (自動生成で上書きしない)
    next_nudged  boolean NOT NULL DEFAULT false,          -- 次枠誘導 (チャット投稿+説明欄追記) 済み。1枠1回
    ended_nudged boolean NOT NULL DEFAULT false,          -- complete 後の説明文「終了」差し替え済み。1枠1回
    error        text,
    CONSTRAINT uq_youtube_slot_window UNIQUE (channel_id, window_start)
);

-- ---- OAuth 認証情報 (チャンネル所有者の同意; 書き込みAPIに必須) ----
CREATE TABLE youtube_credential (
    channel_id    bigint PRIMARY KEY REFERENCES channel(id) ON DELETE CASCADE,
    client_id     varchar(300) NOT NULL,
    client_secret text NOT NULL,                          -- 暗号化
    refresh_token text NOT NULL,                          -- 暗号化
    scopes        varchar(300) NOT NULL DEFAULT 'https://www.googleapis.com/auth/youtube.force-ssl',
    access_token  varchar(1000),                          -- 短期キャッシュ
    token_expiry  timestamptz,
    updated_at    timestamptz NOT NULL
);

-- ---- 枠生成テンプレート/設定 (WebUIで編集) ----
CREATE TABLE youtube_config (
    channel_id           bigint PRIMARY KEY REFERENCES channel(id) ON DELETE CASCADE,
    title_template       varchar(300) NOT NULL DEFAULT '{channel} {date} {start}-{end}',
    description_template text NOT NULL
        DEFAULT E'ICS-TV {date} {start}-{end} (JST) の配信枠です。\n\n{programs}',
    privacy              varchar(12) NOT NULL DEFAULT 'public',  -- public|unlisted|private
    enable_monitor       boolean NOT NULL DEFAULT false,   -- false=created→live直行
    rolling_hours        int NOT NULL DEFAULT 24,          -- 何時間先まで枠を先回り生成
    slot_minutes         int NOT NULL DEFAULT 240,         -- 1枠=240分(4h。通知過多対策で 2h→4h)
    nudge_lead_minutes   int NOT NULL DEFAULT 10,          -- 次枠誘導を出す「終了何分前」(0=無効)
    nudge_template       text NOT NULL
        DEFAULT 'まもなくこの配信は終了します。続きは次の配信でご覧ください ▶ {url}',
    nudge_ended_template text NOT NULL
        DEFAULT 'この配信は終了しています。続きは次の配信でご覧ください ▶ {url}',
    default_thumb_id     bigint REFERENCES asset(id)       -- SET_NULL
);
```

## 番組専用 YouTube 枠 + 配信プリセット（#23）

rolling 枠（`youtube_slot`）と並行する、番組単位の専用 `liveBroadcast`。設計は [youtube.md](youtube.md) #23。
2 本目の永続 liveStream（`channel.youtube_livestream_id_2` ほか）は上記 `channel` に統合済み。

```sql
-- 再利用可能な配信プリセット (Studio 配信設定の登録/再利用)
CREATE TABLE youtube_broadcast_preset (
    id                bigserial PRIMARY KEY,
    name              varchar(100) NOT NULL,
    channel_id        bigint REFERENCES channel(id) ON DELETE CASCADE,  -- NULL=全ch共通
    -- API 反映群 (liveBroadcasts/videos.update/thumbnails.set/playlistItems.insert)
    title_template    varchar(300) NOT NULL DEFAULT '',   -- {program}/{date}/{start}/{end}/{channel}
    description_template text NOT NULL DEFAULT '',
    category_id       int,                                -- videos.update snippet.categoryId
    tags              jsonb NOT NULL DEFAULT '[]',        -- snippet.tags
    privacy           varchar(12) NOT NULL DEFAULT 'public',
    made_for_kids     boolean NOT NULL DEFAULT false,     -- selfDeclaredMadeForKids
    default_language  varchar(10) NOT NULL DEFAULT '',
    default_audio_language varchar(10) NOT NULL DEFAULT '',
    latency           varchar(10) NOT NULL DEFAULT 'low', -- normal|low|ultraLow
    enable_dvr        boolean NOT NULL DEFAULT true,
    enable_embed      boolean NOT NULL DEFAULT true,
    enable_auto_start boolean NOT NULL DEFAULT false,
    enable_auto_stop  boolean NOT NULL DEFAULT false,
    record_from_start boolean NOT NULL DEFAULT true,
    license           varchar(16) NOT NULL DEFAULT 'youtube',  -- youtube|creativeCommon
    public_stats_viewable boolean NOT NULL DEFAULT true,
    thumbnail_id      bigint REFERENCES asset(id),        -- SET_NULL
    playlist_id       varchar(64) NOT NULL DEFAULT '',    -- 追加先 再生リスト
    -- API 不可分群 (Studio 専用 → 手動チェックリストの定義+既定値。既定 14 項目は
    --  youtube/models.py default_manual_checklist を参照)
    manual_checklist  jsonb NOT NULL,                     -- [{key,label,default}]
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL
);

-- preset の title/description テンプレをあらかじめ登録するマスタ (チャンネル非依存)
CREATE TABLE youtube_description_template (
    id                   bigserial PRIMARY KEY,
    name                 varchar(100) NOT NULL,
    title_template       varchar(300) NOT NULL DEFAULT '{program} | {channel}',
    description_template text NOT NULL DEFAULT '',
    created_at           timestamptz NOT NULL,
    updated_at           timestamptz NOT NULL
);

-- 番組単位の専用 broadcast (youtube_slot とは別系統=本番 rolling 経路を隔離)
CREATE TABLE program_broadcast (
    id              bigserial PRIMARY KEY,
    program_id      bigint NOT NULL UNIQUE REFERENCES program(id) ON DELETE CASCADE,  -- 1番組1専用枠
    preset_id       bigint REFERENCES youtube_broadcast_preset(id),  -- SET_NULL
    broadcast_id    varchar(64),                          -- YouTube liveBroadcast id (=video id)
    status          varchar(12) NOT NULL DEFAULT 'created',  -- yt_slot_status を流用
    checklist_state jsonb NOT NULL DEFAULT '{}',          -- 配信ごとの手動チェック状態 {key: bool}
    manual          boolean NOT NULL DEFAULT false,       -- 手動ボタン作成
    error           text,
    created_at      timestamptz NOT NULL,
    updated_at      timestamptz NOT NULL
);
```

`program.youtube_dedicated` / `program.youtube_preset_id`（および series 側の同名列。series は
[sales.md](sales.md)）は上記 `program` の DDL に統合済み。解決順は番組 OR シリーズ
（`wants_dedicated`）、preset は番組 → シリーズ（`resolved_youtube_preset`）。

## as-run / 解決済み送出イベント

```sql
-- server/playout/models.py PlayoutEvent
CREATE TABLE playout_event (
    id               bigserial PRIMARY KEY,
    idempotency_key  uuid NOT NULL UNIQUE,        -- scheduler が決定的 uuid5 を設定 (scheduler.md)
    sync_seq         bigint UNIQUE,               -- gRPC SubscribeEvents の resume cursor。
                                                  --  独立 SEQUENCE + BEFORE INSERT/UPDATE トリガーで
                                                  --  自動採番 (update 時も bump → agent が拾える)
    channel_id       bigint NOT NULL REFERENCES channel(id),        -- PROTECT
    scheduled_at     timestamptz NOT NULL,        -- 実行予定時刻 (壁時計)
    action           varchar(20) NOT NULL,        -- playout_action
    asset_id         bigint REFERENCES asset(id),                   -- SET_NULL (ログ保全)
    program_id       bigint REFERENCES program(id),                 -- SET_NULL
    live_source_id   bigint REFERENCES live_source(id),             -- SET_NULL
    cm_bundle_id     bigint REFERENCES cm_bundle(id),               -- SET_NULL
    youtube_slot_id  bigint REFERENCES youtube_slot(id),            -- SET_NULL
    ad_break_item_id bigint REFERENCES ad_break_item(id),           -- SET_NULL。放確の逆引き (#6 S10)
    params           jsonb NOT NULL DEFAULT '{}', -- AMCP 補助 (layer/in_ms/out_ms/CG cue 等)
    status           varchar(12) NOT NULL DEFAULT 'scheduled',      -- playout_status
    actual_at        timestamptz,                 -- 実際の実行時刻 (as-run)
    note             text,
    created_at       timestamptz NOT NULL
);
CREATE INDEX idx_pe_ch_sched   ON playout_event(channel_id, scheduled_at);
CREATE INDEX idx_pe_scheduled  ON playout_event(status) WHERE status = 'scheduled';
```

## 設計上のポイント（なぜこうしたか）

- **ミリ秒整数（`duration_ms`）を基本単位**に。放送はフレーム精度が要るため、`interval` より演算が安定。
  正規化で fps を固定する前提なのでミリ秒で十分（「フレーム数+fps」案は不採用で確定）。
- **asset を統一テーブル化**（program/cm/filler/bumper/slate を `kind` で区別）。正規化パイプライン
  （`normalize_status`/技術メタ/字幕/オフロード）を一箇所に集約。CM 固有属性のみ `cm_creative` に 1:1 で分離。
  1 素材複数用途は `kind` を増やさず役割フラグ（`usable_as_program`/`usable_as_filler`）で表す。
- **`EXCLUDE USING gist`**（`program_no_overlap_per_channel`）で「同一チャンネルで番組が時間的に
  重ならない」を **DB 側で保証**。フリー編成は人が任意時刻に置くため、この安全網が効く。
- **`chk_grid_multiple`** で CM 枠尺を 15000/20000ms の整数倍に強制（migration 付与）。
- **編成と送出の分離**: 人が組む `program`/`ad_break` と、スケジューラが解決する `playout_event`
  (as-run) を別テーブルに。`idempotency_key`(uuid5・決定的) で、WAN 断・再送時も agent が
  **同じイベントを二重実行しない**。`sync_seq` が gRPC 再購読の resume cursor。
- **enum は varchar + TextChoices**。PG ENUM は値追加のたびに `ALTER TYPE` が要り migration が
  重くなるため実装では採用しなかった（`clear_slate` / `play_vt` / `yt_mirror_*` 等の後付けが容易）。
- **秘密値は `EncryptedTextField` で at-rest 暗号化**（#3。stream key / OAuth secret / agent token /
  SRT passphrase）。外部シークレットストア案は不採用で確定。

## 解決済みの論点（初期設計時の未確定事項）

- `cm_creative.aired_count` の増分タイミング → **実送出確定時（agent の as-run 返送時）**
  （[scheduler.md](scheduler.md)。予約時に増やすと欠送で過大計上）。
- シークレット保管 → DB 暗号化（`EncryptedTextField`）で確定。上記。
- `youtube_slot` の枠長 → 既定 240 分（4h）。初期設計の 2h から通知過多対策で変更
  （[youtube.md](youtube.md)。`youtube_config.slot_minutes` で可変）。
