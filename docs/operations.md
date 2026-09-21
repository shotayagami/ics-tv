# #7 運行・監視（ウォッチドッグ/運行ダッシュボード/延長対応）

実放送局の APC・マスター運行業務に相当する**運用の堅さ**を icstv に実装するための設計。
スコープは 3 つ：

1. **ウォッチドッグ/自動退避の完成** — フィード断→SLATE 自動退避・自動復帰、通知基盤（overview §7 ステップ8 の宿題）
2. **運行ダッシュボード** — [ui.md](ui.md) 運用画面ワイヤーフレームの実装方式とデータ配線の確定
3. **延長対応（押え）** — 生番組延長時の後続繰り下げ（[scheduler.md](scheduler.md) 未確定論点の解消）

agent の dispatch ループは既に APC の中核（運行データ受信・時刻同期送出・as-run 返送）を
実装済み。本書はその「運用変更への対応・監視・緊急対応」を埋める。

## このサブシステムの決定ログ

| # | 論点 | 決定 | 補足 |
|---|------|------|------|
| O1 | 生番組の延長（押え） | **後続繰り下げ＋隙間吸収** | 後続番組を繰り下げ、フィラー隙間で吸収してカスケードを止める。**シフト対象は解決窓（now+48h）内に限定し、窓内で吸収しきれない延長は拒否**。scheduler.md の未確定論点「延長時の扱い」を解消：**明示の押え操作が無ければ既定どおり強制カット** |
| O2 | feed 断からの復帰 | **自動復帰＋ヒステリシス** | publisher 再出現を N 秒連続確認してから生へ戻す。フラップ上限超過で自動復帰を停止し手動へフォールバック |
| O3 | 通報チャネル | **notifier 抽象化＋webhook（Discord等）先行** | Zabbix sender は同一 I/F で後から追加。通知の発火は server 側に一元化（agent は gRPC 報告のみ、webhook を直接持たない） |
| O4 | proto 拡張 | **#7 では proto/agent の拡張を許容**（#5/#6 の「無改修」不変条件は適用外） | 本書は送出実行層そのものの機能のため。`ReportInterrupt` RPC・Heartbeat 拡張・`AgentControl`（wrapper の予約済み余地を使用）・**`CLEAR_SLATE` action（スレート解除。既存 action はどれも 1-90 を退かせないため必須）**。疎結合（agent は ORM を知らない）は維持 |
| O5 | ダッシュボード更新方式 | **HTMX ポーリング 2 秒** | 確定スタック Django+HTMX に整合（ui.md の「WebSocket または SSE」は HTMX SSE 拡張として将来論点へ） |
| O6 | ウォッチドッグの 3 層分担 | **ホスト=watchdog.sh（既存）／agent=feed monitor（新規）／server=死活 beat（新規）** | ホスト層はプロセス死活と最後の砦、agent 層は放送内容レベル、server 層は agent 自体の監視 |
| O7 | heartbeat の可視化 | **`agent_status` テーブル（channel 1:1）に upsert** | ダッシュボード・死活検知の読み出し元 |
| O8 | 巻き（早終い） | **押えと対称に `-N 分` 短縮もサポート** | end_at 短縮→隙間はフィラー自動充填（resolver 既存挙動）。追加実装は薄い |
| O9 | 新 app は作らない | **既存 app の責務内に置く**（notifier=core / agent_status・ダッシュボード=playout / 押え=scheduling） | 営放（#6）と違い独立ドメインではなく、送出系の運用機能のため |
| O10 | SubscribeEvents の配信フィルタ | **status=SCHEDULED の行と tombstone（CANCELLED）のみ配信** | 現実装は sync_seq のみで無フィルタのため、割り込み記録（status=done で INSERT）が agent へエコー配信され**実行済みのスレートを再点火**する。配信フィルタの導入は Phase O-A の必須前提（grpc_service の修正） |

## ウォッチドッグ：3 層の役割分担（O6）

```
[ホスト層] watchdog.sh (systemd-timer 30s) ── 既存・無改修
    AMCP 無応答 → casparcg-server restart / 本線停止 → SLATE 直叩き / encoder・MediaMTX restart
[agent 層] feed monitor ループ (新規・asyncio 第4ループ)
    MediaMTX API で publisher 監視 → goto_slate / 自動復帰 / ReportInterrupt で server へ報告
[server 層] 死活 beat (新規 Celery)
    agent_status.last_heartbeat_at 途絶 (>90s=3周期) → 通知 / resolver の CM 在庫不足 warning → 通知化
```

ホスト層（`deploy/playout-node/scripts/watchdog.sh`）は CasparCG/encoder/MediaMTX の
**プロセスレベル**の最後の砦として現状のまま残す。agent 層は**放送内容レベル**
（「生番組のはずなのにフィードが来ていない」）を担い、両者は重複しない。

### 出力層：黒落ち・停止の外形監視（Zabbix）

上の 3 層はいずれも**内部状態**を見ている。しかし実際には、プロセスは全て active、
AMCP は `200 INFO OK`、HLS セグメントも 2 秒ごとに正常に生成され続けたまま、
中身だけが黒ということが起きる。内部状態を見る限りどの層も健全に見えるため、
**実際に出ている絵**を独立に見る 4 つ目の観測点を置く。

| item key | 返す値 | 検知できる障害 |
|---|---|---|
| `icstv.hls.age[<slug>]` | 最新セグメントの経過秒 | casparcg wedge / encoder 停止（セグメント生成が止まる） |
| `icstv.video.black[<slug>]` | 直前の 144p セグメントの黒フレーム比率 (%) | 層の消失・フリーズ黒（セグメントは出続ける） |

実装は `deploy/playout-node/scripts/hls-health.sh`、UserParameter は
`deploy/playout-node/zabbix/icstv-playout.conf`。判定理由（なぜ 2 本要るか、なぜ 144p か、
なぜ最新ではなく 1 つ前のセグメントを読むか）はスクリプトのヘッダに記述している。

**トリガはヒステリシスを必ず持たせる。** フィラー境界の dip to black（`_should_dip`）は
正常な絵であり、単発の黒で発報してはいけない。既定は「3 分継続」。

