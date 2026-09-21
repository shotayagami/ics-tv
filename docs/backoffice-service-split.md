# バックオフィス分離 — ICS-BACKOFFICE（別リポ Node/TS ＋ 別DB・read-API-first）

> ステータス: **実装済・運用中**（icstv-backoffice **v0.2.3** 本番稼働・3.0〜3.9 完了。
> 残は **3.10** = billing/rights/analytics の write 移管のみ・§9）。初版 2026-06-26。
> studio リファクタ Phase 3（[refactor-service-split.md](refactor-service-split.md) §7④・経営/権利系を 1 サービスへ束ねる）の詳細設計。
> 目的: studio に散在する **経理請求 / 番組予算 / 権利 / 会員分析** の 4 ドメイン（うち番組予算 = `procurement` は追加提供側へ移り、このツリーには含まれない）を、ICS-TV 本体から**別リポジトリ `icstv-backoffice`（Node/TS・専用 Postgres）**へ分離する。納品（[delivery-service-split.md](delivery-service-split.md)）が「完全分離（書き所有権ごと移管）」だったのに対し、バックオフィスは**読み取り API 先行（read-API-first）**で始め、**書き所有権の移管は最後**にする（積極分離だが経営データの整合を優先）。
> ユーザー判断（2026-06-26）: **フル backoffice 即着手**（別リポ Node/TS + 別DB）。read-API-first・write 移管は段階的。

## 1. 目的・背景

studio（編成・制作コンソール）に、放送運用とは毛色の違う「経営・権利」系の画面が積み上がっている: 広告請求（billing）・番組予算（procurement。番組予算は追加提供側の機能で、このツリーの `procurement` はモデルを持たない殻・studio 画面も無い）・楽曲権利（rights。楽曲報告は追加提供側の機能で、このツリーの rights 画面に残るのは配信権の期限一覧のみ）・会員分析（analytics）。これらは
- 放送/編成とライフサイクルが違う（月次・四半期の経理サイクル、外部報告 JASRAC/NexTone — 楽曲報告は追加提供側）、
- 参照は重い集計（ロールアップ・期間集計）だが更新頻度は低い、
- 一人運用では「数字を見る」用途が中心で、編成 UI と混ざると探しにくい。

そこで **経営ダッシュボード = 別コンソール（別ホスト `backoffice.*`・別リポ）**に切り出す。weather/earthquake/delivery と同じ「別リポ Node/TS・独自 admin・k8s・コンテナレジストリのイメージ・内部 seam」方針。ただし請求や予算は**ICS-TV の編成/契約データから集計**するため、最初は **ICS-TV を真実とする読み取り集約**にとどめ、書き所有権の移管は段階的に行う。

### 1.1 delivery 分離との違い
| | ICS-DELIVERY (Phase 2) | ICS-BACKOFFICE (Phase 3) |
|---|---|---|
| 形態 | 完全分離（書き所有権ごと別DB） | read-API-first（読みは集約・書きは段階移管） |
| seam 方向 | DELIVERY→ICS-TV（完成 asset 登録） | **ICS-TV→集約 read（GET）** が主・write 移管は最後 |
| 初期の真実 | DELIVERY 側 | **ICS-TV 側**（移管完了まで） |
| 難所 | ffmpeg 移植 | ドメイン間結合（billing↔sales↔procurement↔delivery、rights↔scheduling、analytics↔members）の読み取り境界設計 |

> 注記: 表中の `procurement`（番組予算）と rights の楽曲使用は**追加提供側**の機能で、このツリーには含まれない。表は設計当時の比較としてそのまま残す。

## 2. 対象ドメイン（現状 ICS-TV）

| ドメイン | ICS-TV app | 主なモデル | studio 現行面 |
|---|---|---|---|
| 経理請求 | `billing` | BillingPeriod / Invoice / InvoiceLine / BroadcastCertificate / Payment | `api/routers/admin_billing.py` |
| 番組予算 | `procurement` | ProgramBudget / DeliveryCost / Payment は追加提供側として除去済み（`procurement` はモデル 0 件の殻） | `api/routers/admin_procurement.py` は追加提供側で、このツリーには無い |
| 権利 | `rights` | DistributionRight（Song / SongUsage は追加提供側として除去済み） | `rights/views.py`（`rights/sheets.py` の JASRAC 報告 CSV は追加提供側で、このツリーには無い） |
| 会員分析 | `analytics`(+`members`) | ViewerPresence / ProgramView（+ Member 統計） | studio「会員統計」 |

