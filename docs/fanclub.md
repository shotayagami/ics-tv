# #27 ファンクラブ/番組公式サイト サブシステム — 市場調査と機能要件

icstv を **B2B2C プラットフォーム**へ拡張する構想の要件定義。クリエイター（サークル・配信者）が
**配信枠をサブスクリプションで購入**すると、**ICS-TV チャンネルでの放送 + YouTube 同時配信 +
番組公式サイト（ファンクラブ収益化レイヤー付き）**がセットで提供される。クリエイターは自分の
ファンに対してティア制会費で収益化でき、icstv 運営は studio で枠サブスクと公式サイト/FC の
両方を管理する。

本書は実装設計の前段として、(1) 国内外のファンクラブ/メンバーシップサービスの市場調査、
(2) 3 層（ファン/クリエイター/運営者）の機能カタログ、(3) 収益モデルと法務要件、
(4) 既存サブシステムへの適用方針、をまとめる。**データモデル DDL・画面設計は次フェーズ**（§7）。

既存資産との関係: [#6 営放](sales.md)は広告主向けの**広告商流**（CM 枠の販売）であり、本書の
**会員商流**（番組枠と会費の販売）とはドメインを分ける。ただし `sponsorship`（番組提供契約 =
series 単位・月額・日割り）は枠契約モデルの相似形として参照する。会員課金は稼働済みの
`subscriptions` app（Stripe）を拡張し、番組公式サイトは `Series` 公開ページ（slug URL +
`SeriesPost` + `AudienceForm`）を基盤にする。YouTube 同時配信は
[#23 番組専用枠](youtube.md)（`ProgramBroadcast` + encoder tee）の延長。

## このサブシステムの決定ログ

| # | 論点 | 決定 | 補足 |
|---|------|------|------|
| F1 | YouTube 同時配信の宛先 | **運営 ch 内専用枠とクリエイター個人 ch 宛の両対応（契約プラン選択制）** | 同一ストリームを両 ch へ二重配信すると重複コンテンツ判定（収益化剥奪）リスクがあるため、プランで宛先を選ぶ方式とし既定では二重配信しない。個人 ch 宛は per-creator OAuth・API クォータ/Compliance Audit・実績帰属の規約整備が前提（§5.5）。運営 ch 宛は #23 の既存実装を流用できるため先行フェーズとする（§7） |
| F2 | FC 課金単位 | **クリエイター単位**（Patreon 型） | `Creator` を第一級モデルとして新設し、配下に複数 `Series` を束ねる。会費はクリエイター単位 1 本で、そのクリエイターの全番組の FC 限定コンテンツにアクセス可。番組公式サイト自体は Series 単位で払い出す（F6） |
| F3 | 決済構成 | **B2B（枠サブスク）/ B2C（FC 会費の分配）とも Stripe Connect（destination charge）に統一**（2026-07-27 実装・追認）。当初案の国産 PSP 収納代行（PAY.JP Platform / KOMOJU Platform Model）は不採用 | 分配は資金決済法（為替取引該当性）の論点が残る（§5.3）。当初は Stripe Connect を国内実務ノウハウ不足で見送る方針だったが、実装が先行し Stripe Connect destination charge が稼働済みのため、追加コストの大きい PSP 切替は行わず現行方式を正式採用とした。★收納代行としての整理・弁護士レビューは実装後に残課題として持ち越し（§5.3・§7） |
| F4 | 本書の範囲 | **市場調査 + 機能要件カタログまで** | DDL・API・画面設計は次フェーズ。§6 は方針レベルの適用指針に留める |
| F5 | ティアモデル | **YouTube チャンネルメンバーシップ型の段階課金 + 機能制限。最下層に無料ティア（登録のみ）を認める** | クリエイターがティアを価格昇順で複数定義（上限は設定可能、既定 3〜6 程度）。上位ティアは下位特典を包含する**累積型**。エンタイトルメントは「契約ティアレベル ≥ コンテンツ要求レベル」の単純比較で解決。レベル 0 = 無料会員（決済不要・会員登録のみ、Patreon 無料メンバーシップ/ニコニコのフォロー相当）は有料転換ファネル兼お知らせ配信先。アップグレード = 即時 + 日割り（Stripe proration）、ダウングレード = 期末適用 |
| F6 | 番組公式サイトの汎化とゲーティング | **FC サイトは「番組公式サイト基盤」として汎化し、ブログ・公開動画もティア基準で閲覧制御** | 無料公開レイヤー（番組情報・お知らせ・アーカイブ導線）と会員ゲートレイヤーを分離。`SeriesPost`（ブログ）・アーカイブ/VOD にコンテンツ単位で必要最低ティアを設定でき、閲覧制御の尺度は「完全公開（未登録可）< 無料会員以上 < 有料ティア N 以上」の一本化されたレベル軸。[site-only-broadcast.md](site-only-broadcast.md) の `exposure_policy` はこのレベル軸へ統合する（§6.4） |

## 1. 構想の全体像

```
[ファン (B2C)]              [クリエイター (B2B)]            [icstv 運営]
Member 登録                 配信枠サブスク購入 ──────────▶ 枠契約・請求管理 (Stripe Billing)
  │ 無料ティア加入 (level 0)   │ セットで提供                  オンボーディング審査
  │ 有料ティア加入 (level 1..N) ├ ICS-TV 放送 (Series/SeriesSlot)  マルチテナント管理
  ▼                          ├ YouTube 同時配信 (F1: 宛先選択)   分配精算 (収納代行, F3)
番組公式サイト閲覧            └ 番組公式サイト (Series 単位)      モデレーション/規約対応
  ├ 完全公開: 番組情報/お知らせ    │
  ├ 無料会員以上: 限定ブログ等     ▼
  └ ティアN以上: 限定配信/       ティア設計・コンテンツ投稿・
     アーカイブ/限定動画 (F5/F6)   公開範囲設定・売上レポート
       会費 ──▶ (収納代行) ──▶ クリエイターへ分配 (手数料控除)
```

- 主体は 4 種: `Member`（視聴者、既存）/ `Creator`（新設、F2）/ スタッフ `User`（既存）/
  `DeliveryAccount`（既存、納品）。Creator は Member・User のどちらとも別の第一級エンティティ。
- 収益は二階建て: **枠サブスク（クリエイター → 運営、固定額）**を主軸とし、
  **FC 会費（ファン → クリエイター、運営は手数料控除）**を従とする（§4.2）。

## 2. 市場調査サマリ

2026-07 時点の調査。料率・価格は変動するため実装時に要再確認。

### 2.1 国内クリエイター向け FC プラットフォーム

| サービス | 運営元 | 手数料/還元 | 特徴 |
|---|---|---|---|
| Fanicon | THECOO | 会費側で収益化。グッズ 1.5%・チケット 0% | タレント・アイドル向け、ガチャ等の演出 |
| Bitfan | SKIYAKI | ブラウザ 80% / アプリ 50% 還元 | オールインワン、会費 120〜5,500 円の 15 段階 |
| Fantia | とらのあな系 | 12.5% + ユーザー側 8%（2025-11 改定） | 同人文化圏、単品/コミッション/投げ銭 |
| pixivFANBOX | ピクシブ | 10%（R-18 は 12.9%） | イラスト・漫画中心 |
| FaM | Nagisa | 最大 80% 還元 | ノーコード FC サイトビルダー |
| CHIP | Rinacita | 10% | スマホで 5 分開設、無料お試しプラン |
| Fanpla Kit | Fanplus | 非公開（従量） | 音楽アーティスト老舗、300 組実績 |
| L4U | L4 | 20% | 専用アプリ化、1 対 1「2 ショット」機能 |
| OFUSE | OFUSE | 約 9% | ファンレター文化、投げ銭 + 月額 |
| Fensi | CAM | 非公開 | 受注生産グッズ（在庫リスクなし） |
| FANTS | スタメン | 10〜30% 程度 | オンラインサロン全般、モデレーション/Q&A |
| Ci-en | エイシス | 月額 10/13%、単発 15% | 同人・ゲーム制作、DLsite 連携 |
| FANCLOVE | — | 8% | 低手数料訴求 |

### 2.2 芸能人・アーティスト公式 FC

| サービス | 形態 | 価格 | 特徴 |
|---|---|---|---|
| FAMILY CLUB (STARTO) | 直営 | 入会金約 1,000 円 + 年会費 4,000〜9,000 円超 | 入会金 + 年会費モデル、紙媒体併存 |
| 乃木坂46 Mobile | 直営 | 月 550 円 | 会員資格がチケット先行・グッズ抽選の実質参加条件 |
| PRIMAL FOOTMARK (Amuse) | 直営 | 年刊フォトブック購入 | 会費と物理特典のバンドル |
| Bitfan / Fanpla Kit | SaaS | 初期・月額無料、会費は主催者設定 | 公式 FC 構築の SaaS 化が主流 |
| EMTG / Plus Member ID | 共通 ID 基盤 | 登録無料 | 複数 FC 横断の共通会員 ID + 電子チケット |
| Weverse (HYBE) | プラットフォーム | 月額 2〜4 ドル、運営 30〜60% 保持 | 階層別サブスク、広告なし高画質視聴、独自通貨 |

従来型 FC の標準機能セット: 会報/ブログ、チケット先行抽選、バースデー特典、デジタル会員証、
会員番号/継続年数表示、グッズ会員割引、オフラインイベント、アプリ/メール通知、継続特典。

### 2.3 海外メンバーシップ

| サービス | 手数料 | 特徴 |
|---|---|---|
| Patreon | 新規一律 10% + 決済 2.9%+$0.30 | ティア自由設計、2025 年ネイティブライブ配信、Discord 風チャット（最大 4ch）、ギフトサブスク、win-back オファー（5〜90% 自動割引）、**無料メンバーシップ** |
| Ko-fi | 5%（Gold $12/月で 0%） | Shop 一体運用 |
| Buy Me a Coffee | 5%（Gold $5/月で 0%） | 単一プランのシンプル構成 |
| Memberful | 4.9% + $25〜100/月 | 既存サイト埋め込み型、チャーン分析 |
| Substack | 10% | ニュースレター中核、無料購読ティア標準 |
| Fourthwall | デジタル 5%（Pro で免除） | 印刷オンデマンド物販 |
| Mighty Networks | $41〜/月 + 1〜3% | コミュニティ機能中核 |
| Discord Server Subscriptions | 10% | 既存サーバーへのオーバーレイ型 |
| Ghost | 0% + $15〜199/月 | 手数料ゼロ + 固定費モデル |
| Uscreen | $49〜/月 + $0〜1.99/購読者 + TVOD 5〜10% | VOD + ライブ + コミュニティの OTT 一体型。icstv 構想に最も近い機能構成 |

### 2.4 ライブ配信×サブスク / 興行チケット

| サービス | 収益化 | 特徴 |
|---|---|---|
| YouTube メンバーシップ | 月額 $0.99〜$99.99 の**最大 6 レベル** | **上位レベルは下位特典を包含する累積型**（F5 の直接の参照モデル）。勤続 8 段階バッジ、メンバー限定動画/ライブ |
| Twitch | Tier1〜3 月額 | 2023-10 マルチストリーミング原則解禁 |
| ニコニコチャンネルプラス | 約 83% 還元 | **動画/生放送ごとの粒度の高いゲーティング**（F6 の参照モデル）、フォロー（無料）併存 |
| SHOWROOM | 都度チケット + 投げ銭 | ファンレベル可視化 |
| ツイキャス | カード 80% / アプリ 60% 還元 | メンバー限定チケットの二段階ゲーティング |
| OPENREC.tv | 55% 還元、最大 3 プラン | サブスク会員はアーカイブ見放題 |
| 17LIVE | 投げ銭 + 階級制月額 | 視覚的特典の演出が濃厚 |
| ZAIKO / Streaming+ / PIA LIVE STREAM / Stagecrowd | 都度課金電子チケット | 興行型。+Archive 方式、1 チケット = 1 デバイスの多重視聴防止等 |

### 2.5 決済・マーケットプレイス基盤

| サービス | 手数料目安 | 特徴 |
|---|---|---|
| Stripe (Billing + Connect) | カード 3.6% + Billing 0.4〜0.7% | 海外実績豊富。国内資金決済法対応は自己責任範囲が大きい |
| PAY.JP (+ Platform) | 2.59〜3.3% | **Payouts 型はテナント個別審査不要**（小規模創作者の収容に有利） |
| GMO-PG / fincode | 3.0〜3.5% 目安 + 初期費用 | テナント管理機能、大規模向け |
| KOMOJU | カード 3.25%、コンビニ 2.75% | **Platform Model（収納代行構成）のドキュメントが充実** |
| Univapay | 2.8%〜 | 多通貨 150 種以上 |

### 2.6 放送枠・チャンネル枠の時間貸しの前例

「時間帯の専有権を第三者へ定額販売する」モデルは SaaS の世界に直接の前例がほぼ無い一方、
放送業界には性質の異なる 3 つの前例が実在する。

| 前例 | 商品 | 現状と教訓 |
|---|---|---|
| 委託放送事業者制度（CS 放送、2011 年廃止） | 帯域・チャンネル枠の月極/年極リース | 最大時ほぼ 100 社が参入し大半が赤字撤退。**帯域使用料が視聴者数と無関係の固定費**として発生する構造が原因（反面教師、§4.1） |
| CATV 自主放送のタイムセールス | 番組枠（タイム CM・PT） | 現役だが料金は個別見積が主流。枠買付を仲介する専門ブローカーが存在する程度にはニッチ市場が実在 |
| コミュニティ FM / ネットラジオの冠番組買い取り | 週 1 回×分数の番組枠 | **現役で、公開料金表あり**。FM まいづる: 週 1×10 分 月 2.5 万円〜60 分 月 10 万円。Tokyo Star Radio: 週 1×10 分 月 4〜8 万円〜55 分 月 12〜24 万円（時間帯変動）。ラジオフチューズ: 週 1×29 分 月 9.35 万円、59 分 月 17.6 万円（**スタジオ・技術料・CM 放送料・ネット同時放送料込み**） |

ラジオフチューズの「放送 + インターネット同時放送料」一式課金は、icstv の
「放送 + YouTube 同時配信」セットの料金テンプレートとして規模感・商慣行の両面で最も近い参照点。

## 3. 機能カタログ（3 層 × Must/Should/Could）

優先度: **Must** = ローンチ（β 含む）に必須、**Should** = 競合水準に達するため早期実装、
**Could** = 差別化・将来拡張。各機能に前例サービスを付記。

### 3.1 ファン向け（番組公式サイト会員側）

**Must**

- 会員登録・ログイン（既存 `Member` 基盤を流用） — 全社共通
- **無料ティア（レベル 0）への登録のみでの加入**（F5） — Patreon 無料メンバーシップ、Substack 無料購読、ニコニコのフォロー
- **階層ティアからの選択加入とアップグレード/ダウングレード**（F5: 累積型、即時アップ + 日割り/期末ダウン） — YouTube（6 レベル）、Bitfan（15 段階）、Patreon
- **ティアレベルに応じた限定コンテンツ閲覧**: ブログ（`SeriesPost`）・画像・限定動画（F6） — Fantia、FANBOX、Patreon、ニコニコ
- 会員限定ライブ配信の視聴（要求ティア以上） — Fanicon、Bitfan、Patreon、Uscreen
- 継続課金中のアーカイブ見放題視聴（ティア基準、F6） — YouTube、ニコニコ、OPENREC、ツイキャス
- オンライン完結の解約導線 — 特商法対応の前提（§5.2）、業界共通義務
- デジタル会員証（会員番号表示） — Fanicon、Bitfan、Fanpla Kit

**Should**

- 会員限定チャット・コメント（要求ティア設定可） — Patreon（Discord 風・最大 4ch）、Substack Chat
- 投げ銭・ギフティング — OFUSE、17LIVE、YouTube Super Chat
- 勤続バッジ・継続年数のロイヤリティ演出 — YouTube（8 段階）、Twitch、17LIVE
- Q&A・アンケート — Bitfan、Mighty Networks（既存 `AudienceForm` を流用可能）
- ギフトサブスク（他ファンへの贈答） — Patreon、Substack
- 誕生日特典の自動配信 — Bitfan、FAMILY CLUB
- 単品コンテンツ課金・バックナンバー購入 — Fantia、FANBOX
- 解約時の win-back オファー（割引提示） — Patreon（5〜90% 自動）、Uscreen

**Could**

- サブスク一時停止（pause） — Patreon のみ標準搭載、先行実装で差別化余地
- ファン同士のコミュニティ（掲示板） — FANTS、Mighty Networks
- 継続年数によるステージ制・ポイント逓増 — 一部公式 FC
- ガチャ/スクラッチくじ — Fanicon、FaM
- グッズ EC 連携（受注生産含む） — Ko-fi Shop、Fourthwall、Fensi
- 1 対 1 トーク・有料 DM — Bitfan、L4U
- 多通貨・多言語対応 — Bitfan、ZAIKO

### 3.2 クリエイター向け（配信枠購入者の管理・収益化）

**Must**

- 配信枠サブスク契約状況の確認（放送スケジュール・ステータス） — icstv 固有、参考: ニコニコの番組枠予約 UI
- 番組公式サイト初期設定（サイト情報・ブランディング） — 各 SaaS 共通の初期フロー
- **ティア設計 UI**（F5: 無料 + 有料 N 段の価格・特典差分、累積特典の定義） — YouTube、Bitfan、CHIP、Patreon
- **コンテンツ投稿と公開範囲設定**（F6: 完全公開 / 無料会員以上 / ティア N 以上のレベル選択） — ニコニコのコンテンツ単位フラグ制御
- 会員数・売上の基本レポート（ティア別内訳含む） — Fanpla Kit、FANTS
- 決済・入金（振込サイクル）の確認 — 各社共通

**Should**

- ファン分析（視聴傾向・エンゲージメント） — bitfan analysis
- モデレーション（コメント削除・ユーザーブロック・通報対応） — FANTS、大手共通
- キャンペーン設定（割引・無料体験期間） — FANTS、CHIP
- チャーン分析（解約率・解約理由） — Memberful、Uscreen
- 誕生日等トリガー配信の自動化 — Bitfan
- 収益ダッシュボード（会費・投げ銭・単品課金の横断表示） — Patreon、Uscreen
- YouTube 配信先設定（F1: 個人 ch 宛プラン利用時の OAuth 認可フロー・配信プリセット選択）— icstv 固有。個人 ch 宛プランを提供するフェーズでは Must に昇格
- 持ち込み楽曲の権利確認ガイド・事前申請ワークフロー — VTuber 業界実務（アカペラ・自作オケ・フリー音源優先、CD/サブスク音源は事前申請制）を参考（§5.4）

**Could**

- グッズ EC・受注生産の設定 — Fensi、Fourthwall
- ガチャ・くじの景品設計 — Fanicon
- コミッション（個別依頼）受付 — Fantia
- 外部連携（Discord ロール自動付与等） — Ko-fi、Fourthwall
- 多言語・海外ファン向け設定 — Bitfan、ZAIKO

### 3.3 プラットフォーム運営者向け（studio / backoffice 側）

**Must**

- 配信枠サブスク契約・請求管理（B2B、F3: Stripe Billing） — 参考: Uscreen の固定 + 従量複合
- クリエイターオンボーディング（本人確認・審査・特商法表記収集） — PAY.JP Platform / KOMOJU の オンボーディングフロー
- 番組公式サイトのマルチテナント管理（Creator/Series 単位の払い出し・停止） — Fanpla Kit、FaM
- 決済分配・精算処理（収納代行構成: 分別管理・非滞留、F3） — KOMOJU Platform Model、PAY.JP Payouts 型
- 特商法表記の一括管理・個人クリエイター住所非公開設定 — BASE（2022-01 実装）
- 解約導線の全テナント横断での標準実装 — 特商法対応（§5.2）
- プラットフォーム全体のモデレーション・規約違反対応 — FANTS

**Should**

- 全体売上ダッシュボード（枠サブスク + FC 会費、クリエイター別/全体） — Patreon Insights、Uscreen
- インボイス登録状況・源泉徴収要否の管理 — 税務実務（§5.6）
- チャージバック管理 — 3D セキュア 2.0 前提の運用（§5.6）
- 放送枠スケジューリングと FC の連携（会員向け視聴予約・先行案内） — 乃木坂46 Mobile 型の「会員 = 参加資格」設計
- YouTube 同時配信の規約・運用管理（F1: 宛先プラン管理、重複コンテンツ回避の差異化運用） — §5.5
- YouTube API 連携基盤（per-creator OAuth 管理・Compliance Audit 対応） — クリエイター数×配信頻度で API 利用量が線形に伸びるため早期にロードマップ化（§5.5）
- JASRAC/NexTone 包括利用許諾契約の締結・楽曲権利処理ワークフロー — §5.4

**Could**

- 前払式ポイント/独自通貨 — 供託義務（§5.3）のため当面非推奨
- 多通貨・海外クリエイター対応 — Weverse
- クリエイター専用ドメイン・UI カスタマイズ — L4U、FaM
- 興行チケット型の単発イベント配信（枠と別建て） — ZAIKO、Stagecrowd
- YouTube MCN 登録 — 当面不要（§5.5）
- 原盤権の個別交渉サポート窓口 — §5.4

## 4. 収益モデル設計

### 4.1 配信枠サブスクの価格設計（5 指針）

1. クリエイター規模（想定ファン数）に応じた複数の固定枠プラン — Uscreen の階層設計とコミュニティ FM の時間帯別価格の折衷
2. 時間帯・頻度（毎日/週 1）・YouTube 同時配信の有無/宛先（F1）による多段階料金 — コミュニティ FM の標準慣行（§2.6）
3. 番組公式サイト基本機能利用料は枠料金に内包し、初期費用は取らない — Bitfan/Fanpla Kit 型の「クリエイター負担ゼロ」慣行
4. **損益分岐点シミュレーションの提示**: 例として枠が月 10 万円なら会費 500 円 × 200 人が分岐点。CS 放送の失敗（価格先行・集客後追い）を避け、想定クリエイター層のファン規模・会費水準から**逆算して枠価格の上限を決めるボトムアップ設計**とする
5. 立ち上げ期（ファン基盤ゼロ）向けの段階的料金・初期割引 — CS 放送の「軌道に乗るまでの空白期間を吸収する仕組みの欠如」への対策

### 4.2 FC 手数料率の相場と方針

| 方式 | 相場 | 代表例 |
|---|---|---|
| 会費一括徴収型 | 実質 20%（ブラウザ）/50%（アプリ内） | Bitfan、Fanicon |
| 都度課金型 | 10〜17.5% | Fantia、FANBOX、Ci-en |
| 海外定額比例型 | 5〜12% + 決済費 | Patreon、Substack、Ko-fi |
| 手数料 0% + 固定費型 | 0% + $15〜199/月 | Ghost |

icstv は「枠サブスク（B2B）+ FC 会費（B2C）」の**二階建て**であり、両方に高率手数料を重ねると
クリエイターの手取りが薄くなりすぎる。**枠の固定収益を主軸とし、FC 会費側の手数料は
10% 前後かそれ以下**（Ghost/Ko-fi 型に近い整理）が二階建てと相性が良い。
R-18 等センシティブコンテンツの可否は決済代行の審査コストに直結（FANBOX/Ci-en の R-18
上乗せ事例）するため、初期に方針決定する（§7 未決事項）。

ティア価格の参考: YouTube $0.99〜$99.99（6 レベル）、Bitfan 120〜5,500 円（15 段階）。
既存 `subscriptions.Plan` の松竹梅 3 段と同水準の 3〜6 段を既定とする（F5）。

### 4.3 決済構成（F3: Stripe Connect に統一・2026-07-27 追認）

> **本番状態 (2026-09 時点)**: 本番 `icstv-secret` には `STRIPE_*` キーが **1 つも投入されて
> いない** (実測 22 キーに含まれず)。したがって本節の「稼働」はコードのデプロイ済みを指し、
> 課金は本番では**実行時失敗** (Checkout 503) する (チップ/ギフトは**追加提供側**の機能で、
> このツリーには含まれない)。有効化は Stripe の利用登録
> (ユーザ作業) が先行する。

- **枠サブスク（B2B）**: 既存 Stripe Billing を流用し自社売上として処理(実装済み・2026-07-28)。
  契約(`SlotContract`)ごとに金額が異なるため事前作成 Price ではなく Checkout 時の price_data で
  都度動的に組む。Connect は使わずプラットフォーム直接課金(FC 会費側の destination charge とは
  別体系)
- **FC 会費（B2C、分配あり）**: 当初案の国産 PSP 収納代行（PAY.JP Platform / KOMOJU Platform Model）
  ではなく、**Stripe Connect Express + destination charge**（`transfer_data.destination` +
  `application_fee_percent`）で実装した。Checkout 時点でクリエイターの Connect アカウントへ
  都度自動送金される構成のため、プラットフォーム側に資金が滞留しない。`FcSettlement` 元帳
  （§6.6・#27 Phase B）が集金額・手数料・送金額を記録する参照専用の会計台帳を担う
- 当初は「Stripe Connect は国内実務ノウハウが手薄なため見送り、国産 PSP を弁護士レビュー後に
  導入」の方針だったが、実装が先行して Stripe Connect 方式のコードが本番へデプロイ済み
  (稼働は上記のとおり STRIPE_* 未投入で開始前) のため、
  追加コストの大きい PSP 切替は行わず現行方式を正式採用する判断とした。
  **★収納代行としての整理・資金決済法上の該当性については実装後の残課題として弁護士レビューが
  必要**（§5.3）。PSP 切替の再検討は将来の海外展開等で Stripe Connect の限界が顕在化した場合のみ
- **ネイティブアプリ課金は採らず Web 決済で完結**させる。アプリ内課金は実質 30% 控除
  （Bitfan 80%→50%、ツイキャス 80%→60%）であり、Web 完結は構造的優位（競合の弱点回避）

## 5. 法務・運用上の必須要件

> 本節は 2026-07 時点の調査に基づく整理であり法的助言ではない。★印は**ローンチ前に
> 専門家レビュー必須**の項目。

### 5.1 放送法上の位置づけ

- icstv の CDN 配信（HTTP ユニキャスト）は放送法上の「放送」ではなく著作権法上の
  「自動公衆送信」に分類される可能性が高い。ABEMA・radiko・TVer が同様の法的ポジション
  （24 時間編成のリニアチャンネルでも放送事業者ではない）の先行事例
- ポジティブ面: 放送免許・番組編集準則・認定/登録義務等の規制コストを負わない
- ネガティブ面: 放送事業者向けの簡易権利処理（放送同時配信等のみなし許諾、著作権法 63 条
  5 項）の対象外 = 通常の個別許諾プロセスが必要（§5.4）
- ★事業化前に放送法・著作権法に詳しい弁護士のレビューを受ける

### 5.2 特定商取引法

- 通信販売としての表記義務（11 条の 15 項目）
- サブスク特有の**申込み最終確認画面での 6 項目表示**（数量・価格・契約期間・解約条件/方法・申込期間等）
- **オンライン完結の解約導線**（2022 年改正対応）。虚偽説明・引き止めは契約取消権リスク。MVP から組み込む
- 個人クリエイターの**住所・電話番号の非公開運用**: 運営が連絡先機能を担う合意があれば非公開
  （開示請求対応）にできる（BASE 2022-01 実装例）。個人配信者の参入ハードルを下げる差別化要素

**現状（2026-07-28 時点）**: 上記のうち解約導線は member 向けサブスク（`subscriptions`）・
FC 有料ティア（`fanclub` CreatorMembership）とも Stripe Customer Portal への誘導で実装済み。
申込み最終確認画面（6 項目表示）はこの 2 系統に実装済み（「加入する」ボタンが Checkout
セッション作成へ直接 POST せず、まず確認画面 `/subscriptions/checkout/<slug>/confirm/` /
`/fanclub/<slug>/tiers/<id>/confirm/` を経由するよう変更）。SlotContract（枠サブスク・B2B）は
クリエイター（事業者）が営業として締結する契約のため特商法 26 条 1 項 1 号の適用除外に
該当すると考えられ、本節の対象外として確認画面は設けていない（★この整理自体は弁護士レビュー
未実施）。11 条の 15 項目の静的表記は運営 `/tokushoho/` とクリエイター単位
`/fanclub/<slug>/tokushoho/` で従来どおり別途対応済み。

### 5.3 資金決済法

- ★FC 会費のクリエイター分配は原則「為替取引」に該当しうる。①契約成立への不可欠な関与 +
  利用規約での取引条件明確化、②資金の非滞留（速やかな精算）、③分別管理、を満たす
  **「収納代行」としての整理**が実務標準（クリエイター取り分 90% 程度が相場観）。
  該当性は実質判断のため利用規約・精算サイクルの設計込みで弁護士レビュー必須
- **現状（2026-07-27 時点。ただし本番は §4.3 冒頭のとおり STRIPE_* 未投入で実収益フロー開始前）**:
  実装は Stripe Connect destination charge が本番へデプロイ済みで、キー投入後は
  Checkout の都度 Stripe 側でクリエイター Connect アカウントへ自動送金される（②資金の非滞留は
  構造的に満たす。Stripe が決済代行事業者として関与するため③分別管理も Stripe 側の枠組みに
  依拠）。ただし①③の法的整理・利用規約の取引条件明確化・全体の該当性判断について
  **弁護士レビューは未実施のまま実装が先行した**。ローンチ前（本番での実収益フロー開始前）に
  弁護士レビューを実施すること
- 前払式支払手段（サイト内ポイント）は未使用残高 1,000 万円超で供託義務（残高の 1/2 以上）。
  **立ち上げ期は導入しない**
- 2025 年資金決済法改正（クロスボーダー収納代行）は国内分配のみなら直接適用されにくいが、
  海外ファン課金を見込む場合は政令指定状況を継続確認

### 5.4 著作権処理（音楽・原盤・二次利用）

- ★**JASRAC/NexTone 双方との契約が原則必要**（管理団体が曲ごとに異なる二元体制）。icstv の
  「編成型・非リクエスト型の常時配信 + 非放送事業者」という性質は既存の使用料規程区分に
  明確に当てはまらず、**JASRAC ネットメディア部との個別協議が必要**。参考: インタラクティブ
  配信区分で情報料等の 4.5〜7.7%・最低月額 5,000 円（最新料率は要確認）
- **YouTube 同時配信分**: YouTube は JASRAC/NexTone と包括契約済みのため管理楽曲は追加支払
  不要が通説。ただしライブ配信規約は配信者に権利保有の表明保証を義務付けており、管理外楽曲
  （原盤・独自アレンジ）はカバーされない。**この責任をクリエイター向け利用規約へ転嫁する条項**が必要
- **原盤権は集中管理団体が存在せず個別交渉**。許諾は「生配信 OK・アーカイブ不可」等の条件付きに
  なりやすく、アーカイブ見放題（§3.1 Must）との整合を契約時に確認。クリエイター向けには
  VTuber 業界実務（アカペラ・自作オケ・フリー音源優先、CD/サブスク音源は事前申請制）を参考にした
  ガイドラインを整備（§3.2 Should）
- **権利帰属**: クリエイターが著作権を保持したまま icstv へ必要範囲の利用許諾を与える設計。
  「ICS-TV 放送」「YouTube 同時配信」「アーカイブ化」「切り抜き」「FC 限定利用」を項目分けして
  許諾範囲を明記し、**契約終了トリガーでのアーカイブ自動非公開フロー**をシステム・契約の両面で用意
- **FC 限定コンテンツの限定性担保**: YouTube「限定公開」は URL 漏洩に無防備で不十分。
  **署名付き URL/トークン認証（視聴者・動画固有の自己失効型）+ 視聴期限**が実装コストとの
  現実的な落とし所（Vimeo OTT 等の実装参考、商用 DRM は投資対効果が見合わない）。加えて
  会員規約での再配布禁止・違反時の資格剥奪条項、restream 時に限定コンテンツが誤って無料公開
  されない編成側ガードレール（`exposure_policy` 統合、§6.4）の契約・技術二重担保

### 5.5 YouTube 規約・API（F1 関連）

- YouTube には他プラットフォームとの同時配信を制限する規約はない（Twitch と異なり元来オープン）。
  一方 **「同一運営者が 2 つの YouTube チャンネルへ同一内容を同時配信する」構成は未規定の
  グレーゾーン**で、重複コンテンツ（Reused content）の機械判定 → 収益化剥奪（チャンネル単位）の
  リスクがある。**同一バイトストリームの完全複製配信が最もリスクが高い**ため、F1 では
  プラン選択制（既定で二重配信しない）とし、両方へ出す場合はサムネイル・タイトル・
  オーバーレイの意図的差異化を運用要件とする
- **実績の帰属**: 個人 ch 宛は登録者・視聴時間がクリエイター資産として蓄積（離脱時も持ち出し可）、
  運営 ch 宛は駆け出しクリエイターが実績を借りられる反面持ち出し不可。**restream の技術設計より
  先に利用規約レベルで確定させる**（音楽配信代行業界で係争になりやすい論点）
- **収益化はチャンネル単位で判定**（YPP Tier1: 登録者 500 人 + 3,000 時間/90 日、Tier2: 1,000 人 +
  4,000 時間/90 日）。ネットワーク実績での補完は不可
- **API クォータ**: Data API 既定 1 日 10,000 ユニット。超過には Compliance Audit（審査、数週間）
  合格が必須。クリエイター数×配信頻度で線形に増えるため早期にロードマップへ組み込む
- **MCN 登録は当面不要**: YPP 基準の緩和にはつながらず、参入審査も厳格化済み。ブランド
  アカウントのマネージャー権限 + API 連携による代行運用で足りる。Content ID 等の権利管理
  ニーズが顕在化した段階で再検討

### 5.6 決済セキュリティ・年齢確認・税務

- EMV 3-D セキュア（2.0）は国内 EC 加盟店へ義務化済み。候補 PSP はいずれも標準対応
- 「サービス内容相違」「二重課金」等の**非不正チャージバック**は 3DS でカバーされない。
  解約後の請求停止・重複課金防止の運用を作り込む
- 未成年対応: 法定代理人の同意のない未成年契約は取消し可能。成人確認チェック・未成年者利用
  不可の明記・保護者からの取消請求窓口を用意。クレーム対応は既存チケット基盤（OTOBO）で吸収
- 税務: オンボーディング時に**インボイス登録有無の確認フロー**（免税事業者前提）。分配金の性質
  （業務委託報酬か収納代行の取次精算か）で源泉徴収義務が変わるため★構成込みで専門家確認。
  免税事業者からの仕入税額控除経過措置（2026-10 以降 3 年間 50%）を分配額計算に織り込む

## 6. 既存仕様への適用方針

DDL・画面設計は次フェーズ（F4）。ここでは接続点と方針のみ確定する。

### 6.1 Creator の新設（F2）

- `Creator` を Member/User/DeliveryAccount と並ぶ第 4 の主体として新設。`Series` に
  `creator` FK を追加し 1 Creator = 1..N Series。既存 `Series.cast`（自由テキスト）は表示用に残置
- 認証は #20 納品ポータルの Google 招待サインイン方式（`DeliveryInvitation` → OAuth バインド）を
  先行事例として流用を検討
- オンボーディング（本人確認・審査・特商法表記収集・インボイス確認）は運営 studio 側の管理画面

### 6.2 subscriptions app の拡張（F5）

- 現行 `Plan`（サイト全体・松竹梅）/ `MemberSubscription`（`OneToOneField(Member)`）を、
  クリエイター別ティアへ拡張: `CreatorTier`（creator FK・level 0..N・price、level 0 = 無料）+
  `CreatorMembership`（member × creator で unique、tier FK、Stripe subscription id は無料ティアでは
  null）の方向。既存のサイト全体プランは併存
- Stripe Checkout / Webhook 冪等化（`ProcessedStripeEvent`）/「状態は Webhook のみが更新する」
  設計はそのまま流用
- エンタイトルメント解決は既存 `subscriptions/services.py` の boolean 特典方式を
  **レベル比較方式**（契約 level ≥ 要求 level）へ拡張。無料会員は level 0、未登録は level なし

### 6.3 番組公式サイト基盤（F6）

- `Series` 公開ページ（slug URL）+ `SeriesPost` + `AudienceForm` を基盤に汎化。無料公開レイヤー
  （番組情報・お知らせ・アーカイブ導線）は現行のまま、会員ゲートレイヤーを追加
- `SeriesPost`・VOD/アーカイブ視聴権に**必要最低ティア（level）**フィールドを追加し、
  閲覧判定は §6.2 のレベル比較に一本化

### 6.4 exposure_policy との統合（実装済み・2026-07-28）

- [site-only-broadcast.md](site-only-broadcast.md) の 4 プリセット（public / site_public /
  site_members / members_yt_site。実装済み）は「会員か否か」の 2 値ゲート。
  本サブシステムのレベル軸（未登録 < 無料会員 < ティア N）へ拡張して統合実装した
  （`scheduling/exposure_gate.py`、既存の `Program.fc_required_level` を VOD 側 (`vod_visibility=
  fanclub`) と共有しつつライブ視聴にも適用。`vod_visibility` に関係なく値が設定されていれば常に
  ライブ視聴のゲートになる）
- 共有 Live Input（`Channel.cf_playback_hls_url` は全番組で同一URL）の露出経路を
  `scheduling.exposure_gate.hls_url_for` 一箇所に集約し、`/api/v1/channels/{slug}`（プレイヤー本体）・
  `/api/v1/home`（ホームのヒーロー/カードのライブプレビュー）・`/live/<slug>/poster.jpg`
  （ライブ静止画）の3経路すべてで同じ判定を通す設計にした。導入前は `channel_detail` のみが
  サイト会員軸のゲートを実装しており、home/poster がそれを素通りしていた
  （「共有 Live Input がゲートされていない問題」の実体）
- restream 時に FC 限定コンテンツが無料公開へ漏れない編成側ガードレール（§5.4）は
  `fanclub/tasks.py::_on_air_program`（creator 個人 YouTube チャンネル宛シミュルキャストの
  on-air 窓判定）に `fc_required_level is not None` の除外条件を追加して担保した
- ゲート理由の語彙は `''|login|subscribe|fc_join|fc_unavailable`（サイト会員軸の
  `login/subscribe` とファンクラブ軸の `login/fc_join/fc_unavailable` を統合。未ログインは
  どちらの軸が原因でも `login` で共有し、プレイヤー UI 側は汎用的な「ログインが必要です」文言に
  倒した上で、ログイン済みでティア未加入の場合のみ `fc_join`＝「ファンクラブ限定です」の専用導線を出す）
- 署名トークン（`core/hls_auth.py`、docs/site-only-broadcast.md §4.7）はサイト会員軸・
  ファンクラブ軸のどちらのゲートでも同じ仕組みで発行するようにした。**2026-07-30 追記**:
  当初はアプリ層の HMAC 検証のみでエッジ実強制が本番未配線だったため「URL を API 越しに
  渡さないことが実効ゲート、署名は流出時の追加防御」に留まっていたが、**Cloudflare Worker
  によるエッジ強制を本番で有効化済み**（`HLS_AUTH_ENFORCE = "true"`）。token 無しのアクセスは
  エッジで 403 になり、署名は装飾ではなく実効的なゲートになった。詳細は
  docs/site-only-broadcast.md §5 リスク#3

### 6.5 YouTube 同時配信（F1）

- 運営 ch 宛: #23 の `ProgramBroadcast` + `YoutubeBroadcastPreset` + encoder tee を流用（先行フェーズ）
- 個人 ch 宛: 当初想定の per-creator `YoutubeCredential`（OAuth）+ Compliance Audit は見送り、
  クリエイター自身が YouTube Studio で取得した永続ストリームキーを creator ポータルで手動設定し、
  Cloudflare Stream Live Output でシミュルキャストする方式で実装した（2026-07-27、§9）。
  宛先はプランで選択し既定では二重配信しない（§5.5）

### 6.6 管理画面の分担

- 日常運用（ティア設計・投稿・モデレーション・オンボーディング・枠契約管理）: 本体 studio
  （Django + HTMX + React islands）。クリエイター向けセルフサービス画面も studio 系ホストに新設
- 経営集計（FC 収益・クリエイター別分配・チャーン）: icstv-backoffice の read API seam を拡張
  （`GET /api/v1/internal/backoffice/fanclub` 系の新設が自然）
- 依存方向は S6 を踏襲: **scheduling / medialib / playout → fanclub の依存を作らない**。
  24/7 送出の安全性を最優先し、fanclub 不在でも現行送出がそのまま動く構造を保つ

## 7. フェーズ分けと未決事項

> 2026-07-27 改訂: 当初想定(PSP 段階導入) より実装が先行し、有料ティア加入・FC 会費の
> Stripe Connect 分配・分配元帳(`FcSettlement`)まで Phase A の枠組みのまま実装済みとなった
> （§4.3・§9）。以下は現状に合わせた区分。

| Phase | 内容 | 状態 |
|---|---|---|
| A（β・実装済み） | Creator 新設 + 無料/有料ティア（Stripe Connect destination charge）+ 分配元帳（`FcSettlement`）+ 番組公式サイト（ブログのレベルゲート）+ 運営 ch YouTube 専用枠（#23 流用）+ クリエイター個人 ch 宛シミュルキャスト（ストリームキー手動設定方式、per-creator OAuth 不使用）+ 枠サブスク Stripe Billing（自動課金）+ 特商法対応（申込み最終確認画面・解約導線）+ ライブ限定配信のレベルゲート（exposure_policy 統合、**実装済み・2026-07-28**） | 実装済み・dev マージ済み |
| B | JASRAC/NexTone 個別協議 + ★資金決済法・収納代行整理の弁護士レビュー（§5.3、実装は先行済みだが未レビュー）+ per-creator OAuth 前提の Google Compliance Audit（現行のストリームキー手動方式で当面代替） | ★要外部対応 |

未決事項（実装前に確定させる）:

- ★弁護士レビュー: Stripe Connect destination charge の収納代行としての整理（利用規約・精算サイクル）、放送法/著作権法ポジション、源泉徴収の整理（§5）
- JASRAC/NexTone 個別協議の開始時期と料率
- FC 手数料率の確定（§4.2 は 10% 前後以下の方針のみ。実装は `settings.STRIPE_CONNECT_APPLICATION_FEE_PERCENT` で運用中）
- R-18 等センシティブコンテンツの可否方針（決済審査に直結）
- ティア数上限の既定値（3〜6 の間）
- 枠プランの具体価格表（§4.1 のボトムアップ設計で策定）

## 9. Phase A 実装状況（2026-07-25 完了）

§6 の適用方針に沿って **Phase A（無料ティア + ブログ/VOD のレベルゲート + creator.\* ポータル）を
実装完了**。データモデル・API・画面の詳細設計は本書の対象外だったため（F4）、実装後の要点を記録する。

### データモデル（新app `fanclub`）

- `Creator`（name/slug/description/status）: Member/User/DeliveryAccount に次ぐ第4の主体
- `CreatorSeriesLink`: `sales.CmAdvertiserLink` と同型（series 側を OneToOne+PK、schedulng への
  依存は文字列参照 `"scheduling.Series"` のみで import なし）
- `CreatorTier`（level0=無料は常設・アプリ層で自動整備、有料は `stripe_price_id`）/
  `CreatorMembership`（member×creator 一意、退会後の再加入は既存行を再利用、Stripe
  customer/subscription id を保持）/ `SlotContract`（枠契約台帳、依然 Stripe Billing なしの
  手動請求）/ `FcSettlement`（2026-07-27 追加。FC 会費の分配元帳、参照専用の会計台帳）
- `CreatorAccount` / `CreatorInvitation`（トークンURL方式の招待）
- scheduling 側は `SeriesPost.fc_required_level` / `Program.fc_required_level`（プレーン int、
  fanclub への FK なし）+ `VodVisibility.FANCLUB` を追加するのみ

### エンタイトルメント解決（`fanclub/services.py`）

`member_level` / `can_view_level`（NULL=完全公開 < 0=無料会員以上 < n=有料ティアn以上、creator
停止中/未紐付はフェイルクローズ）/ `fc_gate_reason`（''/login/fc_join/fc_unavailable）/ `join`・
`leave`・`join_free_tier`。**2026-07-27 更新**: 当初の `JOINABLE_LEVELS = {0}`（無料ティアのみ
加入可能に絞るグローバル定数）は撤去済み。有料ティアは `tier_is_joinable(tier)`
（`stripe_price_id` 設定済み AND `creator.stripe_connect_onboarded`）で個別に解禁され、
`fanclub/views.checkout` → Stripe Checkout（destination charge）→ webhook が
`CreatorMembership` を作成/更新する（`join()` は無料ティア専用のまま、有料ティアを渡すと
即座に拒否）。

### ゲート統合

`scheduling/vod.py`（`can_watch`/`gate_reason`）と `core/views.py`（`_render_series_detail`/
`public_series_post`/`public_vod_detail`）に接続。ロック中の `SeriesPost` は本文/メディア/OG
descriptionを一切テンプレへ渡さない設計。一覧（`GET /api/v1/series/{id}`）は投稿を隠さず
`locked`/`gate_reason` を付けて返す「ロック表示」方針（発見性を優先）。

### 認証・ホスト構成

`creator.*` を第4のホストとして新設（`HostUrlconfMiddleware` に分岐追加）。Google 招待サインイン
は `core/admin_views.py` の YouTube OAuth（PKCE付き Flow）を流用しつつ、id_token 検証は
`google.oauth2.id_token` によるローカル検証（tokeninfo への外部 HTTP 往復が不要）。

### 管理画面

- studio SPA: 新ナビグループ「ファンクラブ」→ `/creators`・`/creators/:id`（タブ: 基本情報/番組/
  ティア/招待/枠契約/会員）。`SeriesPost` 管理は既存の `SeriesDetailPage` に新タブ「投稿」として統合
- creator.* セルフサービス（SSR）: dashboard/ティア設計/番組閲覧/投稿CRUD（テナント境界=他
  クリエイターの Series は 404）/会員集計（ティア別内訳のみ、個々の会員の PII は返さない）/
  枠契約閲覧

### 実装スコープの調整（F1〜F6 からの変更点）

- **F1（YouTube宛先プラン選択制）は当初 per-creator OAuth 前提で Phase C 相当として見送っていたが、
  OAuth を使わない代替方式で実装した**（2026-07-27）。クリエイター自身が YouTube Studio で取得した
  永続ストリームキーを creator ポータルで手動設定し、Cloudflare Stream の Live Output で
  シミュルキャストする。共有 Live Input を常時タップすると契約枠外の番組まで配信されるため、
  Celery beat が `CreatorSeriesLink` 経由の on-air 窓に合わせて Output の enabled を切替える
  （`fanclub/tasks.py`）。`SlotContract.youtube_destination` は当初の台帳のみから実体化された。
  共有 Live Input（CasparCG 本線の出力をそのまま常時ミラー）自体を exposure_policy に応じて
  ゲートする仕組みは無いため、`fc_required_level`/`site_members` 等でゲート対象の番組は
  `fanclub/tasks.py::_on_air_program` が防御的にシミュルキャスト対象から外す
  （**2026-07-28 追記**: `fc_required_level` の除外条件を追加し、サイト側でティア限定にした番組が
  クリエイター自身の公開 YouTube チャンネルへ無条件流出することを防いだ。専用ミラー channel
  相当の分離が要る本線 Live Input 自体のゲートは引き続き対象外）
- **F3（決済構成）は Stripe Connect に統一**（§4.3・§7）。有料ティアの実加入
  （`fanclub/views.checkout` → Stripe Checkout destination charge → webhook）と
  FC 会費の分配元帳（`FcSettlement`、`invoice.payment_succeeded`/`charge.dispute.*` webhook で記録）
  を実装済み。当初案の国産 PSP 収納代行は不採用（§4.3 に理由を記載）
- **F6（VOD/アーカイブのゲート）は Phase A に前倒し**した。当初 §7 の Phase 表は VOD ゲートを
  Phase B（exposure_policy 統合）としていたが、`scheduling/vod.py` への `FANCLUB` 分岐追加は
  低コストだったため、ユーザー決定 F6（「ブログ・公開動画もティア制御」）どおり Phase A に含めた。
  当時 Phase B に残った**ライブ限定配信**（exposure_policy のエッジ認証部分）も
  **2026-07-28 に実装完了**し Phase A へ吸収した（§6.4 参照）。旧 Phase C（弁護士レビュー等の
  外部対応）を新 Phase B として §7 の表を整理した

### 未実装（残作業）

> 2026-07-28 改訂: 当初この節に列挙していた項目のうち複数が実装済みとなった(下記に反映)。
> 有料ティア実加入・FC会費の分配元帳(Stripe Connect)・クリエイター個人ch宛シミュルキャスト
> (ストリームキー手動設定方式)・枠サブスクのStripe Billing化・特商法の申込み最終確認画面・
> ライブ限定配信のティアゲート(exposure_policy統合)は完了。詳細は各所の更新履歴参照。
> 残るのは弁護士レビュー等の外部対応(§7 Phase B)のみ。
>
> **2026-08-10 改訂**: 上記の「残るのは外部対応のみ」は誤りだった。§3.1 ファン向け Must の
> うち**ティアのアップグレード/ダウングレード**と**デジタル会員証(会員番号表示)**の2件が
> 未実装のまま残っていた(前者は有料在籍者を Stripe カスタマーポータルへ丸投げしており、
> しかも `webhook._sync_from_subscription` が `tier` を同期していなかったためポータル側で
> 変更されても DB のティアが追随しなかった。後者は会員番号の概念自体が無かった)。
> **2026-08-10 に両方実装**(下記「ティア変更」「デジタル会員証」)。

- ★資金決済法・収納代行としての整理の弁護士レビュー（§5.3、Stripe Connect destination charge は
  実装済みだが未レビューのまま本番稼働している）

### ティア変更（アップグレード/ダウングレード、2026-08-10 実装）

F5 の「累積型・即時アップ + 日割り/期末ダウン」を実装した。ティアは累積型で level の昇順が
特典の包含順序と一致するため、方向判定は level 比較で行う（`price_jpy` は同額の並列ティアが
あり得るので判定には使わない）。

- **アップグレード**: `Subscription.modify` の `proration_behavior="always_invoice"`。差額の
  日割り請求をその場で確定し、上位ティアの特典を次回請求日を待たずに開放する。
  `application_fee_percent` / `transfer_data` はサブスク側の設定なので Price 差し替えでは
  失われず、destination charge の分配構成は維持される
- **ダウングレード**: Subscription Schedule の2フェーズ構成（現フェーズ = 現行 Price をそのまま /
  次フェーズ = 新 Price を1周期、`end_behavior="release"`）。**支払い済み期間の特典を
  取り上げない**ことを優先した。既存スケジュールがあるときは作り直さず差し替える
  （`SubscriptionSchedule.create(from_subscription=...)` は二重作成を許さず、
  仮に通っても請求が二重化する）。予約の取り消しは `SubscriptionSchedule.release`
- **ティアの真値は Stripe 側**: `webhook._apply_tier_from_subscription` が
  `customer.subscription.updated` の明細 Price から `CreatorTier` を引いて在籍ティアを同期する。
  これによりアプリ経由の変更・期末の自動適用・カスタマーポータルからの変更のすべてが
  同じ経路で DB へ反映される。どのティアにも一致しない Price のときは**触らない**
  （未知の Price で在籍ティアを壊さない fail-safe）
- `CreatorMembership.pending_tier` / `pending_tier_effective_at` は**画面表示専用の写し**で、
  適用の実体は Stripe のスケジュール側にある。期末が到来して tier が切り替わった時点で
  Webhook がこの2列をクリアする
- 「Stripe 系フィールドは Webhook のみが更新する」規律の例外として、アップグレード成功時のみ
  `services.change_tier` が `tier` を進める。`modify()` が成功した時点で Stripe 側の Price は
  確定しており推測ではないため（`_sync_from_checkout` が metadata から tier を確定させるのと
  同じ扱い）、後続の `customer.subscription.updated` は同じ値を冪等に再確認するだけになる
- **既知の限界**: `application_fee_percent` が Subscription Schedule の
  `default_settings` に継承されることは実 Stripe での確認が要る（テストは monkeypatch のため
  未検証）。ダウングレード予約を1件でも通したら、初回は Stripe ダッシュボードで
  connected account への送金額を実測して確認する

### デジタル会員証（2026-08-10 実装）

`CreatorMembership.member_no`（creator 単位の連番）と、公開サイト側の
`/members/fanclub/`（加入中一覧）・`/members/fanclub/<slug>/card/`（会員証）を追加した。

- 採番は `services.ensure_member_no` が **creator 行のロック下**で行う。既存の membership 行だけを
  `select_for_update` しても同時実行の INSERT は防げず同番になりうる（ファントム）ため、
  「その creator への採番」という単一の資源を creator 行で表現している
- 採番点は加入時（無料 = `services.join` / 有料 = `webhook._sync_from_checkout`）。
  実装前から在籍していた行は migration 0009 の `RunPython` で **加入順**
  （`joined_at` → `id`）に backfill する。表示時の遅延採番だけに任せると
  「先に会員証を開いた人」の順になり、加入順と食い違った番号が永久に残る
- 退会後の再加入は行を再利用する設計なので**番号も引き継がれる**（会員証の番号が変わらない
  ことをファンに保証する）。番号は creator 単位なので、他クリエイターの会員数は漏れない
- 継続月数（`enrolled_months`）は応当日が来ていない月を数えない（4/10 加入なら 5/9 までは0ヶ月）
- 会員証は本人の在籍分のみ。他会員の slug を指定しても一覧へリダイレクトする

**ライブ限定配信ティアゲートの既知の限界（2026-07-28、実装後のアドバーサリアル レビューで発見・
意図的に本フェーズの対応外とした事項）**:

- 署名トークン（`core/hls_auth.py`）は channel + 有効期限のみを検証し、番組/ティアを
  一切見ない。**発見当時（2026-07-28）は** CDN/送出ノード側のエッジ強制が本番未配線だったため、
  公開番組の時間帯に取得した URL（署名無し）や、下位ティアで正当発行されたトークンを、
  同じ共有 Live Input 上で後から始まるゲート対象番組にもそのまま使い回せてしまっていた。
  **2026-07-29 訂正（本番実測）**: 旧記述は対処を「Cloudflare Stream `requireSignedURLs`
  有効化」としていたが、実配信経路は ABR ladder 移行後 `https://tv.yagamin.net/hls2/<slug>/master.m3u8`
  （送出ノードの nginx 静的配信 → k8s ingress-nginx → Cloudflare プロキシ）であり、
  CF Stream は再生経路にいないため**この対処は成立しない**。かつこの URL は固定パスで
  **認証なしに誰でも 200 が取れる**（実測）ため、本線ライブのゲートは実質「UI に URL を出さない」
  だけであり、署名トークンは検証者が存在せず装飾的。したがって TTL 短縮等のアプリ層のみの変更で
  実効性は生まれない。**2026-07-29 に (b) Cloudflare Worker を採用・実装**
  (`deploy/cloudflare-worker-hls/`)。24/7 送出の可用性を Django に
  連結させないことを最優先した。併せてサーバ側は完全公開の番組にも署名を付ける方式へ変更している
  (トークン無しで再生できる経路が残るとエッジ強制が成立しないため)。
  **2026-07-30 完了**: 監視モード (`HLS_AUTH_ENFORCE = "false"`) での判定確認 —
  正当 token=ok / 無し・改竄・期限切れ・別 channel=invalid — と実再生の連鎖
  (master → 子プレイリスト → セグメント) を確認したうえで **enforce へ切替済み**。
  token 無しのアクセスはエッジで 403 になる。**これによりこの節の冒頭に挙げた
  「トークンの使い回し」は、公開番組時間帯の署名無し URL については解消**した
  (完全公開の番組にも署名を付ける方式へ変えたため、署名無し URL がそもそも成立しない)。
  **残る限界**: トークンは channel と期限しか束縛せず、番組/ティアの境界をエッジからは
  判定できない。したがって下位ティアで正当発行されたトークンは有効期限内であれば
  同じ channel の後続番組でも通り、エンタイトルメント変更が効くまでの最大遅延は TTL
  (`ICSTV_HLS_TOKEN_TTL_SEC`、既定 3600 秒) そのもの。ロールバックは
  `HLS_AUTH_ENFORCE = "false"` に戻して `wrangler deploy` (反映は数秒、伝播に十数秒)。
  詳細は [site-only-broadcast.md](site-only-broadcast.md) §5 リスク#3 に集約した
- ~~`fanclub/tasks.py::reconcile_creator_youtube_outputs` は1分周期のポーリングのため、
  番組が公開からファンクラブ限定へ切り替わってから最大約60秒、クリエイター自身の
  公開YouTubeチャンネルへゲート対象の映像が中継され続ける~~ → **2026-07-29 に最大約10秒へ短縮**。
  ビート周期を 60 秒 → 10 秒にし、CF API が詰まったときに tick が積み上がらないよう
  `expires: 25` を付けた（遅れた tick は捨て、次の tick が冪等に回収する）。
  **残る限界**: 番組境界の到来は DB 書き込みを伴わないため Django のシグナルでは捕捉できず、
  ポーリングである以上ゼロにはならない。ゼロ遅延にするには境界時刻を eta とする Celery タスクを
  事前投入する機構が要るが、編成変更のたびに旧タスクを取り消す設計が必要で、現行規模では
  費用対効果が見合わないため見送った（`resolver.py` の PlayoutEvent 方式は送出エージェント側の
  実行系列で、CF API 呼び出しは相乗りできない）
- ~~`_on_air_program`（同ファイル）は creator に紐づく全 series を横断して `.first()` で
  1件のみ取得するため、同一クリエイターが複数チャンネルへ同時に出演する編成では、
  一方がファンクラブ限定なだけで他方（無関係の完全公開番組）の YouTube 中継まで
  巻き添えで止まりうる~~ → **2026-07-29 修正済み**。on-air 判定を channel 単位
  （`_on_air_programs` + `_is_simulcastable`）へ変更し、ゲート対象は「その channel を候補から
  落とす」だけに留めた。宛先ストリームキーは `Creator.youtube_destination_stream_key` の1本
  しか無いため有効化するのは引き続き高々1 channel で、候補が複数あるときは現に有効な channel を
  優先する（番組境界ごとに宛先が入れ替わって YouTube 側の配信が切れるのを避ける stickiness）。
  併せて `.first()` が DB の行順序に依存していた点も `(start_at, channel_id)` 順で固定した
- ~~`live_poster_serve` のキャッシュ判定はリクエスト時点の状態で決まるため、公開→ゲート対象へ
  切り替わる境界の最大10秒(`SERVE_TTL`)の間に発行された「公開扱い」のキャッシュ済みレスポンスが
  CDN 上に残っていれば、切り替わり後の一瞬だけそのまま配信されうる~~ → **2026-07-29 に編成境界
  分を修正済み**。public キャッシュの `max-age` を次の編成境界（現在番組の `end_at`、現在番組が
  無ければ次番組の `start_at`）までの残り秒数へクランプし（`core/views._poster_max_age`）、
  境界を跨いで公開扱いのレスポンスが残らないようにした。併せて、ゲート対象時に返す 404 にも
  `private, no-store` を付けた（`.jpg` は CDN 既定でキャッシュ対象になりやすく、無指定だと
  ゲート解除後も 404 が居座って全視聴者へ配られる逆向きの漏れがあった）。
  **残る限界**: 放送中に管理画面から `fc_required_level`/`exposure_policy` を直接書き換える
  アドホックな変更は編成境界を伴わないため、このクランプでは追随できず最大 `SERVE_TTL` 秒の
  窓が残る。閉じるには Cloudflare の Zone Cache Purge 権限を新規発行して能動パージする必要がある。
  **2026-07-29 の本番実測で判明した前提**: Cloudflare zone の Browser Cache TTL が 14400 秒
  (4時間) に設定されており、`/live/<slug>/poster.jpg` のレスポンスは**ブラウザ向けの `max-age` が
  4時間へ上書きされて返る**（`cache_level: aggressive`、Page Rules は 0 件）。ただし
  `cf-cache-status: EXPIRED` が出ることから **CF のエッジキャッシュはオリジンの `Cache-Control`
  (クランプ済みの値) に従って失効・再検証している**ため、本項目が問題にしていた「共有キャッシュに
  公開扱いのレスポンスが残って他の視聴者へ配られる」経路は塞がっている。残るのは各視聴者の
  ブラウザキャッシュだけで、ホームのライブ静止画は `?ts=floor(now/10)` のバケット化により
  10 秒ごとにキャッシュキーが変わるため実質的に回避されている。zone 設定を
  「Respect Existing Headers」へ変えればアプリの意図がブラウザまで一貫するが、zone 全体の
  ブラウザキャッシュ挙動が変わるため**触らない判断**とした (2026-07-29)

## 10. クリエイター個別ページの機能棚卸し（2026-08-10 調査）

§3.1 の Must 消化後に残る大物が「ファンクラブとしての**クリエイター個別ページ**」。現状の公開ホストは
`/series/<id>/`（シリーズ単位の番組紹介サイト）しか無く、**クリエイターを主語にしたページが存在しない**
（FC 参加導線もシリーズ詳細のサイドカードに間借りしている）。`Creator` モデルも name/slug/description +
法務/Stripe 欄のみで、カバー画像・アバター・テーマカラー・SNS リンク等の見た目に関わるフィールドがゼロ。
主要 5 サービスのクリエイターページ構成を調査し、必要機能を洗い出した。

### 10.1 各サービスのページ構成要素

| 要素 | Patreon (新ページ) | FANBOX | Fantia | Fanicon | Bitfan |
|---|---|---|---|---|---|
| カバー画像 | ✓ (2500×1000 推奨) | ✓ | ✓ | ✓ | ✓ (メイン画像) |
| アバター/ロゴ | ✓ | ✓ (pixiv 連動) | ✓ | ✓ | ✓ (アイコン+ロゴ別) |
| プロフィール文 | ✓ (About、左カラム) | ✓ | ✓ | ✓ | ✓ |
| 外部リンク/SNS | ✓ (カスタムリンク) | ✓ | ✓ | ✓ (会員証にSNS連携) | ✓ |
| 投稿フィード | ✓ (Home のシェルフ) | ✓ (5形式) | ✓ (予約投稿可) | ✓ (タイムライン) | ✓ (ブログ+動画埋込) |
| ロック投稿のプレビュー | ✓ | ✓ | ✓ | — (完全会員制) | ✓ |
| プラン比較カード | ✓ (専用ページ) | ✓ (ファンカード付) | ✓ (人数上限/ギフト) | — (単一プラン中心) | ✓ (プラン別会員証) |
| ページ内カスタマイズ | シェルフ並替/カスタムタブ | ポートフォリオ | — | — | **カラー/フォント/レイアウトテンプレ** |
| デジタル会員証 | — | ✓ (プラン別デザイン) | ✓ (ファン証明書) | ✓ (+バースデーカード) | ✓ (**番号形式/プラン別デザイン**) |
| チップ/投げ銭 | — | — | ✓ (投稿単位) | ✓ (ライブ中ギフト) | ✓ |
| 会員限定チャット | コミュニティ | — | トーク | **グルチャ+1on1** | グルチャ |
| バックナンバー販売 | Collections 単位販売 | — | ✓ (月単位自動) | — | — |
| EC/チケット | Shop | — | ✓ | ✓ (外部連携) | ✓ |

観点として重要だったもの:
- **Patreon の新ページは「Home タブ + シェルフ」構造**。最近の投稿/コレクション/ライブ/ショップを
  シェルフとして並べ替え・差し替えできる。ティア一覧は本編から追い出して専用ページに置き、
  トップは作品を見せることに徹する
- **Bitfan だけがサイトデザイン customization を本格提供**(カラー/フォント/複数レイアウトテンプレ)。
  他はカバー+アバター+プロフの範囲で、テンプレ切替までは持たない
- **Fanicon は「ページ」より「アプリ内コミュニティ」**。ページ的要素は薄く、グルチャ/ライブ/
  会員証が中核。完全有料会員制で荒らしがいないことを売りにする

### 10.2 icstv に必要な機能の洗い出し

前提: icstv の FC は「番組公式サイト基盤の汎化」(§1) であり、無料公開レイヤーと会員ゲートの分離が
設計原則。Bitfan 型のフルカスタマイズは運用者が居ないと破綻するため、**トークン差し替え
(テーマカラー/画像) + 固定レイアウト**から始める。

**Must（ページとして成立する最低線）**
- 公開クリエイターページ `/fc/<slug>/`: カバー + アバター + プロフィール + SNS リンク +
  ティア比較カード (加入/変更導線 = 既存 `_fc_tiers.html` 流用) + 会員証リンク (在籍者)
- `Creator` プロフィール拡張: `cover_image` / `avatar_image` / `theme_color` / SNS リンク群
- 投稿フィード: 紐付く全シリーズの `SeriesPost` を creator 単位で集約 (ロック投稿は
  既存の locked プレビュー方式をそのまま適用)
- 放送予定シェルフ: 紐付くシリーズの直近 `Program` (EPG から供給、完全公開情報)
- クリエイターポータルでのセルフ編集 (プロフィール/画像/カラー)
- シリーズ詳細 → クリエイターページへの相互導線

**Should（競合水準、§3.1 の Should と重なる）**
- 勤続バッジ (YouTube 型): `enrolled_months` から算出、会員証・チャットで表示
- チップ/投げ銭 (Fantia/Bitfan 型): Stripe 一回払い destination charge、投稿またはページ単位
- 会員限定チャット (Fanicon グルチャ型): 既存 Channels 基盤 (`CommentConsumer` の
  HTTP投稿+WSブロードキャスト方式) を creator room + ティアゲートで流用
- ギフトサブスク (Patreon/Fantia 型): ティアの N ヶ月分を購入 → 引換コード → 受領者が償還
- VOD シェルフ: 紐付くシリーズの見逃し配信を集約表示

**Could（差別化・将来）**
- Patreon 型シェルフ並べ替え / Bitfan 型レイアウトテンプレ
- バースデーカード / プラン別会員証デザイン
- バックナンバー販売 (Collections 単位課金)
- 1on1 トーク・スクラッチくじ・EC

### 10.3 デザインカスタマイズの方針

- 公開サイトの既存ダークテーマ (public_base.html のデザイントークン) を土台に、creator の
  `theme_color` で `--accent` 系トークンだけを差し替える。レイアウト自体は固定
- カバー/アバターは R2 (既存 `thumb_serve` 経路) に置き、未設定時はテーマカラーの
  グラデーションでフォールバック (Fanicon/Bitfan とも画像必須にはしていない)
- Bitfan 型のフォント切替・レイアウトテンプレは、クリエイター数が増えて要望が出てから
  (§10.1 のとおり提供しているのは 5 社中 1 社のみ)

### 10.4 実装状況（2026-08-10）

§10.2 の Must と Should の大半を実装した。**Could は未着手**（Patreon 型シェルフ並べ替え /
Bitfan 型レイアウトテンプレ / バックナンバー販売 / 1on1 トーク / スクラッチくじ / EC）。

**Must（すべて実装済み）**

- 公開クリエイターページ `/fc/<slug>/`: カバー + アバター + プロフィール + SNS +
  ティア比較カード + 会員証リンク。`Creator.status=suspended` は 404（fail-closed）
- `Creator` プロフィール拡張: `avatar_url` / `cover_url` / `theme_color` / SNS リンク群
- 投稿フィード（creator 単位で全シリーズの `SeriesPost` を集約、ロック投稿はプレビュー表示）
- 放送予定シェルフ（紐付くシリーズの直近 `Program`）・番組シェルフ
- クリエイターポータルの「ページ設定」（プロフィール / 画像 / テーマカラー / SNS / チャット公開範囲）
- シリーズ詳細 ⇔ クリエイターページの相互導線

**Should**

- 勤続バッジ: `CreatorMembership.loyalty_badge`（1 / 3 / 6 / 12 / 24 ヶ月）。会員証とチャットに表示
- チップ / 投げ銭: `FcTip`。Stripe Checkout（`mode=payment`）の destination charge 一回払い
- 会員限定チャット: `FcChatMessage` + `Creator.chat_required_level`。投稿は HTTP、配信は WS
- ギフトサブスク: `FcGift` + `CreatorMembership.gift_expires_at`。引換コード方式
- VOD シェルフ: **未実装**（番組シェルフから各シリーズ詳細へ遷移すれば見逃しに到達できるため後回し）

> 注記: 上記のうち**チップ / 投げ銭**（`FcTip`）と**ギフトサブスク**（`FcGift`）は**追加提供側**の
> 機能で、このツリーには含まれない（`server/fanclub/models.py` に両モデルは無く、migration
> `0015_delete_fcgift_fctip` が削除済み。`CreatorMembership.gift_expires_at` の列だけが移行由来の
> 既存行のために残る）。一覧は 2026-08-10 時点の実装記録としてそのまま残す。

### 10.5 実装後の既知の限界（2026-08-10）

意図的にこのフェーズの対応外とした事項。§9 の書き方に倣って残す。

> 注記: 以下のうち**ギフト / チップ**に関する 3 項目（`FcTip` / `FcGift` /
> `expire_gifted_memberships`）は**追加提供側**の機能についての記録で、このツリーには含まれない。
> 当時の記録としてそのまま残す。

- **チャットのゲートは WS 接続時にしか評価されない**。接続を張ったあとに退会 / ティア降格 /
  creator の suspended が起きても、切断されるまで新着発言を受け取り続ける。周期的な再検査か
  「資格喪失時に group から discard する」機構が要るが、退会は稀でセッションも長くないため
  見送った（HLS トークンの TTL と同じ「境界後の猶予」クラスの限界）。**投稿側は毎回 HTTP で
  再検査するため、資格を失った会員が発言することはできない**
- **ギフトの残余期間は 2 経路で失効する**: (1) サブスク加入（`_sync_from_checkout` が
  `gift_expires_at` をクリア。有料契約が上位互換なので意図どおり）(2) 自ら退会（`leave()` が
  クリア）。いずれも返金や日割りの払い戻しは行わない
- **ギフト失効の反映は最大 1 時間遅れる**（`expire_gifted_memberships` の beat 周期）。
  ギフトは期限が事前に確定していて境界の即時性が要らないため、番組境界（10 秒周期）とは
  別の粒度にしている
- **`FcTip` / `FcGift` の PENDING 行は掃除しない**。「Checkout を開いたが支払わなかった」の
  記録として残す（削除すると離脱率が追えなくなる）。会計上は PAID のみを集計する
- **デザインカスタマイズはテーマカラーのみ**。§10.3 のとおりレイアウト / フォントの切替は
  提供しない（調査した 5 社中 1 社のみの機能で、運用者不在では破綻する）
- **チャットの運営モデレーションは Django admin の `deleted_at` 編集のみ**。クリエイター自身に
  よる他会員の発言削除・ブロックは未実装（§3.2 Should のモデレーション項目として残る）

### 10.6 出典（2026-08-10 調査時点）

- Patreon: [Your updated creator page](https://support.patreon.com/hc/en-us/articles/36972391815693-Your-updated-creator-page) /
  [Customize your creator page](https://support.patreon.com/hc/en-us/articles/360026139111-Customize-your-creator-page) /
  [Collections](https://support.patreon.com/hc/en-us/articles/16666733679757-How-to-use-Collections-to-organize-your-work) /
  [Public view](https://support.patreon.com/hc/en-us/articles/360033339832-The-public-view-of-your-Patreon)
- FANBOX: [公式](https://www.fanbox.cc/) / [使い方解説 (ライブトレンド)](https://liver.doneru.jp/pixivfanbox/) /
  [クリエイター登録手順](https://sudare-mochi.net/pixivfanbox-1/)
- Fantia: [機能マニュアル (公式note)](https://note.com/fantia_csteam/n/n9a01a877b0ca) /
  [出来ることおさらい①](https://note.com/fantia_csteam/n/n9e8b25ce212a) /
  [②](https://note.com/fantia_csteam/n/nad1c0200c1c2) /
  [バックナンバー機能](https://x.com/fantia_jp/status/1155462912506564610)
- Fanicon: [機能ヘルプ (公式)](https://fanicon.net/fanicon_feature_guide) /
  [チュートリアル (公式)](https://fanicon.net/support/entries/220) /
  [機能解説 (ライブトレンド)](https://liver.doneru.jp/fanicon/)
- Bitfan: [デジタル会員証設定 (公式ガイド)](https://info.bitfan.id/contents/2340) /
  [サイトデザイン設定 (公式ガイド)](https://info.bitfan.id/contents/5977) /
  [Fanclub サービス概要 (公式)](https://bitfan.id/service/fanclub) /
  [解説 (DIGLE MAGAZINE)](https://mag.digle.tokyo/artist/bitfan-about)

## 11. 主要出典（2026-07 調査時点）

> 旧「§8」。§9/§10 への他文書・コード内参照を保つため、§8 を欠番にして出現順が単調になるよう
> §11 へ振り直した（2026-09。内容は不変）。

- 放送枠時間貸し: [FM まいづる料金](https://775maizuru.jp/price/) /
  [Tokyo Star Radio](https://775fm.com/sponsor/) /
  [ラジオフチューズ放送利用料](https://radio-fuchues.tokyo/usagefee/) /
  [みずほ産業調査(有料放送市場)](https://www.mizuhobank.co.jp/corporate/industry/sangyou/pdf/1015_04.pdf) /
  [総務省 配信サービスとガイドラインの適用関係](https://www.soumu.go.jp/main_content/000771379.pdf)
- YouTube 規約・API: [simulstreaming ヘルプ](https://support.google.com/youtube/answer/16404722) /
  [チャンネル収益化ポリシー](https://support.google.com/youtube/answer/1311392) /
  [API 利用規約](https://developers.google.com/youtube/terms/api-services-terms-of-service) /
  [Quota and Compliance Audits](https://developers.google.com/youtube/v3/guides/quota_and_compliance_audits) /
  [MCN overview](https://support.google.com/youtube/answer/2737059)
- 著作権: [JASRAC 放送等](https://www.jasrac.or.jp/aboutus/detail/broadcast.html) /
  [JASRAC 使用料早見表(配信)](https://www.jasrac.or.jp/users/internet/tariff/) /
  [NexTone 放送・有線放送](https://www.nex-tone.co.jp/copyright/users/broadcasting.html) /
  [文化庁 放送同時配信等ガイドライン](https://www.bunka.go.jp/seisaku/bunkashingikai/kondankaito/kyodaku/93341101.html) /
  [骨董通り法律事務所 原盤権コラム](https://www.kottolaw.com/column/211129.html)
- 各サービスの手数料・機能は各社公式サイト/ヘルプ（2026-07 参照）。料率は改定されるため実装時に再確認
