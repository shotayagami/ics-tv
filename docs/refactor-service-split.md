# studio リファクタリング方針 — サービス分離 ＋ モード別コンソール

> ステータス: **実施済（Phase 1/2/3 完了・残は Phase 3.10 の write 移管のみ）**。初版 2026-06-25
> 時点は叩き台だったが、その後 Phase 1（コンソール分割）・Phase 2（ICS-DELIVERY 分離）・
> Phase 3（ICS-BACKOFFICE 分離 + 本体 delivery app 撤去）まで本番稼働に到達した（§8 の状態列参照）。
> 目的: 「増設に増設を重ねて使いにくくなった」studio 管理面を、(A) ICS-WEATHER / ICS-EARTHQUAKE のように分離可能なものはサブシステム化し、(B) システム内に残すものは UI/UX を考慮して再配置し、(C) ワンオペの外出先即応のため**モバイル対応**（特に放送面・§11）を同時に行う、という方針で全体を見直す。本ドキュメントはその**方針の正本**であり、以降の作業（Phase 1〜3）の判断基準とする。
> 関連: [overview.md](overview.md)（意思決定 #14 モジュラモノリス＋将来サービス抽出 / #15 納品ポータル）, 別リポ `~/icstv-earthquake`（分離の前例・本リポ側 seam は `server/api/routers/internal.py`）, [delivery.md](delivery.md), [operations.md](operations.md), [youtube.md](youtube.md), [scheduler.md](scheduler.md)。

## 1. 目的・背景

### 1.1 問題
studio.* の管理面に、**性質の異なる 9 ドメイン**が 1 枚のフラットなサイドバー（14 項目）＋大量の旧 Django 画面に同居している。深夜の放送当直（リアルタイム即応）と、月末の納品書発行（月次帳票）が**同じナビ・同じ密度**で並ぶため、「今やりたいこと」に対して常に過剰な選択肢を漕がされる。これが「使いにくさ」の根本原因。

現状同居している 9 ドメイン:

| ドメイン | 主な画面 | 使用ペース | 操作の型 |
|---|---|---|---|
| 放送当直 | ops / health / 通知 / CG キュー / YT コンソール / 生入力 | 常時・即応 | 見て即反応・アラーム駆動 |
| 編成 | timeline / series / slots / 番組フォーム / breaks | 日次・週次 | 作る・編集 |
| 素材 | medialib / cuesheet / 正規化 | 随時 | 作る・確認 |
| 納品(B2B) | delivery / QC / 業者管理 / 進行表 | 案件ごと | 受け取る・審査 |
| 営業 CM | sales / band editor | 随時 | 割り付ける |
| 経理 | 請求(納品書) / 番組予算(AP)（追加提供側・このツリーには含まれない） | 月次 | フォーム・帳票 |
| 権利 | 楽曲報告（追加提供側・このツリーには含まれない）/ 配信権 | 月次 | フォーム・帳票 |
| 会員分析 | 会員統計 | 随時 | 閲覧 |
| システム設定 | チャンネル / プリセット | まれ | 設定 |

### 1.2 確定した方針判断（ユーザー回答 2026-06-25）
- **運用体制 = ほぼ一人で全役割**。→ 再編軸は「人(ペルソナ)」ではなく「**今やりたいこと（モード/ペース）**」。
- **分離方針 = 積極分離（サービス化）**。→ 境界の綺麗なものは別サービスへ切り出す。ただし §3 の制約条件を守る。
- **再編軸 = モード/ペース別コンソール**。→ 残す機能は「即応 / 制作 / 月次」の面に分けて入口・密度・操作型を別物にする。

## 2. 線引きの原則 — 何が「分離可能」か