**黒のトリガは `icstv.slate.active[<slug>]=0` を条件に加える。** 休止帯のスレート
（`slate/please_wait`）は黒背景に小さな白文字で、144p では文字が全画素の 2% に満たない。
`blackdetect` の `picture_black_ratio_th` は既定 0.98 なので、**正常なスレートが「黒」と
判定される**。放送終了と同時に誤発報し、長時間 PROBLEM のまま残ることになる。
閾値を締める対処は 144p での文字の画素占有率に依存して脆いため、「スレートが出ているか」を
独立したシグナルとして持ち、トリガ側で除外する。

検知系統の全体像（Zabbix 以外の 3 系統・通知先の実配線・既知の盲点）は本書後半の
「監視・通知経路の全体像」を参照。

### 制御プレーン層の外形監視（Zabbix）

server 層の死活 beat は**監視対象クラスタの中**の Celery beat/worker で動いている。beat か
worker か Redis が止まれば `agent_offline` は出ないし、`offline_notified` が latch されたまま
誰も見なければそれきり黙る（退役した channel が何か月もその状態のまま残りうる）。つまり
「通知が無い」は「正常」と「検知系ごと死んでいる」を区別できない。

そこで検知系の**外**に読み出し口を置く。`GET /api/v1/internal/monitor/heartbeat`
（X-Internal-Token = `MONITOR_READ_TOKEN`、内部ホストのみ・公開 tv.* には存在しない）は
request 経路の icstv-web が `agent_status` を直接読み、有効 channel ごとの経過秒・`offline`・
`on_air` を返す。Celery を経由しない。副作用も無い（通知は beat 側の責務のまま）。

Zabbix サーバ（クラスタ外）が 30 秒ごとに `ops.<内部ドメイン>` 経由で取得し、LLD で
channel を発見して次のトリガを持つ。

| トリガ | 条件 | 意味 |
|---|---|---|
| heartbeat 未達（放送中: High / 休止中: Warning） | `offline=1` が 2 回連続 | beat と同じ判定を**独立した経路**で二重化 |
| endpoint 沈黙（High） | `nodata(icstv.heartbeat.raw, 3m)` | icstv-web / ingress / DB / 監視経路のいずれかが死んでいる。**これだけが検知系そのものの死を拾う** |

閾値と on_air は `playout.tasks.OFFLINE_THRESHOLD_SEC` / `Channel.is_on_air` をそのまま使い、
beat と別の真実を作らない。channel を LLD にしているのは、退役（`enabled=False`）で JSON から
消えた channel の item が lost-resources の期限で自動的に消えるためで、退役 channel の
「latch されたまま誰も消さない」を監視側で再演しないための選択。監視側の適用手順は
導入者の配備基盤側（このリポジトリの範囲外）。

### casparcg 再起動で失われるもの／復帰させるもの

casparcg を再起動すると、そのプロセスが持っていた**全レイヤの状態が消える**。executed 済みの
イベントは `due_for_take` に再び乗らないため、放置すると次の予定 TAKE まで戻らない。
agent は再接続を検知して以下を貼り直す（`main._retake_current` / `main._retake_slate`）。

| レイヤ | 復帰のしかた | 復帰しない場合に起きること |
|---|---|---|
| 本線 10 | 直近 executed の PLAY_ASSET / PLAY_FILLER / CUT_LIVE を `PLAY ... LOOP` | 次の予定 TAKE まで黒。フィラーは実尺 67〜113 分なので最悪 1 時間超 |
| CG（L バー等） | `OverlayManager.restore` で再 ADD | テロップ・時計が消えたまま |
| スレート 90 | `monitor.slate_active` なら `slate_command` で再 PLAY | スレートで隠していた本線が露出する |

スレートの復帰が要るのは、編成の休止帯スレートが 5 分周期で再発行される一方、**手動／緊急
スレートと feed 断退避には再発行が無い**ため。前者も再発行までの最大 5 分は「休止中」ではなく
フィラー本編が映ってしまう。

### agent: feed monitor ループ（新規）

`agent/icstv_agent/main.py` に第 4 の asyncio タスクを追加する。

- **監視対象**: MediaMTX API `GET http://127.0.0.1:9997/v3/paths/list`（同一ホスト・軽量）を
  **1 秒周期**でポーリングし、パスごとの publisher 有無（`ready`）を取得する
  （casparcg.md §4.5 検知 A=一次。検知 B=INFO/OSC は将来の保険のまま）。
- **判定の有効条件**: **現在時刻が「live 区間」内**のときのみ feed 断判定を行う。live 区間とは、
  ローカルキュー上で cut_live の `scheduled_at` から**次の通常イベント（play_asset / play_filler）**
  の `scheduled_at` までの範囲。play_cm_bundle（CM IN）・play_slate・clear_slate 等の割り込みを
  挟んでも live 区間は継続する（「最新 executed が cut_live」判定だと CM IN 後に監視が
  無効化されたままになるため、この定義を採る）。録画・フィラー中は判定しない。
- **退避**: 対象パスの publisher 消失を **2 秒（2 回連続）**確認 → `goto_slate(channel)`（既存
  プリミティブ）→ `ReportInterrupt(kind=SLATE_ON, reason=feed_drop)` を**即時送信**
  （失敗時のみ interrupt 専用 outbox に退避し heartbeat 周期で再送。CRIT 通知を 30s 遅らせない）。
  内部状態 `slate_active=true, slate_by_monitor=true`。
- **自動復帰（O2）**: `slate_active` 中に publisher 再出現が**ヒステリシス N 秒（既定 10s）連続**
  したら、`LOADBG`→`INFO 確認`→`PLAY` で生を本線へ載せ直し、`CLEAR {ch}-90` でスレートを
  退かす（casparcg.md §4.5 の復帰列のとおり）。`ReportInterrupt(kind=FEED_RESTORED)` を送信。
- **手動スレートとの調停**: 自動復帰が解除してよいのは**自分（feed monitor）が出したスレートのみ**
  （`slate_by_monitor` フラグ）。手動の play_slate イベントを実行済みで clear_slate 未受領の間は
  自動復帰をサスペンドする（運用者が意図的に隠している映像を publisher 復帰だけで露出させない。
  1-90 には手動スレート・watchdog.sh・feed monitor の 3 つの書き手がいるため明示調停が必要）。
