# UI: 運行・監視（#7 [operations.md](operations.md) の画面設計）

> ICS-TV の UI 仕様は [ui.md](ui.md) を全体の出所（IA・共通シェル・視覚言語・ロール表）とし、
> 本書はそのサブセットとして #7 運行・監視の画面を定義する。本書は主に [ui.md](ui.md) の既存
> 「運用画面（状態 A〜E）」への**差分**（押え/巻き・agent_status・スレート解除・通知センター）として
> 設計する。視覚言語・記号体系は ui.md に従う。

## 運用 ▸ 押え/巻き・ヘルス拡張・スレート解除・通知センター (#7 運行・監視 差分)

本書は docs/ui.md の「運用画面（状態 A〜E）」と「通知パネル（3 層）」に対する **足す/変える差分**として #7 (docs/operations.md, 決定 O1〜O10) で必要になった画面を設計する。既存の記号体系・バッジ語彙・社内共通シェルはそのまま踏襲し、新規ブロックと既存ブロックの接続点を各画面冒頭に明記する。

データモデル: `agent_status` / `notification` / `playout_event`(action に `clear_slate` 追加) / `program`(start_at / end_at)。

更新方式（実装正・2026-09）: 本画面群は **ops.\* React SPA**（`frontend/apps/ops`）として実装済み。🔴放送コンソール（`OpsConsole.tsx`）は JSON API を **5 秒ポーリング**（`POLL_MS = 5000`）、タイムキーパー（`Timekeeper.tsx`）は **2.5 秒ポーリング**に加え **WS push**（`/ws/playout/<slug>/`）で即時更新する（ポーリングは WS 再接続中の欠落に備えた fallback として残す設計）。旧設計の HTMX `hx-trigger="every 2s"` パネル別ポーリング (O5) は React 化で置き換え済み — 以下のワイヤー中の hx-trigger 記述は当時の設計案として読むこと。

---

### 1. NOW PLAYING への押え/巻きコントロール

> 差分: ui.md **状態B（生番組 cut_live 送出中）の NOW PLAYING ペイン**に、残尺/経過表示の直下へ押え・巻きの操作行を足す。状態A（録画 play_asset）には出さない（type=live 限定 / O8）。

- 対象ロール: 運用者 / 管理者
- 活性条件: `program.type=live` かつ on-air（live 区間内）。終了 60 秒前ガード (`ms_until(end_at) < 60_000`) で全ボタン非活性 (O1 終了直前ガード)。
- 巻き `[−N分]` は type=live 限定。`end_at − delta > max(start_at, now)` をサーバ検証。