ICS-WEATHER が別リポへ分離できたのは、**共有 DB を一切持たない「コンテンツフィード生成器」**だから。ICS-TV 側は生成された天気フィードを R2 バケット 1 点で**取り込むだけ**で済み、新しい HTTP API も認証層も増やさずに保てた（天気予報の自動取り込みはこのツリーには含まれない）。**地震/EEW 速報も同じ型で分離済**（別リポ `~/icstv-earthquake`・Node/TS・prod v0.8.30）。ICS-TV 側は内部 API 1 点（`/internal/breaking-telop`）だけを公開し、ポーリング・判定・admin はサービス側に持つ（§7②）。**この 2 例が本リファクタにおける「分離可能」の基準形**。

一方、studio 内の他モジュールの多くは **scheduling + medialib の DB に直接読み結合**している:
- 納品 → Episode → `apply_episode_asset`（承認 asset を Program へバックフィル）
- 請求(納品書) → Program / Episode を直接読む
- 番組予算 → Program / 業者 を参照（番組予算は追加提供側。このツリーの procurement app はモデルを持たない殻だけが残る）
- 権利 → 楽曲使用＝Program / 素材に紐づく（楽曲使用の記録は追加提供側。このツリーの rights に残るのは配信権 DistributionRight のみ）

したがって**「分離可能性」は結合の太さで判定する**:

| 結合の型 | 例 | 分離方式 |
|---|---|---|
| **フィード型**（共有 DB なし・narrow interface） | 天気 / 地震速報 | 別リポ・別サービス（ICS-WEATHER 型）。**2 例とも分離完了** |
| **読み取り結合**（DB を読むが書きは少） | 経理 / 権利 / 会員分析 | サービス化可。ただし**読みは API/read-only から、書き所有権の移管は最後** |
| **B2B 別認証・別ペース** | 納品 | サービス化可。業者ポータルは既にホスト分離済。seam＝asset 受け渡し |
| **低遅延・DB 密結合** | 放送 / 編成 / 素材 | **切らない**。母艦に残し UI だけ再配置 |

> 重要な再定義: **「サービス化」＝細い API 境界を確定すること**であって、**初日から N 個の別 Pod を動かすことではない**。境界が資産であり、別ランタイム化は §3 の容量と一人の手に負える範囲で段階的に進める。

## 3. 制約条件（一人運用で破綻させないために）

送出ノードは容量飽和（4 コア LXC・単一 iGPU・k8s-worker 同居で 2ch 同時すら不可。[reference: youtube_ingestion_starved]）。一人運用。この前提で「積極分離」を安全に実現するための不変条件:

1. **「別リポ/別境界」と「別ランタイム」を分けて考える。** 月次・低トラフィックなサービス（経理/権利）は、最初は *別 Django プロジェクト＋同居デプロイ* で **API 境界だけ確定**し、Pod 分離は後回しでよい。
2. **共有 DB を初日に割らない。** 読み取りは API / read-only から始め、**書き込み所有権の移管は各サービスの最後の工程**にする。一気に DB を割ると数ヶ月仕事になり一人では回らない。
3. **母艦（放送＋編成＋素材）は触らず軽くする。** ここはノード容量と直結。新サービスは全て *軽量・別ノード可* のものだけを選ぶ。
4. **新サービスは送出ノードに載せない。** 放送経路のリソースを侵食しない（k8s 側 or 別ノードで動かす）。

## 4. 目標アーキテクチャ — 4 サービス ＋ 2 コンソールの母艦

> **2026-09-02 更新**: 切り出しは全て完了し、稼働中の分離サブシステムは **6 種**になった —
> ① weather / ② earthquake / ③ delivery = **✅完了**
> （icstv-delivery v0.5.3 本番・[delivery-service-split.md](delivery-service-split.md)）/
> ④ backoffice = **✅完了**（icstv-backoffice v0.2.3 本番・
> [backoffice-service-split.md](backoffice-service-split.md)。残は 3.10 の write 移管のみ）/
> さらに本書の後に同型で加わった ranking と
> slidecast。以下の図・説明は設計当時（③④が候補だった時点）の記録。