- **フラップ制限**: 直近 10 分間の自動復帰が **3 回**を超えたら自動復帰を一時サスペンドして
  `ReportInterrupt(kind=AUTO_RETURN_SUSPENDED)` を送信（以後は手動復帰のみ）。**これは operator
  トグル `auto_return`（意図）とは別の agent 内部状態**として持ち、`agent_status.auto_return_suspended`
  に反映する。Heartbeat の `auto_return` は operator トグル値（ON のまま）を報告し続けるため、
  AgentControl の reconciliation がトグルを書き戻してフラップ保護を解除してしまうことはない。
  再開はダッシュボードの再開操作（トグルを ON で再 push＝サスペンド解除指示）で行う。
- **トグル**: 自動復帰の ON/OFF は server からの `AgentControl`（後述）で切り替え。agent は
  ローカル SQLite に保持し再起動でも維持する。

### proto 拡張（O4）

```protobuf
service PlayoutAgent {
  // (既存 3 RPC に追加)
  // agent 起点の割り込み報告 (feed断退避/復帰等)。事前生成イベントが無いため
  // ReportResult と別 RPC。interrupt_key (agent 生成 UUID) で冪等。
  rpc ReportInterrupt(ReportInterruptRequest) returns (ReportInterruptResponse);
}

message ReportInterruptRequest {
  string channel_slug = 1;
  string interrupt_key = 2;            // agent 生成 UUID (再送冪等)
  InterruptKind kind = 3;
  google.protobuf.Timestamp at = 4;
  string detail = 5;                   // 対象 path / 理由等
  enum InterruptKind {
    INTERRUPT_KIND_UNSPECIFIED = 0;
    INTERRUPT_KIND_SLATE_ON = 1;       // feed断→自動退避
    INTERRUPT_KIND_FEED_RESTORED = 2;  // 自動復帰で生へ戻した
    INTERRUPT_KIND_AUTO_RETURN_SUSPENDED = 3;  // フラップ上限で自動復帰停止
  }
}
message ReportInterruptResponse { bool accepted = 1; }

// SubscribeEventsResponse の wrapper 余地 (proto コメントで予約済み) を使用:
message SubscribeEventsResponse {
  PlayoutEvent event = 1;
  AgentControl control = 2;            // 追加。event と排他で流れる
}
message AgentControl {
  optional bool auto_return = 1;       // 自動復帰トグル
}

// PlayoutAction に追加:
//   PLAYOUT_ACTION_CLEAR_SLATE = 8;   // スレート解除 (planner: CLEAR {ch}-90)
//
// HeartbeatRequest に追加 (後方互換なフィールド追加):
//   bool slate_active = 6;            // SLATE 退避中か
//   string feed_state = 7;            // 監視対象パスの状態 (ok / lost / idle)
//   bool auto_return = 8;             // operator トグルの意図 (フラップ停止でも ON のまま)
//   bool auto_return_suspended = 9;   // フラップ保護による一時サスペンド (agent 内部状態)
```

**AgentControl の配送規約**：control にはイベントの sync_seq のような再送カーソルが無いため、
(1) **（再）接続時に server は現在の control 状態を必ず初送**する、(2) Heartbeat の
`auto_return` と server 値（agent_status.auto_return）の乖離を検出したら再送する、の 2 点で
WAN 断中のトグル変更の取りこぼしを防ぐ。**ロールアウト順は agent 先行**（`resp.event` しか
見ない旧 agent に control-only メッセージを送ると空イベントとして取り込まれ planner で
永久リトライになるため、event/control を分岐する agent を先に配ってから server を更新する）。

**interrupt の再送冪等**：server 側 ReportInterrupt ハンドラは `interrupt_key` の get_or_create
で受け、UNIQUE 違反（再送）も accepted=true を返す。agent 側は ReportResult 用の既存 outbox
（payload を ReportResultRequest として parse する）と型が異なるため、**interrupt 専用 outbox**
を queue_db に追加する。

server 側 `ReportInterrupt` ハンドラは、ui.md の注記「feed 断は DB 上イベント未事前生成。
退避/復帰とも actual_at=now の割り込み記録」に従い：

- `SLATE_ON` → `playout_event(action=play_slate, idempotency_key=interrupt_key,
  scheduled_at=actual_at=at, status=done, params={reason: feed_drop})` を INSERT ＋ CRIT 通知
- `FEED_RESTORED` → `playout_event(action=cut_live, ..., params={interrupt: auto_return})` を
  INSERT ＋ INFO 通知
- `AUTO_RETURN_SUSPENDED` → 通知のみ（WARN）

これらの割り込み**記録**（status=done で INSERT）は、O10 の配信フィルタ
（SCHEDULED＋tombstone のみ配信）により agent へエコーされない。
なお resolver の cancel scope からは PLAY_SLATE に加えて**割り込み起源のイベント全般**
（`params.interrupt` 付き・即時 play_cm_bundle / clear_slate 等）を除外する（割り込みは
resolver 管轄外）。

### server: agent_status と通知基盤（O3・O7）

```sql
-- Heartbeat の反映先 (channel 1:1)。grpc_service.Heartbeat ハンドラが upsert
CREATE TABLE agent_status (
    channel_id        bigint PRIMARY KEY REFERENCES channel(id) ON DELETE CASCADE,
    last_heartbeat_at timestamptz NOT NULL,
    last_received_seq bigint NOT NULL DEFAULT 0,
    queue_depth       int NOT NULL DEFAULT 0,
    caspar_health     text,                      -- healthy / degraded / down
    feed_state        text,                      -- ok / lost / idle
    slate_active      boolean NOT NULL DEFAULT false,
    auto_return       boolean NOT NULL DEFAULT true,   -- operator トグルの意図 (Heartbeat と双方向同期)
    auto_return_suspended boolean NOT NULL DEFAULT false,  -- フラップ保護による一時サスペンド (agent 内部状態)
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- 通知 (ui.md「通知パネル 3 層」のデータ源)
CREATE TYPE notification_severity AS ENUM ('crit','warn','info');
CREATE TABLE notification (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_id      bigint REFERENCES channel(id),
    severity        notification_severity NOT NULL,
    kind            text NOT NULL,        -- feed_lost / slate_on / agent_offline / cm_stock_low / yt_transition_failed / ...
    message         text NOT NULL,
    link_url        text,                 -- 遷移先 (運用 / YT枠 / 素材)
    acknowledged_at timestamptz,
    acknowledged_by bigint REFERENCES auth_user(id),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_notification_unack ON notification(created_at) WHERE acknowledged_at IS NULL;
```

