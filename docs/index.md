# ICS-TV

circle-ics.com の YouTube 配信を、OBS 手動配信から 24 時間自動配信へ刷新する
Web システム。ウェザーニューズ社の仕組みを参考にした、リニアチャンネルの
プレイアウト自動化。詳細は各ドキュメントを参照。

> ドキュメント運用: 各 doc は冒頭にステータス行（実装済み/設計案/歴史的記録と改訂日。
> 書式は [site-only-broadcast.md](site-only-broadcast.md) 冒頭を参照）を置く。
> **ドキュメントの正は実装** — DDL・ナビ・API 契約は `server/` / `frontend/` のコードが正本で、
> doc はそこへのポインタと設計意図の記録。

## 全体

- [Overview](overview.md) — 全体設計・意思決定ログ（#1〜#27）
- [Requirements](requirements.md) — 要件
- [Operations](operations.md) — 運行・監視（#7 決定 O1〜O10）
- [R2 Layout](r2-layout.md) — R2 バケット/プレフィックス全数棚卸しとライフサイクル（2026-09-02 実測）
- [バックアップと復旧](backup-restore.md) — DB の取得・復元、暗号化フィールドと鍵、書き込み後の復旧
- [Usage](usage.md) — アクセス先・ログイン・ローカル起動・定期タスク・送出ノード・CI/デプロイ

## Design

- [Data Model](datamodel.md) — 中核 17 テーブルの DDL（それ以外の正本は `server/<app>/models.py`）
- [Scheduler](scheduler.md) — リゾルバ/agent の状態機械・CM 枠充填
- [CasparCG](casparcg.md) — AMCP・レイヤ規約
- [CG Layers](cg-layers.md) — CG レイヤ割当と OverlayManager
- [YouTube](youtube.md) — rolling 枠（#3）+ 番組専用枠（#23）
- [Sales](sales.md) — 営放（#6。`series`/`series_slot` と営放・請求系 DDL の正本）
- [Fanclub](fanclub.md) — ファンクラブ/番組公式サイト（#27）
- [Timekeeper Live](timekeeper-live.md) — 生放送タイムキープ（#25）
- [Site-only Broadcast](site-only-broadcast.md) — exposure_policy（#27 ミラー制御）
- [Viewing Experience](viewing-experience.md) — VOD 字幕・タイムシフト（#24）
- [Normalize Offload](normalize-offload.md) — 正規化の Windows オフロード
- [ARIB Safety Zone](arib-safety-zone.md) — CG/時計の画面端基準
- [Backoffice Service Split](backoffice-service-split.md) — 経理系の別リポ分離（Phase 3）
- [Delivery Service Split](delivery-service-split.md) — 納品の別リポ分離
- [Refactor Service Split](refactor-service-split.md) — studio/ops サービス分離の正本
- [(歴史) Delivery](delivery.md) — 納品ポータル設計（**ICS-DELIVERY へ移管済み・歴史的記録**）

## UI

- [UI 全体IA](ui.md) — スタック・共通シェル・中核画面（ナビ/ルートの正本は `frontend/apps/studio/src/nav.ts`）
- [UI Operations](ui-operations.md) — 運行・監視画面（実装は ops.\* React SPA）
- [UI Sales](ui-sales.md) — 営放画面（冒頭の実装状況表参照）
- [(歴史) UI Delivery](ui-delivery.md) — 納品画面（**移管済み・歴史的記録**）

開発フロー ([CONTRIBUTING.md](../CONTRIBUTING.md)) は本 TechDocs の対象外
(nav に無い) なのでリポジトリを直接参照する。
