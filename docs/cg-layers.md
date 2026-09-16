# #18 CG レイヤ実装（レイヤ状態可視化 + 手動グラフィック + 番組/フィラー自動グラフィック）

Phase 2 残件「CG レイヤ実装」の仕様。[operations.md](operations.md) の「速報テロップ（1-40）は CG レイヤ実装とセットで
Phase 2」「レイヤ別状態の取得は Heartbeat 拡張の将来論点（`repeated LayerState`）」、[ui-operations.md](ui-operations.md)
の **レイヤ状態パネル**、[casparcg.md](casparcg.md) §1.1/§3 のレイヤ設計を実装に落とす。本書は

- **A. レイヤ状態の可視化**（各レイヤに何が出ているかを ops で一覧）
- **B. 手動グラフィック操作**（任意レイヤ N-1〜N-89 へ 画像/動画(alpha,単発/ループ)＋文字 を複数組、即時で載せる・外す）
- **C. 番組/フィラー単位の自動グラフィックセット**（組み合わせ・各組のタイミングを番組/フィラー毎に事前定義し自動表示）

を一体で規定する。素材の「画像」は便宜表現で、実体は静止画 or alpha 付き動画（単発/ループ）を含む。合成は全て
CasparCG（送出ノード）側で行われ、配信側（encoder/MediaMTX/HLS）は合成済み単一フレームをエンコードするだけ
＝**CasparCG が唯一の合成点**であることを前提とする。

## 0. レイヤマップ確定（casparcg.md §1.1 を更新）

[casparcg.md](casparcg.md) §1.1 の固定割当に、agent 実装で追加した **次番組予告** を正式採番する。docs が予約済の
`1-40`（速報・割り込みテロップ）と衝突しないよう、次番組予告は 10 刻みの隙間 **`1-35`** へ挿入する
(「将来の差し込み余地」方針どおり)。

| layer | 役割 | producer | 管理 |
|---|---|---|---|
| `N-10` | 本線(PGM 背景) | FFmpeg | dispatch (LOADBG/PLAY) |
| `N-11` | 生退避 | FFmpeg | §4 |
| `N-20` | CM バンパー | HTML | plan().cg (stateless) |
| `N-30` | Lバー / 番組情報 | HTML | **OverlayManager** |
| `N-35` | **次番組予告**（新規・本書で採番） | HTML | **OverlayManager** |
| `N-36` | **朝・夕の左上時計**（daypart。resolver が `channel.clock_windows` を `cg_cues` に合成、`clock/corner`） | HTML | **OverlayManager** (cg_cues) |
| `N-37` | **津波ミニマップ**（地図+凡例のみ・画面右下・常時表示。`map/tsunami-corner`） | HTML | 発火元＝別リポ icstv-earthquake / studio 手動、経路＝内部API `POST /api/v1/internal/hazard-map`（`kind=tsunami_corner`、`X-Internal-Token` 認証）。`server/core/views.py` `_HAZARD_MAP_SPECS` |
| `N-38` | **災害フルスクリーン地図**（津波沿岸/震度/震度ズーム/地方別自動選択/EEW横書き/EEW縦書き。排他。40 の下＝速報テロップが前面） | HTML | 同上 API（`kind=tsunami\|seismic\|seismic_zoom\|seismic_regional\|eew_panel\|eew_band`） |
| `N-40` | 速報・割り込みテロップ（**本書 B で実装**） | HTML | 手動 op |
| `N-41` | **速報チャイム**（音声・速報と同時発火） | FFmpeg(音声clip) | `fire_chime`（kind=video の PLAY/STOP。[casparcg.md](casparcg.md) §3.5） |
| `N-45` | 手動フリーグラフィック（ベース画像＋文字を**複数枚**。本書 B） | HTML(複数要素) / 透過動画(alpha) | 手動 op |
| `N-50` | 提供表示 | **HTML or 透過動画(alpha)** | plan().cg (stateless) |
| `N-90` | 緊急スレート | FFmpeg/HTML | goto_slate |

