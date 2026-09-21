# #6 営放サブシステム（広告出稿・CM割付・放送確認・請求精算）

民放の基幹システム「営業放送システム（営放）」のフル機能 —— **編成（基本パターン）・営業（タイム/
スポット契約）・CM管理（契約駆動割付・競合排除・考査）・放送管理（放確・欠送/振替）・請求精算
（月次締め・放送確認書・請求書・入金）** —— を icstv に実装するための設計。

icstv は既に「編成 → 運行データ → APC → 送出 → as-run 還流」の閉ループの相似形を持つ
（`scheduling.resolver` = 運行表作案、agent dispatch = APC、`apply_result()` = 放確ポイント）。
本書はその上に**商流の閉ループ（契約 → 割付 → 放送実績 → 確認書 → 請求 → 入金）**を重ねる。
素材の入口は [delivery.md](delivery.md)（#5 納品ポータル）、本書は**商売の台帳**を担う。

## このサブシステムの決定ログ

| # | 論点 | 決定 | 補足 |
|---|------|------|------|
| S1 | 契約形態 | **タイム（番組提供）＋スポット両対応** | 提供クレジットは casparcg.md §3 の CG 提供レイヤと将来連動 |
| S2 | 代理店 | **Agency マスタ＋手数料率。NULL=直販** | 請求時に手数料控除を計算 |
| S3 | 請求・精算 | **フル内製** | 月次締めバッチ＋放送確認書PDF＋請求書PDF（インボイス制度対応）＋入金ステータス管理。外部会計連携は将来 CSV 出力で足せる形 |
| S4 | 割付制約 | **業種競合排除（同一枠＋隣接枠）・線引き（時間帯指定）・指定番組割付** | 全部入り。隣接枠は実装コスト高のため Phase 内で最後 |
| S5 | app 構成 | **`sales` ＋ `billing` の 2 app 新設**（計8 apps） | 決定#14「app 境界=将来のサービス抽出シーム」に整合。billing は確定データを read-only 参照する性質で最も抽出されやすい境界 |
| S6 | 依存方向 | **scheduling/medialib/playout → sales の依存を作らない** | 割付制約は settings 登録式 constraint provider で注入。広告主紐付けも sales 側 link テーブルで持つ。sales 不在でも現行の均等ローテ送出がそのまま動く（24/7 送出の安全性最優先） |
| S7 | 放確ポイント | **`apply_result()` の初回 DONE から `transaction.on_commit` で Celery 発火し `airing` 台帳を記録** | 送出クリティカルパスに同期処理を足さない。broker 不達は日次リコンサイルで回収 |
| S8 | 考査 | **業態考査=advertiser、表現考査=cm_creative。考査ゲートは契約割付（優先順位1〜3）のみに適用** | 契約外フリー素材（優先順位4）は現行条件のまま＝導入時のフィラー全滅を構造的に防ぐ。既存データは data migration で approved にバックフィル |
| S9 | 料金算定 | **本数×単価（タイムランク別料金表）。GRP/パーコストは対象外** | 視聴率データが無いため。YouTube Analytics 連携による実績按分は将来論点 |
| S10 | 放確の逆引き経路 | **`playout_event` に `ad_break_item` FK を追加**（scheduling 内部の拡張） | (program_id, asset_id) では同一番組複数枠・同一素材複数契約で一意にならず誤請求になるため。sales 非依存の変更で S6 と矛盾しない |
| S11 | 契約変更の反映 | **未送出の枠は割付を破棄→再充填**。それ以外は「契約は最大 48h 後の枠から効く」 | fill_break は充填済み枠を再利用するため（決定性）、契約/考査の変更は別経路で窓内に反映する |
| S12 | 生番組のバンドルCM | **play_cm_bundle の DONE でも CmBundleItem を展開して airing を記録** | 放送確認書の網羅性を守る。`airing` は (playout_event, bundle_seq) で冪等 |

## アーキテクチャ：商流の閉ループ

```
[sales]                       [scheduling]                  [playout/agent]            [billing]
ad_contract (タイム/スポット)
  ├ sponsorship(提供)──┐
  └ spot_order(線引き)─┤ constraint provider
                       ▼ (settings 注入)
                resolver.fill_break ──▶ ad_break_item ──▶ playout_event ──▶ agent 送出
                       │                     ▲   ▲ (ad_break_item FK; S10)   │
                placement (割付⇔契約)────────┘   └──────────────── apply_result (初回DONE)
                                                                       │ on_commit → Celery
                                                                 airing (放確台帳)
                                                                       │ 月次締め
                                                   broadcast_certificate (放送確認書PDF)
                                                   invoice + invoice_line (請求書PDF) ──▶ payment (入金)
```

## アーキ上の不変条件（崩さない）

- **proto / agent は無改修**。商流はすべてコントロールプレーン内で完結する。
- **scheduling / medialib / playout は sales を import しない**。これらへの変更は sales 非依存の
  内部拡張のみに限る：
  - `playout_event.ad_break_item_id` FK（S10、`ON DELETE SET NULL`）
  - `fill_break` の残尺ベース化＋ `provider` / `air_at` 引数（未指定なら現行動作）
  - `cm_creative.screening_status`（表現考査。素材属性として medialib に置く）
  - 広告主の FK 紐付けは **sales 側の `cm_advertiser_link`** が持ち、`CmCreative.advertiser`
    （自由テキスト）は表示用に残置（resolver の `params={"advertiser": ...}` も無改修）
