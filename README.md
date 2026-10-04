# ICS-TV 24時間自動配信システム

<p align="center">
  <img src="design/branding/icstv-horizontal.svg" alt="ICS-TV" width="460">
</p>

## ブランド素材

ロゴは [`design/branding/`](design/branding/) にまとめています。`ICS-TV` はプロジェクトブランド、`アイシーエス` は運営・制作主体の親ブランドとして使い分けます。

正式採用前の初版であり、ロゴの色、文字のアウトライン、商標・類似確認は別途行います。

circle-ics.com の YouTube 配信を、OBS手動配信から **24時間自動配信** へ刷新する
Webシステムの設計・実装プロジェクト。ウェザーニューズ社（ウェザーニュースLiVE / SOLiVE）の
仕組みを参考にした、リニアチャンネルのプレイアウト自動化。

## 使い方・アクセス (URL・ログイン)

**[docs/usage.md](docs/usage.md) に利用・運用ガイド** (アクセス先・画面別 URL・ログイン/権限・
ローカル起動・定期タスク・送出ノード・トラブルシュート) をまとめている。要点:

| 環境 | 内部 (導入者の LAN) | 外部公開 |
|------|------|------|
| 本番 (公開・視聴者向け) | `https://tv.<内部ドメイン>` | `https://tv.<公開ドメイン>` (公開番組表) |
| 本番 (管理・フル機能) | `https://studio.<内部ドメイン>` | `https://studio.<公開ドメイン>` (**導入者のアクセス制御**で保護する) |
| ローカル | `http://localhost:8000` | — |

`<内部ドメイン>` は導入者が内部 DNS (split-horizon DNS 等) に用意するドメイン、`<公開ドメイン>` は
外部公開に使うドメインのプレースホルダ
(詳細・注意事項は [docs/usage.md](docs/usage.md) §1)。

主要画面 (`<slug>` = channel スラッグ、**いずれも管理ホスト `studio.*` 限定・公開ホスト
`tv.*` では 404**): 編成 `/scheduling/ch/<slug>/timeline/`、運行 `/ops/ch/<slug>/dashboard/`、
営業 `/sales/allocation/`、請求 `/billing/`、管理 `/admin/`。いずれも staff ログイン
(`is_staff=True`) が必要。公開ホスト (`tv.*`) はチャンネル一覧 `/`・番組表 `/guide/`・
視聴 `/ch/<slug>/`・番組詳細 `/program/<id>/`・検索 `/search/`・VOD `/vod/`・会員 `/members/`・
SPA `/app/` など**視聴者向け画面のみ** (編成/運行/営業/請求/admin は 404)。
納品は別リポジトリ `icstv-delivery` へ移管済み — 本体の `/delivery/` は撤去済みの
ghost ルートで到達不能。

```bash
# ローカルで一通り触る
# ⚠ 先に frontend をビルドして server/frontend_dist へ配置すること (手順は docs/usage.md §4)。
#   未ビルドのまま起動すると compose のバインドマウントでイメージ内蔵の dist が空ディレクトリに
#   隠れ、/studio/ が空白・/app/ が 503 になる。要約:
#     cd frontend && npm ci && npm run build && cd ..   # Node 24
#     (apps/*/dist を server/frontend_dist/ へ配置。コピー先の対応は docs/usage.md §4)
docker compose up -d
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py seed_demo   # demo / demo12345
# → http://localhost:8000/admin/login/ でログイン後、上記 URL (slug=demo1) を開く
```

## ドキュメント

| 文書 | 内容 |
|------|------|
| [docs/requirements.md](docs/requirements.md) | 要件（背景・経緯と機能要件） |
| [docs/overview.md](docs/overview.md) | 設計概要（意思決定ログ・アーキテクチャ・コンポーネント・状態機械・運用・フェーズ） |
| [docs/datamodel.md](docs/datamodel.md) | #1 データモデル（PostgreSQL DDL） |
| [docs/scheduler.md](docs/scheduler.md) | #2 スケジューラ状態機械（リゾルバ + agent実行ループ + 割り込み） |
| [docs/youtube.md](docs/youtube.md) | #3 YouTube枠管理 APIシーケンス（永続liveStream + 4h rolling + WebUI管理） |
| [docs/casparcg.md](docs/casparcg.md) | #4 CasparCG AMCP コマンド設計（レイヤ構成・action→AMCP・CG・生入力・agentプリミティブ・Linux運用） |
| [docs/delivery.md](docs/delivery.md) | **(歴史)** #5 納品ポータル 設計時の元記録。2026 Phase 3.9 で実装は別リポ `icstv-delivery` へ移管済み（本体の `/delivery/` は撤去済みの ghost ルート） |
| [docs/sales.md](docs/sales.md) | #6 営放サブシステム（タイム/スポット契約・契約駆動CM割付・放確・月次締め/放送確認書/請求） |
| [docs/operations.md](docs/operations.md) | #7 運行・監視（ウォッチドッグ/feed断自動退避・復帰・通知基盤・運行ダッシュボード・押え/巻き） |
| [docs/ui.md](docs/ui.md) | UI ワイヤーフレーム（全体IA・共通シェル・ロール表・編成タイムライン・公開番組表・運用画面・素材CM・YouTube枠） |
| [docs/ui-delivery.md](docs/ui-delivery.md) | **(歴史)** UI: 納品ポータル（#5 の画面設計。実装は別リポ `icstv-delivery` へ移管済み） |
| [docs/ui-sales.md](docs/ui-sales.md) | UI: 営放サブシステム（#6 の画面設計。営業＋請求・精算） |
| [docs/ui-operations.md](docs/ui-operations.md) | UI: 運行・監視（#7 の画面設計。押え/巻き・agent_status・通知センター） |
| [docs/viewing-experience.md](docs/viewing-experience.md) | #24 視聴体験の深化（VOD 字幕 faster-whisper 自動生成 + 短窓タイムシフト・Phase A/B/C 分割） |