**ドメイン間結合（読み取り境界の設計が肝）**:
- billing ← `sales`（広告契約 SpotOrder/Sponsorship/Advertiser）から請求を起こす。
- procurement ← `delivery`（DeliveryCost が delivery.Delivery/ProductionCompany を参照）← `scheduling`（Series 予算ロールアップ）。**この procurement↔delivery 結合が [delivery-service-split.md](delivery-service-split.md) §11 の 2.10 撤去ブロッカー**。番組予算は追加提供側の機能で、このツリーの `server/procurement/models.py` に ProgramBudget / DeliveryCost は無い。
- rights ← `scheduling`（番組/Program での楽曲使用）。楽曲使用の記録は追加提供側で、このツリーの `server/rights/models.py` に Song / SongUsage は無い。
- analytics ← `members`（会員）+ 視聴ログ。

## 3. 目標アーキテクチャ

```
┌─ icstv-backoffice (別リポ・Node/TS・専用 Postgres・コンテナレジストリ) ──────────┐
│  backoffice.* ホスト: staff(Google OAuth) 経営ダッシュボード                      │
│   経理請求 / 番組予算 / 権利(JASRAC報告) / 会員分析 の集約ビュー + レポート(CSV)    │
│  read-first: ICS-TV read seam を叩いて集計・表示（自前 DB は write 移管時に増やす）│
│  k8s: Deployment(web)+Ingress(backoffice.*)+(後で worker)                        │
└──────────────────────────┬───────────────────────────────────────────────────────┘
                           │ 内部 read API（X-Internal-Token・BACKOFFICE_READ_TOKEN）
                           ▼
┌─ ICS-TV ───────────────────────────────────────────────────────────────────────┐
│  GET /api/v1/internal/backoffice/billing      (期間別 請求/入金サマリ)            │
│  GET /api/v1/internal/backoffice/budget       (番組/シリーズ別 予算 vs 費用)      │
│  GET /api/v1/internal/backoffice/rights       (楽曲使用/権利・JASRAC 報告素材)    │
│  GET /api/v1/internal/backoffice/member-stats (会員数/継続/視聴の集計)            │
│  （write 移管フェーズで該当ドメインの POST/PUT を backoffice へ寄せる）           │
└──────────────────────────────────────────────────────────────────────────────────┘
```

> 注記: 図中の「権利(JASRAC報告)」の集約ビューと `GET /api/v1/internal/backoffice/rights` は**追加提供側**の機能で、このツリーの `openapi.json` にこの path は無い。図は設計当時の全体像をそのまま残している。
> なお、図中の「番組予算」の集約ビューと `GET /api/v1/internal/backoffice/budget` も**追加提供側**の機能で、このツリーには含まれない。path は `openapi.json` に残るが、行は常に空を返す。

## 4. read-API-first 戦略（段階移管）

**前半（読み取り集約）= 本体は無改修に近い**: ICS-TV に「経営集計を返す read seam（GET・X-Internal-Token）」を足すだけ。backoffice はそれを叩いて表示する。studio の該当画面は当面据え置き（二重に見えてよい・cutover で backoffice へ寄せる）。自前 DB はほぼ不要（キャッシュ程度）。

**後半（書き所有権移管）= ドメイン単位で 1 つずつ**:
1. backoffice 側に該当ドメインの DB テーブルを作る（Drizzle）。
2. ETL でデータ移管（delivery 2.9 と同型）。
3. 書き API を backoffice 側に実装し、studio からは backoffice へ誘導（or studio 画面撤去）。
4. ICS-TV から該当 app を撤去（urlconf/models/tasks）+ drop migration。

