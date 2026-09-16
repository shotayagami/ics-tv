# 納品サブシステム分離 — ICS-DELIVERY（別リポ Node/TS ＋ 別DB）

> ステータス: **実装完了・本番稼働**（icstv-delivery **v0.5.3**・deliver-new.<内部ドメイン>・
> ArgoCD app `icstv-delivery`。本体 delivery app の撤去 = 2.10 も Stage G/H で完了・§11）。
> 初版 2026-06-25。studio リファクタ Phase 2（[refactor-service-split.md](refactor-service-split.md) §7③・決定②=完全分離）の詳細設計。
> 目的: 納品（B2B 業者ポータル＋アップロード＋QC＋正規化＋進行表）を ICS-TV 本体から**別リポジトリ `icstv-delivery`（Node/TS・専用 Postgres）**へ完全分離する。ICS-WEATHER / ICS-EARTHQUAKE と同方針（別リポ・独自 admin・k8s/ArgoCD・harbor image）だが、規模は遥かに大きい（深く結合した ~2000 行の Django app の作り直し）。
> 関連: [refactor-service-split.md](refactor-service-split.md)（方針・§7③）, [delivery.md](delivery.md)（ICS-TV 同居時代の納品仕様＝**歴史的記録**）, 別リポ icstv-earthquake（分離の前例）, [datamodel.md](datamodel.md)（Asset/Episode/Program）, [overview.md](overview.md) §3.2（正規化 mezzanine）。
> ユーザー判断（2026-06-25）: **分離形態 = 別リポ Node/TS 書換**、**データ = 別DBへ移管**（最も分離度の高い形・積極分離方針と一貫）。

## 1. 目的・背景

納品は B2B サブシステム（外部制作会社が Google サインインで納品）で、業者ポータルは既に `deliver.*` ホスト（`config.urls_delivery`）で構造分離済み。しかし実装は ICS-TV 本体の Django app（`server/delivery/`）で、**medialib.Asset を直接生成し、scheduling.Episode/Program/Series に FK で結合**している（深い結合）。CPU 律速の正規化（mezzanine ffmpeg）もプレイアウト系と同居。

天気/地震（共有 DB 無しの小さなコンテンツ生成器）と違い、納品はリレーショナルな業務データ＋メディア処理パイプラインを持つ。完全分離は「別リポ＋別DB＋API seam」で、ICS-TV からは**完成 asset の登録 API 1 系統**でしか繋がらない疎結合にする。

### 1.1 分離の便益
- **CPU 隔離**: 正規化（CPU 律速）がプレイアウトノード/本体ワーカーと別ノードへ → 送出への影響ゼロ。
- **B2B の独立**: 業者向けの可用性・デプロイ・スキーマ進化を本体から切り離す。
- **本体の軽量化**: ICS-TV から納品 app（~2000 行＋QC/正規化結合）が消え、放送/編成に専念。

## 2. 分離前アーキテクチャ（ICS-TV モノリス・歴史的記録）

> 本節の `server/delivery/*`（`delivery.qc.run_qc` / portal / admin_portal 等）は Phase 3.9 の
> Stage G/H で**撤去済み**（§11）。現行実装は ICS-DELIVERY 側にある。

```
業者(deliver.*) ─upload→ DeliveryFile(r2://quarantine)
   │  submit_qc → delivery.qc.run_qc (ffprobe 技術 + loudnorm 測定) → QcReport (測って警告)
   │  approve_file → medialib.Asset(kind=program, source=r2://quarantine) 生成
   │      └ post_save signal → medialib.tasks.normalize_asset (queue=normalize)
   │            mezzanine: ffmpeg 1080p60 + loudnorm 2-pass → R2 → Asset.normalize_status=READY
   │      └ Asset READY signal → scheduling.tasks.apply_episode_asset
   │            Episode.asset を該当 Program へバックフィル (end_at 再計算・衝突通知)
   └ 進行表: delivery.sheets (PDF 出力) / 業者・招待管理: delivery.admin_portal
```

データ（`server/delivery/models.py`）: ProductionCompany / DeliveryAccount / DeliveryInvitation / Delivery / DeliveryFile / QcReport / SheetExport。
結合: `medialib.models`(Asset/AssetKind/NormalizeStatus/CmCreative/CmGrid)・`scheduling.models`(Episode/Program/Series)・`core.r2`。

## 3. 目標アーキテクチャ（ICS-DELIVERY 別リポ）

