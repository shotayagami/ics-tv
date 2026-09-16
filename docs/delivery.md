# #5 納品ポータル（メディアブランチ相当）

> ステータス: **本サブシステムは別リポ ICS-DELIVERY（`~/icstv-delivery`、Node/TS・別 Postgres・
> deliver-new.<内部ドメイン>・harbor `icstv/delivery`）へ完全移管済み**（2026-07-08、リファクタ
> Phase 3.9 Stage G/H）。**本書は歴史的記録**。ICS-TV に残るのは内部 seam
> （`/api/v1/internal/delivery-asset`・`/api/v1/internal/delivery-refs`、`server/api/routers/internal.py`）
> と ghost app `server/delivery`（`migrations/0006_drop_delivery_tables.py` で旧 7 テーブルを
> DROP 済み・モデル無し）のみ。現行の納品仕様は ICS-DELIVERY リポジトリを、分離の設計は
> [delivery-service-split.md](delivery-service-split.md) を参照。本書中の DDL・画面・RBAC は移管前の
> 設計であり ICS-TV の DB/コードには存在しない。`server/delivery/portal.py`・`admin_portal.py`・
> `auth.py` などへの参照も**撤去済みファイルへの死参照**。決定ログ D1〜D11 や QC/ラウドネス基準は
> ICS-DELIVERY 側へ移植された設計の原典として読み替えること。

制作側が完パケ素材を**オンラインで納品**し、**プレビュー・QC・納品状況の一元可視化・キューシート出力**まで
を行うサブシステム。TBS テレビの「オンライン完パケ納品システム メディアブランチ」（2023 民放連賞・技術部門
優秀賞）の主機能を参考に、icstv の素材パイプライン（[overview.md](overview.md) §3.2）の **入口** として設計する。

本サブシステムは既存 `asset` / 正規化パイプライン（[datamodel.md](datamodel.md) / `medialib/normalize.py`）の
**手前**に位置し、出口（Asset→正規化→R2→送出）は二重実装せず再利用する。

## 位置づけ（現状との差分）

現状の素材フローは「admin が手で `asset(source_path)` を作る → `post_save` で正規化 → R2 → `ready` →
playout agent が clip 名規約で引く」。**制作側がオンライン納品し、QC して、納品状況を可視化する
ワークフローが無い**（人手でレコードを起こしている）。これがメディアブランチ相当の不足分。

```
[制作会社/社内]              [納品ポータル(本書)]                 [既存 medialib]            [送出]
 ブラウザ ──presigned PUT──▶ Delivery / DeliveryFile(R2 quarantine)
                              └ run_qc (qc キュー)
                                  技術/ラウドネス/リーダー → QcReport
                              └ 人手QC(合否)
                                  承認 ──purpose 分岐──▶ broadcast/distribution: Asset 生成 ─▶ normalize ─▶ playout
                                                        promo/sales/archive : 保管/署名DLリンク
```

## このサブシステムの決定ログ