```
■ 状態B-ext: 生番組送出中（cut_live）の NOW PLAYING に「押え/巻き」操作行を追加
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ICS-TV 運用 / 送出コントロール   ● ON AIR LIVE   NTP同期 ●OK 13:48:20  自動更新[5s+WS ▼]      │
├────────────────────────────────────────────────────┬─────────────────────────────────────────┤
│ NOW PLAYING（本線 1-10）  ● ON AIR LIVE            │ レイヤ状態                              │
│  ◉ 生中継 駅前スタジオ                             │ 1-10 本線   ● PLAYING  cut_live         │
│    program.type=live id=820  on-air(live区間内)    │ 1-30 Lバー  ● ON                        │
│    action=cut_live live_source=cam1                │ 1-40 速報   ○ off  ←操作可              │
│    経過 00:43:42（生は残尺なし）                   │ feed  ●OK MediaMTX publisher 検出       │
│    予定終了 14:00 end_at / 次番組で強制カット(既定)│                                         │
│ ─── 押え/巻き（type=live・on-air のみ）─────────── │                                         │
│  延長(押え): [ +5分 ◆ ] [ +10分 ◆ ] [ +カスタム ▸ ]│ ← 後続繰り下げ＋隙間吸収プレビュー確認 │
│  早終い(巻き): [ −5分 ◆ ] [ −N分 ▸ ]               │ ← 後続は動かさず再解決でフィラー充填   │
│  ※ 押え/巻きは確認ダイアログで影響範囲を提示後に発火                                          │
├────────────────────────────────────────────────────┴─────────────────────────────────────────┤
│ 操作   [CM IN ▸選択 ◆](要確認) [速報テロップ](無効) [緊急スレート ◆◆](要確認) [本線再ロード] │
└──────────────────────────────────────────────────────────────────────────────────────────────┘

■ 状態B-ext: 終了60秒前ガード作動時（押え/巻きを非活性化）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ─── 押え/巻き（type=live・on-air のみ）─────────────────────────────────────────────────────  │
│  延長(押え): [ +5分 ](無効) [ +10分 ](無効) [ +カスタム ](無効)                               │
│  早終い(巻き): [ −5分 ](無効) [ −N分 ](無効)                                                  │
│  ⚠ 予定終了 60 秒前のため押え/巻き不可（旧イベント発火とのレース回避 / O1）。本線再ロードで対応│
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

注記（モデル対応）:
- 「予定終了 14:00」「予定終了 60 秒前」= `program.end_at`。経過は `now − cut_live の playout_event.actual_at`。
- 押えボタン → `scheduling/services.py::extend_program(program_id, delta_ms, user)`、巻きボタン → `shorten_program`（`program.end_at -= delta`）。いずれも確認ダイアログ経由で idempotency_key 付き POST（二重発火防止、CM IN/スレートと同様）。
- 活性条件 `(無効)` は `program.type != live` または on-air でない、または `ms_until(program.end_at) < 60_000`。

---

### 2. 押え 確認ダイアログ（シフト影響範囲プレビュー付き）

> 差分: 状態C（緊急スレート確認）と同じ**確認ダイアログの様式**を流用。押え固有の「後続が何番組・何分シフトするか」のプレビュー表を持つ。窓内吸収不能なら**拒否表示**で発火非活性。

- 対象ロール: 運用者 / 管理者
- 単段確認（`◆`）。スレート(状態C)のような文字列一致(`◆◆`)は不要。
- プレビュー = `extend_program` の dry-run（行ロックせず shifts を試算）。各後続 program の `start_at` 隙間で減衰した shift 量、フィラー隙間での吸収、48h 窓境界を提示。

```
■ 押え 確認ダイアログ（影響範囲プレビュー / 後続繰り下げ＋隙間吸収 O1）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│     ╔════════════════════════════════════════════════════════════════════════════════╗       │
│     ║ ◆ 生番組を +10分 押えます（後続を繰り下げ・フィラー隙間で吸収）                  ║       │
│     ║ ────────────────────────────────────────────────────────────────────────────────  ║      │
│     ║ 対象 ch1 / 駅前スタジオ(id=820 type=live)                                        ║       │
│     ║   end_at 14:00:00 → 14:10:00 JST（+10:00）                                       ║       │
│     ║ ──── 影響範囲（解決窓 now+48h 内・start_at 昇順）────────────────────────────── ║       │
│     ║  #  番組                start_at→新        シフト  隙間吸収                       ║       │
│     ║  1  夕方ワイド id=821   14:05→14:10 JST    +5:00   gap5:00→残5:00 繰下げ          ║       │
│     ║  2  天気 id=822         14:30→14:33 JST    +3:00   gap2:00 で2分吸収              ║       │
│     ║  3  夜のニュース id=823 15:00→15:00 JST    ±0      gap3:00 で完全吸収→以降不動    ║       │
│     ║  → 計 2 番組が繰り下げ、3 番組目の手前で吸収完了（カスケード停止）               ║       │
│     ║ ──────────────────────────────────────────────────────────────────────────────── ║      │
│     ║ 公開番組表は program 追従で自動更新。YouTube 2h 枠は送出独立のため影響なし。     ║       │
│     ║ 発火後 on_commit で当該 ch のみ即時再解決（5分周期を待たない）。                 ║       │
│     ║                                          [ キャンセル ]   [ +10分 押え 発火 ◆ ]  ║       │
│     ╚════════════════════════════════════════════════════════════════════════════════╝       │
└──────────────────────────────────────────────────────────────────────────────────────────────┘