> 注記: このうち weather（天気予報の自動取り込み）・ranking（ランキング企画の自動取り込み）・slidecast（LT 発表の自動動画化）は**このツリーには含まれない**。図中 ④ の内訳にある番組予算 (procurement) も追加提供側で、**このツリーには含まれない**。上のブロックと以下の図は当時の記録としてそのまま残す。

```
── 自動フィード型サービス（別リポ・分離済）──────────────────────
  ① ICS-WEATHER ……… 済。天気フィード生成（~/icstv-weather）。R2 ドロップで取り込み
  ② ICS-EARTHQUAKE … 済。地震/EEW 速報（~/icstv-earthquake・Node/TS・prod v0.8.30）。
                       外部ポーリング＋独自 admin。ICS-TV 側は内部 API 1 点
                       `POST /api/v1/internal/breaking-telop` のみ（weather と同型）

── 切り出し候補（データ結合あり・段階的に）────────────────────
  ③ ICS-DELIVERY …… 納品。取込→QC→正規化→完成asset まで全所有（完全分離・決定②）。
                       seam ＝「完成asset を ICS-TV medialib へ登録」（天気の R2 ドロップ型）
  ④ ICS-BACKOFFICE … 請求(納品書)/番組予算/権利/会員分析。月次・読み取り中心・
                       遅延許容 ＝ サービス化の好適。ICS-TV を API 越しに読む

── 母艦を 2 コンソールに分割（最初からホスト分離・面ごとに別シェル・決定④⑤）──
  🔴 放送コンソール（即応・別ホスト ops.* 案）  ops・health・通知・CG キュー(ライブ)・
                              YT 配信・配信枠の稼働監視・生入力・take/extend・手動速報送出
  🟡 編成・制作（日次/週次・studio.*）  timeline・series・配信枠予約・番組フォーム・breaks・
                              CG キュー(オーサリング) ＋ 素材/cuesheet ＋ CM 割付
  ⚙ 設定（まれ・studio.* 内）  チャンネル・プリセット（編成ホスト内の小さな設定面）
```

結果として studio.* の管理面は **9 ドメイン同居 → 2 コンソール＋小設定**に減り、フィード型（天気・速報）は分離済、残る切り出し候補（納品・経理権利）は境界の綺麗な順に外へ出る。

> 注: 視聴者向け会員ページ（members の account/login/2FA 等）とサブスク（subscriptions）と公開サイト（public）は**既に公開ホスト tv.\* 側に分離済み**。本リファクタの対象は studio.* 管理面。

## 5. コンソール IA（母艦に残す 2 コンソール）

各コンソールは**入口・トップ・情報密度・操作型を別物**にする。1 枚のサイドバーに横並びにしない。**最初からホスト分離**（決定④）＝🔴放送は別ホスト（例 `ops.*`）、🟡編成・制作＋⚙設定は `studio.*`。各ホストが対応する**別シェル/別エントリ**（決定⑤）を配信し、DS/API/コンポーネントは共有パッケージで重複回避。実装は HostUrlconfMiddleware の既存 3 区分（管理/公開/納品）に放送ホストを足す。**モバイル対応の優先度はコンソールごとに異なる（§11）**。

### 🔴 放送コンソール（リアルタイム / 即応・モバイルファースト）
- トップ = 運行ダッシュボード（now-playing・出力 health・通知・同時接続）。一目で見える・アラーム駆動。
- 操作 = take / extend / 押え・SLATE 退避・CG オーバーレイ手動 op・YT 配信状態。
- 対象画面: ops dashboard / health / 通知 / now_playing / asrun / concurrent / extend / youtube_console / 配信枠の稼働監視(slots) / live-sources / CG ライブ手動 op / 手動速報送出。
- 設計原則: 編集フォームを置かない（即応に集中）。ch 切替を常設。**外出先スマホからの即応が要件**（ワンオペ・§11）＝モバイルファースト設計。
- 進行中の整合: main `38a9684`「YouTube 配信を『設定』から独立メニューに切り出す」で YT 配信は既に設定から分離着手済＝**放送コンソール集約と整合**（本リファクタはこれを放送面へ寄せる）。