- `ICSTV_FILL_CONSTRAINT_PROVIDER`（settings、dotted path）未設定なら従来の均等ローテに
  デグレードする。
- **playout の as-run（`playout_event`）が唯一の実績の真実**。billing は `airing`（放確台帳）経由で
  read-only 参照し、締め済み（`billed=true`）の行と issued 済み invoice は不変とする。
- UI は確定スタック Django + HTMX。帳票 PDF は WeasyPrint、出力履歴は #5 の `sheet_export` 方式を踏襲。

## データモデル（PostgreSQL DDL）

> 記載順 = 実行順（FK 解決順）。series 系 → 既存テーブルへの ALTER → sales → billing。

### 編成強化（scheduling 拡張）

タイム契約・指定番組割付は「毎回の Program」ではなく「レギュラー番組」を対象とするため、
番組マスタ（series）を導入する。**Phase A では series マスタと `program.series_id` のみ**を入れ、
週間パターン（series_slot）と自動展開は Phase C。

```sql
-- ---- レギュラー番組(番組マスタ) ---- ※ Phase A
CREATE TABLE series (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id  bigint NOT NULL REFERENCES channel(id),
    title       text NOT NULL,
    description text,
    is_active   boolean NOT NULL DEFAULT true
);

-- program にレギュラー紐付け(単発は NULL のまま) ※ Phase A
-- series.channel と program.channel の一致はアプリ層 (Form/clean) で検証する
ALTER TABLE program ADD COLUMN series_id bigint REFERENCES series(id);

-- ---- 週間基本編成スロット ---- ※ Phase C
CREATE TABLE series_slot (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    series_id      bigint NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    dow            int NOT NULL CHECK (dow BETWEEN 0 AND 6),  -- 0=月
    start_time     time NOT NULL,
    duration_ms    bigint NOT NULL,
    program_type   program_type NOT NULL,            -- recorded | live
    live_source_id bigint REFERENCES live_source(id), -- live のとき必須
    default_asset_id bigint REFERENCES asset(id),     -- recorded のとき必須(汎用/再放送素材)
    effective_from date NOT NULL,                     -- 改編期の版管理
    effective_to   date,
    CONSTRAINT chk_slot_source CHECK (
        (program_type='recorded' AND default_asset_id IS NOT NULL) OR
        (program_type='live'     AND live_source_id   IS NOT NULL)
    )
);
```

- **展開タスク**（Phase C）：Celery beat（週次）`expand_series_slots` が series_slot を N 週先まで
  `Program` 行へ展開する。既存の `chk_source` CHECK（recorded は asset 必須）を満たすため、
  録画レギュラーは **`default_asset_id`（汎用/再放送素材）で枠を確保**し、当該回のエピソードが
  納品されたら編成 UI で差し替える運用とする（CHECK は無改変）。
- 既存 Program と重なる場合は `EXCLUDE USING gist` が弾く。**展開は行単位の savepoint**
  （`transaction.atomic` ネスト）で行い、衝突行のみスキップ＋警告ログにする（bulk insert は
  1 件の違反で全体が失敗するため不可。手動編成優先）。

### 既存テーブルへの拡張（scheduling / playout / medialib）

```sql
-- 放確の確定的な逆引き経路 (S10)。resolver が PLAY_CM 生成時にセットする。
-- ad_break_item は program 削除で CASCADE するため SET NULL。過去実績の契約帰属は
-- airing 側スナップショット (spot_order_id/sponsorship_id) が保持する。
ALTER TABLE playout_event
    ADD COLUMN ad_break_item_id bigint REFERENCES ad_break_item(id) ON DELETE SET NULL;

-- 表現考査 (素材属性として medialib に置く。S6/S8)
CREATE TYPE screening_status AS ENUM ('pending','approved','rejected');
ALTER TABLE cm_creative
    ADD COLUMN screening_status screening_status NOT NULL DEFAULT 'pending';
-- data migration: 既存行は 'approved' (みなし考査済) に UPDATE する (S8)
```

`CmCreative.grid` は変更しない。**grid の意味は「素材が適合する枠グリッド」**であり、30 秒提供
素材は `grid='15s'` ＋ `duration_ms=30000` で登録する規約とする（`CmGrid` への G30 追加はしない）。
なお既存の `chk_grid_multiple` は **ad_break の枠尺**に対する CHECK であり素材尺は保証しない —
素材尺の検証（grid の整数倍か）は割付側（fill_break の pool 条件）とアプリ層で行う。

### sales app