| # | 論点 | 決定 | 補足 |
|---|------|------|------|
| D1 | 納品ユーザ | **外部制作会社も**対応 | 組織スコープ RBAC ＋ admin と分離した専用ポータル |
| D2 | 対象素材 | **全種**（本編/配信/番宣/番販/保管） | `delivery_file.purpose` 軸で用途横断の一元管理 |
| D3 | QC 深さ | **技術 + R128ラウドネス + リーダー検出** | ffprobe / ebur128 / blackdetect・silencedetect・1kHzトーン |
| D4 | ラウドネス整合 | **mezzanine で強制統一** | `normalize.py` に `loudnorm` 2-pass 追加。QC は測定・警告に専念 |
| D5 | ラウドネス目標 | **-14 LUFS / TP ≤ -1.5 dBTP** | 地上波(-24 LKFS)ではなく**配信基準**。全 kind に一律適用し継ぎ目の一貫性を機械保証 |
| D6 | カラーバー判定 | **リーダー検出のみ**（MVP） | 視覚的 SMPTE バー色判定は誤検知が多く費用対効果が薄い → Phase B |
| D7 | 番販 outbound | **保管＋期限付き署名DLリンク** | 買い手ポータル/ライセンス管理は Phase 2 |
| D8 | 納品の対象定義 | **`target_kind`（series_episode / oneoff / cm）で「どのチャンネル/どの番組(回)/どこが/いつ」を記録**（決定#19） | 週間編成番組の特定回は専用品。回を第一級 `Episode` モデルに昇格し納品が指す |
| D9 | 回(エピソード)の表現 | **`scheduling.Episode`（series×回数×放送予定日）。回=コンテンツで放映ではない（再放送は 1 Episode→複数 Program）** | air_date は予定/初回の情報で unique でない。`episode_no` は自動採番しない。承認本編で `Episode.asset` 確定 |
| D10 | 確定素材の編成反映 | **`Episode.asset` を `Program.asset` に載せる（proto/agent 無改修）。展開時優先採用＋展開済みは正規化後バックフィル** | 差替は no-op でない: `end_at`/`ad_break` を再計算し EXCLUDE 衝突は通知（`server/scheduling/tasks.py` `apply_episode_asset`） |
| D11 | CM の枠指定 | **枠指定あり=sales 契約（spot_order / sponsorship のいずれか一方）に紐付、無指定=一般CMプール** | 広告主/グリッドは承認時に入力し `cm_creative` 起こし（既存 `_approve_cm`）。`chk_delivery_cm_one` で両指定を排除 |

## アーキ上の不変条件（崩さない）

- **proto / agent は無改修**。agent は `ready` な Asset の clip 名規約しか見ない。本サブシステムは
  コントロールプレーン内（Django）で完結する。
- 出口（`asset`→`normalize`→R2 mezzanine）は再利用。R2 クライアント（`medialib/normalize.py` の
  `_r2_client()`）は `core` へ切り出して共用する。
- 5 apps の境界＝将来のサービス抽出シーム（[overview.md](overview.md) 決定#14）に倣い、**独立 app `delivery`** とする。
- UI は確定スタック **Django + HTMX**（[ui.md](ui.md)）に合わせる。納品ポータルは Django admin と URL/認証を分離。

## データモデル（PostgreSQL DDL）