**notifier 抽象**（`core/notify.py`）：`notify(severity, kind, message, channel=None, link=None)`
が notification 行を INSERT し、settings 登録のバックエンド列
（`ICSTV_NOTIFY_BACKENDS`、既定 `["core.notify.WebhookBackend"]`）へ配送する。

- `WebhookBackend`：`ICSTV_NOTIFY_WEBHOOK_URL`（Discord/Slack 互換 webhook）へ POST。
  失敗は通知自体を壊さない（DB 行は残る。配送は best-effort、ログのみ）。
- `ZabbixBackend`：同一 I/F で後から追加（zabbix_sender 相当）。
- **スロットリング**：同一 (kind, channel) は **5 分のクールダウン**内は webhook 配送を抑止
  （DB には毎回記録。フラップ時の通知氾濫防止）。

**通知の発火元（初期セット）**：

| kind | severity | 発火元 |
|---|---|---|
| feed_lost / slate_on | CRIT | ReportInterrupt(SLATE_ON) |
| feed_restored | INFO | ReportInterrupt(FEED_RESTORED) |
| auto_return_suspended | WARN | ReportInterrupt(AUTO_RETURN_SUSPENDED) |
| agent_offline | CRIT | 死活 beat（last_heartbeat_at が 90 秒超過） |
| agent_recovered | INFO | 死活 beat（途絶からの復帰） |
| slate_stuck | CRIT | スレート固着 beat（放送中にスレートが 7 分超継続） |
| slate_stuck_cleared | INFO | スレート固着 beat（固着の解消） |
| playout_failed | WARN | apply_result（FAILED 受信時） |
| cm_stock_low | INFO | resolver fill_break（在庫不足 warning の通知化） |
| yt_transition_failed | WARN | youtube.tasks（既存 error の通知化） |

死活 beat（`playout.tasks.check_agent_liveness`、1 分周期）は通知の二重発火を防ぐため
agent_status に「通知済みフラグ」ではなく**状態遷移（online↔offline）の検出**で発火する。