### 🟡 編成・制作（日次 / 週次 / 作る）
- トップ = 編成タイムライン（当日 + 数日）。カレンダー / フォーム中心。
- 操作 = 番組の配置・尺・ad break・series/slot 編集・素材取り込み・cuesheet・CM 割付・CG キューのオーサリング。
- 対象画面: timeline / series / slots（配信枠予約作成）/ program-form / breaks / graphic-cues(authoring) / medialib / cuesheet / sales 割付。
- 設計原則: リアルタイム監視を置かない（作る作業に集中）。**デスクトップ主・モバイル許容**（重い編集は PC、外出先は閲覧/軽操作・§11）。

### ⚙ 設定（まれ・studio.* 内）
- チャンネル / channel settings / broadcast preset。編成ホスト（studio.*）内の小さな設定面（独立コンソール/独立ホストにはしない）。

## 6. 現状全画面 → 目標の面 の対応表（レビューの核）

現状の studio 全画面（SPA + 旧 Django）を、目標の面へ割り付けた一覧。**ここが叩き台の主たるレビュー対象。** 「行き先」に違和感があれば指摘いただきたい。

| 現状画面 | 実体 | 行き先 | 備考 |
|---|---|---|---|
| Dashboard `/` (SPA) | ランチャ | 母艦トップ | コンソール選択の入口に再設計 |
| OpsPage `/ops/:slug` | SPA | 🔴 放送 | 運行ダッシュボード本体 |
| ops/dashboard・_health・_now_playing・_asrun・_concurrent・_extend・notifications | Django | 🔴 放送 | SPA ops へ統合済/未済混在。放送コンソールへ集約 |
| youtube_console | Django(admin_ui) | 🔴 放送 | YT 配信操作 |
| program_broadcast_dashboard | Django(admin_ui) | 🔴 放送 | 番組専用枠の手動操作（#23） |
| SlotsPage `/slots/:slug`・slot_dashboard | SPA/Django | 🟡 編成（予約作成）/ 🔴 放送（稼働監視） | **決定①-a: 両面**。予約作成は編成、稼働監視は放送 |
| LiveSourcesPage `/live-sources` | SPA | 🔴 放送 | 生入力 |
| GraphicCuesPage `/graphic-cues/...`・graphic_cues | SPA/Django | 🟡 編成（オーサリング）/ 🔴 放送（ライブ手動 op） | **決定①-c: 二面で出す**。cue 作成は編成、放送中の手動 show/hide は放送 |
| TimelinePage `/scheduling/:slug`・timeline | SPA/Django | 🟡 編成 | 編成タイムライン本体 |
| SeriesPage `/series/:slug`・series_edit/list | SPA/Django | 🟡 編成 | 週間編成 |
| ProgramFormPage `/program-form/:slug`・program_form | SPA/Django | 🟡 編成 | 番組フォーム |
| breaks（ad break 編集） | Django | 🟡 編成 | |
| slot_form | Django | 🟡 編成 | |
| MedialibPage `/medialib`・medialib/dashboard | SPA/Django | 🟡 編成（素材） | |
| CueSheetPage `/cuesheet/:id`・cuesheet | SPA/Django | 🟡 編成（素材） | |
| asset_form・bundle_form・cm_form・filler_form | Django | 🟡 編成（素材） | 素材入力フォーム群 |
| SalesPage `/sales`・sales/allocation・band_editor | SPA/Django | 🟡 編成（割付）/ ④ バックオフィス（契約・請求） | **決定①-b: 分割**。割付=編成、契約/放確/月次請求=④ |
| ChannelsPage `/channels`・channel_list | SPA/Django | ⚙ 設定 | |
| ChannelSettingsPage・channel_settings | SPA/Django | ⚙ 設定 | フィラー/スレート/CDN 等 |
| broadcast_preset_list/form | Django(admin_ui) | ⚙ 設定 | YT 配信プリセット（#23） |
| DeliveryPage `/delivery`・DeliveryDetailPage | SPA | ③ ICS-DELIVERY | staff 審査側 |
| delivery/dashboard・detail・qc_review・vendor_admin・program_sheets・new/edit | Django | ③ ICS-DELIVERY | 業者ポータルは既に deliver.* 分離済 |
| BillingPage `/billing`・billing/dashboard・invoice_pdf | SPA/Django | ④ ICS-BACKOFFICE | 納品書 |
| ProcurementPage `/procurement`・procurement/dashboard | SPA/Django | ④ ICS-BACKOFFICE | 番組予算/AP（追加提供側・このツリーには含まれない） |
| RightsDashboard `/rights`・rights/dashboard | SPA/Django | ④ ICS-BACKOFFICE | 楽曲報告（追加提供側）/配信権 |
| MembersStats `/members/stats`・members/stats | SPA/Django | ④ ICS-BACKOFFICE | 会員統計（分析） |
| 地震/EEW 速報 | 別リポ ICS-EARTHQUAKE（済） | ② サービス（分離済） | ICS-TV 残置＝内部 API `/internal/breaking-telop` ＋ studio 手動速報送出。組込 beat/model/`earthquake*.py` は **P1.5 で撤去済**（migration 0019 で DROP・§8） |
| 会員 account/login/2FA/subscriptions/public | Django(tv.*) | （対象外） | 既に公開ホスト分離済 |
| Django admin `/admin/` | Django | 母艦（保守用） | 全モデルの最終手段。残置 |