```sql
-- ---- 列挙 ----
CREATE TYPE delivery_purpose AS ENUM ('broadcast','distribution','promo','sales','archive');
CREATE TYPE delivery_status  AS ENUM ('draft','uploading','uploaded','qc_processing','qc_failed','qc_passed','approved','rejected');
CREATE TYPE upload_state     AS ENUM ('pending','uploading','complete','aborted');
CREATE TYPE account_role     AS ENUM ('external','internal_qc','internal_admin');
CREATE TYPE qc_verdict       AS ENUM ('ok','warn','ng');
CREATE TYPE sheet_kind       AS ENUM ('cue_sheet','record_sheet');

-- ---- 外部制作会社(組織) ----
CREATE TABLE production_company (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text NOT NULL,
    contact_email text,
    is_active     boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- ---- アカウント(User ⇔ 組織/ロール) ----
CREATE TABLE delivery_account (
    user_id    bigint PRIMARY KEY REFERENCES auth_user(id) ON DELETE CASCADE,
    company_id bigint REFERENCES production_company(id),  -- NULL = 社内
    role       account_role NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_role_company CHECK ((role = 'external') = (company_id IS NOT NULL))
);

-- ---- 納品ヘッダ(番組単位 or 単発) ----
CREATE TABLE delivery (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id   bigint REFERENCES production_company(id),  -- owner(行スコープの基点)。NULL = 社内制作
    program_id   bigint REFERENCES program(id),             -- 営放/番組情報紐づけ(任意)
    title        text NOT NULL,
    due_date     date,
    delivered_by bigint REFERENCES auth_user(id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
-- delivery はグルーピングに徹し、status は delivery_file 側に持つ（QC/承認はファイル単位）
CREATE INDEX idx_delivery_company ON delivery(company_id);
CREATE INDEX idx_delivery_program ON delivery(program_id);

-- ---- 納品ファイル(1納品に複数。用途ごとの成果物) ----
CREATE TABLE delivery_file (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    delivery_id       bigint NOT NULL REFERENCES delivery(id) ON DELETE CASCADE,
    purpose           delivery_purpose NOT NULL,
    version           int NOT NULL DEFAULT 1,      -- 再納品で +1。現行 = (delivery,purpose) 内の最大 version
    status            delivery_status NOT NULL DEFAULT 'draft',  -- QC/承認はファイル単位
    intended_kind     asset_kind,                  -- broadcast/distribution のとき Asset.kind に写像
    r2_key            text,                        -- quarantine 領域の納品オブジェクト
    original_filename text NOT NULL,
    size_bytes        bigint,
    checksum          text,                        -- クライアント計算 SHA256。完了時サーバ再計算で照合
    upload_state      upload_state NOT NULL DEFAULT 'pending',
    asset_id          bigint REFERENCES asset(id), -- 承認後(kind=program)に生成された Asset
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (delivery_id, purpose, version)
);
CREATE INDEX idx_delivery_file_delivery ON delivery_file(delivery_id);
CREATE INDEX idx_delivery_file_status   ON delivery_file(status);

-- ---- QC レポート(DeliveryFile に原則 1:1。再納品は新 version の delivery_file になる) ----
CREATE TABLE qc_report (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    delivery_file_id bigint NOT NULL REFERENCES delivery_file(id) ON DELETE CASCADE,
    auto_result     jsonb NOT NULL DEFAULT '{}',   -- ffprobe/ebur128/black/silence/tone の生結果
    auto_verdict    qc_verdict NOT NULL,
    loudness_i      numeric(6,2),                  -- Integrated LUFS(原音実測)
    loudness_lra    numeric(6,2),
    true_peak       numeric(6,2),                  -- dBTP(原音実測)
    reviewer_id     bigint REFERENCES auth_user(id),
    human_verdict   qc_verdict,                    -- NULL = 未レビュー
    comment         text,
    reviewed_at     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_qc_report_file ON qc_report(delivery_file_id);

-- ---- キューシート/記録表 出力履歴(生成物の記録) ----
CREATE TABLE sheet_export (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    program_id bigint NOT NULL REFERENCES program(id) ON DELETE CASCADE,
    kind       sheet_kind NOT NULL,
    r2_key     text,                               -- 生成 PDF/CSV
    created_by bigint REFERENCES auth_user(id),
    created_at timestamptz NOT NULL DEFAULT now()
);
```

### 納品ターゲティング（決定#19・D8-D11）

回(エピソード)を第一級モデル `scheduling.Episode` に昇格し、納品がそれを指す。`delivery` に
ターゲット列を追加する（既存の `delivery` テーブルへの増分）。