agent↔server の既存 TCP 接続が片方向 blackhole になった場合も、agent の unary RPC deadline
（10 秒）と HTTP/2 keepalive（20 秒間隔、10 秒 timeout）で検出し、再接続へ進む。
SubscribeEvents は正常時に切れない長時間 stream のため有限 deadline は付けない。server は agent の
keepalive 間隔を明示的に許可する。設定値と展開順序は
[agent/README.md](../agent/README.md#grpc-の障害検出)を正本とする。

`agent_offline` は、Proxmox ホスト側の自動更新と同時に送出ノードの gRPC/RTMP 通信が停滞し、
既存 gRPC 接続が half-open のまま十数分残る、という形でも発生する。ホストも送出ノードも再起動
しておらず送出はローカルで継続するため、送出側からは無症状に見える。対策は上記の gRPC 障害検出
短縮に加え、ホストの自動更新を休止時間帯へ寄せること。

### スレート固着 beat（`playout.tasks.check_stuck_slate`、1 分周期）

スレート層（layer 90）は本線（layer 10）と独立なので、本線が正常に流れていても画面はスレートの
まま＝視聴者には停波と同じになる。それでも送出イベントは成功し続けるため、死活 beat にも
`playout_failed` にも掛からない盲点になる（休止明けにスレートが残ると、運用者が手動で解除
するまで誰も気付かないまま数時間続きうる）。この beat がその盲点を埋める。

発火条件は「`agent_status.slate_active` かつ放送中（`broadcast_windows` 窓内）かつ直近実行の
`PLAY_SLATE` が resolver 管轄（`params.off_air`）かつ猶予 7 分超」。以下は除外する。

- **休止中**：スレートが出ているのが正常
- **feed 断由来**（`feed_state="lost"`）：`slate_on` / `feed_lost` で別途通知済み
- **運用者の手動/緊急スレート**（`off_air` param 無し）：運用意図なので鳴らさない
- **猶予内**：休止明けの `CLEAR_SLATE` 実行や resolver の自己修復（最大 1 beat＝5 分）が働く余地を
  残し、正常な遷移中の一過性状態で鳴らさない

死活 beat と同じく `agent_status.slate_stuck_notified` による**状態遷移の検出**で発火する。

## 監視・通知経路の全体像

「icstv が止まったら誰に届くか」の整理。検知系統は 4 本あり、どれも通知先まで
配線して初めて意味を持つ。残る穴のうち §盲点の 2 件
（YouTube 枝・notifier webhook）が最重大。

### 検知 4 系統と通知先

| 系統 | 見ているもの | ルール正本 | 通知先 |
|---|---|---|---|
| ① メトリクス監視 / アラートルータ | k8s 面（pod 異常・Job 失敗）。icstv 固有ルールは 0 件だが、汎用 kubernetes-apps ルール（KubePodCrashLooping / KubePodNotReady / KubeJobFailed 等 17 本）が `namespace=~".*"` で icstv ns を全数カバー | 導入者の配備基盤側（監視 ns のアラートルール） | 全アラート → チャット webhook + メール（group_wait 30s / repeat 12h）。severity=critical は並行してプッシュ通知（repeat 1h） |
| ② ログ集約のルーラ | icstv アプリの `logger=icstv.security` ログ（webhook 署名/ハンドラ失敗=critical、認証スパイク/ロックアウト/M2M token=warning の 5 本）。セレクタ `{namespace="icstv", app="icstv"}` は実ログと一致・評価稼働中 | 導入者の配備基盤側（ruler 用 ConfigMap をラベルで sidecar 搬入。**ConfigMap を置くだけでは評価されず**、ログ基盤側のルーラ設定とセットで初めて有効になる） | アラートルータへ合流（→①と同じ配送） |
| ③ Zabbix | 送出ノード（監視サーバにホスト登録）。ICS-TV 固有 3 トリガ: **黒 3 分継続**（prio4、`icstv.slate.active=0` 条件付き＝上記「出力層」節）・**HLS age**（prio4）・**取得不能**（prio2 のメタ監視）。ほかホスト死活（prio3）・セキュリティエージェントの切断検知 | item 実装は `deploy/playout-node/scripts/hls-health.sh` + `deploy/playout-node/zabbix/icstv-playout.conf`。トリガは監視基盤側の設定 | チャット webhook + メール = 全重大度 / プッシュ通知 = High(4) 以上のみ / チケット基盤へ自動起票 = Average(3) 以上。→ 黒画面・HLS 凍結は 4 経路すべてに届く |
| ④ プッシュ通知直叩き CronJob | `image-pin-check`（日 4 回: レジストリ上の pin 実在 + イメージ署名 (`.sig`) の検査。**配備側のポリシーで拒否されると定期実行が無症状で止まりうる**ため、その予防。経緯はスクリプト自身の長文コメントに自己文書化）・`update-check`（毎朝）・`security-scan-notify`（6h 毎: 脆弱性スキャン・ポリシー・GitOps の増分） | 導入者の配備基盤側（各 ns の CronJob 内スクリプト） | プッシュ通知のみ |

送出ノードのログ可視性: ノード上のログ転送エージェント（systemd unit）が journal を
`host=<送出ノードのホスト名>` でログ基盤へ送っており、agent/encoder/watchdog/casparcg/mediamtx の
unit ログをログ閲覧 UI から追える（放送の実検知は③が独立経路なので、これは調査用）。

### 障害シナリオ → 検知の対応

| シナリオ | 検知 | 備考 |
|---|---|---|
| icstv pod の CrashLoop / NotReady / Deployment 縮退 | ① 汎用ルール | warning → チャット webhook + メール |
| icstv ns の CronJob（weather 7 + ranking 4）の**失敗** | ① KubeJobFailed（warning, 15m） | |
| 同 CronJob の**沈黙**（Job が作られない） | △ pin 消失起因のみ ④ image-pin-check | それ以外の原因（suspend 混入等）は**無検知**（§盲点） |
| セキュリティ（webhook 署名失敗・認証スパイク） | ② icstv.security 5 本 | |
| 送出ノードのホスト死活 | ③ Zabbix agent（prio3） | プッシュ通知には行かない（High 未満） |
| 本線黒画面 / HLS 凍結 | ③ 固有トリガ（prio4） | slate 除外条件付き（正常なスレートを黒と誤判定させないため） |
| **YouTube 枝の単独死 / ingestion starved** | **無し** | §盲点 1 |
| feed 断（agent → ReportInterrupt） | △ DB の notification 行のみ | webhook 未設定で外部通知ゼロ（§盲点 2） |
| セキュリティエージェントの切断 | ③ Zabbix トリガ | |

### 既知の盲点（重大度順）

1. **YouTube 出力の単独死・ingestion starved が全監視の空白**（high）。
   encoder の ffmpeg tee は YouTube 枝に `onfail=ignore` + fifo drop を指定しており
   （`deploy/playout-node/systemd/icstv-encoder@.service`）、YouTube 枝だけが死んでも
   ffmpeg・自前 HLS・③の黒画面/HLS age 監視はすべて健全のまま。server 側にも
   liveStream health（noData/ingestionStarved）のポーリングは存在しない
   （`youtube/api.py` にコメント言及のみ）。本番運用スコープが「YouTube 配信のみ」で
   ある以上、主力配信面の停止に気づく自動経路が視聴者の指摘以外に無い。
   **実際に発生した事例がある**（[youtube.md](youtube.md) §本番運用で踏んだ SaaS 側の実況）。
   候補: server 側で放送中のみ liveStreams.list の healthStatus を beat ポーリング
   （quota 消費あり）、または送出ノード側で tee 枝統計を Zabbix item 化（方式はユーザ判断）。
2. **`ICSTV_NOTIFY_WEBHOOK_URL` が未設定だと CRIT が外へ出ない**（high）。
   「server: agent_status と通知基盤」の実装が配備済みでも、配備側の設定
   （config / secret）にこのキーが無ければ、feed_lost 等の CRIT は DB の
   notification 行に残るだけで誰にも届かない。設計・実装・配備まで済んでいても
   **最後の 1 行（webhook URL の投入）が欠けるだけで検知は無音になる**。
3. icstv ns CronJob の沈黙検知が image-pin-check（pin 起因）限定（med）。
   `kube_cronjob_status_last_successful_time` 型の「最後に成功してから X 時間」ルールは
   一部の ns に限って置かれており icstv には無い。同型ルールの新設が候補。
4. 公開面（tv.\*/studio.\*/ops.\*）の外形監視ゼロ（med）。監視基盤の web シナリオにも
   疎通 probe の対象にも icstv 系は入っていない。
5. 本体 DB の論理バックアップとその成否監視が既存の傘の外（med）。
6. フィラー滞留・EPG 乖離・正規化/QC 滞留など**業務レベル異常**は無検知（low）。
   resolver バグの再発は現状ログを人が見ない限り気づけない。
7. celery beat / worker の内部停止（pod Running のままスケジュール発行停止）は
   KubePodNotReady 頼み（low）。transition worker 停止は朝枠無配信の再発形。
   icstv ns に ServiceMonitor/PodMonitor は 0 件。
8. 送出ノードのログ転送エージェントのメタ監視なし（low。k8s 内のログ転送の死活ルールは
   DaemonSet のみが対象）。死ぬと watchdog/agent ログの可視性が黙って失われる（放送検知は③が
   独立経路なので実害は限定的）。

### 運用注記

- Linux 汎用トリガ **High CPU / High swap / Load average** は送出ノードでは
  誤発報しやすい（送出機は高負荷が常態）。無効化するか送出向けに校正するかを決め、
  **その理由を必ず記録に残す**こと。残さないと、後から恒久措置か暫定かを判別できない。
- 監視基盤の problem は ACK 運用を決めておかないと回らない（重大度の低い切断 problem が
  数日未 ACK のまま滞留する、という形で現れる）。
- メタ監視: ログ基盤の停止・取り込み停止=critical、ログ転送の死活（k8s 内のみ）、
  Zabbix「出力監視の値を取得できない」prio2、image-pin-check は検査不能時に
  自 Job を fail させて①経由で表面化する。

## 運行ダッシュボード（O5）

画面設計は **[ui.md](ui.md) 運用画面（状態 A〜E）が正**。本書はデータ配線と操作 API を確定する。

- **更新方式**：HTMX `hx-trigger="every 2s"` のパネル別ポーリング（NOW PLAYING／レイヤ・ヘルス／
  AS-RUN の 3 パネル独立）。SSE 化は将来論点。
- **データ源**：
  - NOW PLAYING / NEXT：`playout_event`（**`scheduled_at <= now` の最新非 CANCELLED** を on-air と
    みなす＝`core/views.home` と同じ規約。status=EXECUTING は現状どの経路でも設定されないため
    使わない。take 時報告による EXECUTING 導入は論点へ）＋ scheduled_at 昇順の未来分
  - ヘルス：`agent_status`（heartbeat 反映値）＋ `youtube_slot` 状態
  - レイヤ状態：Heartbeat の `caspar_health` を当面の粒度とする（レイヤ別 INFO の収集は
    Heartbeat 拡張の将来論点。ui.md のレイヤ表は取得可能な範囲で描画）
  - AS-RUN ライブログ：`playout_event` 新しい順（既存 ui.md 設計どおり、CSV は既存予定）
- **操作 API**（すべて既存の「即時 PlayoutEvent INSERT」機構に統一。緊急 SLATE ボタンと同型）：

| 操作 | 実装 |
|---|---|
| 緊急スレート | 実装済み（POST → play_slate, scheduled_at=now） |
| スレート解除 | POST → **`clear_slate`, scheduled_at=now** INSERT（planner が `CLEAR {ch}-90`。録画中は本線 1-10 が保持されているためこれだけで復帰＝ui.md 状態C のとおり） |
| 生の本線復帰（手動） | POST → `cut_live` を params.interrupt=true で即時 INSERT ＋ clear_slate（生は producer を載せ直す必要があるため 2 段） |
| **CM IN**（生） | POST → `play_cm_bundle, scheduled_at=now` INSERT（server 側＝build step 6 の未実装分を解消。**agent 側も追加作業あり**：現 reel は後続 CM の AUTO 連結のみで**末尾の生復帰ステップを積まない**ため、bundle params に戻り先 rtmp_url を含め、reel 末尾に `LOADBG {ch}-10 <rtmp> AUTO` を追加する。生退避レイヤ 1-11 への park は論点） |
| CM 戻り（手動） | POST → `cut_live, scheduled_at=now` INSERT（残尺自動の戻りは上記 reel 末尾ステップが担う） |
| 本線再ロード | POST → 現行イベントを params.interrupt=true で再 INSERT。**play_asset は `in_ms + (now − scheduled_at)` で頭出しを補正**（無補正だとセグメント頭からの巻き戻りになる） |
| 自動復帰トグル | POST → agent_status.auto_return 更新 ＋ AgentControl を SubscribeEvents ストリームへ push |
| 速報テロップ | Phase 1 対象外（CG レイヤ実装＝casparcg.md §3 とセット。ボタンは無効表示） |

割り込み系の即時 INSERT はすべて **status=SCHEDULED で入れて配信させ**（O10 のフィルタを通る）、
agent の実行報告で done になる。server が直接 done で書く割り込み**記録**（ReportInterrupt 由来）
とは経路が異なる点に注意。

- **権限**：staff のみ（`staff_member_required`）。危険操作（CM IN/スレート）は ui.md どおり
  確認ダイアログ＋idempotency_key で二重発火防止。

## 延長対応：押え（O1・O8）

### 押え（延長）

運用画面の NOW PLAYING（type=live の番組が on-air＝live 区間内）に `[+5分] [+10分] [+カスタム]`
を置く。実装は `scheduling/services.py::extend_program(program_id, delta_ms, user)`：

```python
WINDOW = timedelta(hours=48)          # 解決窓と同じ (シフト対象の上限)
GUARD_MS = 60_000                     # 終了 60 秒前を切ったら押え不可

def extend_program(program_id, delta_ms):
    with transaction.atomic():
        prog = Program.objects.select_for_update().get(pk=program_id)  # 行ロック+再読込 (stale 防止)
        if ms_until(prog.end_at) < GUARD_MS:
            raise ExtendRejected("終了直前は延長不可")   # 旧イベント発火とのレース回避 (下記)
        tail = (Program.objects
                .filter(channel=prog.channel,
                        start_at__gte=prog.end_at,
                        start_at__lt=now() + WINDOW)     # 窓内のみ走査・ロック (O1)
                .order_by("start_at").select_for_update())
        # 隙間吸収: 各後続番組のシフト量は手前の隙間で減衰する
        shifts, remaining, cursor = [], delta_ms, prog.end_at
        for p in tail:
            gap = ms_between(cursor, p.start_at)        # 直前との隙間 (フィラー)
            remaining = max(0, remaining - gap)
            if remaining == 0:
                break                                   # この隙間で完全吸収
            shifts.append((p, remaining))
            cursor = p.end_at
        if remaining > 0 and shifts:                    # 窓内の全後続を動かしても吸収不能
            raise ExtendRejected("解決窓内で吸収できない延長量")   # O1: 暴走カスケード防止
            # (後続が窓内にゼロ = shifts 空なら延長は何とも重ならないので許可。
            #  窓境界直後の番組との衝突は EXCLUDE が最終防壁としてロールバックさせる)
        for p, shift in reversed(shifts):               # 逆順更新で EXCLUDE の一時重複を回避
            p.start_at += ms(shift); p.end_at += ms(shift); p.save()
        prog.end_at += ms(delta_ms); prog.save()
        transaction.on_commit(lambda: resolve_channel_now.delay(prog.channel_id))
```

- **EXCLUDE 制約との整合**：後続を `start_at` 降順（逆順）に更新することで一時的な重複を作らない
  （制約を DEFERRABLE 化しない）。
- **即時再解決**：on_commit で当該 ch のみ即時 resolve（5 分周期を待たない）。実行済/実行中
  イベント不変・SCHEDULED のみ差替という resolver の既存特性が安全性を担保する。
- **終了直前ガード**：再解決の伝搬（Celery → commit → agent の 5s polling）より先に旧 filler/
  次番組イベントが発火すると、cut_live は start_at 不変＝同一冪等キーが done 済みのため再 push
  されず、延長残り時間がフィラーのまま固定される。**残り 60 秒を切った延長は拒否**（UI 非活性
  ＋server 検証）し、このレースを構造的に避ける。発火後の回復経路（interrupt 付き cut_live の
  即時 INSERT）は論点。
- **二重発火防止**：押え POST も確認ダイアログ＋冪等キー（CM IN/スレートと同様）の対象とする。
- **影響範囲**：公開番組表は program を読むため自動追従。YouTube 4h 枠は送出と独立
  （overview §3.6、v0.8.52 で 2h→4h）のため影響なし。48h 解決窓の外の番組は動かさない（O1）。
- **既定動作は不変**：押え操作をしなければ従来どおり次イベントで強制カット
  （scheduler.md の論点を「既定=強制カット、明示操作で押え」として解消）。

### 巻き（早終い・O8）

`[−N分]` は `prog.end_at -= delta` のみ（後続は動かさない）。空いた区間は再解決で
フィラーが自動充填される（resolver の既存挙動。追加実装は UI とサービス関数のみ）。

- **type=live 限定**。recorded に適用すると不変条件 `end_at = start_at + 素材尺 + Σbreaks` が
  壊れ、emit_recorded のイベント列が短縮後の end_at を越えて残る。
- **境界検証**：`end_at − delta > max(start_at, now)` をサーバ側で検証
  （`chk_time` CHECK 違反の 500 を防ぐ）。

## Celery タスク追加

| タスク | 周期/契機 | 内容 |
|---|---|---|
| `playout.check_agent_liveness` | 1 分 | agent_status 途絶（>90s）の状態遷移検出 → 通知 |
| `scheduling.resolve_channel_now` | 押え/巻き/編成変更から都度 | 単一 ch の即時再解決（既存 resolve の単 ch 版） |

## Phase 切り（合意済みの優先順）

- **Phase O-A（ウォッチドッグ完成）**：**SubscribeEvents 配信フィルタ（O10、最初に入れる）**→
  proto 拡張（ReportInterrupt・Heartbeat 拡張・AgentControl・CLEAR_SLATE、`buf generate` で
  server/agent 両更新。**agent 先行デプロイ**）→ agent feed monitor ループ＋自動復帰＋
  フラップ制限＋手動スレート調停＋interrupt 専用 outbox → `agent_status`・notifier（webhook）・
  死活 beat → 通知パネル（最小：未確認一覧＋既読化）。
- **Phase O-B（運行ダッシュボード）**：ui.md 運用画面の実装（HTMX 2s）、操作 API 一式
  （CM IN は server 側 INSERT に加え **agent reel 末尾の生復帰ステップ**を含む）、
  通知 3 層（トースト/ステータスバー）の完成。
- **Phase O-C（延長/巻き）**：extend/shorten サービス＋運用画面・編成タイムラインのボタン、
  scheduler.md 論点の解消反映。

## 分離サブシステム seam トークンの投入（運用）

別リポの各サブシステムと ICS-TV 本体は `X-Internal-Token` 共有秘密の内部 API（seam）で連携する。
トークンは settings 既定 `""` の **fail-closed**（未設定なら該当 `/internal/*` は常に 401）。
投入が要るトークン（当該サブシステムを使うなら必須）:

| env | 用途 | 投入 |
|---|---|---|
| `WEATHER_IMPORT_TOKEN` | icstv-weather → `/internal/weather-import` | 要 |
| `EARTHQUAKE_FIRE_TOKEN` | icstv-earthquake → `/internal/breaking-telop` | 要 |
| `DELIVERY_REGISTER_TOKEN` | icstv-delivery ⇄ ICS-TV（`/internal/delivery-asset`・`/delivery-refs`・読み seam 双方向） | 要 |
| `BACKOFFICE_READ_TOKEN` | icstv-backoffice → `/internal/backoffice/*`（read 集計） | 要 |

投入/ローテーション手順:

1. **生成**: `openssl rand -hex 32`。高エントロピー必須（内部エンドポイントにはレート制限が無い）。
2. **投入**: 導入者の配備基盤側（このリポジトリの範囲外）の Secret へ、既存キーの値に触れず
   新しいキーだけを追加する。反映は通常フロー（PR → dev → main）に乗せ、配備基盤の同期に
   任せる。全キーを作り直す方式を採る場合は、生成されるキー集合が稼働中の Secret の全キーを
   カバーしているかを `server/.env.example` と突き合わせること（欠けたキーの seam は
   fail-closed で 401 のまま黙って落ちる）。
3. **サブシステム側**: 複製投入は**不要**。icstv-delivery / icstv-backoffice の Deployment には
   同じ Secret の同キーを `secretKeyRef` で参照させればよい（env 名だけ
   `ICSTV_DELIVERY_TOKEN` / `ICSTV_BACKOFFICE_TOKEN` に変わる）。Secret 更新後に
   icstv-web / icstv-delivery / icstv-backoffice を rollout restart するだけでよい。
4. **疎通確認**: `icstv.security` ログの `internal.token` fail が止まること・backoffice の集計
   4 ページ（billing/budget/rights/member-stats）が表示されること・番組予算 picker に納品/業者一覧が
   出ること。
   （注記: 上記のうち **rights と番組予算 (budget) は追加提供側**で、このツリーの `openapi.json` に
   `/api/v1/internal/backoffice/rights` は無く、`budget` は `rows` が常に空、`picker` が返すのは
   `series` だけで `deliveries` は常に空である。**この 2 つの確認項目はこのツリーでは通らない。**）

## 未確定・論点

- ヒステリシス秒数（既定 10s）・フラップ上限（10 分 3 回）は実フィードの瞬断特性を見て調整。
- レイヤ別状態（ui.md のレイヤ表を完全に埋める粒度）の取得は Heartbeat をどこまで太らせるか
  とのトレードオフ。当面は caspar_health 1 値＋slate_active で運用し、必要なら
  `repeated LayerState` を足す。
- HTMX ポーリング → SSE（htmx-sse 拡張）への移行条件（同時閲覧者数・サーバ負荷）。
- 速報テロップ（1-40）は CG レイヤ実装（casparcg.md §3、Phase 1 残）とセットで Phase 2。
- 通知の既読を「個人ごと」にするか「全体共有」にするか（現設計は全体共有＝acknowledged 1 つ）。
- MediaMTX API のレスポンス互換（v3 パス）。MediaMTX 更新時に追従確認。
- 押えの一回あたり最大延長量（窓内吸収の検証に加え UI 側上限を置くか。例: 60 分）。
- `RESULT_STATUS_EXECUTING` の導入（take 時に agent が報告し NOW PLAYING を厳密化）。
  現状は「scheduled_at ≤ now の最新非 CANCELLED」規約で代替。
- CM IN 中の生退避レイヤ 1-11 への park（casparcg.md §4.4）。現実装は本線直再生のため
  復帰時に RTMP 再ハンドシェイクが発生する。頻度と継ぎ目品質を見て判断。
- 終了直前ガード（60 秒）を超えてレースが発生した場合の回復経路
  （interrupt 付き cut_live の即時 INSERT による再テイク）。

## リリース昇格 runbook (コンテナレジストリ / CI)

本節は上記 #7 (運行・監視) の設計とは別の話題で、**dev → main 昇格〜本番 pin bump** の
実務手順と落とし穴を記録する。ブランチ運用そのものの正本は
`CONTRIBUTING.md`（「ブランチ運用」「レジストリの保持ポリシー」節）。
ここでは開発側の CI とコンテナレジストリ側の判定基準を扱う。

### レジストリ側の合格判定は「タグの存在」だけでは不十分

過去に、中断された別の昇格が先に同じ `vX.Y.Z` タグとイメージを作っていたため、
「レジストリにタグが在るか」だけを確認して合格と判定し、**修正を含まない古いイメージが
本番に出た**事故がある。

正しい判定は、**`vX.Y.Z` タグと、昇格させたい commit の `sha7` タグの両方**が同一 digest で
レジストリに存在することを確認すること (`crane digest <registry>/<project>/server:vX.Y.Z`
と `:<sha7>` が同じ digest を返すか、レジストリの API で当該 repository の artifact 一覧を
引いて確認する)。

### 同一タグを打ち直しても再ビルドは走る (ただし遅れることがある)

タグの re-push (`git tag -f` → force push) は **再ビルドを止めない**。開発側の CI の
`build-push` の concurrency group は ref 単位 (`build-push-${{ github.ref }}`) なので、
タグ push と `dev` push の run が互いを横取りする事故は構造的に解消済み。残るのは
runner capacity=1 によるキュー待ちのみで、**タグ push から 1 時間近く遅れて成功することが
ある**(「開発側の CI の run 一覧に `build-push` の行が無い」だけでは横取りと断定できない)。
焦って同じタグを二重に打つと、かえって上記の「同 digest 判定」を壊す。

**判定はレジストリの最終状態 (v タグ + sha7 タグの同居) で行い、慌てず CI の完走を待つこと。**

### imagePullPolicy の既定は IfNotPresent

配備基盤側のマニフェスト (このリポジトリの範囲外) が `imagePullPolicy` を明示しなければ
既定は `IfNotPresent` になる (検証環境だけ `Always` へ寄せる構成が多い)。したがって:

- 本番ノードが一度 pull した digest は、**同じタグ名でレジストリ側が指す digest が変わっても
  再取得しない**。上記の「旧イメージで合格判定してしまった」事故が実害化したのはこのため。
- 誤った pin で一度 rollout してしまった場合、同じタグを直しても本番ノードは更新されない
  ことがある。**新しい pin タグを切る (または digest pin にする)** ことで確実に更新する。

### レジストリの保持ポリシーとの関係

pin に使うタグは必ず `v*` 形式にすること。`v*` タグを持たない repository は保持
ルールで保護されない (実際に事故が起きている)。理由と確認手順は
`CONTRIBUTING.md` の「レジストリの保持ポリシー」節を参照。

### CI 共通ワークフロー（`@v1` moving tag への委譲）

サブシステム 6 リポ（backoffice / delivery / earthquake / ranking / slidecast / weather）の
CI は、開発側の共通ワークフローリポジトリの `node-ci.yaml` /
`docker-build-push.yaml` へ **`@v1`（moving tag）** で委譲している。タグ運用の共通規約:
dev push → `:sha7` + `:dev`、main push → `:sha7` のみ、tag `v*` → `:sha7` + `:vX`
（本番は pin tag 駆動）。

- **icstv 本体は委譲していない**。開発側の CI に同一の build + イメージ署名のロジックを
  inline 実装しているため、**署名ツールのバージョンや CA fingerprint を変えるときは
  共通ワークフローと本体側の CI 定義の両方を変える**こと。
- **v1 は moving tag**。進め忘れると「main に修正がマージ済みなのに利用側 CI に効かない」
  — 実際に、署名ツールのバージョン固定が main 入り済みなのに v1 が古いコミットを指したままで、
  サブシステムのリリースが署名検証の `no signatures found` で止まったことがある。
  **「マージ済みか」でなく「v1 がどこを指すか」を確認する**。
  逆に v1 を進めれば参照している全リポの CI に無審査・即時に波及するので、
  共通ワークフロー側にタグ保護・ブランチ保護を置くかどうかは先に決めておく。
- 進め方・保護・参照リポ一覧の**正本は開発側の共通ワークフローリポジトリの
  README**（本書は要約のみ）。**イメージ署名ツールのバージョンは、検証側のポリシーエンジンが
  読める出力形式に合わせて固定する**（新しい系列が書く referrer/bundle 形式を検証側が読めず、
  legacy の `sha256-<digest>.sig` タグ形式でなければ通らないことがある）。署名検証の一次情報は
  ルート `README.md` の「イメージ署名」節。