■ 押え 拒否（解決窓内で吸収しきれない / O1 暴走カスケード防止）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│     ╔════════════════════════════════════════════════════════════════════════════════╗       │
│     ║ ⚠ この +30分 押えは解決窓(now+48h)内で吸収できません — 発火できません            ║       │
│     ║ ────────────────────────────────────────────────────────────────────────────────  ║      │
│     ║ 窓内の全後続を繰り下げても残 +12:00 が吸収不能（remaining>0 / ExtendRejected）。  ║       │
│     ║ 窓境界 06-13 13:48 JST 以降の番組は動かしません（O1）。                          ║       │
│     ║ 対応: より小さい延長量で再試行 / 該当時間帯の後続を編成タイムラインで調整。       ║       │
│     ║                                              [ 閉じる ]   [ +30分 押え 発火 ](無効)║      │
│     ╚════════════════════════════════════════════════════════════════════════════════╝       │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

注記（モデル対応）:
- プレビュー表の「start_at→新」「シフト」= `program.start_at` と `extend_program` の shifts 試算値（各後続の繰り下げ量 = 直前 gap で減衰した remaining）。
- 「end_at 14:00:00 → 14:10:00」= 対象 `program.end_at += delta_ms`。
- 拒否は `ExtendRejected("解決窓内で吸収できない延長量")`（窓内の全後続を動かしても remaining>0）。サーバ検証と UI 非活性の二重防壁。
- 発火 = `transaction.on_commit(resolve_channel_now.delay(channel_id))`。

---

### 3. 巻き 確認ダイアログ

> 差分: 押えと対称（O8）。後続は動かさないため影響範囲は「空いた区間のフィラー自動充填」のみ。