```sql
-- ---- 回(エピソード) = scheduling app。納品の主ターゲット ----
CREATE TYPE episode_status AS ENUM ('planned','confirmed','aired');

CREATE TABLE episode (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    series_id   bigint NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    episode_no  int,                                  -- 回数(人/納品が付与・非自動採番)
    air_date    date,                                 -- 放送予定日/初回(情報・非unique)
    title       text NOT NULL DEFAULT '',
    asset_id    bigint REFERENCES asset(id),          -- 承認本編で確定する回別素材(ON DELETE RESTRICT)
    status      episode_status NOT NULL DEFAULT 'planned',
    created_at  timestamptz NOT NULL DEFAULT now()
);
-- 回数は series 内で一意(未採番=NULL は複数可)。再放送は 1 Episode → 複数 Program。
CREATE UNIQUE INDEX uq_episode_series_no ON episode(series_id, episode_no) WHERE episode_no IS NOT NULL;
CREATE INDEX idx_episode_series_date ON episode(series_id, air_date);

-- program に「この放映が表す回」を追加 (再放送は同一 Episode を複数 Program が指す)
ALTER TABLE program ADD COLUMN episode_id bigint REFERENCES episode(id) ON DELETE SET NULL;

-- delivery にターゲット列を追加 (channel=どのチャンネル / episode=どの番組のどの回 / company=どこが / created_at=いつ)
ALTER TABLE delivery
    ADD COLUMN target_kind text NOT NULL DEFAULT 'oneoff',   -- series_episode | oneoff | cm
    ADD COLUMN channel_id     bigint REFERENCES channel(id),
    ADD COLUMN episode_id     bigint REFERENCES episode(id)     ON DELETE SET NULL,
    ADD COLUMN spot_order_id  bigint REFERENCES spot_order(id)  ON DELETE SET NULL,  -- CM 枠指定(スポット)
    ADD COLUMN sponsorship_id bigint REFERENCES sponsorship(id) ON DELETE SET NULL;  -- CM 枠指定(提供)
-- CM 枠指定は spot_order/sponsorship のたかだか一方 (両NULL=一般CMプール)
ALTER TABLE delivery ADD CONSTRAINT chk_delivery_cm_one
    CHECK (NOT (spot_order_id IS NOT NULL AND sponsorship_id IS NOT NULL));
CREATE INDEX idx_delivery_episode     ON delivery(episode_id);
CREATE INDEX idx_delivery_target_kind ON delivery(target_kind);
```

| target_kind | 意味 | 主ターゲット列 |
|---|---|---|
| `series_episode` | 週間編成番組の特定回 | `episode`（→series→channel を自動設定）。本編承認で `Episode.asset` 確定 |
| `oneoff` | 単発番組の突発納品 | `channel` + `title`（決まっていれば `program` を紐付け） |
| `cm` | CM | `channel` ＋（枠指定あり）`spot_order` / `sponsorship`、無指定=一般CMプール |

**確定素材の編成反映フロー**：本編(BROADCAST)承認 → `Asset(PENDING)` 生成＋`Episode.asset` 確定
（`server/delivery/portal.py` `_confirm_episode_asset`）→ 正規化(READY) → ①未展開なら
次回 `expand_series_slots` が `default_asset` より優先採用、②展開済みなら post_save シグナルが
`apply_episode_asset`（`server/scheduling/tasks.py`）を投入し、当該回の未来 Program の
`asset`/`end_at`/`ad_break` を作り直して `resolve_channel_now` で再解決。新尺が後続に食い込む場合は
EXCLUDE 衝突を捕捉して差替を見送り通知（握り潰さない）。**proto / agent は無改修**。

### purpose による下流分岐（D2 の肝）

承認（`approved`）時、`delivery_file.purpose` で振る舞いが変わる。**「納品＝必ず Asset 化」ではない**。

| purpose | 承認後の振る舞い |
|---|---|
| `broadcast` / `distribution`（`intended_kind=program`） | `asset(kind=program)` を生成し `delivery_file.asset_id` に紐付け → 既存 `normalize` → R2 mezzanine → playout が引く |
| `promo`（番宣） | Asset 化は任意。基本は保管＋手動利用（SNS/番宣枠） |
| `sales`（番販） | **playout に流さない**。保管＋内部 admin が期限付き署名DLリンクを発行して買い手に手渡し（D7） |
| `archive` | 保管のみ |

> **MVP は Asset 生成パスを `intended_kind=program` に限定**。CM の Asset 化は `cm_creative`
> （advertiser/grid/campaign）の入力経路が必要なため、CM 出稿管理と一緒に設計する。それまで
> CM/番宣/番販/保管は「管理・可視化・保管」まで。→ **この保留は [sales.md](sales.md)（#6 営放
> サブシステム、決定 S8）で解除**：承認画面で advertiser FK/grid/campaign を入力して `cm_creative`
> を生成し、表現考査を人手QCと同一画面に統合する。

## 状態機械（`delivery_file` 単位）