```
┌─ icstv-delivery (別リポ・Node/TS・専用 Postgres・harbor icstv/delivery) ──────┐
│  deliver.* ホスト: 業者ポータル(Google OAuth) + staff 審査UI + 業者/招待 admin │
│  upload → R2(quarantine) → QC(ffprobe/loudnorm) → 正規化(ffmpeg mezzanine)    │
│  → 完成 asset(R2 mezzanine) → ICS-TV へ登録                                    │
│  専用 Postgres: ProductionCompany/Account/Invitation/Delivery/File/Qc/Sheet  │
│  k8s: Deployment(web)+Worker(ffmpeg・別ノード)+Ingress(deliver.*)+PVC+ArgoCD  │
└──────────────────────────┬───────────────────────────────────────────────────┘
                           │ 内部 API（X-Internal-Token・weather/earthquake と同型）
                           ▼
┌─ ICS-TV ─────────────────────────────────────────────────────────────────────┐
│  POST /api/v1/internal/delivery-asset  (完成 asset 登録)                       │
│    → medialib.Asset(READY) 生成 + Episode リンク → 既存 apply_episode_asset    │
│  GET  /api/v1/internal/delivery-refs   (series/episode/channel 解決・参照用)   │
│  本体の delivery app は撤去（カットオーバー後）。medialib 正規化は studio 直     │
│  アップロード用に残置（正規化コアは共有しないが ICS-TV 側は現行のまま）          │
└──────────────────────────────────────────────────────────────────────────────┘
```

## 4. seam 契約（ICS-TV 側に新設する内部 API）

weather（`/internal/weather-import`）・earthquake（`/internal/breaking-telop`）と同型。`X-Internal-Token`（`DELIVERY_REGISTER_TOKEN`）認証。

| API | 方向 | 役割 |
|---|---|---|
| `GET /api/v1/internal/delivery-refs` | DELIVERY→ICS-TV | series/episode/channel/program の解決（業者が納品先を選ぶための参照）。`Episode` の get_or_create も含む |
| `POST /api/v1/internal/delivery-asset` | DELIVERY→ICS-TV | **完成（正規化済）asset を登録**。payload = r2_key / duration_ms / metadata / kind / episode 特定情報。→ medialib.Asset(READY) 生成 + Episode.asset リンク → 既存 `apply_episode_asset` がバックフィル |
| `GET /api/internal/deliveries`・`GET /api/internal/vendors` | ICS-TV→DELIVERY | **読み取り seam（Phase 3.9 で実装済・エンドポイントは ICS-DELIVERY 側 `src/internal.ts`）**: 番組予算 picker が納品/業者一覧を読む。ただし読み手の番組予算 picker と ICS-TV 側クライアント `server/integrations/delivery_read.py` は**追加提供側**で、このツリーには含まれない（エンドポイント自体は ICS-DELIVERY 側に実在）。ベース URL = `ICS_DELIVERY_READ_URL`（本番 configmap で `http://icstv-delivery`）、認証 = `DELIVERY_REGISTER_TOKEN` を**双方向再利用**（security-review L-6 の分割は未対応） |

- **正規化は DELIVERY 側で完了済み**を前提に、ICS-TV は登録時 `normalize_status=READY` で受ける（再正規化しない）。
- **冪等**: r2_key or (series, episode) 一意で重複登録を弾く。
- ~~ICS-TV → DELIVERY 方向の API は基本不要（DELIVERY が主導）。納品状況を studio で見たい場合のみ将来 read API を検討。~~
  → **読み取り seam は Phase 3.9 で実装済み**（上表の `GET /api/internal/deliveries`・`/vendors`）。

> **運用状態（2026-09-02 更新）**: `DELIVERY_REGISTER_TOKEN` は **2026-09-02 に本番投入済み**
> （SealedSecret 24 キー化）。`/internal/delivery-asset`・`/delivery-refs` と ICS-TV→DELIVERY
> 読み seam は稼働状態になった。ローテーション手順は [operations.md](operations.md)
> 「分離サブシステム seam トークン」参照。

## 5. データ移行（別DB）

`server/delivery/models.py` の 7 モデルを ICS-DELIVERY の Postgres へ移管。Episode/Program/Series/Asset への FK は **API 参照**（id を保持し、解決は ICS-TV `/internal/delivery-refs`）へ置換。

移行手順（カットオーバー時）:
1. ICS-DELIVERY を本番並行稼働（新規納品は DELIVERY、既存は ICS-TV）。
2. 既存 delivery テーブルを ICS-DELIVERY Postgres へ ETL（id/FK を API 参照へ写像）。
3. deliver.* ホストを ICS-DELIVERY へ切替（ingress/Tunnel）。
4. ICS-TV の delivery app を撤去（urlconf/models/tasks/templates）+ テーブル drop migration。

## 6. 技術スタック（icstv-earthquake に倣う）