> レイヤ 37/38 は agent の固定割当（`agent/icstv_agent/amcp_planner.py` `LAYER_HAZARD_CORNER=37` / `LAYER_HAZARD_MAP=38`）にもレイヤ状態パネル（`LAYER_ROLES`、`hazard_corner`/`hazard_map`）にも組み込み済みで、上記 §B.1 の「空きレイヤ」からは除外する（[casparcg.md](casparcg.md) §1.1 に採番の正本を同期済み）。CG テンプレはノードへの手動配備が必要（自動同期されない。[README.md](../deploy/playout-node/README.md)）。

> 移行: 既存 agent (`amcp_planner.LAYER_PREVIEW`) を 40→**35** に変更し再デプロイ。速報(40)/フリーグラフィック(45)
> を空ける。`N-45` は将来の二段差し込みに備え 40 と 50 の間に確保。

## A. レイヤ状態の可視化

### A.1 取得（agent → server, proto 拡張）

`proto/icstv/v1/playout.proto` に `LayerState` を追加し Heartbeat に同梱する。

```proto
message LayerState {
  int32 layer = 1;        // 10/20/30/35/36/37/38/40/45/50/90
  string role = 2;        // "main","bumper","lbar","preview","clock","hazard_corner","hazard_map","breaking","freeform","sponsor","slate"
  bool occupied = 3;      // producer あり (empty でない)
  string producer = 4;    // "ffmpeg" / "html" / "image" / "empty"
  string content = 5;     // テンプレ名 or clip名 or 主要 data (例: lbar=タイトル, preview=次番組)
  bool visible = 6;       // 表示中 (CG=play 状態, MIXER opacity>0)
}
// HeartbeatRequest に追加:
//   repeated LayerState layers = 10;
```

- agent は heartbeat 周期（30s）で対象レイヤ（10/20/30/35/36/37/38/40/45/50/90）を `INFO {ch}-{layer}` で取得し、
  既存の `_parse_foreground`（`agent/icstv_agent/caspar.py`） を全レイヤへ一般化して `LayerState` を作る。
- CG レイヤ（30/35/40/45/50）は OverlayManager / 手動 op が持つ**内部状態**（テンプレ名・data・visible）と
  突合して `content`/`visible` を補う（INFO だけでは visible= CSS class まで分からないため）。
- 高頻度の INFO はコスト要因なので **heartbeat 周期のみ**（毎 tick はしない）。INFO 失敗レイヤは `occupied=false`
  で素通し（best-effort）。

### A.2 保存（server）

`server/playout/models.py` `AgentStatus` に `layers = JSONField(default=list)` を追加
（`[{layer,role,occupied,producer,content,visible}, …]`）。Heartbeat ハンドラ（grpc_service）が更新する。
別テーブルにしないのは「最新スナップショットのみ」で履歴不要のため。

### A.3 表示（ops UI）

[ui-operations.md](ui-operations.md) のレイヤ状態パネルを実装（HTMX `every 2s` ポーリング、運行ダッシュボード右ペイン）。

```
レイヤ状態
 1-10 本線         ● filler/9 (loop)
 1-20 バンパー     ○ off
 1-30 Lバー        ● ON  "夜のニュース"
 1-35 予告         ○ off
 1-36 時計         ○ off
 1-37 津波ミニマップ ○ off
 1-38 災害地図     ○ off
 1-40 速報         ○ off   ←操作可
 1-45 自由         ○ off   ←操作可
 1-50 提供         ○ off
 1-90 スレート     ○ off
```

各レイヤ行に occupied/visible・content を表示。`1-40`/`1-45` は **操作可**（B の操作 UI を併設）。
行の並びは実装の `LAYER_ROLES`（`agent/icstv_agent/amcp_planner.py`）と同一。`1-41`（速報チャイム）は
音声のワンショット再生で `LAYER_ROLES` に含まれない＝**パネルには出ない**。

## B. 手動グラフィック操作

### B.1 操作モデル

「任意レイヤに グラフィックを 載せる / 差し替える / 外す」を server op → 即時イベント → agent で行う。
既存 op（op_cm_in 等（`server/core/ops_views.py`））と同じく、即時 `PlayoutEvent(scheduled_at=now)` を INSERT し
agent が実行する。新 action を 1 つ追加する:

```proto
enum PlayoutAction { … PLAYOUT_ACTION_OVERLAY_OP = 9; }

message OverlayOpPayload {
  int32 layer = 1;        // 操作対象 = N-1〜N-89 の任意 (90=緊急スレート予約)
  string op = 2;          // "show" | "update" | "hide" | "clear"
  string kind = 3;        // "graphic"(画像/動画+文字の複数要素 HTML) | "video"(透過動画単体) | "text"(速報等)
  string template = 4;    // kind=graphic/text: テンプレ名 (既定 graphic="graphic/freeform", text="telop/breaking")
  string clip = 5;        // kind=video: 透過動画(alpha付き) clip 名 (PLAY)。loop は data_json.loop
  string data_json = 6;   // kind=graphic: {"elements":[{media,url,loop,text,x,y,w,h,size,color,align,z}, …]}
                          //   media="image"|"video" (動画も alpha 付き。単発/ループは loop)。複数枚=要素を足す。
                          // kind=video:   {"loop":true|false}
                          // kind=text:    {"text":"速報: …"}
}
```

「画像」は便宜表現で、**実体は静止画 or alpha 付き動画（単発/ループ）**。要素 media で切替える。

agent 側ハンドラ（OverlayManager を拡張 or 新 `manual_overlay`）:
- `kind=graphic`/`text` → `CG {ch}-{layer} ADD 0 {template} 1 {data}`（show）/ `UPDATE {data}`（差し替え・**複数枚はこの
  elements 配列を更新**）/ `STOP`（hide）/ `CLEAR`。1 HTML producer = 1 テンプレ制約のため、**同一レイヤの複数枚は
  テンプレ内の `elements[]` で多重描画**する。動画要素は CEF(`<video>` autoplay/loop) で再生（後述 B.3）。
- `kind=video`（透過動画単体）→ `PLAY {ch}-{layer} {clip} {LOOP?}`（show）/ `STOP`（hide）。1 レイヤ 1 producer。
- **対象レイヤは N-1〜N-89 の任意**（90=緊急スレートのみ予約）。ただし 10=本線 / 30=Lバー / 35=予告 / 50=提供 は
  既定 role を持つので**手動 op で上書きすると自動 CG/本線と競合**する旨をレイヤ状態パネルで警告表示し、確認の上で許可
  （ハード禁止は 90 のみ。運用は空きレイヤ 42-44/46-49/51-89 等を使う想定。37/38=地図CG、41=速報チャイム、45=手動フリーグラフィックは既に使用済み）。

### B.2 速報テロップ（1-40）

ops の `[速報テロップ]`（現在無効ボタン）を有効化。テキスト入力 → `OverlayOpPayload{layer:40, op:show,
kind:text, template:"telop/lower-third"（流用） or 新 "telop/breaking", data:{text}}`。STOP で消す。
[ui-operations.md](ui-operations.md) のとおり「全局面で最前面の CG」（提供50より前面が要れば 40<50 の見直しは別途）。

### B.3 手動フリーグラフィック（任意レイヤ）— 画像/動画＋文字を組で複数枚

**前提**: フリーグラフィックは「ベース素材＋文字が重なる」ことを既定とする。**素材は静止画 or alpha 付き動画
（単発/ループ）**で、「便宜上の画像」は動画も含む。**文字＋素材は 1 つの「組（element）」**として扱い、**1 レイヤに
複数の組を載せられる**。

- **freeform テンプレ（複数組, HTML）**: 汎用 **`graphic/freeform.html`**（新規）が data の `elements[]` を全要素描画。
  各要素（＝1 組）= `{media:"image"|"video", url, loop, text, x, y, w, h, size, color, align, z}`。CEF が `url`
  （R2 公開 URL or `/t/<key>`）を静止画(`<img>`)/動画(`<video autoplay [loop] muted>`, alpha 付き webm 等)としてロード
  し、`text` を重ねる。複数組は要素を足すだけ（1 HTML producer = 1 テンプレ制約をテンプレ内多重描画で満たす）。
  - 追加/差し替え/削除は `op=update` で `elements[]` 全体を入れ替え（agent は最新 elements を保持）。各組の表示/非表示
    タイミングは §C（番組/フィラー単位）で扱う。手動 op では即時 show/hide。