```
draft ──(ファイル登録)──▶ uploading ──(全 presigned 完了)──▶ uploaded ──▶ qc_processing
                              ▲                                                  │
                              │                              ┌── qc_failed ──────┤ (auto_verdict=ng)
                       (再納品)│                              │   (再納品)         │
                              └──────────────────────────────┘                   ▼
                                                                    qc_passed (auto_verdict=ok|warn)
                                                                        │  人手QC
                                                          rejected ◀────┼────▶ approved ──[purpose 分岐]
                                                          (再納品で uploading へ)
```

- `qc_failed`（自動 NG）は承認不可。制作側に差し戻し、再納品で `uploading` へ戻る。
- `qc_passed` は `auto_verdict=ok|warn`。**warn でも人手QCで承認可能**（ラウドネスは mezzanine が吸収するため）。
- 人手QCで `rejected` → 差し戻し。`approved` → purpose 分岐。
- **状態はファイル単位**：1納品内で本編＝`approved`（オンエア済）と番宣＝`qc_processing` が併存しうる。
- **再納品＝新しい `version` の `delivery_file`**（前バージョンは履歴として残置）。原本世代を保全。
- （任意）`auto_verdict=ok` は自動承認も選択可。`warn` のみ人手レビュー必須にすると社内QC負荷を下げられる。

## 認証・権限（D1）

- **ポータルは admin と分離**：`/delivery/` 配下、`login_required`。Django admin は社内運用専用のまま温存。
- **行レベルスコープ**：外部ユーザの全 queryset を `delivery.company_id == request.user.delivery_account.company_id`
  でフィルタ。他社・他番組の納品は一切露出しない。
- **ロール（`account_role`）**：

  | role | できること |
  |---|---|
  | `external` | 自社の納品作成・アップロード・状況/QC結果閲覧・再納品 |
  | `internal_qc` | 全納品の人手QC（合否・コメント） |
  | `internal_admin` | 承認→Asset化・署名DLリンク発行・キューシート出力・組織/アカウント管理 |

- **2 層の権限合成（社内ユーザ）〔歴史的記述〕**：`delivery_account.role` と operator-shell の
  Django グループ（ui.md 5 ロール表）の AND 合成で機微な delivery 操作を判定する初期設計だった。
  **この合成は ICS-TV では実装されないまま移管された**（ICS-TV の管理権限は現在も
  `is_superuser`/`is_staff` の 2 値のみ。[ui.md](ui.md)「ロール（現行実装）」）。納品側の RBAC は
  現行 ICS-DELIVERY の所管であり、本項は移管前の設計案として残す。

- **アップロードのセキュリティ**：presigned PUT は `delivery_file` 単位・短命・content-type/size 上限を
  署名条件で固定。アップロード完了まで quarantine（`upload_state != complete`）、**QC 通過まで承認不可**。
  checksum 照合（クライアント計算 SHA256 をサーバ側で再計算、または R2 の `ChecksumAlgorithm=SHA256`）で
  改竄/破損を検知。**マルチパート ETag は単純ハッシュでないため照合には使わない**。

## アップロード

- ブラウザ → R2 への **presigned マルチパートアップロード**（大容量・レジューム対応）。
- フロー：`delivery_file` 行を `pending` で作成 → サーバが presigned URL（マルチパート）を払い出し →
  クライアントが分割 PUT → 完了通知で `upload_state=complete` → `run_qc.delay(delivery_file_id)`。
- 保存先は quarantine プレフィクス（例 `delivery/inbox/{delivery_id}/{file_id}/...`）。承認後の Asset 化で
  既存 normalize が mezzanine（`mezzanine/{kind}/{asset_id}.mp4`）を別途生成するため、納品原本とは分離。

## QC パイプライン（`qc` キュー）

正規化（`normalize` キュー）と分離した専用キュー。`run_qc(delivery_file_id)`：

1. **技術チェック** — `ffprobe` で 解像度/fps/尺/コーデック/音声トラック有無 → 規定スペックと突合。
2. **ラウドネス** — `ffmpeg -af ebur128=peak=true -f null -` で **Integrated(LUFS)/LRA/True Peak** を取得。
   目標 **-14 LUFS ±1**（|Δ|≤1=ok / 1〜2=warn / 超=warn）、TP **≤ -1.5 dBTP**。`qc_report` に実測を記録。