```sql
-- ---- 列挙 ----
CREATE TYPE contract_kind    AS ENUM ('time','spot');
CREATE TYPE contract_status  AS ENUM ('draft','active','fulfilled','cancelled','expired');
CREATE TYPE time_rank        AS ENUM ('A','SB','B','C');                  -- タイムランク

-- ---- 業種マスタ(競合排除の判定軸) ----
CREATE TABLE industry (
    id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code text NOT NULL UNIQUE,
    name text NOT NULL
);

-- ---- 広告主マスタ ----
CREATE TABLE advertiser (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name             text NOT NULL,
    industry_id      bigint NOT NULL REFERENCES industry(id),
    screening_status screening_status NOT NULL DEFAULT 'pending',  -- 業態考査
    note             text,
    is_active        boolean NOT NULL DEFAULT true,
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- ---- CM素材 ⇔ 広告主の紐付け(S6: sales 側で持ち medialib へ依存を逆流させない) ----
CREATE TABLE cm_advertiser_link (
    cm_asset_id   bigint PRIMARY KEY REFERENCES cm_creative(asset_id) ON DELETE CASCADE,
    advertiser_id bigint NOT NULL REFERENCES advertiser(id)
);
CREATE INDEX idx_cm_adv_link_advertiser ON cm_advertiser_link(advertiser_id);

-- ---- 広告代理店マスタ(S2: NULL FK で直販を表現) ----
CREATE TABLE agency (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name            text NOT NULL,
    commission_rate numeric(5,2) NOT NULL DEFAULT 15.00,  -- 手数料率%
    contact_email   text,
    is_active       boolean NOT NULL DEFAULT true
);

-- ---- タイムランク定義(channel×曜日×時間帯 → ランク) ----
-- 時間帯規約: 区間は [start_time, end_time) の半開。日跨ぎ帯は 2 行に分割する
-- (PostgreSQL の time は '24:00' を許容: 例 23:30-24:00 と 00:00-01:00)。
-- dow は実放送時刻の曜日で判定する。同一 channel×dow 内の時間帯重複はアプリ層で検証。
CREATE TABLE time_band_rank (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id bigint NOT NULL REFERENCES channel(id),
    dow        int NOT NULL CHECK (dow BETWEEN 0 AND 6),
    start_time time NOT NULL,
    end_time   time NOT NULL,
    rank       time_rank NOT NULL,
    CONSTRAINT chk_tbr_time CHECK (start_time < end_time)
);

-- ---- 料金表(正価。S9: 本数×単価) ----
CREATE TABLE rate_card (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id     bigint NOT NULL REFERENCES channel(id),
    rank           time_rank NOT NULL,
    unit_seconds   int NOT NULL,                -- 15/20/30
    price          int NOT NULL,                -- 円
    effective_from date NOT NULL,
    UNIQUE (channel_id, rank, unit_seconds, effective_from)
);

-- ---- 契約ヘッダ(タイム/スポット共通) ----
CREATE TABLE ad_contract (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind          contract_kind NOT NULL,
    advertiser_id bigint NOT NULL REFERENCES advertiser(id),
    agency_id     bigint REFERENCES agency(id),  -- NULL = 直販
    title         text NOT NULL,
    period_start  date NOT NULL,
    period_end    date NOT NULL,
    status        contract_status NOT NULL DEFAULT 'draft',
    note          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_contract_period CHECK (period_end >= period_start)
);

-- ---- タイム契約明細: 番組提供(S1) ----
CREATE TABLE sponsorship (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contract_id         bigint NOT NULL REFERENCES ad_contract(id) ON DELETE CASCADE,
    series_id           bigint NOT NULL REFERENCES series(id),
    period_start        date,                    -- NULL = 契約期間に従う(期中降板/開始用)
    period_end          date,
    seconds_per_episode int NOT NULL,            -- 提供秒数(30の倍数が基本)
    monthly_fee         int NOT NULL,            -- 月額(円)。請求の基礎
    credit_text         text,                    -- 提供クレジット文言(CG 連動は Phase C)
    UNIQUE (contract_id, series_id)
);
CREATE TABLE sponsorship_material (               -- 提供CMの素材(ローテーション)
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sponsorship_id bigint NOT NULL REFERENCES sponsorship(id) ON DELETE CASCADE,
    seq            int NOT NULL,
    cm_asset_id    bigint NOT NULL REFERENCES cm_creative(asset_id),
    UNIQUE (sponsorship_id, seq)
);

-- ---- スポット契約明細: 出稿オーダー(線引きの本体) ----
CREATE TABLE spot_order (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    contract_id  bigint NOT NULL REFERENCES ad_contract(id) ON DELETE CASCADE,
    channel_id   bigint NOT NULL REFERENCES channel(id),  -- 出稿先ch(線引き照合・単価根拠)
    period_start date NOT NULL,
    period_end   date NOT NULL,
    target_count int NOT NULL,                   -- 契約本数
    unit_seconds int NOT NULL,                   -- 発注秒数(15/20/30。rate_card のキー)
    unit_price   int NOT NULL,                   -- 1本単価(円)。rate_card から算定した値のスナップショット
    note         text,
    CONSTRAINT chk_spot_period CHECK (period_end >= period_start)
);
CREATE TABLE spot_order_band (                    -- 線引き: 希望時間帯(複数可)
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    spot_order_id bigint NOT NULL REFERENCES spot_order(id) ON DELETE CASCADE,
    dow_mask      int NOT NULL DEFAULT 127 CHECK (dow_mask BETWEEN 1 AND 127),  -- bit0=月..bit6=日
    start_time    time NOT NULL,                  -- 時間帯規約は time_band_rank と同じ
    end_time      time NOT NULL,
    CONSTRAINT chk_band_time CHECK (start_time < end_time)
);
CREATE TABLE spot_order_program (                 -- 指定番組(任意。指定があれば優先割付)
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    spot_order_id bigint NOT NULL REFERENCES spot_order(id) ON DELETE CASCADE,
    series_id     bigint NOT NULL REFERENCES series(id),
    UNIQUE (spot_order_id, series_id)
);
CREATE TABLE spot_order_material (                -- 出稿素材(ローテーション)
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    spot_order_id bigint NOT NULL REFERENCES spot_order(id) ON DELETE CASCADE,
    seq           int NOT NULL,
    cm_asset_id   bigint NOT NULL REFERENCES cm_creative(asset_id),
    UNIQUE (spot_order_id, seq)
);

-- ---- 割付⇔契約の対応(S6: sales 側が持つことで依存方向を保つ) ----
-- 割付理由(match_kind)を保存する: spot_order_id だけでは指定番組(program)と線引き(band)を
-- 区別できない(同一 spot_order が両方を持ち得る)ため、provider の persisted フックで確定した
-- 割付理由を記録する。契約外フリー素材は placement を作らない(airing 側で両 NULL)。
CREATE TYPE placement_match AS ENUM ('sponsorship','program','band');  -- 優先順位 1/2/3
CREATE TABLE placement (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ad_break_item_id  bigint NOT NULL UNIQUE REFERENCES ad_break_item(id) ON DELETE CASCADE,
    spot_order_id     bigint REFERENCES spot_order(id),
    sponsorship_id    bigint REFERENCES sponsorship(id),
    match_kind        placement_match NOT NULL,   -- 割付理由(提供/指定番組/線引き)。割付ビューの帰属表示の正
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_placement_one CHECK (num_nonnulls(spot_order_id, sponsorship_id) = 1),
    CONSTRAINT chk_placement_match CHECK (
        (match_kind='sponsorship') = (sponsorship_id IS NOT NULL)   -- sponsorship 帰属 ⇔ sponsorship_id
    )
);
CREATE INDEX idx_placement_order       ON placement(spot_order_id);
CREATE INDEX idx_placement_sponsorship ON placement(sponsorship_id);

-- ---- 放確台帳(確定放送実績。1行 = CM 1本の確定オンエア) ----
-- 録画 CM (play_cm) は bundle_seq=0、生番組バンドル (play_cm_bundle) は CmBundleItem.seq (S12)。
-- program/title/duration はスナップショット (program 削除後も確認書を再現できる)。
CREATE TABLE airing (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    playout_event_id bigint NOT NULL REFERENCES playout_event(id),
    bundle_seq       int NOT NULL DEFAULT 0,
    channel_id       bigint NOT NULL REFERENCES channel(id),
    cm_asset_id      bigint NOT NULL REFERENCES cm_creative(asset_id),
    spot_order_id    bigint REFERENCES spot_order(id),     -- placement 経由で確定
    sponsorship_id   bigint REFERENCES sponsorship(id),
    program_id       bigint REFERENCES program(id) ON DELETE SET NULL,
    program_title    text,                                 -- スナップショット
    duration_ms      bigint NOT NULL,                      -- 素材秒数スナップショット
    aired_at         timestamptz NOT NULL,                 -- actual_at (NULL時は scheduled_at で補完+印)
    aired_at_estimated boolean NOT NULL DEFAULT false,     -- 補完した場合 true
    billed           boolean NOT NULL DEFAULT false,       -- 月次締めでロック
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (playout_event_id, bundle_seq),
    CONSTRAINT chk_airing_one CHECK (num_nonnulls(spot_order_id, sponsorship_id) <= 1)  -- 両NULL=契約外
);
CREATE INDEX idx_airing_order_period       ON airing(spot_order_id, aired_at);
CREATE INDEX idx_airing_sponsorship_period ON airing(sponsorship_id, aired_at);
CREATE INDEX idx_airing_unbilled           ON airing(aired_at) WHERE NOT billed;

-- ---- 欠送・振替(make-good) ----
CREATE TYPE make_good_status AS ENUM ('open','replaced','credited');
CREATE TABLE make_good (
    id                       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    spot_order_id            bigint REFERENCES spot_order(id),
    sponsorship_id           bigint REFERENCES sponsorship(id),
    missed_playout_event_id  bigint REFERENCES playout_event(id),  -- NULL = 手動起票
    reason                   text,
    status                   make_good_status NOT NULL DEFAULT 'open',
    replacement_airing_id    bigint REFERENCES airing(id),
    created_at               timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_mg_one CHECK (num_nonnulls(spot_order_id, sponsorship_id) = 1)
);
CREATE UNIQUE INDEX uq_mg_missed_event ON make_good(missed_playout_event_id)
    WHERE missed_playout_event_id IS NOT NULL;                     -- 検知の冪等性
CREATE INDEX idx_mg_open ON make_good(status) WHERE status = 'open';
```