```
■ 巻き 確認ダイアログ（早終い・後続不動・フィラー自動充填 O8）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│     ╔════════════════════════════════════════════════════════════════════════════════╗       │
│     ║ ◆ 生番組を −5分 早終いします（後続は不動・空き区間はフィラーで充填）            ║       │
│     ║ ────────────────────────────────────────────────────────────────────────────────  ║      │
│     ║ 対象 ch1 / 駅前スタジオ(id=820 type=live)                                        ║       │
│     ║   end_at 14:00:00 → 13:55:00 JST（−5:00）                                        ║       │
│     ║   検証 end_at−delta(13:55:00) > max(start_at 13:05, now 13:48) … OK             ║       │
│     ║ ──── 影響範囲 ──────────────────────────────────────────────────────────────────  ║      │
│     ║  ・後続番組のシフトなし（巻きは end_at 短縮のみ）                                ║       │
│     ║  ・13:55:00〜14:05:00 の空き 10:00 は既定フィラーのループで自動充填（再解決）    ║       │
│     ║                                          [ キャンセル ]   [ −5分 早終い 発火 ◆ ] ║       │
│     ╚════════════════════════════════════════════════════════════════════════════════╝       │
└──────────────────────────────────────────────────────────────────────────────────────────────┘

■ 巻き 拒否（境界違反 / chk_time CHECK 500 を防ぐサーバ検証）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│     ║ ⚠ −20分は end_at が現在時刻/開始を下回ります — 発火できません                    ║       │
│     ║   end_at−delta(13:40) ≤ max(start_at 13:05, now 13:48) … NG                     ║       │
│     ║   対応: 残り経過に収まる短縮量で再試行 / 即時終了は緊急スレート＋次番組テイク。   ║       │
│     ║                                              [ 閉じる ]   [ −20分 発火 ](無効)    ║       │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

注記（モデル対応）:
- 「end_at 14:00:00 → 13:55:00」= `program.end_at -= delta`（後続 program は不変）。
- 境界検証 = `end_at − delta > max(start_at, now)`（type=live 限定。recorded は不変条件破壊のため対象外）。

---

### 4. ヘルス監視パネルの拡張（agent_status 反映）

> 差分: ui.md **状態A・B・D・E のヘルス監視ペイン**を `agent_status`（Heartbeat 反映値）で全面差し替え。既存の「agent ●OK PING / feed / OSC」行に `last_heartbeat_at` 経過・`queue_depth`・`caspar_health` 3 値・`feed_state` 3 値・`slate_active`・`auto_return`・`auto_return_suspended` を足す。

- 対象ロール: 運用者 / 管理者
- ヘルス: `●OK ●NG ───(取得不可)`。`caspar_health`= healthy/degraded/down → ●OK/▲degraded/●NG。`feed_state`= ok/lost/idle。
- 死活: `last_heartbeat_at` > 90s(3 周期) で agent ●NG（死活 beat が agent_offline 通知）。

```
■ ヘルス監視（agent_status 反映 / 状態A・B 共通ペインの拡張）
┌─────────────────────────────────────────┐
│ ヘルス監視（agent_status / 1:1 ch）       │
│ ─────────────────────────────────────────│
│ agent   ●OK  last_hb 2s前 13:48:18        │  ← last_heartbeat_at（>90s で ●NG）
│ queue   ●OK  queue_depth 4 件             │  ← agent ローカルキュー残
│ caspar  ●OK  caspar_health=healthy        │  ← degraded→▲ / down→●NG
│ feed    ●OK  feed_state=ok                │  ← live区間外は idle 表示
│ slate   ○ slate_active=false              │  ← true で ◆ SLATE 表示へ
│ 自動復帰 [ auto_return ON ▼ ]             │  ← agent_status.auto_return トグル
│ 出力    ●OK UDP→encoder tee(YT/MediaMTX)  │
│ OSC進行 ●OK 最終 13:48:17                 │
└─────────────────────────────────────────┘