- **レイヤ単体の透過動画（重い素材/単発）**: HTML 内 `<video>` が重い・尺の長い透過動画は `kind=video` で
  `PLAY {layer} {clip} {LOOP?}`（FFmpeg producer, alpha 合成）。文字を重ねるなら別レイヤの freeform を上に重ねる。
- 素材は medialib `kind=graphic`（静止画 / alpha 動画）として R2 へ。HTML(`<img>`/`<video>`)参照は URL のみ、
  `kind=video`(PLAY)方式は本編同様 prefetch 経路で送出ノードへ先読みする。

### B.4 経路・安全

- server: ops ビュー `op_overlay`（staff_member_required, POST）→ `_dispatch`（`server/core/views.py`） で
  即時 `PLAYOUT_ACTION_OVERLAY_OP` イベントを INSERT（idempotency_key は (channel,overlay,layer,now)）。
- 手動 op で出した CG は **resolve の自動 CG（OverlayManager）と別レイヤ**なので干渉しない。手動 op の消し忘れ
  防止に「現在手動表示中のレイヤ」をレイヤ状態パネルで明示し、各行に `[消す]` を出す。
- 監査: OverlayOp も as-run（ReportResult）を返し、運用ログに残す。

### B.5 提供表示（1-50）の透過動画対応

提供は現状 HTML テンプレ（`credit/sponsor`, resolver の `cg_sponsor` 駆動）だが、**透過動画（alpha 付き素材、例:
透過 AVI / .mov）での合成**もあり得る。これに備え、提供の producer を HTML か動画か選べるようにする:

- resolver の提供ヒントを拡張: `cg_sponsor`（文言, HTML 用）に加え `cg_sponsor_clip`（透過動画 clip）/`cg_sponsor_kind`
  （`html`|`video`）を持たせる。Series/Program かスポンサー設定に「提供素材=テンプレ文言 or 透過動画」を持つ。
- agent: `kind=video` なら `CG` ではなく `PLAY 1-50 <clip>`（終端で自然に消えるか、尺後 `STOP`）。HTML なら従来どおり
  `CG 1-50 ADD … credit/sponsor`。これは手動 op (B.1) の `kind=video` 経路と同じ実装を提供にも使う。
- 透過動画はアルファ付きコーデック必須（§F）。提供素材も medialib `kind=graphic`（透過動画）として R2 prefetch する。

> 注: 1-45 のフリーグラフィック・1-50 の提供・1-90 のスレートはいずれも「透過動画 producer」を共有しうるため、
> agent 側の動画オーバーレイ発射（`PLAY <layer> <clip>` + alpha 合成）は共通実装にする。

## C. 番組/フィラー単位のグラフィックセット（自動・タイミング付き）

手動 op（§B）とは別に、**番組・フィラー毎に「グラフィックの組み合わせ（セット）」を事前に紐づけ、放送時に agent が
自動でタイミング表示する**経路を設ける。Lバー(30)/予告(35) はこのセット機構の特化版で、本節は**運用者が定義する
汎用グラフィック**を扱う。

### C.1 要件
- **組み合わせが番組/フィラー毎に変わる**: 番組(Program/Series)・フィラー(チャンネル/プレイリスト)ごとに別セット。
- **文字＋素材は「組」**: 1 グラフィック = §B.3 の element（画像/動画＋文字）。
- **各組に表示タイミング**: 番組(またはフィラークリップ)頭からの相対オフセットで show/hide。

### C.2 データモデル（所有粒度）

セットの**所有**は既存キューシート（Series 原本 → 展開時 Program へコピー → 個別編集）と同じ思想で、次の 4 規則とする:

| 対象 | セットの所有 | 振る舞い |
|---|---|---|
| **繰り返し番組（Series）** | **Series**（繰り返しマスタの原本セット） | 原本を 1 つ持つ |
| **繰り返しの個別回** | **Program**（展開時に Series 原本を**コピー**） | コピー後は**回ごとに編集可**（override） |
| **単発番組** | **Program**（直接） | その番組専用 |
| **フィラー / CM** | **Channel**（基本セット, context で区別） | 各 1 つの基本セットで足りる（個別不要） |

`GraphicCue`（セット = 同一 owner の cue 群。ad_break が Program の行であるのと同様）:

| 列 | 意味 |
|---|---|
| owner | `series` / `program` / `channel`(+`context`) のいずれか 1 つ非 NULL |
| context | channel 所有時のみ: `"filler"` \| `"cm"` |
| layer | 表示レイヤ (N-1〜N-89。空きレイヤ運用) |
| kind | graphic(element[]) / video / text |
| media | element[]（画像/動画+文字の組, 複数可）or video clip or text |
| show_at_ms | 番組(クリップ)頭からの表示開始オフセット。`whole`=尺全体 |
| hide_at_ms | 表示終了オフセット（or duration_ms）。`whole` なら番組末で消す |
| seq | 同一レイヤ内の重なり順/管理用 |

- **展開時コピー**: `expand_series_slots`（`server/scheduling/tasks.py`）が Series→Program 生成時に
  Series の GraphicCue 群を Program へコピー（ad_break/CueSheet と同じタイミング）。以後は Program 側を編集＝個別回 override。
- **解決時**: resolver は番組イベントに **Program の** GraphicCue（実効セット）を、フィラー/CM には **Channel の**
  基本セット（context=filler/cm）を `cg_cues` として載せる。タイミングは番組=番組頭、フィラー=クリップ頭、CM=CM 頭 起点。

### C.3 配線（resolver → agent）
- resolver は番組/フィラーイベントに、owner の `GraphicCue` 群を **cg ヒント `cg_cues`（layer/kind/media/
  show_at/hide_at の配列 JSON）** として載せる（既存 cg_lbar/preview と同じ params 経路）。
- agent の OverlayManager は各 cue について **show/hide のタイマー**を張る（予告 `cg_preview_at` 機構の一般化）。show 時に
  該当レイヤへ §B のハンドラで描画、hide 時に STOP。番組境界(次 take)で全 cue タイマーをキャンセル＆再評価。casparcg 再
  起動 retake でも現在 show 中の cue を復帰。
- CM/スレート中は番組系 cue も退避（agent の hide-path で `_apply_cues("")` → 全 cue CLEAR）。フィラー系は CM 無し。
  CM 上にグラフィックを出す CM 基本セット(context=cm)は段階⑤と一体で保留（§C.4 末尾の注）。

### C.4 編集 UI（実装: `scheduling/graphic_views.py` + `templates/scheduling/graphic_cues.html`）
共通エディタ 1 枚を owner で切替（URL `scheduling:graphic_cues` owner=`series|program|filler`）。staff 限定。
追加フォームは手動 op (B.1) と同じ data 形式を組む（layer 1-89 / kind=graphic|video|text / 画像 upload or URL +
文字 + x/y/w/size/align/color / 透過動画 clip+loop / 表示開始・終了秒 / seq / テンプレ）。
- **Series 編集** → 「🖼 自動グラフィックセット編集」（原本。展開時に各回へコピー）。
- **Program 編集（個別回/単発）** → 同編集。繰り返し回は展開時に原本コピーが入っており回ごとに上書き。
- **週間編成一覧** → 「🖼 フィラー基本グラフィック」（context=filler。フィラー全クリップに常時適用）。
- 素材は画像アップロード（thumbnails.store → R2 /t/<key>、公開 URL で絶対化）or 直接 URL。
- レイヤ状態パネル（§A）で手動 op / 常駐(Lバー/予告)の現在表示を一覧。

> **CM 基本セット (context=cm) は未提供（段階⑤と一体で保留）。** CM 中は agent が Lバー/予告/自動 cue を
> 退避する現挙動（検証済）で、CM 上にグラフィックを出す = 提供透過動画（B.5/段階⑤）と同じ表示面・同じ
> 「CM 退避挙動の変更」を要するため、両者をまとめて段階⑤で扱う。resolver の CM への `cg_cues` 配線も保留。

## D. データモデル / proto 変更まとめ