## 7. 各サービスの seam（結合点）詳細

### ② ICS-EARTHQUAKE（地震/EEW 速報）— 分離完了（前例）
- 状態: **別リポ `~/icstv-earthquake`（Node20/TS・harbor icstv/earthquake・prod v0.8.30）へ分離済**。weather と同方針＝独立リポ＋独自 admin（発報履歴/ソース/配信基準/手動速報送出）＋15s poller。k8s Deployment/Service/Ingress（earthquake.<内部ドメイン>）＋ArgoCD app `icstv-earthquake`。
- seam（ICS-TV 側）: **内部 API 1 点のみ** — `server/api/routers/internal.py` `POST /api/v1/internal/breaking-telop`（`EarthquakeToken` 認証＝共有トークン `EARTHQUAKE_FIRE_TOKEN`）→ `core.views.fire_breaking_telop`（`server/core/views.py`） が layer40 速報テロップを全 enabled ch へ op_overlay show/(future)hide。studio 手動送出からも同経路を再利用可。
- cutover: 組込 ingest は configmap `EARTHQUAKE_ALERT_ENABLED=false` で停止済、別リポ poller が**現行唯一の発報系**。
- 残務（cleanup）: **✅完了（P1.5・§8）**。組込コード（`server/scheduling/earthquake.py` /
  `earthquake_ingest.py` / `EarthquakeAlert` モデル / beat / settings）は撤去済みで現存しない
  （migration 0019 で DROP。scheduling 側に残るのは migration 0017/0019 の履歴のみ）。
- 含意: **フィード型サービスの抽出は weather に続き 2 例目が完了**＝§2 の線引きとサイドカー方式が実証済。③④ もこの型に倣う。