■ ヘルス監視（degraded / queue 滞留の警告表示）
┌─────────────────────────────────────────┐
│ agent   ●OK  last_hb 4s前 13:48:14        │
│ queue   ▲    queue_depth 132 件（滞留）   │  ← 送出遅延の兆候
│ caspar  ▲    caspar_health=degraded       │  ← AMCP 応答鈍化
│ feed    ●OK  feed_state=ok                │
│ slate   ○ slate_active=false              │
│ 自動復帰 [ auto_return ON ▼ ]             │
└─────────────────────────────────────────┘
```

注記（モデル対応）:
- agent 行 = `agent_status.last_heartbeat_at`（相対経過表示。>90s = 3 周期で ●NG → 死活 beat が `notification(kind=agent_offline, CRIT)`）。
- queue = `agent_status.queue_depth` / caspar = `agent_status.caspar_health`(healthy/degraded/down) / feed = `agent_status.feed_state`(ok/lost/idle) / slate = `agent_status.slate_active`。
- 自動復帰トグル = `agent_status.auto_return`。POST で更新 ＋ `AgentControl` を SubscribeEvents ストリームへ push（O5 操作 API 表）。

---

### 5. feed 断 状態D の拡張（自動復帰トグル / フラップ自動復帰停止）

> 差分: ui.md **状態D（feed 断→自動スレート退避）**の `[自動復帰 ON ▼]` を `agent_status.auto_return` 連動に変え、フラップ上限超過時の「自動復帰停止」表示（`agent_status.auto_return_suspended`）と手動スレート中の「自動復帰サスペンド」表示を足す。operator トグル `auto_return` と、フラップ保護による一時停止 `auto_return_suspended` は別フィールド。

- 対象ロール: 運用者 / 管理者
- 自動復帰は **feed monitor 自身が出したスレートのみ**解除可（`slate_by_monitor`）。手動 play_slate 実行済み・clear_slate 未受領の間は**自動復帰サスペンド**。
- 直近 10 分の自動復帰 3 回超でフラップ上限 → `auto_return_suspended=true`（以後は手動復帰のみ）。operator トグル `auto_return` は ON のまま保持し、`auto_return_suspended` でサスペンド表示する。再開は再 push でサスペンド解除（`auto_return_suspended=false`）。

```
■ 状態D-ext: feed断→自動スレート退避中（auto_return 連動・ヒステリシス復帰待ち）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ▓▓ ALERT 13:22:07 ch1 feed断検知（MediaMTX publisher 消失 §4.5検知A）→自動スレート [消音]    │
├────────────────────────────────────────────────────┬─────────────────────────────────────────┤
│ NOW PLAYING（本線 1-10） ◆ SLATE(自動退避)         │ ヘルス監視（agent_status）              │
│   action=play_slate (goto_slate, 自動 monitor発)  │ 出力  ●OK（encoder→YT/MediaMTX は継続） │
│   退避元 id=820 cam1  slate経過 00:00:38           │ agent ●OK last_hb 1s前                  │
│   復帰待ち: publisher 再出現を 10s 連続でヒス復帰   │ caspar ●OK caspar_health=healthy        │
│ ─── 復帰操作 ───────────────────────────────────── │ feed   ●NG feed_state=lost              │
│  自動復帰 [ auto_return ON ▼ ]  ヒス進捗 ░░░ 0/10s  │ slate ● slate_active=true (by_monitor)  │
│  手動 [ 生を本線へ戻す ◆ ](要確認)                 │ ── 自動復帰 ──                          │
│                                                    │  auto_return=true  monitor が管轄        │
├────────────────────────────────────────────────────┴─────────────────────────────────────────┤
│ ※ feed断は DB上イベント未事前生成。退避/復帰とも actual_at=now の割り込み記録（ReportInterrupt）│
└──────────────────────────────────────────────────────────────────────────────────────────────┘

■ 状態D-ext: フラップ上限超過で自動復帰停止（AUTO_RETURN_SUSPENDED / WARN）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ▲▲ WARN 13:31:02 ch1 自動復帰を停止（直近10分の自動復帰3回超＝フラップ）→以後 手動復帰のみ    │
├────────────────────────────────────────────────────┬─────────────────────────────────────────┤
│ NOW PLAYING ◆ SLATE(自動退避・復帰停止中)          │ ヘルス監視（agent_status）              │
│   slate経過 00:01:54  自動復帰サスペンド            │ feed_state=lost / 復帰3回(10分窓)        │
│ ─── 復帰操作 ───────────────────────────────────── │ slate_active=true                       │
│  ⚠ auto_return_suspended=true（フラップ保護で一時停止）│ auto_return=ON (トグルは維持)          │
│  自動復帰 [ 再 push でサスペンド解除 ◆ ]（要確認） │  auto_return_suspended=true → 手動復帰のみ│
│  手動 [ 生を本線へ戻す ◆ ](要確認) を推奨           │  ← 再 push で suspended=false に          │
└────────────────────────────────────────────────────┴─────────────────────────────────────────┘

■ 状態D-ext: 手動スレート中の自動復帰サスペンド（feed publisher 復帰中でも露出させない）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ NOW PLAYING ◆ SLATE(手動 play_slate)               │ slate_active=true (by_monitor=false)    │
│  ⚠ 手動スレート中は自動復帰サスペンド               │ feed_state=ok（publisher 復帰済）        │
│   （運用者が意図的に隠した映像を publisher 復帰だけ │ auto_return=true だが monitor 管轄外    │
│    では露出させない / 1-90 の3書き手調停）          │  → clear_slate まで解除しない           │
│  復帰は [ スレート解除 ▸本線復帰 ◆ ](要確認) で明示 │                                         │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