### billing app

```sql
-- ---- 月次締め ----
CREATE TABLE billing_period (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    year      int NOT NULL,
    month     int NOT NULL CHECK (month BETWEEN 1 AND 12),
    closed_at timestamptz,                        -- NULL = 未締め
    closed_by bigint REFERENCES auth_user(id),
    UNIQUE (year, month)
);

-- ---- 請求書(S3: インボイス制度対応) ----
-- 関係式: total = subtotal − commission_amount + tax_amount (CHECK で強制)
CREATE TYPE invoice_status AS ENUM ('draft','issued','paid','void');
CREATE TABLE invoice (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_number    text NOT NULL UNIQUE,       -- 連番(例 INV-2026-06-0001)
    period_id         bigint NOT NULL REFERENCES billing_period(id),
    contract_id       bigint NOT NULL REFERENCES ad_contract(id),
    bill_to_agency    boolean NOT NULL,           -- 既定: agency_id IS NOT NULL なら true(発行前に draft 上で個別変更可)
    subtotal          int NOT NULL,               -- 税抜合計(円)。マイナス可(調整のみの赤伝)
    commission_amount int NOT NULL DEFAULT 0,     -- = floor(subtotal × commission_rate / 100)
    tax_rate          numeric(4,2) NOT NULL DEFAULT 10.00,
    tax_amount        int NOT NULL,               -- 税率ごとに1回・切捨て
    total             int NOT NULL,
    status            invoice_status NOT NULL DEFAULT 'draft',
    issued_at         timestamptz,
    pdf_r2_key        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_invoice_total CHECK (total = subtotal - commission_amount + tax_amount)
);
CREATE TABLE invoice_line (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id     bigint NOT NULL REFERENCES invoice(id) ON DELETE CASCADE,
    description    text NOT NULL,                 -- 例「スポットCM 6月分 30本」「番組提供料 6月分」「前月欠送分減額」
    quantity       int NOT NULL,
    unit_price     int NOT NULL,                  -- マイナス可(調整行)
    amount         int NOT NULL,
    spot_order_id  bigint REFERENCES spot_order(id),
    sponsorship_id bigint REFERENCES sponsorship(id),
    make_good_id   bigint REFERENCES make_good(id),  -- 調整行の精算元(billing→sales 方向の FK)
    CONSTRAINT chk_line_one CHECK (num_nonnulls(spot_order_id, sponsorship_id) <= 1)  -- 両NULL=調整行
);
-- credited の二重計上防止: 「未請求の credited」= 有効(非 void)な invoice の invoice_line から
-- make_good_id で参照されていない credited 行。void 時は参照ごと無効になるため再計上対象に戻る。
CREATE INDEX idx_invoice_line_make_good ON invoice_line(make_good_id)
    WHERE make_good_id IS NOT NULL;

-- ---- 放送確認書(発行時に明細を確定保存。program 削除に依らず不変) ----
CREATE TABLE broadcast_certificate (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    period_id   bigint NOT NULL REFERENCES billing_period(id),
    contract_id bigint NOT NULL REFERENCES ad_contract(id),
    detail      jsonb NOT NULL,                   -- 発行時点の airing 明細スナップショット
    pdf_r2_key  text,
    created_by  bigint REFERENCES auth_user(id),
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---- 入金 ----
CREATE TABLE payment (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    invoice_id  bigint NOT NULL REFERENCES invoice(id),
    paid_amount int NOT NULL,
    paid_at     date NOT NULL,
    method      text,                              -- 振込等
    note        text
);
```