3. **リーダー検出（D6）** — 冒頭の `blackdetect`・`silencedetect`、加えて音声先頭の **1kHz トーン**を検出し、
   「頭に校正リーダー（バー/黒/トーン）混入の疑い」を **warn**。視覚的バー色判定は Phase B。
4. **判定** — 上記を統合し `auto_verdict`（ok/warn/ng）を確定。技術不適合（解像度/音声無し等）や尺ゼロは **ng**
   で承認ブロック、ラウドネス外れ・リーダー疑いは **warn**（人手判断）。
5. （bonus / Phase B）`freezedetect`（フリーズ）・極端な無音区間。

> **ラウドネスは「測って弾く」ではなく「測って可視化＋警告」**。実際のレベル統一は次節の mezzanine 側で行う。

## ラウドネス正規化（mezzanine 側 / D4・D5）

`medialib/normalize.py` を改修し、mezzanine 化時に **全 kind（program/cm/filler/bumper/slate）へ一律**
`loudnorm` 2-pass を適用する。24/7 リニアチャンネルでは継ぎ目で音量が飛ばないことが最重要のため、
目標値そのものより**全コンポーネントが同一目標に揃っていること**を機械保証する。

- 目標：**Integrated = -14 LUFS / LRA = 11 / True Peak = -1.5 dBTP**（配信基準。YouTube/CF は -14 LUFS 正規化）。
- 手順：
  1. 1-pass目 `loudnorm=I=-14:LRA=11:TP=-1.5:print_format=json -f null -` で measured 値（I/TP/LRA/thresh）取得。
  2. 2-pass目 `loudnorm=I=-14:LRA=11:TP=-1.5:measured_I=..:measured_TP=..:measured_LRA=..:measured_thresh=..:linear=true`。
     `linear=true` で極力リニアゲイン（過圧縮回避）、後段に `-ar 48000` を固定。
- **QC との重複解析を省略可**：QC が同一原本を `ebur128` 測定済みなら、その I/TP/LRA/thresh を `measured_*` に
  流用して 1-pass目を省ける（QC と normalize が同一入力を読む前提。入力が違えば無効）。
- 地上波の -24 LKFS（ARIB TR-B32）は**使わない**。-24 で揃えると YouTube 上で相対的に小さく聞こえる
  （YouTube は -14 より大きい音を下げるが、小さい音は持ち上げないため）。

## キューシート・記録表

`program` ＋ `ad_break`/`ad_break_item`/CM 割付（[datamodel.md](datamodel.md)）から **生成物**として出力。

- **キューシート**：番組内の CM 割付・尺・進行（offset 順）。広告レポート/進行表の根拠。
- **記録表**：納品素材の技術メタ（解像度/fps/尺/ラウドネス実測）と QC 結果のサマリ。
- 出力（PDF/CSV）した履歴を残す場合のみ `sheet_export` に記録。テーブルは出力のキャッシュ/監査用で、
  正は常に編成データ側。

## UI（HTMX）

- **納品ダッシュボード**：`program × purpose` のマトリクスで「未納品 / アップロード中 / QC待ち / QC NG /
  承認済」を一元可視化（メディアブランチの「納品状況の可視化」相当）。外部ユーザは自社行のみ。
- **納品作成 → アップロード**：番組選択（or 単発）→ purpose 指定 → ブラウザ presigned アップロード（進捗）。
- **プレビュー**：R2 上の納品原本/ mezzanine を署名 URL で HTML5 video 再生。
- **QC レビュー（社内）**：`qc_report` の自動結果・ラウドネス実測を表示し、合否・コメント入力 → 承認。
- **番販 DL リンク発行（社内）**：`purpose=sales` に期限付き署名 URL を発行。

## Phase 切り

- **Phase A（MVP）**：`delivery`/`delivery_file`/presigned アップロード + 自動QC（技術+ラウドネス+リーダー）
  + 納品マトリクス可視化 + 人手QC + 承認→Asset化（`intended_kind=program` のみ）。`normalize.py` に loudnorm 追加。