- **Node20 / TypeScript / tsx**（earthquake と同）。Web = Hono or Express、ジョブ = BullMQ(Redis) or 簡易キュー。
- **Postgres**（delivery のリレーショナルデータ）。Prisma or Drizzle ORM。
- **R2**（aws-sdk S3 互換）= quarantine + mezzanine。ICS-TV と同じバケット規約。
- **Google OAuth**（業者サインイン・現行 delivery.auth と同等。bind_identity/招待）。
- **ffmpeg/ffprobe**（QC + 正規化）= Node から spawn。現行 Python の `run_qc`/`normalize_asset` のロジックを忠実移植（technical/loudnorm/mezzanine 1080p60 loudnorm 2-pass・長尺 passthrough・原本バッジ相当）。
- **k8s**: Deployment(web) + Worker(ffmpeg・**別ノード/CPU 確保**) + Ingress(deliver.*) + PVC(作業領域) + ArgoCD app `icstv-delivery` + harbor `icstv/delivery`。

## 7. QC / 正規化の移植（最大の作り込み）

現行 Python（`server/delivery/qc.py`〔※撤去済み・移植元の歴史的参照。現行 QC 実装は
icstv-delivery 側〕/ `server/medialib/tasks.py`）の忠実移植が要：
- **QC**: ffprobe 技術メタ（解像度/fps/尺/コーデック/音声有無）+ loudnorm 測定（Integrated/LRA/True Peak vs -14 LUFS）。「測って警告」（弾かない）。各 subprocess に timeout（corrupt 入力のハング対策）。
- **正規化(mezzanine)**: ffmpeg 1080p60 + loudnorm 2-pass。長尺（>MEZZ_MAX_SOURCE_SEC）は passthrough（映像 copy + 音声 loudnorm）で原本素材化（原本バッジ相当）。CPU 律速 → 専任 worker・concurrency 制御・time_limit。
- invariant（ffprobe ≤ ffmpeg ≤ soft ≤ hard < visibility < stale）を Node 側でも踏襲。

## 8. カットオーバー戦略

ICS-EARTHQUAKE の cutover（組込 disable → 別リポ単独）に倣うが、データ移行があるぶん慎重に:
1. **並行稼働**: ICS-DELIVERY を deliver-new.* 等で先行稼働・本体 delivery は現役。seam API（ICS-TV 側）は先に用意。
2. **新規だけ DELIVERY**: feature flag で新規納品を DELIVERY へ。既存案件は ICS-TV で完了させる（自然減）。
3. **ETL**: 残存データを移行。
4. **ホスト切替**: deliver.* を DELIVERY へ。
5. **撤去**: ICS-TV delivery app 削除 + テーブル drop（earthquake P1.5 と同型の dead-code 撤去）。

> **ホスト切替（手順 4）は未実施（2026-09-02 実測・未決）**: ICS-DELIVERY は今も
> **deliver-new.\*** で稼働しており、`deliver.*` は ICS-TV 側の `urls_delivery`/`DELIVERY_URLCONF`
> 撤去（Phase 3.9）後は **ICS-TV 公開サイト（urls_public）へフォールバックする死んだルート**になって
> いる（`DJANGO_DELIVERY_HOSTS` を読むコードも無い）。deliver.* を ICS-DELIVERY へ向けるか
> ingress ルールごと撤去するかは**ユーザ判断**（外部 CF Tunnel 設定と連動。configmap/ingress の
> マニフェスト変更は ArgoCD 反映を伴うためリリース手順で扱う）。

## 9. サブフェーズ分解（独立 PR / 各々検証）

| Sub | 内容 | 場所 | 規模 |
|---|---|---|---|
| **2.0** | 本設計ドキュメント（青写真・seam 契約・移行計画） | ICS-TV docs | ← 今ここ |
| **2.1** | **ICS-TV seam API**（`/internal/delivery-asset` + `/internal/delivery-refs` + token）。本体に先に用意（DELIVERY が叩く先） | ICS-TV | 中 |
| **2.2** | `icstv-delivery` リポ scaffold（Node/TS・Postgres・R2・k8s/ArgoCD・harbor・最小 web） | 新リポ | 中 |
| **2.3** | Google OAuth + 業者/招待 admin（delivery.auth/admin_portal 相当） | 新リポ | 中 |
| **2.4** | 納品ポータル + アップロード（R2 quarantine・DeliveryFile） | 新リポ | 大 |
| **2.5** | QC パイプライン（ffprobe/loudnorm 移植） | 新リポ | 大 |
| **2.6** | 正規化 worker（mezzanine ffmpeg 移植・別ノード） | 新リポ | 大 |
| **2.7** | 完成 asset 登録（→ ICS-TV seam）+ 進行表 PDF | 新リポ | 中 |
| **2.8** | データ ETL（delivery 7 モデル → DELIVERY Postgres） | 移行 | 中 |
| **2.9** | カットオーバー（ホスト切替・並行→単独） | 運用 | 中 |
| **2.10** | ICS-TV delivery app 撤去（urlconf/models/tasks/templates + drop migration） | ICS-TV | 中 |