**順序の要点**: **procurement（番組予算）の write 移管が delivery 2.10 をアンブロックする**。procurement が delivery.Delivery/ProductionCompany への FK 依存を解く（id 参照 + DeliveryCost のスナップショット化 + 必要なら DELIVERY 読み seam）と、本体 delivery app を撤去できる。よって write 移管は **billing → procurement → rights → analytics** の順を推奨（procurement を早めに移して 2.10 を解く）。

> 注記: ここに書かれた番組予算（procurement）の write 移管は完了し、その後**番組予算そのものが追加提供側へ移った**。このツリーに procurement のモデル・書き API・studio 画面は無い（`server/procurement` はモデルを持たない殻）。段落は設計当時の順序判断としてそのまま残す。

## 5. seam 契約（ICS-TV 側に新設する read 内部 API）

weather/earthquake/delivery と同型。`X-Internal-Token`（`BACKOFFICE_READ_TOKEN`）認証・未設定なら 401。すべて **GET（読み取り専用）**。

| API | 役割 |
|---|---|
| `GET /internal/backoffice/billing?period=YYYY-MM` | 請求書/明細/入金の期間サマリ（billing から集計） |
| `GET /internal/backoffice/budget?fiscal_year=YYYY` | 番組/シリーズ別 予算 vs 実費（procurement + scheduling.Series ロールアップ）。番組予算は**追加提供側**へ移ったため、このツリーの実装は口と応答の形だけを残し、行は常に空を返す |
| `GET /internal/backoffice/rights?period=...` | 楽曲使用 → 権利者別集計（rights・JASRAC/NexTone 報告素材）。**追加提供側**の機能で、このツリーの `openapi.json` にこの path は無い |
| `GET /internal/backoffice/member-stats` | 会員数/プラン別/継続率/視聴（members + analytics 集計） |

- 返却は集計済み JSON（行スコープ無し＝staff のみが叩く前提・トークンで保護）。
- write 移管フェーズで、該当ドメインの書き API を backoffice 側へ新設し、ICS-TV read seam は撤去 or 縮小する。

> **運用状態**: `BACKOFFICE_READ_TOKEN` を導入者の配備基盤側の Secret として配らないと、
> `/internal/backoffice/*` は常時 401 になり、backoffice コンソール集計 4 ページ
> （billing / budget / rights / member-stats）は取得失敗表示になる。ローテーション手順は
> [operations.md](operations.md)「分離サブシステム seam トークン」参照。
> なお、上記のうち rights ページ向けの `/internal/backoffice/rights` は**追加提供側**の機能で、このツリーの `openapi.json` にこの path は無い。

## 6. 技術スタック（icstv-delivery に倣う）

- **Node20 / TypeScript / tsx**。Web = Hono。
- **Postgres + Drizzle**（write 移管フェーズで使用。read-first 期はほぼ未使用・キャッシュのみ）。
- **Google OAuth**（staff サインイン。delivery の auth コア＝oauth/session/bind/validate を流用。招待ではなく社内 staff allowlist で十分）。
- **k8s**: Deployment(web) + Ingress(`backoffice.*`)。イメージはコンテナレジストリから配備する。worker は集計バッチが要るフェーズで追加。

## 7. サブフェーズ分解（独立 PR / 各々検証）

> 状態（2026-09-02）: **3.0〜3.9 は ✅ 完了**（§9）。残は **3.10** のみ。

| Sub | 内容 | 場所 | 規模 |
|---|---|---|---|
| **3.0** | 本設計ドキュメント（青写真・seam 契約・read-first 戦略） | ICS-TV docs | ← 今ここ |
| **3.1** | ICS-TV read seam（billing/budget/rights/member-stats の GET 集約・token） | ICS-TV | 中 |
| **3.2** | `icstv-backoffice` リポ scaffold（Node/TS・Hono・k8s・コンテナレジストリ・最小 web + seam client） | 新リポ | 中 |
| **3.3** | Google OAuth（staff）+ 共通シェル（ダッシュボード骨格） | 新リポ | 中 |
| **3.4** | 経理請求ダッシュボード（read・期間別請求/入金） | 新リポ | 中 |
| **3.5** | 番組予算ダッシュボード（read・予算 vs 実費ロールアップ） | 新リポ | 中 |
| **3.6** | 権利ダッシュボード（read・楽曲使用・JASRAC 報告 CSV） | 新リポ | 中 |
| **3.7** | 会員分析ダッシュボード（read・会員/継続/視聴） | 新リポ | 中 |
| **3.8** | **write 移管①: 番組予算 procurement**（DB/ETL/書き API・delivery FK 依存解消） | 新リポ + ICS-TV | 大 |
| **3.9** | **delivery 2.10 アンブロック → 本体 delivery app 撤去**（[delivery-service-split.md](delivery-service-split.md) §11） | ICS-TV | 中 |
| **3.10** | write 移管②③④: billing / rights / analytics（順次）+ studio 該当画面撤去 | 新リポ + ICS-TV | 大 |