### ③ ICS-DELIVERY（納品）— 完全分離（決定②）
- 現状: 業者ポータルは既に `config.urls_delivery`（deliver.* ホスト・Google サインイン・CF Access 無し）で構造分離。承認 → asset ready → `server/scheduling/signals.py` の `apply_episode_asset_on_ready`（Asset post_save）→ `apply_episode_asset` が Episode.asset を Program へバックフィル。
- 目標: **取込→QC→正規化→完成 asset まで納品サービスが全所有**（B2B を最もクリーンに独立）。ICS-TV へは**完成（正規化済）asset を登録**するだけ＝天気の「R2 に完成物をドロップ」型と同形。seam ＝ medialib への asset 登録 API（登録後は ICS-TV 既存 signal が Episode バックフィルを駆動）。
- 移設対象: `delivery.qc`（run_qc）＋ medialib の正規化(mezzanine) パイプライン＋ QC レビュー/業者管理/進行表 UI。**CPU 律速の正規化ワーカーがサービス側へ移る**（送出ノードとは無関係の k8s ワーカー）。※移設は完了済み — `delivery.qc` を含む本体 delivery app は Phase 3.9（Stage G/H・v0.8.87）で撤去され、現行実装は ICS-DELIVERY 側（[delivery-service-split.md](delivery-service-split.md) §11）。
- ICS-TV 残置: **studio 直アップロード（medialib asset_form 等）用の正規化は ICS-TV 側にも残す**。当面は両系に正規化が存在しうるため、正規化コアは共有ライブラリ化して重複を抑える（§9）。範囲大ゆえ Phase 2。

### ④ ICS-BACKOFFICE（経理/権利/分析）— 1 サービスに束ねる（決定③）
- 現状: billing/procurement/rights/members(stats) が Program/Episode/業者/Member を**読み取り中心**で参照。月次。
- 構成: **経理(請求/予算)＋権利＋会員分析を 1 サービスに集約**（内部はモジュール分割）。一人運用＝可動部最小（決定③）。
- seam: **読み取り API（または read-only DB アクセス）**から開始。請求書発行・予算記帳・楽曲報告の**書き込みはサービス側で完結**し、ICS-TV へは書き戻さない（参照のみ）。楽曲報告と番組予算は追加提供側の機能で、このツリーには含まれない（番組予算の内部 API は口だけが残り、常に空を返す）。
- 評価: 遅延許容・低トラフィック → §3-1 の通り「別境界・同居デプロイ」で十分。Pod 分離は任意。

## 8. 段階移行計画

| Phase | 内容 | 効果 / リスク | 状態 |
|---|---|---|---|
| ― | 自動フィード型分離（ICS-WEATHER / ICS-EARTHQUAKE） | サイドカー方式の実証 | **完了済**（前例・§7②） |
| **0** | 本ドキュメント（目標アーキ＆IA）作成 | 方針の正本 | ✅ 完了（a42f0f1） |
| **1** | **コンソール分割**: 🔴放送=別ホスト(ops.*)・🟡編成+⚙設定=studio.*、面ごとに別シェル/別エントリ（決定④⑤）＋ **モバイル対応（§11）** ＋ 組込地震コードの掃除 | フロント中心・DB 不変。ホスト/CF/認証の追加はあるが**最速で使いやすさ**。先頭 | ✅ 完了 P1.1〜1.5（branch feat/studio-console-split） |
| **2** | ③ 納品**完全分離**: 取込→QC→正規化→完成asset をサービスへ移し、ICS-TV へ完成 asset 登録 API（決定②） | 範囲大（正規化パイプライン移設を含む） | ✅ **完了**（icstv-delivery v0.5.3 本番稼働・cutover 済・本体 delivery app も Stage G/H で撤去 = [delivery-service-split.md](delivery-service-split.md) §11） |
| **3** | ④ バックオフィス分離（**1 サービス**・読み取り API → 所有権移管は最後・決定③） | 月次・低トラ ＝ 安全に最後尾 | ✅ **完了**（icstv-backoffice v0.2.3 本番稼働・procurement write 移管 3.8 済。残は **3.10** = billing/rights/analytics の write 移管のみ = [backoffice-service-split.md](backoffice-service-split.md) §9） |

**Phase 1 を先頭に置く理由**: バックエンドを一切触らず、一人運用の日々の苦痛（深夜の当直画面と月次帳票が同じナビ）が即座に消える。かつ UI を面分割する過程で各サービスの seam が可視化され、Phase 2/3 の抽出が楽になる。組込地震コードの掃除（§7②）は DB 不変・低リスクなので Phase 1 に相乗りさせる。各 Phase は独立 PR に分割する（[feedback: commit_granularity]）。