| 変更 | 場所 |
|---|---|
| `LayerState` message + `HeartbeatRequest.layers` | proto |
| `OverlayOpPayload` + `PLAYOUT_ACTION_OVERLAY_OP` | proto |
| `AgentStatus.layers` (JSONField) | server migration |
| `medialib` に `kind=graphic`（静止画＋**透過動画**。提供の透過動画含む）| server migration |
| 提供ヒント拡張 `cg_sponsor_clip`/`cg_sponsor_kind` (透過動画提供) | server resolver + agent |
| **`GraphicCue` モデル** (owner=Series原本/Program個別/Channel基本(filler\|cm), layer/kind/media/show_at/hide_at/seq) | server migration |
| `expand_series_slots` で Series→Program 生成時に GraphicCue をコピー (ad_break と同タイミング) + 編集 UI | server tasks + UI |
| resolver: 番組=Program実効/フィラー・CM=Channel基本 の **`cg_cues`** ヒントを載せる | server resolver |
| `LAYER_PREVIEW` 40→35, `LAYER_BREAKING=40`, `LAYER_FREEFORM=45` | agent amcp_planner |
| `graphic/freeform.html`(elements[]複数組, 画像/動画), `telop/breaking.html` テンプレ | playout-node |
| agent: 全レイヤ INFO→LayerState, OverlayOp ハンドラ, **cg_cues の show/hide タイマー** | agent |
| server: heartbeat に layers 保存, `op_overlay` ビュー, レイヤ状態パネル+操作 UI | server |

## E. 段階実装計画

1. **移行（先行・小）**: 次番組予告 40→35。速報40/自由45 を空ける。agent+server 再デプロイ。
2. **A 可視化**: proto `LayerState`→ agent INFO 収集 → server 保存 → ops レイヤ状態パネル（read-only）。
   ここまでで「何がどのレイヤに出ているか」が見え、デバッグ基盤が整う。
3. **B 速報テロップ（1-40）**: `OverlayOpPayload` + op_overlay + 速報テンプレ。最小の手動操作。
4. **B フリーグラフィック（任意レイヤ）**: `graphic/freeform.html`（**elements[] 複数組** 画像/動画＋文字）→ 文字重なり
   前提の複数組グラフィック。`kind=video` で透過動画も。medialib `kind=graphic`（静止画/alpha動画）+ R2。
5. **B 提供の透過動画（1-50）**: 共通の video オーバーレイ発射を提供にも適用（B.5）。
6. **C 番組/フィラー自動グラフィックセット**【実装済 v0.3.44】: `GraphicCue` モデル + 専用編集 UI（series/program/
   filler）+ resolver `cg_cues` + agent の show/hide タイマー（予告機構の一般化）。組み合わせ・タイミングを番組/
   フィラー毎に事前定義し自動表示。CM 基本セットは⑤と一体で保留（§C.4）。

各段は proto 変更を伴うため buf 生成 + agent/server 同時整合に注意（feedback: django-pytest-pitfalls の
gRPC smoke、grpc handler 内 DB は sync_to_async 経由）。

## F. 未確定・論点

- **透過動画のコーデック**: CasparCG(FFmpeg producer) でアルファ合成するには素材がアルファチャンネルを持つ必要が
  ある。対応見込み: `.mov`(QTRLE / PNG / ProRes4444)、`.webm`(VP9 alpha)、アルファ対応 AVI(UTVideo RGBA / Lagarith /
  PNG codec)。**「透過 AVI」は使うコーデックが RGBA/YUVA を保持していること**が条件（要 1 本実機検証）。loudnorm 等の
  正規化対象外（グラフィック素材）として medialib に取り込む。
- 速報(40) と 提供(50) の前後: docs は 40<50（提供が前面）。速報を最前面にしたいなら 40/50 の入替 or 速報を 55 へ。
- フリーグラフィック画像の配置方式: B.3 は「URL 参照（CEF ロード, 軽量）」を既定。`kind=video`(透過動画)は PLAY 用に
  R2 prefetch で送出ノードへ先読み（CEF URL 参照では動画再生が重いため）。
- 同一レイヤ複数枚は HTML テンプレの `elements[]` 多重描画で実現（1 HTML producer = 1 テンプレ制約のため）。透過動画は
  1 レイヤ 1 本のため、複数動画/動画＋文字は別レイヤ(45/46/47…)に分ける。
- レイヤ状態の visible 精度: INFO では CG の CSS 表示状態まで取れないため、agent 内部状態を一次情報とする。
- マルチ ch（最大4ch）展開時はレイヤパネル/op も per-channel。