> 注記: 表のうち、3.1 の read seam に挙がる rights（`/api/v1/internal/backoffice/rights`）と 3.6 の権利ダッシュボード（楽曲使用・JASRAC 報告 CSV）は**追加提供側**の機能で、このツリーには含まれない（`openapi.json` に該当 path は無い）。表は当時のサブフェーズ記録としてそのまま残す。
> なお、3.5 の番組予算ダッシュボードと 3.8 の write 移管①（番組予算 procurement）が扱う番組予算も**追加提供側**の機能で、このツリーには含まれない（`server/procurement` はモデルを持たない殻）。

**着手順**: 3.1（read seam）→ 3.2（scaffold）を先に固め、3.4〜3.7 の read ダッシュボードで価値を出す。write 移管（3.8〜）は読みが安定してから。**3.8（procurement 移管）を早めに通すと 3.9（delivery 2.10）が解ける**。

## 8. リスク・留意点

- **ドメイン間結合**: billing↔sales↔procurement↔delivery↔scheduling の参照網。read seam の境界（どこまで集計を ICS-TV 側でやり、どこから backoffice 側でやるか）を 3.1 で明確化する。
- **二重表示の整合**: read-first 期は studio と backoffice の両方に同じ数字が出る。真実は ICS-TV（移管完了まで）。混乱回避のため backoffice 側に「集計元: ICS-TV」を明示。
- **会計の正確性**: 請求/入金は金額が絡む。集計ロジックは ICS-TV 側 service を再利用（seam が既存 billing.services を呼ぶ）し、backoffice 側で再実装しない（二重計算の乖離回避）。
- **一人運用の負荷**: サービスがまた 1 つ増える。read-first ゆえ worker/DB は最小から。監視は health + seam 疎通。
- **procurement↔delivery の解き方**: 3.8 で DeliveryCost の delivery FK を id 参照 + スナップショット（vendor 名/delivery タイトル）へ。delivery 側の参照が要る集計は DELIVERY 読み seam を足す（delivery-service-split.md §6 の「将来 read API」をここで実装）。

> 注記: 上記のうち番組予算（procurement）に関わる結合とその解き方は、番組予算が**追加提供側**へ移ったことでこのツリーの課題ではなくなった（`server/procurement` はモデル 0 件の殻で `DeliveryCost` も無い）。一覧は設計当時のリスク評価としてそのまま残す。

## 9. 実装状況（2026-06-26 追記）

3.0〜3.7（設計 / read seam / scaffold / staff OAuth / 4 read ダッシュボード）に続き、**write 移管① 番組予算 procurement を 3.8a / 3.8b に分割**して実装した。