## 契約駆動の CM 割付（S4・S6）

### fill_break の内部拡張（sales 非依存）

provider 対応の前提として、`fill_break` 自体を次のとおり拡張する（provider 未使用でも有効）：

- **シグネチャ**：`fill_break(br, air_at, provider=None)`。`air_at` は emit_recorded が管理する
  実放送時刻カーソル（program.start_at ＋ 先行本編・先行ブレーク実尺の累積）。線引き・タイムランクの
  時間帯判定はこの値を使う（`start_at + offset_ms` 近似では先行 CM 尺ぶんズレるため）。
  時間帯境界をまたぐ枠は**枠の開始時刻**で判定する。
- **残尺ベース選定**：現行の「n_slots 固定×尺=grid 厳密一致」を、`remaining_ms` を管理し
  「`duration_ms % unit == 0` かつ `duration_ms <= remaining_ms`」の候補を選ぶ方式へ変更
  （30 秒素材は 15s 枠の 2 スロット分を消費）。充填は**長尺優先**（残尺の断片化を防ぐ）、
  埋まらない残尺は現行どおり PLAY_FILLER。
- **PLAY_CM イベントへの `ad_break_item_id` 設定**（S10）：fill_break は `CmCreative` のリスト
  ではなく `(AdBreakItem, CmCreative)` ペアを返し、emit_recorded が playout_event に
  ad_break_item FK を載せる。

### constraint provider インタフェース

```python
# settings.py
ICSTV_FILL_CONSTRAINT_PROVIDER = "sales.constraints.ContractConstraintProvider"
```

provider は **resolve 1 実行 = 1 インスタンス**（`resolve()` が import_string で生成し
fill_break へ引き渡す）。インスタンス内に「この実行で予約済みの本数」を保持し、複数ブレークに
またがる残本数の動的減算を行う（target_count 超過割付の防止）。フックは 3 つ：

1. `candidates(ad_break, air_at, base_qs) -> 順序付き候補列` — 契約条件で絞り込み・優先順位付け。
2. `accept(ad_break, chosen, candidate) -> bool` — 枠内検証（業種競合等）。False なら次候補へ。
3. `persisted(ad_break, items: list[AdBreakItem])` — `bulk_create` 直後（PostgreSQL は
   bulk_create が PK を返す）に呼ばれ、provider はここで **placement を一括 INSERT** する。
   このとき各 item の**割付理由 `match_kind`（sponsorship/program/band）を記録**する（候補を
   どの優先順位で採用したかは candidates 段で確定済み）。item の永続化と placement 記録は同一
   `transaction.atomic` で括る。割付ビュー（[ui-sales.md](ui-sales.md)）の帰属表示はこの
   `placement.match_kind` を正とする（spot_order_id だけでは program/band を区別できないため）。

### 割付の優先順位