注記（モデル対応）:
- 自動復帰トグル = `agent_status.auto_return`（operator が ON/OFF する意図フラグ）。フラップ上限超過時は別フィールド `agent_status.auto_return_suspended=true` を立てて一時サスペンドを表現し、`auto_return` トグルは ON のまま撤回しない。`notification(kind=auto_return_suspended, WARN)` を発火（ReportInterrupt(AUTO_RETURN_SUSPENDED)）。再開は再 push（AgentControl）で `auto_return_suspended=false`。
- 「by_monitor」= agent 内部 `slate_by_monitor`（自動復帰可否の判定。手動 play_slate は false→自動復帰サスペンド）。`agent_status.slate_active` は退避中フラグ。
- 退避/復帰の as-run = `playout_event(action=play_slate / cut_live, params.interrupt)` の割り込み記録（server 側 ReportInterrupt ハンドラ INSERT、O10 で agent へエコーされない）。

---

### 6. スレート解除コントロール（clear_slate action）

> 差分: ui.md **状態C 末尾の「スレート解除 ▸本線復帰」**を `playout_event(action=clear_slate)` に正式配線。録画中は本線 1-10 保持のため `CLEAR {ch}-90` のみで復帰（状態C のとおり）、生は 2 段（cut_live + clear_slate）。

- 対象ロール: 運用者 / 管理者
- `clear_slate` は単段確認（`◆`）。手動スレート中（自動復帰サスペンド中）の解除はこのボタンが唯一の正規復帰経路。