## 実装深掘りの進行

依存関係順（schema → scheduler → 外部連携 → 実行層）で実装レベルまで詰める。

- [x] #1 データモデル（DDL） → [docs/datamodel.md](docs/datamodel.md)
- [x] #2 スケジューラ状態機械（リゾルバ + agent実行ループ） → [docs/scheduler.md](docs/scheduler.md)
- [x] #3 YouTube枠管理 APIシーケンス（liveStream/liveBroadcast bind・transition） → [docs/youtube.md](docs/youtube.md)
- [x] #4 CasparCG AMCP コマンド設計 → [docs/casparcg.md](docs/casparcg.md)
- [x] UI ワイヤーフレーム（編成タイムライン / 公開番組表 ほか） → [docs/ui.md](docs/ui.md)
- [x] #5 納品ポータル（メディアブランチ相当：納品ワークフロー・QC・用途別一元管理） → [docs/delivery.md](docs/delivery.md)
- [x] #6 営放サブシステム（広告出稿・CM割付・放送確認・請求精算） → [docs/sales.md](docs/sales.md)
- [x] #7 運行・監視（ウォッチドッグ/運行ダッシュボード/延長対応） → [docs/operations.md](docs/operations.md)
- [x] UI: #5-7 サブシステムの画面設計（ロール5分類再編・外部ポータル含む） → ui-delivery / ui-sales / ui-operations

## ライセンス

本プロジェクトは GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later) で提供する。
全文は [LICENSE](LICENSE) を参照。脆弱性の報告は公開の Issue ではなく [SECURITY.md](SECURITY.md)
の手順に従うこと。

**AGPL とは別の条件が付く第三者の素材は [NOTICE](NOTICE) にまとめてある** (地図 CG のジオメトリの
出典、第三者のロゴやブランド標識の扱い、同梱していない依存の位置づけ)。再配布するときはこの
NOTICE も一緒に配ること。

Copyright (C) 2026 アイシーエス

`tools/spdx_headers.py` が対象とするソースファイル (`.py` `.ts` `.tsx` `.js` `.mjs` `.sh` `.css` `.scss` `.proto`) は、
冒頭 (shebang や coding 宣言があればその直後) に SPDX ヘッダ 2 行 (`SPDX-FileCopyrightText` / `SPDX-License-Identifier`)
を持つ。ファイル側の年は開始年 `2026` のまま変えず、年が改まったら上の著作権表示だけを `2026-<現在年>` の形に更新する。

## 開発参加

セットアップ・コミット時の品質ゲート (pre-commit) ・AI コードレビュー (`/code-review`)
の手順は [CONTRIBUTING.md](CONTRIBUTING.md) を参照。

## 確定スタック概要

- **送出**: CasparCG (Linux headless/GPU) ← クラウド
- **配信**: 送出ノードの `icstv-encoder` が ffmpeg tee で YouTube RTMP ingest へ直接 push + ローカル MediaMTX（自前 HLS）。YouTube 枠は 4h rolling 自動生成（放送時間帯外は生成しない）。CF Live Input/Output は本線経路に無い（fanclub simulcast 専用。[docs/youtube.md](docs/youtube.md)）
- **制御**: オンプレ (導入者のサーバ) の Django+PostgreSQL → as-run push → 送出ノードの playout agent（ローカルAMCP）
- **素材**: 共有ストレージ (NAS) の master → 正規化(mezzanine) → オブジェクトストレージ (R2) → 送出ノード prefetch
- **Phase 1**: 1ch・生番組まで・サブスク課金は対象外


## イメージ署名 (配備側でデプロイの必須条件にする場合)

> 注記: この節は**導入者の配備基盤側**のパイプライン (コンテナレジストリへの push・イメージ署名・
> ポリシーエンジンによる検証) の記述で、このツリーには含まれない。このツリーの CI は
> イメージの push も署名も行わない。

署名の無いイメージをポリシーエンジンで拒否する構成を採る場合、CI の署名ステップが
**無署名のまま素通りしていないか**を確認すること。署名鍵を渡す Secret が未設定だと、
署名ステップは **exit 0 で黙ってスキップされ、CI は緑のまま無署名イメージが push される**
形になりやすい。

**止まり方が分かりにくい**点にも注意する。ポリシーエンジンに拒否されても Pod は旧イメージで
動き続け、GitOps コントローラの health も正常のままなので通知が出ない。**サービスは正常なのに
リリースだけが静かに止まる**。同期状態が失敗になっていないか、レジストリ側に署名が
付いているかを確認すること。

ポリシーの設定元と鍵の配り方の正本は導入者の配備基盤側にあり、このリポジトリには含めない。

## レジストリの保持ポリシー (本番 pin は必ず `v*` タグにする)

`v*` タグを持たない repository はレジストリの保持ルールで保護されず、本番 pin が
無言で削除されることがある。理由・確認手順は
[CONTRIBUTING.md](CONTRIBUTING.md) の「レジストリの保持ポリシー」節を参照。