1. **提供CM（sponsorship）** — `program.series_id` に有効な sponsorship があれば、その素材を
   提供秒数分だけ最優先で割付（指定番組割付の必須形）。
2. **指定番組スポット** — `spot_order_program` が当該 series を指す order。
3. **線引き合致スポット** — `spot_order_band` が当該枠の `air_at`（dow×時間帯）に合致し、
   `spot_order.channel_id` が一致する order。同率なら**消化率（確定 airing ＋未送出 placement /
   target_count）が低い順**で選定し、納期内消化を平準化。
4. **契約外フリー素材** — 現行の均等ローテ（aired_count 昇順、campaign 期間・max_airings 条件）。
5. 埋まらなければ現行どおり PLAY_FILLER で残尺充填。

### 制約（candidates / accept フック）

- **考査ゲート**（S8）：優先順位 1〜3（契約由来候補）に対してのみ、`advertiser.screening_status`
  と `cm_creative.screening_status` がともに approved であることを要求。**優先順位 4 の契約外
  フリー素材には適用しない**（現行条件のまま）— provider 導入時に既存素材が全滅して全枠フィラー
  落ちする事故を構造的に防ぐ。
- **業種競合・同一枠**：同一 ad_break 内に同じ `industry` の CM を 2 本入れない
  （fill ループの `chosen_industries` セットで判定。業種は `cm_advertiser_link` → advertiser →
  industry で引く）。
- **業種競合・隣接枠**：同一番組内の前後 ad_break、および番組境界をまたいで連続するブレークの
  先頭/末尾と同業種が連続しないこと。前後の ad_break_item を引いて判定する。実装コストが
  高いため Phase 内で最後に着手（S4 補足）。
- **提供番組の競合排除**：sponsorship のある番組の枠には、**提供主と同業種かつ提供主以外の
  広告主**のスポットを入れない（提供主自身の追加スポット出稿は許容する）。

### 消化と再割付（S11）

- **残本数の定義**：`target_count − 確定 airing 数 − 未送出 placement 数`。
  「未送出 placement」= 対応する playout_event（`ad_break_item_id` で結合）のうち
  status が scheduled / executing のものが存在する placement。failed / skipped / cancelled
  しか残っていない placement は数えない（欠送分が引かれ続ける二重控除を防ぎ、
  「open な make_good は残本数に自動で戻る」を成立させる）。
- **fill_break の既存 item 再利用（決定性）はそのまま**。したがって契約の新規登録・変更・考査
  rejected 化は、充填済みの枠（解決窓は常に 48h 先まで充填済み）には自動では反映されない。
- **再充填パス**：契約登録/変更/考査変更時に `sales.tasks.refill_window` を発火し、影響範囲の
  ad_break について「紐づく playout_event がすべて SCHEDULED（未実行）」であれば
  ad_break_item を削除（placement は CASCADE で消える）→ 次回 resolve が再充填する。
  EXECUTING / DONE を含む枠は不変。**フォールバック仕様として「契約は登録から最大 48h 後の
  枠から効く」**を営業 UI に明示する（refill が確実に走れば実際はすぐ反映される）。

## 放送管理：放確・欠送・振替（S7・S10・S12）

- **放確台帳の記録**：`playout/grpc_service.apply_result()` の「初回 DONE」判定（aired_count
  増分と同じ箇所）で、`transaction.on_commit(lambda: record_airing.delay(ev.id))` を登録する
  （atomic ＋ select_for_update の内側で直接 `.delay()` を呼ぶと、commit 前実行 race・
  rollback 時の phantom 発火・ロック保持中の broker I/O の 3 点で危険）。
  - `record_airing` は `playout_event.ad_break_item_id` → `placement` の経路で契約帰属を
    **確定的に**逆引きし、`airing` を upsert（`(playout_event_id, bundle_seq)` UNIQUE で冪等）。
  - `action=play_cm_bundle` の DONE では `CmBundleItem` を展開して**バンドル内の CM 1 本ごとに**
    airing を起こす（S12）。`aired_at` はイベント actual_at ＋ バンドル内尺オフセットで近似。
    バンドル CM の契約帰属は sponsorship_material / spot_order_material との素材一致で解決し、
    一意に決まらない場合は契約外（両 NULL）として記録＋警告。
  - `actual_at` が NULL の DONE（agent 実装上あり得る）は `scheduled_at` で補完し
    `aired_at_estimated=true` を立てる（NOT NULL 違反の poison task を避ける）。
- **日次リコンサイル**：`reconcile_airings`（日次）— DONE なのに airing が無い play_cm /
  play_cm_bundle を走査して record_airing を再発火（broker 不達の回収。冪等なので安全）。
- **欠送検知**：`detect_missed_airings`（時間毎）— **failed / skipped のみ**を対象に、placement
  済みイベントから `make_good(open)` を get_or_create（`missed_playout_event_id` の部分 UNIQUE で
  冪等）。**CANCELLED は対象外**：idempotency_key は scheduled_at から決定的に算出されるため、
  番組の開始時刻を動かしただけで旧イベントが一斉 CANCELLED ＋ 同内容の新イベントが新キーで
  生成されるのが正常動作であり、これを欠送扱いすると誤検知が量産される。編成削除等による
  真の欠送は**期末リコンサイル**（線引き期間終了時に消化数 < target_count を検出して通知）で拾う。