**Phase 1 実装記録（2026-06-25・branch feat/studio-console-split・origin/dev 起点）**:
- P1.1 `@icstv/ops` 骨格 + ops.* ホスト（HostUrlconfMiddleware → config.urls_ops・読み取り監視 shell・モバイルファースト）
- deploy: ops.* ingress + env（DJANGO_OPS_HOSTS 等。CF Tunnel/Access は dashboard 作業＝ユーザ）
- P1.2 送出操作（緊急SLATE/CM/本線リロード/自動復帰/手動速報 layer40/通知ack）。`core.urls.OPS_OPERATIONS` を studio.*/ops.* で共有
- P1.3a 押え/巻き（OpsStatusOut.live_program_id 追加）/ P1.3b 配信枠（YT配信状態）監視カード
- P1.4 studio 再構成（運用ops を studio から削除→編成中心 IA・放送コンソール導線を footer に）
- P1.5 組込地震 dead-code 撤去（earthquake*.py / EarthquakeAlert / beat / settings・migration 0019 で DROP）
- **P1.3 の据え置き**（低モバイル価値 or デスクトップ適性）: 生入力（読み取り ingest URL）/ YT配信遷移操作 / フルCG opパネル → studio 据え置き
- 検証: 960 passed（CIゲート相当 `not grpc and not e2e`）/ 全 workspace typecheck / studio+ops build。失敗2件は sandbox の staticfiles 権限 artifact のみ（CI では緑）

## 9. リスク・留意点

- **ノード容量**: 新サービスは送出ノードに載せない（§3-4）。k8s 側の軽量ワークロードとして配置。
- **共有 DB の罠**: 読み取り結合を甘く見て一気に DB を割ると数ヶ月仕事。§3-2 を厳守（書き所有権の移管は最後）。
- **正規化/QC の移設（決定②=完全分離）**: 納品サービスが正規化/QC を全所有。CPU 律速パイプラインの移設が Phase 2 の主工数。**ICS-TV 側は studio 直アップロード用の正規化を残す**ため、当面は両系に正規化が存在しうる（正規化コアの共有ライブラリ化で重複を抑える）。
- **一人運用の認知負荷**: サービス／ホストを増やすほど「どこを見ればいいか」が散る。**外出先スマホから各面・各サービスへ一望できる入口**（§11）を必ず用意し、サイロ化を防ぐ。
- **二面性のある画面（CG キュー / YT スロット / CM）**: 決定①で確定。CG キューと YT スロットは「作る=編成 / 監視・発火=放送」、CM は「割付=編成 / 契約請求=バックオフィス」。同一データを別ホスト・別シェルで出すため、共有 API/コンポーネントで一貫させる。

## 10. 確定した詳細判断（2026-06-25 ユーザー回答）

§10 の未決を 1 件ずつ確認し、以下に確定（§4〜§9・§11 に反映済み）。

| # | 論点 | 決定 |
|---|---|---|
| ①-a | YT スロットの面 | **両面**＝予約作成は🟡編成 / 稼働監視は🔴放送（CG キューと同じ二面性） |
| ①-b | CM 営業(sales)の面 | **割付=🟡編成 / 契約・放確・月次請求=④バックオフィス** |
| ①-c | CG キューの二面性 | **二面で出す**＝オーサリング🟡編成 / ライブ手動 op🔴放送 |
| ② | ③納品の正規化/QC 所在 | **完全分離**＝サービスが取込→QC→正規化→完成 asset まで全所有し、ICS-TV へ完成 asset を登録。※ICS-TV は studio 直アップロード用の正規化は残す |
| ③ | ④バックオフィスの粒度 | **1 サービスに束ねる**（ICS-BACKOFFICE = 経理+権利+分析・内部はモジュール分割） |
| ④ | コンソールのホスト戦略 | **最初からホスト分離**＝🔴放送は別ホスト（例 `ops.*`）/ 🟡編成＋⚙設定は `studio.*` |
| ⑤ | Phase 1 の実装単位 | **面ごとに別シェル/別エントリ**（DS/API/コンポーネントは共有パッケージ） |
| ⑥ | 手動速報送出の所在 | **両方に残す**（studio 放送コンソール ＋ ICS-EARTHQUAKE admin・同一 API・緊急時の冗長） |