**着手順の推奨**: 2.1（ICS-TV seam）を先に固めると、新リポは「seam に向けて作る」だけになり依存が一方向化する。2.1 は本体内の小さな追加で、既存 delivery と共存できる（並行稼働の土台）。

## 10. リスク・留意点

- **規模**: 複数週〜のプロジェクト。weather/earthquake より遥かに大きい（フル B2B ＋メディア処理）。サブフェーズで刻む。
- **ffmpeg 移植の忠実性**: QC/正規化の挙動（loudnorm 2-pass・長尺 passthrough・timeout invariant）を Python から Node へ正確に移す。差異は素材品質に直結 → 移植時に Python 版と出力突合（同一入力で mezzanine の ffprobe メタ一致を検証）。
- **データ移行の整合**: FK→API 参照の写像。並行稼働中の二重管理を最小化（新規=DELIVERY / 既存=ICS-TV の明確な線引き）。
- **一人運用の負荷**: 新サービス（web+worker+Postgres+別ノード）の運用が増える。earthquake/weather より重い。監視・バックアップ・障害対応の手順を 2.2 で用意。
- **正規化ノード**: CPU 律速の正規化を載せる別ノードの確保（送出ノードとは別）。容量計画が要。

## 11. 実装状況と 2.10 の顛末（完了記録・2026-09-02 更新）

新リポ `~/icstv-delivery` 側は 2026-06-26 時点で **2.2〜2.9 まで実装完了**（scaffold / 別DB / OAuth+admin / ポータル+R2 アップロード / 自動 QC / 正規化 worker / 承認+完成 asset 登録(seam)+記録表 / ETL スクリプト）し、その後**本番稼働に到達**（現行 **v0.5.3**・ArgoCD app `icstv-delivery`・deliver-new.<内部ドメイン>）。studio 側 delivery 面の cutover（§8 手順 1〜3 相当）は **2026-06-27 の v0.8.43** で完了した。

> 注: §9 の番号と実装の番号は 1 つずれている（実装では 2.3=別DB、2.4=OAuth+admin、2.5=ポータル+アップロード、2.6=QC、2.7=正規化、2.8=承認+登録+記録表、2.9=ETL）。正は新リポ README のサブフェーズ表。

**2.10（ICS-TV delivery app 撤去）も完了済み**。当初「単純撤去できない」としたブロッカー
（`procurement.DeliveryCost` の delivery FK 結合 / `api/routers/admin_delivery.py` /
`rights/sheets.py` の `delivery.sheets` 再利用 / `seed_demo`・テスト依存）は、backoffice Phase
**3.8a（procurement→delivery FK 切離し。[backoffice-service-split.md](backoffice-service-split.md) §9）**
で DB 結合が解け、**2026-07-08 の Stage G/H（撤去 3 コミット ab0bd54 / 902dff1 / 072e722・
main 昇格 6a5f558）で撤去、本番投入は v0.8.87**:

- migration `delivery/0006_drop_delivery_tables` で **7 テーブルを DROP**（PreSync migrate で適用）。
- CSV 共通処理（旧 `delivery.sheets.to_csv`）は `server/core/sheets.py` へ移設。
- `api/routers/admin_delivery.py` は撤去（studio の納品面は ICS-DELIVERY 側 UI へ）。
- `server/delivery/` は **migration 依存のためだけの ghost app** として残置
  （現存は `__init__` / `apps` / `models` / `migrations` のみ。ビュー/タスク/テンプレは無い）。
- seam（`/internal/delivery-asset`・`/delivery-refs`）は予定どおり撤去後も残置（§4。ただし
  トークンは 2026-09-02 に投入済み — §4 の運用状態注記）。

**日付・呼称の注意**（記録の混同防止）:

- **撤去の日付は 2026-07-08（v0.8.87）が正**。2026-06-27 は migration 0006 のヘッダ生成日と
  cutover（v0.8.43）の日であって、撤去日ではない。
- procurement FK 切離し migration 0002 の docstring は「リファクタ **Phase 3.8**」表記。本書の
  番号体系（delivery **2.10** / backoffice **3.9**）と migration docstring のフェーズ呼称を
  混同しないこと。

残: **ホスト切替（deliver.\* → ICS-DELIVERY）のみ未実施・未決**（§8 の注記参照。ユーザ判断）。