- **振替**：open な make_good は残本数定義（上記）により自動的に再割付対象へ戻る。代替放送が
  確定したら `replaced`（replacement_airing_id を記録）、減額対応なら `credited` にして請求側で
  調整行を起こす。タイム契約（sponsorship）の欠送も記録対象とするが、monthly_fee 固定のため
  金銭処理は原則行わず、減額が必要な場合のみ credited → 調整行で対応する。

## 請求・精算（S3）

### 月次締めフロー

```
close_month(year, month):
  0. 事前チェック: (a) 当月の play_cm/play_cm_bundle に status=scheduled/executing (as-run 未返送)
     が残っていれば警告して中断可能 (store-and-forward の遅延を考慮)。
     (b) sponsorship の有効期間が series の終了 (series_slot.effective_to 超過 or is_active=false)
     と矛盾していれば警告 (改編で番組が終わったのに提供料請求が走り続けるのを防ぐ。
     対処は sponsorship.period_end の手動短縮)
  1. billing_period を作成
  2. 締め対象 = airing (billed=false AND aired_at <= 当月末) をロック → billed=true
     ※「∈ 当月」ではなく「<= 当月末」: 締め後に遅延出現した過月実績を翌月で必ず回収する
  3. 契約ごとに集計:
       スポット: airing 数 × spot_order.unit_price (過月分は「前月分」明細行を分けて計上)
       タイム  : sponsorship.monthly_fee × (月内の有効日数 / 当月暦日数) で日割り
                 (有効期間 = sponsorship.period_start/end、NULL は契約期間に従う)
  4. broadcast_certificate を契約×月で生成。明細 (日時/番組タイトル/素材/秒数) は airing の
     スナップショット列から detail (jsonb) へ確定保存
  5. invoice を draft で生成:
       bill_to_agency 既定 = (contract.agency_id IS NOT NULL)。true ⇒ agency 必須 (アプリ検証)
       commission_amount = floor(subtotal × agency.commission_rate / 100)
       tax_amount = floor((subtotal − commission_amount) × tax_rate / 100)   -- 税率ごとに1回・切捨て
       total = subtotal − commission_amount + tax_amount                      -- CHECK で強制
       未請求の make_good(credited) (= 有効な invoice の invoice_line.make_good_id から
       参照されていないもの) を持つ契約は、当月 airing がゼロでも調整行のみの
       invoice (マイナス total 可 = 赤伝相当) を生成し、調整行に make_good_id をセットする
  6. 内部 admin が確認して issued へ → PDF を R2 に保存・送付
  7. 入金登録 (payment) で消込。全額一致で paid。振込手数料相当の許容差額
     (ICSTV_PAYMENT_TOLERANCE、既定 1000 円) 以内の不足は paid とし差額を note に記録。
     それ以外は手動で paid 確定可能とする
```

- **インボイス制度**：請求書 PDF に自社の適格請求書発行事業者登録番号（`ICSTV_INVOICE_REG_NO`、
  settings）・税率別合計・消費税額を記載。
- **締めの不変性**：`billed=true` の airing と issued 済み invoice は変更不可。再発行は void →
  新規発行とし、再生成は「billing_period × contract の billed 済み airing から再集計する独立関数
  （ロック処理を伴わない）」で行う。`make_good(credited)` の減額は翌月の調整行（マイナス
  invoice_line、契約 FK は両 NULL の調整行も可）で表現する。

## 考査（S8）

- **業態考査** = `advertiser.screening_status`。広告主の初回登録時に internal_admin が審査。
- **表現考査** = `cm_creative.screening_status`（medialib 列）。素材ごとに審査。
- **適用範囲**：考査ゲートは**契約由来の割付（優先順位 1〜3）のみ**。契約外フリー素材は対象外。
- **バックフィル**：data migration で既存の cm_creative（および移行で自動生成する advertiser）は
  `approved`（みなし考査済）に初期化する。以後の新規のみ pending から運用。
- **#5 との接続**：delivery ポータルの CM 納品パス（#5 で保留）を解除する。
  `delivery_file(purpose=broadcast, intended_kind=cm)` の承認画面で advertiser（FK 選択）・
  grid・campaign を入力して `cm_creative` ＋ `cm_advertiser_link` を生成し、表現考査は delivery の
  人手QC と同一画面で行う（QC verdict と考査 verdict は別フィールド）。

## CmCreative の移行

- `CmCreative.advertiser`（自由テキスト）は**残置**（表示用・resolver の params 用）。FK 化は
  medialib → sales の依存を作り S6 に反するため行わない。広告主の正規紐付けは sales 側の
  `cm_advertiser_link` が持ち、data migration で既存の advertiser 文字列から advertiser 行と
  link を自動生成する（industry は「未分類」を初期値に手で整備）。
- `campaign_start/end`・`max_airings` は**当面残置**（契約外フリー素材の現行ローテで使用）。
  契約に紐づく素材は spot_order/sponsorship 側の条件が優先される。二重管理を避けるため、
  Phase B 完了後に新規 CM は契約経由のみとする運用へ移行。

## UI（studio React SPA + 一部旧 Django テンプレ）

2026-09 時点の実体（旧「UI（HTMX・社内向け）」節を実装正へ更新。画面詳細は [ui-sales.md](ui-sales.md) 冒頭の実装状況表）:

- **割付ビュー（実装済）**：studio SPA `/sales`（SalesPage）。日別の ad_break × 割付結果（どの契約の
  消化か）に加え、**未送出枠の CM 入替（swap）ドロップダウンで手動差し替え可**
  （`/sales/items/<id>/swap/`。送出済は S11 不変で 409）。