## 11. モバイル対応方針（ワンオペ・外出先即応）

運用はワンオペで、**外出先からスマホで即応する**ことが要件（CF Access は既設・外部到達は現状でも実施中。追加の認証基盤作業は不要）。コンソールを面ごとに分割（決定④⑤）するので、**面ごとにモバイル優先度を変えられる**のが利点。フラット 1 枚のサイドバーを全部モバイル化するより、即応面だけを本気でモバイルファーストにできる。

### 11.1 コンソール別のモバイル優先度
| 面 | 優先度 | 主ユースケース（モバイル） | 方針 |
|---|---|---|---|
| 🔴 放送コンソール | **モバイルファースト** | 通知→出力 health 確認→take/extend・SLATE 復帰・手動速報・CG show/hide | 片手・縦持ち・通知起動を前提に**新規レイアウト設計**。親指操作で即応が完結 |
| 🟡 編成・制作 | デスクトップ主・**モバイル許容まで** | 外出先での確認・承認・軽微な差替 | timeline 等の**編成編集 UI はモバイルで作り込まない（PC 限定）**。モバイルは閲覧＋軽操作に絞る（決定 §11.3） |
| ⚙ 設定 | デスクトップ | — | モバイルは閲覧可で十分 |
| ④ バックオフィス | デスクトップ | 月次帳票 | モバイル最適化は後回し（閲覧可で可） |
| 🟧 ICS-EARTHQUAKE admin | **モバイル対応**（別サービス） | 外出先からの手動速報・履歴確認 | 決定⑥（手動速報は放送面にも置く）と整合。サービス側 admin もモバイルで |

### 11.2 実装の考え方
- **別シェル/別エントリ（決定⑤）= 各面が独自レスポンシブを持てる**。放送コンソールは「通知から起動→1 アクション」に最適化した専用モバイルレイアウトを新規に設計し、他面は既存 DS の `@media`（[[project_mobile_responsive]] / [[project_ds_redesign]]）と studio サイドバー IA のモバイル資産（ハンバーガー/ドロワー/密テーブル横スク/tap 44px・[[project_studio_sidebar_ia]]）を再利用する。
- **放送コンソールのモバイル設計指針**: トップ＝運行ダッシュボードを縦 1 カラム（health → now-playing → 通知の優先順）。主要アクション（take/extend/SLATE/手動速報/CG）は**画面下部の親指リーチ圏に固定**。アラームはバッジ＋色で一目。確認ダイアログは緊急操作の誤爆防止に。
- **CF Access 既設**＝外部到達・認証は追加作業なし。新ホスト（`ops.*`）を足す際の CF Access ポリシー設定のみ。
- **BP は「スマホ / デスクトップ」の 2 段**（決定 §11.3＝タブレットは即応対象外）。タブレット専用 BP は設けず、スマホ or デスクトップにフォールバックさせる。
- **Phase 1 に同梱**: コンソール分割と同時にモバイル対応を作る（後付けより、別シェル新設時に最初からモバイルファーストで組む方が安い・§8 Phase 1）。

### 11.3 確定（2026-06-25 ユーザー回答）
- 🟡 **編成は「モバイル許容」止まり**＝外出先は閲覧＋軽操作（承認/軽微な差替）まで。**timeline 等の編成編集 UI は PC 限定**（モバイルで編集を作り込まない）。
- **即応端末はスマホのみ**＝タブレットは含めない。放送コンソールのモバイル設計は**スマホ（縦持ち・片手）を主対象**とし、BP は実質「スマホ / デスクトップ」の 2 段で設計する。