```
■ スレート解除（状態C 発火後の本線ヘッダ → clear_slate / CLEAR {ch}-90）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ NOW PLAYING（本線 1-10） ◆ SLATE ON AIR 00:02:31                                              │
│  退避元 action=play_asset id=812（録画＝本線 1-10 は CLEAR せず保持中）                       │
│  [ スレート解除 ▸本線復帰 ◆ ](要確認)   ← 録画中はこれのみで復帰（CLEAR 1-90）               │
│  ※ 手動スレート中は自動復帰サスペンド中。解除はこのボタンが唯一の正規復帰経路。              │
└──────────────────────────────────────────────────────────────────────────────────────────────┘

■ スレート解除 確認ダイアログ
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│     ╔════════════════════════════════════════════════════════════════════════════════╗       │
│     ║ ◆ スレートを解除して本線へ復帰します（CLEAR {ch}-90）                           ║       │
│     ║ ────────────────────────────────────────────────────────────────────────────────  ║      │
│     ║ 対象 ch1 ICS-TV  現在 slate_active=true（slate経過 00:02:31）                    ║       │
│     ║ 録画中: 本線 1-10 は保持中 → CLEAR 1-90 のみで保持中の映像が前面復帰。           ║       │
│     ║ （生退避時は別途「生を本線へ戻す」= cut_live + clear_slate の2段が必要）         ║       │
│     ║ as-run: playout_event(action=clear_slate, scheduled_at=now, status=SCHEDULED)。  ║       │
│     ║   配信→agent 実行報告で done（割り込み記録 ReportInterrupt とは別経路）。        ║       │
│     ║                                          [ キャンセル ]   [ スレート解除 発火 ◆ ]║       │
│     ╚════════════════════════════════════════════════════════════════════════════════╝       │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

注記（モデル対応）:
- スレート解除 = `playout_event(action=clear_slate, scheduled_at=now, status=SCHEDULED)` を即時 INSERT（O5 操作 API 表「スレート解除」）。planner が `CLEAR {ch}-90`。配信(O10 フィルタ通過)→agent 実行報告で done。
- 「録画中はこれのみで復帰」= 本線 1-10 保持（状態C「本線 1-10 は CLEAR せず保持」）。生退避は cut_live(params.interrupt=true) + clear_slate の 2 段。

---

### 7. 通知センター画面（通知パネル 3 層の履歴一覧版）

> 差分: ui.md **『通知パネル（障害アラートの 3 層）』** の (3) 履歴一覧をフル画面化。`notification` テーブル全件の一覧・kind 別フィルタ・既読化・遷移リンク。トースト/ステータスバーの 2 層は既存のまま。

- 対象ロール: 運用者 / 管理者
- `severity`= crit/warn/info → `● CRIT / ▲ WARN / ・INFO`。`kind`= feed_lost / slate_on / feed_restored / auto_return_suspended / agent_offline / agent_recovered / playout_failed / cm_stock_low / yt_transition_failed。
- 既読化 = `acknowledged_at` / `acknowledged_by` を打つ（現設計は全体共有＝acknowledged 1 つ）。
- ポーリング: 未確認件数とリストは ops SPA の 5 秒ポーリング（`POLL_MS = 5000`）で更新（旧設計の hx-trigger 2s は React 化で置換済み）。

```
■ 通知センター（notification 履歴一覧 / kind 別フィルタ・既読化・遷移）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ICS-TV 運用 ▸ 通知センター        未確認 4 件   [ 全件確認 ◆ ]  [ 通知設定 ]  自動更新[2s ▼]  │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ severity (●全)(CRIT)(WARN)(INFO)   状態 (●全)(未確認)(確認済)   ch (●全)(ICS-1)(ICS-2)        │
│ kind (●全)(feed_lost)(slate_on)(feed_restored)(auto_return_suspended)(agent_offline)          │
│      (agent_recovered)(playout_failed)(cm_stock_low)(yt_transition_failed)                     │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ sev   created_at  ch     kind                  message                        遷移 / 確認      │
│ ─────────────────────────────────────────────────────────────────────────────────────────────│
│ ● CRIT 13:22:07  ICS-1  slate_on              feed断→自動スレート退避(cam1)  [運用へ][確認]   │
│ ● CRIT 13:22:05  ICS-1  feed_lost             publisher 消失 path=live/cam1  [運用へ][確認]   │
│ ▲ WARN 13:31:02  ICS-1  auto_return_suspended フラップ上限超過→自動復帰停止  [運用へ][確認]   │
│ ▲ WARN 12:40:11  ICS-2  playout_failed        play_asset FAILED 在庫不足代替 [運用へ][確認]   │
│ ・INFO 13:25:40  ICS-1  feed_restored         publisher 復帰→生へ自動復帰    [運用へ]✓13:26   │
│ ・INFO 12:30:00  ICS-2  cm_stock_low          break=812 grid15s 在庫不足     [素材へ]✓12:31   │
│ ・INFO 11:58:10  ICS-1  agent_recovered       heartbeat 復帰(途絶42s)        [運用へ]✓11:59   │
│ ▲ WARN 11:58:08  ICS-1  yt_transition_failed  枠#77 transition失敗(未active) [枠へ]  ✓11:59   │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ 遷移: [運用へ]→運用DB該当ch(link_url) / [枠へ]→YT枠DB / [素材へ]→cm_creative / [確認]→既読化  │
│ 確認済は「✓HH:MM」(acknowledged_at) を表示。CRIT は (1)トースト・(2)status バーにも反映済み。 │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

注記（モデル対応）:
- 行 = `notification`。sev = `notification.severity`(crit/warn/info)、created_at = `notification.created_at`、ch = `notification.channel_id`、kind = `notification.kind`、message = `notification.message`、遷移 = `notification.link_url`。
- [確認] = `notification.acknowledged_at` / `acknowledged_by` を打つ POST（idx_notification_unack は acknowledged_at IS NULL の部分 index）。「✓HH:MM」= acknowledged_at。
- kind の発火元（operations.md 発火元表）: feed_lost/slate_on=ReportInterrupt(SLATE_ON,CRIT) / feed_restored=ReportInterrupt(FEED_RESTORED,INFO) / auto_return_suspended=ReportInterrupt(AUTO_RETURN_SUSPENDED,WARN) / agent_offline・agent_recovered=死活 beat(check_agent_liveness) / playout_failed=apply_result(WARN) / cm_stock_low=resolver fill_break(INFO) / yt_transition_failed=youtube.tasks(WARN)。
- [全件確認] は表示中フィルタ範囲の未確認をまとめて既読化（通知パネル既存の「全件確認」と同義）。