- **線引きエディタ（旧 Django テンプレ据え置き）**：spot_order の曜日×時間帯グリッドは
  band_editor テンプレ（`/sales/orders/<id>/bands/`）へ委譲。
- **請求（実装済）**：studio SPA `/billing`（BillingPage）＋ `billing.services`（月次締め →
  確認書/請求書 draft → 発行 → 入金登録）。帳票 PDF は WeasyPrint。出力履歴は billing 側
  テーブル（pdf_r2_key）に記録。
- **契約・マスタ（専用画面は未実装）**：ad_contract、advertiser/agency/industry/rate_card/
  time_band_rank は Django admin で管理。営業ダッシュボード・契約編集・放確/欠送一覧の
  専用画面は未実装（設計案は [ui-sales.md](ui-sales.md)）。

## Celery タスク追加

| タスク | 周期/契機 | 内容 |
|---|---|---|
| `sales.record_airing` | apply_result の on_commit から都度 | playout_event → placement 逆引き → airing upsert（冪等） |
| `sales.reconcile_airings` | 日次 | DONE かつ airing 不在の play_cm/play_cm_bundle を走査し record_airing 再発火 |
| `sales.refill_window` | 契約/考査の変更時 | 未実行の枠の ad_break_item を削除し次回 resolve で再充填（S11） |
| `sales.detect_missed_airings` | 時間毎 | failed/skipped の placement 済イベント → make_good を get_or_create |
| `sales.reconcile_orders` | 日次 | 線引き期間が終了した order の消化数 < target_count を検出して通知（期末リコンサイル） |
| `billing.close_month` | 手動トリガ（月初） | 月次締め・確認書/請求書 draft 生成 |
| `scheduling.expand_series_slots` | 週次 | series_slot → N 週先まで Program 展開（行単位 savepoint で衝突スキップ）。**実装済**（`scheduling/tasks.py`。旧記載の「Phase C」は完了） |

## Phase 切り（実装状況付き・2026-09 実査）

- **Phase A（営業基盤＋契約駆動割付）** — **済**：
  - series マスタ＋`program.series_id`（**最小版を前倒し**。series_slot/展開は Phase C）
  - `playout_event.ad_break_item_id` FK ＋ fill_break の残尺ベース化・`(item, cm)` ペア返却・
    `air_at`/`provider` 引数（scheduling 内部拡張）
  - industry/advertiser/agency/rate_card/time_band_rank マスタ、ad_contract＋spot_order
    （線引き・指定番組・素材）、cm_advertiser_link（既存 advertiser 文字列から移行）
  - constraint provider（考査ゲート・線引き・業種同一枠・指定番組）＋ placement＋refill_window
  - airing 台帳（apply_result on_commit フック・play_cm_bundle 展開・reconcile_airings）
- **Phase B（請求・精算フル）** — **概ね済**：billing app 一式（月次締め・放送確認書・請求書 PDF・入金）、
  欠送検知・make_good・期末リコンサイル、月次締め UI（BillingPage）は実装済み。
  **営業ダッシュボード UI のみ未実装**（Django admin 代替）。
- **Phase C（タイム契約＋仕上げ）** — **一部済**：series_slot＋expand タスク（編成強化）は**済**
  （`SeriesSlot` は recurrence_kind/exposure_policy まで拡張済み・studio「週間編成 /week」「配信枠 /slots」）、
  **割付手動差し替え（swap）も済**。sponsorship（提供）はモデル/割付優先度実装済み。
  残: 隣接枠の業種競合排除の深掘り、提供クレジット CG 連動（casparcg.md §3）、
  線引きエディタの作り込み（旧テンプレ据え置き）。考査の delivery 統合は納品の
  別リポ移管（[delivery.md](delivery.md) 冒頭）により前提が変わったため設計見直し対象。

## 未確定・論点

- スポット単価は order 単位スナップショット（S9）。ランク別実績単価（airing 時点の time_band_rank で
  単価を変える方式）は請求の複雑度が上がるため見送り。需要が出たら invoice_line の集計軸を増やす。
- 視聴データ：GRP の代替として YouTube Analytics / CF Stream 視聴数を確認書に併記する案は Phase 2+。
- EDI（広告EDIセンター）は対象外（取引先が放送業界標準を使う規模ではない）。
- series 展開と手動編成の衝突ポリシー：現状「手動優先・展開スキップ」。逆（パターン優先）が
  必要になったら series_slot に優先フラグを足す。
- 隣接枠競合の判定範囲：同一番組内＋直近のステブレまでとし、番組をまたぐ深い探索はしない。
- time_band_rank の同一 channel×dow 内の時間帯重複は当面アプリ層検証（btree_gist による
  EXCLUDE 化は分単位 int4range への変換が要るため、必要になったら導入）。
- 入金消込の自動化（銀行明細取込）は対象外。手動登録のみ。
- 提供クレジットの表示形態（画面表示/読み上げ・秒数による出し分け）は CG テンプレ設計
  （casparcg.md §3）側の論点として送る。
- バンドル CM（生番組）の契約帰属は素材一致による解決のため、同一素材が複数契約に載る場合は
  契約外扱い＋警告となる。厳密化が必要なら cm_bundle_item に契約 FK を足す（Phase C 以降）。