- **Phase B**：プレビュー強化 + キューシート/記録表出力 + 視覚バー判定 + freeze/silence 等 QC 拡張 + 通知。
- **Phase C / Phase 2 連動**：外部買い手ポータル（番販 outbound 本格化・ライセンス管理）、ラウドネス実測に基づく
  目標値の最終確定、マルチチャンネル対応。

## 番組予算（決定#20 P4）

D1 で「内部 admin が組織/アカウント管理」と定めたが、専用画面が無く Django admin 依存だった。
決定#20 では P2（業者・アカウント管理）・P3（Google 招待サインイン）・P4（番組予算）を追加した。
旧 P2（業者・アカウント管理画面 `server/delivery/admin_portal.py`）・旧 P3（Google 招待サインイン
`server/delivery/auth.py`）はいずれも対応コードが ICS-TV に存在せず（ICS-DELIVERY 移管 =
Stage G/H で撤去され完全な死文になっていた）ため、記述を削除した（2026-09-02）。業者管理・招待・
Google サインインの現行実装は ICS-DELIVERY 側（同リポの admin/auth）にあり、設計の経緯は
git 履歴と [delivery-service-split.md](delivery-service-split.md) を参照。P4 番組予算
（`procurement`）も**追加提供側**で、このツリーには含まれない。

### 番組予算（決定#20 P4。新 app `procurement` / 制作費 = 支払 AP）

> 注記: P4 の番組予算は**追加提供側**の機能で、このツリーには含まれない。以下の 3 モデル
> （`ProgramBudget` / `DeliveryCost` / `Payment`）も studio SPA の `/procurement` 画面も無く、
> `server/procurement` はモデルを持たない殻として残るだけ。書き所有権を ICS-BACKOFFICE へ
> 移した経緯（Phase 3.8）は [backoffice-service-split.md](backoffice-service-split.md) §9 を
> 参照。以下は設計当時の記録としてそのまま残す。
- 納品に対する制作会社への**支払**は billing（広告収入=AR）とは別系統。`server/procurement/models.py`：
  - `ProgramBudget`(series × 年度 × 予算額)…Series 単位の予算枠（`uq_program_budget`）。
  - `DeliveryCost`…納品単位の発生費用。**納品サービスの別リポ分離（Phase 3.8）に伴い delivery への
    FK は廃止し、`delivery_id = models.IntegerField()`（ICS-DELIVERY 側 Delivery.pk の id 参照）+
    `delivery_title`/`vendor_id`/`vendor_name` の名称スナップショット方式**（`models.py:43-79` の
    docstring 参照）。予算ロールアップに使う series だけは ICS-TV 本体に残る scheduling への FK のまま。
  - `Payment`(cost, 支払額, 支払日)…複数回払い可。
- studio SPA の「番組予算」`/procurement`（staff 専用）に予算枠ダッシュボード
  （予算/消化Σcost/支払Σpayment/残・超過フラグ）＋費用計上＋支払登録。

## 未確定・論点

- ラウドネス目標 -14 LUFS は配信基準の既定値。**実配信での実測**（YouTube/CF の正規化挙動）で最終確定する。
- 視覚的カラーバー（SMPTE 色配列）判定は Phase B。MVP はリーダーヒューリスティック（黒み/無音/1kHzトーン）止まり。
- `promo`（番宣）を Asset 化して送出系（番宣枠）に載せるか、保管＋手動利用に留めるかは運用を見て決める。
- 番販 outbound は Phase 1 では署名 DL リンクのみ。買い手アカウント/受領確認/透かしは需要が見えたら Phase 2。
- presigned マルチパートの分割サイズ・並列度・レジューム実装方式（クライアント JS）。
- 納品原本の保持期間 → **意図的に永久保持と決定（2026-09-02）**。R2 quarantine への LRU/期限削除は
  導入せず、業者のファイル削除操作（icstv-delivery `portal.ts`）のみが削除経路。