---

### タイムキーパー・本書に未記載の実装済み操作

- **タイムキーパー画面**（ops.\* の `/<slug>/timekeeper`）は本書に含めない。**正本は [timekeeper-live.md](timekeeper-live.md) §7**（cue 操作 `op_cm_now` / `op_roll_vt` / `op_skip_cue` も同 doc）。
- 本書に未記載のまま実装済みの運用操作（`server/core/urls.py` の棚卸し、2026-09）:
    - `op_slot_transition` — YT 枠の手動 transition（`POST /ops/ch/<slug>/slot/<id>/transition/`）。
    - `op_cut_live_return` — 生本線への復帰（`CUT_LIVE` 再発行＋slate 解除。[timekeeper-live.md](timekeeper-live.md) §2 参照）。

### 8. 空 / ロード / エラー 状態

> 差分: ui.md 状態E の「空/ロード」方針を、押え/巻き・拡張ヘルス・通知センターの各新規ブロックへ展開。一覧は `<tbody>` 部分差し替えで表す。

```
■ 押え/巻き — agent 断 / 取得不可
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ─── 押え/巻き ─────────────────────────────────────────────────────────────────────────────── │
│  [ +5分 ](無効) [ +10分 ](無効) [ −5分 ](無効)                                                │
│  ⚠ agent 応答なし（last_hb 途絶 >90s）。押え/巻きは再解決の伝搬経路が断のため保留。[再接続] │
└──────────────────────────────────────────────────────────────────────────────────────────────┘

■ ヘルス監視 — ロード / 取得不可（agent_status 未取得・WAN断）
┌─────────────────────────────────────────┐
│ ヘルス監視（agent_status）                │
│ agent   ─── 取得不可（last_hb 途絶 >90s）│  ← 死活 beat が agent_offline CRIT を発火
│ queue   ─── ──   caspar ─── ──            │
│ feed    ─── ──   slate  ─── ──            │
│ 自動復帰 [ ─── ](無効・接続断)            │
│ ［ロード中］各値スケルトン「接続中…」最終既知値はグレー残置                                  │
└─────────────────────────────────────────┘

■ 通知センター — 空 / ロード / エラー（<tbody> 部分差し替え）
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ sev   created_at  ch     kind        message                                  遷移 / 確認       │
│ ─────────────────────────────────────────────────────────────────────────────────────────────│
│ ［空］ 該当する通知はありません（フィルタ条件 / 未確認なし時は「新着なし」）                  │
│ ［ロード中］skeleton 行 ░░░░░░░░░░ を 3 行表示（初回フェッチ・5s ポーリング待ち）              │
│ ［エラー］通知の取得に失敗しました（DB/接続）。[ 再読込 ]  ※トースト/status バーは別経路で継続│
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```

注記（モデル対応）:
- 押え/巻きの `(無効)` 理由は終了 60 秒前ガード（状態B-ext）か agent 断（last_heartbeat_at 途絶）。後者は本線再ロード同様、送出ノード到達不可のため保留（状態E と同方針）。
- ヘルス `───(取得不可)` = `agent_status.last_heartbeat_at` 途絶（>90s）。死活 beat が `notification(kind=agent_offline, CRIT)` を発火、復帰で agent_recovered(INFO)。
- 通知センターの空/ロード/エラーは一覧 `<tbody>` 部分差し替え（素材・CM管理と同方針）。トースト(1)・status バー(2) は通知センターと別経路で継続表示。

---

※編成タイムライン上の押え/巻きボタン（operations.md Phase O-C）は ui.md 編成タイムライン側で別途設計。本書は運用画面 NOW PLAYING への差分に限る。