- **3.8a ✅ procurement→delivery FK 切離し（ICS-TV）**: `procurement.DeliveryCost` の `delivery`(FK PROTECT)/`vendor`(FK) を **id 参照 + 名称スナップショット**（`delivery_id`/`delivery_title`/`vendor_id`/`vendor_name`）へ。migration 0002 は名称列追加→FK 生存中バックフィル→`AlterField(db_constraint=False)` で FK 制約のみ DROP→`SeparateDatabaseAndState` で state だけ FK→IntegerField（データ無損失）。`series` は本体残留の scheduling ゆえ FK 据置。これで delivery 本体撤去（§7 の 3.9 / delivery-service-split §11 の 2.10 ブロッカー）の **DB レベル結合が解消**。
- **3.8b ✅ backoffice 側の procurement 所有**: 専用 Postgres + Drizzle で `program_budget`/`delivery_cost`/`procurement_payment` を所有（別DBゆえ Series/Delivery/ProductionCompany/User は id 参照 + 名称スナップショット）。ロールアップは純関数（vitest）+ 薄い drizzle DB アクセス。write コンソール `/procurement` + ETL（ICS-TV→backoffice・id 保存・冪等）。**選択肢（Series/納品）の供給** は当面 **2 リポ構成**: ICS-TV に `GET /internal/backoffice/picker`（Series 一覧 + 直近納品を series/支払先 解決済で返す）を新設し backoffice が叩く。
- **picker の供給元（read 境界の現実解）**: §8 は「delivery 参照は DELIVERY 読み seam」を想定したが、cutover 前は納品の真実が ICS-TV にある。よって picker の deliveries は当面 ICS-TV 由来とし、cutover（3.9）後に ICS-DELIVERY 読み seam へ差し替える（3 リポ化を cutover まで遅延）。→ **差し替え済み（cutover 完了）**: 現行は `server/integrations/delivery_read.py` が ICS-DELIVERY の `GET /api/internal/deliveries`・`/vendors` を読む（`ICS_DELIVERY_READ_URL` は本番 configmap で設定済み・旧 delivery.models フォールバックは Stage G/H で撤去）。`DELIVERY_REGISTER_TOKEN` の配布は導入者側で、配って初めて read seam が稼働する（§5 の運用状態注記参照）。

> 注記: §9 は 2026-06-26 時点の実装ログ。その後**番組予算（procurement）は追加提供側へ移った**ため、このツリーに `ProgramBudget` / `DeliveryCost` / `procurement_payment` のモデルも書き API も無い（migration 0004 で DROP 済・`server/procurement` はモデルを持たない殻）。backoffice 側の `/procurement` コンソール・ETL・picker の納品供給元（`server/integrations/delivery_read.py`）も同様にこのツリーには含まれず、`GET /api/v1/internal/backoffice/picker` は残るが、返すのは本体の `scheduling.Series` だけで `deliveries` は常に空である。記録は当時の実装ログとしてそのまま残す。

### 9.1 cutover 後の到達点と残（2026-09-02 更新）

- **cutover ✅ 完了（2026-06-26〜27）**: backoffice 本番デプロイ（現行 **v0.2.3**）+ DB + ETL +
  seam + OAuth + TLS。続く **3.9（delivery 本体撤去）も 2026-07-08 Stage G/H・v0.8.87 で完了**
  （[delivery-service-split.md](delivery-service-split.md) §11。`admin_delivery.py` /
  `delivery.sheets` 再利用 / `seed_demo` の残依存もそこで解消）。**残は 3.10
  （billing / rights / analytics の write 移管 + studio 該当画面撤去）のみ**。
- **studio 番組予算面の現状（要注意・「参照専用へ降格」ではない）**: backoffice `/procurement` への
  誘導は**案内バナーのみ**で、`frontend/apps/studio/src/pages/ProcurementPage.tsx` には登録フォームが
  3 つ（予算枠追加 / 支払 / 発生費用追加）現存し、`server/api/routers/admin_procurement.py` の
  `POST /admin/procurement/budgets`・`/costs`・`/costs/{id}/pay` も生きている。**操作すれば ICS-TV 側
  DB に実際に書けて、backoffice 所有の procurement データとサイレントに乖離する**。閉塞
  （POST 撤去 or read-only 化）は 3.10 の検討対象。
  - 注記: この乖離は**このツリーには無い**。番組予算は追加提供側へ移り、
    `frontend/apps/studio/src/pages/ProcurementPage.tsx` も
    `server/api/routers/admin_procurement.py` も存在せず、`openapi.json` に
    `/admin/procurement/*` の path も無い。記述は当時の運用記録としてそのまま残す。
- **ETL の実行有無は運用者確認事項**: procurement データの ICS-TV→backoffice 移送（ETL）が本番で
  実行済みかは本書からは確定できない。3.10 着手前に突合すること。
  - 注記: 番組予算の ETL はこのツリーには含まれない（追加提供側の作業）。
