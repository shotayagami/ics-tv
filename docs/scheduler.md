# #2 スケジューラ状態機械

[datamodel.md](datamodel.md) の `program` / `ad_break` / `filler` を読み、送出可能な
`playout_event`（as-run）列へ解決し、送出ノードの agent がローカル AMCP で実行する。
以下は擬似コード（Python風）。AMCP の具体コマンドは #4 で詰める。

## 構成

- **リゾルバ（制御プレーン / Django + Celery beat）**：編成を時間窓ごとに `playout_event` 列へ解決し agent へ push。
- **agent 実行ループ（送出ノード / Proxmox LXC）**：`playout_event` を `scheduled_at` にローカル AMCP で実行。冪等・store-and-forward。
- **割り込み**：生cut / CM IN・戻り / feed断→SLATE / 緊急SLATE。

**冪等キーは決定的に算出**する（再解決で同一キー＝自然な差分反映、実行済イベントは不変）。

```python
NS_ICSTV = uuid5_namespace("icstv")
def ikey(channel_id, scheduled_at, action, seg):
    return uuid5(NS_ICSTV, f"{channel_id}:{scheduled_at.isoformat()}:{action}:{seg}")

def PE(channel, at, action, seg, asset=None, live_source=None, cm_bundle=None, params=None):
    return PlayoutEvent(
        idempotency_key=ikey(channel.id, at, action, seg),
        channel=channel, scheduled_at=at, action=action,
        asset=asset, live_source=live_source, cm_bundle=cm_bundle,
        params=params or {})
```

## リゾルバ（制御プレーン）

入力：`channel`, 解決窓 `[t0, t1)`（常に「今 + 48h」を維持）。
出力：`playout_event` の冪等 upsert ＋ agent への push。

```python
def resolve(channel, t0, t1):
    events = []
    programs = (Program.objects
                .filter(channel=channel, end_at__gt=t0, start_at__lt=t1)
                .order_by("start_at"))
    cursor = t0
    for i, prog in enumerate(programs):
        if prog.start_at > cursor:                    # 番組間の隙間 → フィラー
            events += emit_filler(channel, cursor, prog.start_at)
        events += (emit_recorded(prog) if prog.type == "recorded"
                   else emit_live(prog))
        # exposure_policy(#27) の YT ミラー制御 (yt_mirror_filler / yt_mirror_route)。
        # 隣接番組と必要性が連続する区間は先頭/末尾でだけ発行 (同時刻 2 イベントのフリッカ防止)。
        # 詳細設計は site-only-broadcast.md §4.3 (二重管理しない)。
        events += emit_exposure_mirror(prog,
                                       prev_prog=programs[i-1] if i > 0 else None,
                                       next_prog=programs[i+1] if i+1 < len(programs) else None)
        cursor = prog.end_at
    if cursor < t1:                                   # 末尾の隙間 → フィラー
        events += emit_filler(channel, cursor, t1)
    upsert_playout_events(events)                     # idempotency_key で diff 反映
    push_to_agent(channel, events)
```

### 放送時間帯（`Channel.broadcast_windows`, v0.8.70）

チャンネルに放送時間帯（`HH:MM-HH:MM` の配列、`24:00` 終端可）を設定すると、リゾルバは
解決窓を放送中／休止区間に分割して解決する。未設定なら従来どおり 24h 放送。
導入動機は急激な登録者減：通知過多には 4h 枠化（[youtube.md](youtube.md)）で対応しつつ、
現有コンテンツ量では 24h 編成を維持できないため、休止時間を設ける運用を選択した。

- 休止区間には `PLAY_SLATE`（`params.off_air=true`）を発行。放送中区間のみ番組・フィラーを解決し、
  休止中に開始する番組はスキップ。スキップ判定は**未クリップの窓幾何** (`Channel.is_on_air`) で
  行う。t0 クリップ済み区間で判定すると放送中に開始済みの番組 (start_at < now) まで resolve の
  たびに除外され、残り尺にフィラーが被せ発行されて本編を奪う（v0.8.83 で修正）。
- フィラーの `elapsed_ms` は**放送中時間のみ積算**し、休止明けは中断されたクリップの中断位置
  (オフセット) から再開する（後述「実番組による中断からの再開」と同じ規約）。
- **休止区間には `PLAY_FILLER` を発行しない**（v0.8.10x）。上の「放送中時間のみ積算」と対になる
  不変条件で、この 2 つが揃って初めて「サーバが思っている位相 = 実際に本線 (layer 10) で流れて
  いる位置」が成立する。`emit_filler` は発行下限を `max(not_before, anchor)` にし、休止をまたぐ
  区間では呼び出し側 (`_filler_seg_phase`) が `anchor` に**窓オープン時刻**を渡すことでこれを担保する。
  休止中の本線は休止直前のクリップを `LOOP` し続ける（スレートの下なので不可視）。
- 休止明け（窓オープン）には、**休止に入った瞬間の位相から再開する `PLAY_FILLER` を必ず 1 件発行**
  する。位相は `_filler_phase_at()` が「直近の発行済み `PLAY_FILLER` の位相 + そこからの放送中経過
  時間」で復元する（ステートレス・冪等）。窓オープン時刻も位相も resolve 周期で動かないため、
  周期再解決で再生中クリップを奪わない。
- 位相原点 (`anchor`) は `cursor=t0=now` を**採らない**（動く値を原点にすると再生中クリップを毎周期
  奪う旧不具合に戻る）。候補は「同じ放送中窓の中で終わった直近番組の `end_at`」か「窓オープン時刻」
  の 2 つだけ（`_filler_seg_anchor`）。
- `_last_program_end_before()` は**開始時刻が休止中の番組（= 送出スキップされる番組）を除外**する。
  除外しないと、実際には放送されていない番組の `end_at` が位相アンカーになり、送出中フィラーが
  `_onair_filler_anchor` の探索窓 `[anchor, now]` から外れて鎖状継続が切れる。そのギャップには
  境界が 1 つも入らず既存の後続境界が cancel され、次の番組明けが「中断位置からの再開」ではなく
  無関係なクリップへのハードカットになる（実障害として発生した）。
- `_commit()` は `off_air=true` の resolver 管轄スレートのみキャンセル対象にする
  （運用の手動スレートは巻き込まない）。
- **スレート層 (layer 90) は本線 (layer 10) と独立**のため、休止明け (放送中区間の先頭) には
  `CLEAR_SLATE` も発行してスレートを解除する（`emit_resume` / v0.8.76）。本線側 (`emit_filler`)
  の再開だけではスレートが上に残り続けて画面がスレート固着になる。`_needs_slate_clear()` が
  「直近の `off_air` `PLAY_SLATE` より後に `CLEAR_SLATE` が無い」ときだけ発行するため、resolve
  周期 (beat) ごとの無条件再発行は起きず、運用の手動/緊急スレートを奪い返すこともない。
- 解除は**フィラー区間の先頭だけでなく番組の直前でも判定**する（v0.8.83）。窓オープンちょうどに
  番組が始まる編成では直前ギャップに on-air フィラー区間が無く、フィラー側の解除経路だけでは
  `CLEAR_SLATE` が出ないまま番組がスレートの下で流れ続ける。判定は同一 resolve 実行内の未コミット
  イベント (pending) も時系列に含め、直前フィラー区間の解除と重複発行しない。
- 未来にプリスケジュールした `CLEAR_SLATE` (SCHEDULED) は**同じ idempotency_key で再発行し続けて
  保持**する（v0.8.83）。「解除済み」とみなして再発行をやめると次の `_commit()` が tombstone 化
  →次周期で復活のフラップになり、休止明け時刻にちょうど CANCELLED 側だと agent が実行せず
  スレートが最大 1 beat 残る。
- 休止明けまで **30 秒 (`_STANDBY_TAIL_GUARD`) を切ったら `off_air` スレートを発行しない**。
  休止中は beat ごとに「now」でスレートを再発行するため、窓オープン直前の 1 発は agent への
  gRPC 到達 + ディスパッチ遅延（実測 1〜6 秒）の間に休止明けの `CLEAR_SLATE` に追い越され、
  復帰済みの本線の上へ再点灯して固着する（運用者が手動解除するまで長時間固着した
  実例がある）。スレートは
  `loop=true` で既に出続けているため、最後の 1 発を落としても画面は変わらない。agent 側も
  `params.until` を過ぎた `off_air` スレートは AMCP を撃たず SKIPPED で畳む（多重防御）。
- `_needs_slate_clear()` の「解除済み」判定は `scheduled_at` の前後だけでなく **`actual_at` の
  実行順の逆転も見る**。上記の追い越しが起きると `scheduled_at` 上は解除済みに見えるため、
  旧実装は再発行せず運用者が手動解除するまで復旧しなかった。実行順が逆転していれば次の
  beat（最大 5 分）で `CLEAR_SLATE` を再発行して自己修復する。
- YouTube 枠生成・遷移も放送時間帯に連動する（[youtube.md](youtube.md) 参照）。

### 実番組による中断からの再開（フィラー、v0.8.9x）

フィラー再生中に実番組（生放送や休止明け窓オープン）が割り込んだ場合、番組明けは**中断された
クリップ自身を、中断位置 (オフセット) から残り尺だけ再生してから**通常ローテーションに戻る
（旧 v0.8.28 の「リスト上の次のクリップから継ぐ」仕様を置き換え）。番組表は特別な統合表示を
持たず、実際に発行されるイベントの `scheduled_at`/`until` をそのまま投影するため、中断された
クリップは「前半→番組→後半 (残り尺)」の3ブロックとして表示され、結果的にそのクリップの
見かけ上の占有時間が中断分だけ延びる（仕様として許容）。

- `_filler_resume_index(durs, filler_elapsed_ms)` は「次のクリップ index」ではなく
  `(中断されたクリップの index, クリップ内オフセット ms)` を返す。連鎖する複数回の中断があっても、
  `filler_elapsed_ms`（各ギャップの実経過時間の単純合計）をそのまま使えば正しいクリップ内位置に
  一致する（「一度オフセット再開したクリップは以後その位置から実時間 1:1 で進む」という不変条件）。
- `_filler_cycle` の `start_offset_ms` が先頭クリップの実効尺を `durs[start_idx]-start_offset_ms`
  に短縮し、後続クリップの境界計算にもその短縮分を伝播させる。送出 (`emit_filler`) と EPG 投影
  (`project_filler_segments`/`_project_chain`) の両方がこの共有関数を使うため、番組表と実放送は
  一致し続ける。
- 再開クリップは `params.in_ms`（シーク位置）/`params.out_ms`（クリップの自然尺）を持ち、
  `PlayFillerPayload.in_ms`/`out_ms`（proto）経由で送出ノードへ渡る。agent はこれを
  `LOADBG ... LOOP SEEK <f> LENGTH <f>` に変換する（[casparcg.md](casparcg.md) §2.6）。通常
  (非中断) のフィラーは従来どおり `in_ms` 無し = `LOADBG ... LOOP` のまま。
- 鎖状継続 (`_onair_filler_anchor`) パス: on-air クリップ自身が再開クリップ (`in_ms` 付き) の場合、
  次境界の算出はその `in_ms` を差し引いた実効尺を使う（フルの自然尺のままだと次境界が後ろにずれる）。
- **放送休止をまたぐ場合**（v0.8.10x）: 中断位置の算出は `_filler_phase_at()` に一本化した。
  「直近の発行済み `PLAY_FILLER` の位相 + そこからの**放送中**経過時間」で決まるため、途中の境界
  イベントを 1 件も見なくても正しく、休止をまたいでも「休止に入った瞬間の位置」が保たれる。
  EPG 投影 (`project_filler_segments` / `_project_chain`) も同じ規約に揃えてあり、放送中区間ごとに
  サイクルを敷き直して区間の実時間だけ位相を進める（休止中は進めない）。
  回帰: この 2 系統がずれていた頃は、休止をまたいだクリップを実番組が中断すると再開位置が
  「休止中に再生された分」だけ手前に戻っていた（実障害として観測された）。

### 録画番組：本編とCM枠のインターリーブ

不変条件：`end_at == start_at + asset.duration_ms + Σ(ad_break.duration_ms)`
（CM枠は尺を**足す**。end_at は編集時にこの式で算出し、EXCLUDE 制約に効かせる）。

```python
def emit_recorded(prog):
    ev, breaks = [], sorted(prog.ad_breaks, key=lambda b: b.offset_ms)
    head = 0            # 素材内オフセット(ms)
    air  = prog.start_at
    seg  = 0
    for br in breaks:
        # 本編セグメント [head, br.offset_ms)
        ev.append(PE(prog.channel, air, "play_asset", seg, asset=prog.asset,
                     params={"in_ms": head, "out_ms": br.offset_ms}))
        air += ms(br.offset_ms - head); head = br.offset_ms; seg += 1
        # CM枠：残尺ベース充填 (残尺が残れば play_filler(reason=cm_insufficient) で埋める)
        for i, (item, cm) in enumerate(fill_break(br, air)):
            ev.append(PE(prog.channel, air, "play_cm", f"{seg}.{i}", asset=cm.asset,
                         ad_break_item=item,          # 放確の逆引き (#6 S10)
                         params={"advertiser": cm.advertiser}))
            air += ms(cm.asset.duration_ms)
        seg += 1
    # 最終本編セグメント [head, 素材尺]
    ev.append(PE(prog.channel, air, "play_asset", seg, asset=prog.asset,
                 params={"in_ms": head, "out_ms": prog.asset.duration_ms}))
    assert air + ms(prog.asset.duration_ms - head) == prog.end_at   # 検算
    return ev
```

### CM枠の充填（残尺ベース・契約 provider・均等ローテへのデグレード）

実装は `scheduling/resolver.py` の `fill_break(br, air_at, provider=None)`。旧設計（slot 数 × 均等ローテ・house_cm フォールバック）から以下へ拡張済み。契約駆動割付の優先順位（提供 > 指定番組 > 線引き > フリー）と provider の詳細は [sales.md](sales.md)「契約駆動の CM 割付」を参照（二重管理しない）。

```python
def fill_break(br, air_at, provider=None):
    """枠を充填し (AdBreakItem, CmCreative) ペア列を返す (playout_event.ad_break_item FK 用)。"""
    existing = br.items.order_by("seq")           # 既存 AdBreakItem があれば再利用
    if existing:                                  #  (再解決の決定性。送出済み枠は不変 = S11)
        return [(item, item.cm_asset) for item in existing]
    remaining = br.duration_ms
    pool = free_pool(br.grid, remaining, air_at.date())
        # READY・campaign 期間内 (start/end の NULL は「期間無制限」として許容)・max_airings 未達
    if provider is not None:                      # 契約駆動割付 (sales。settings で注入)
        candidates = provider.candidates(br, air_at, pool)
    else:                                         # provider 無し: 長尺優先 (残尺の断片化防止)
        candidates = sorted(pool, key=lambda cm: (-cm.asset.duration_ms, cm.aired_count))
    chosen = []
    while remaining >= unit(br.grid):             # 残尺ベース: 30 秒素材は 15s 枠の 2 スロット分
        cm = pick(candidates, remaining, exclude=chosen)
            # 尺が残尺以内かつ unit の整数倍・同一枠内重複なし・provider.accept(考査/業種競合)
        if cm is None:
            break                                 # 在庫不足: ここでは埋めない (下記)
        chosen.append(cm); remaining -= cm.asset.duration_ms
    items = persist_items(br, chosen)             # AdBreakItem 永続化 + provider.persisted
    return list(zip(items, chosen))               #  (placement 記録) を同一 atomic で括る
```

- **埋まらない残尺は呼び出し側（emit_recorded）が既定フィラーで充填**する（`PLAY_FILLER`、
  `params.reason="cm_insufficient"`・`loop=False`・`duration_ms=残尺`。air 進行を維持して
  `end_at` の検算を崩さない）。**house_cm / SLATE 代替は不採用で確定**。
- provider 未指定なら従来の均等ローテ（長尺優先 → 同尺は `aired_count` 昇順）にデグレード
  （24/7 送出の安全性最優先。sales 不在でも動く）。
- `aired_count` の増分は **実送出確定時（agent の as-run 返送時）** に行う（予約時に増やすと欠送で過大計上）。

### 生番組：cut を事前生成、CM は手動割り込み ＋ cue 単位の自動発火予約

```python
def emit_live(prog):
    # 生の離脱(prog.end_at)は次イベント(filler/次番組)が担うため明示不要。
    # CMの基本は「任意タイミング(CM IN)」＝agentの実行時割り込み。bundleを参照だけ渡す。
    events = [PE(prog.channel, prog.start_at, "cut_live", 0,
                 live_source=prog.live_source,
                 params={"cm_bundle_id": prog.cm_bundle_id})]
    events += _emit_live_auto_fire_cues(prog)   # auto_fire=true の LiveCue を絶対時刻で予約発行
    return events
```

CM/VT は**手動発火が主**のまま、`auto_fire=true` を立てた `LiveCue` は **resolve 時に絶対時刻の
PlayoutEvent として予約発行**される（`_emit_live_auto_fire_cues`。anchor=start は
`start_at + auto_offset_ms`、anchor=wallclock は番組日の固定時刻。`state=PENDING` のみ対象で、
手動発火/スキップ済み cue は再発行しない）。詳細は [timekeeper-live.md](timekeeper-live.md)
§6／Phase3 D1（二重管理しない）。

### フィラー：隙間をループで充填

```python
def emit_filler(channel, start, end):
    return [PE(channel, start, "play_filler", 0,
               params={"filler_playlist_id": channel.default_filler_id,
                       "loop": True, "until": end.isoformat()})]
```

agent は `play_filler` を次イベントの `scheduled_at`（=`until`）まで loop 再生する。
フィラー境界イベントの先読み発行は **now + 3h（`_FILLER_HORIZON`）まで**に制限し、長大な
ギャップは resolve 周期（beat）の再解決で漸進的に敷く。

### 再解決（編成変更の反映）

Celery beat が定期的に窓 `[now, now+48h)` を再解決。`idempotency_key` が決定的なので
upsert は自然な diff になる。**実行済 / 実行中（過去〜直近）のイベントは不変**として扱い、
未来分のみ差し替える。

## agent 実行ループ（送出ノード / Proxmox LXC）

`LOADBG`（背面ロード）→ `PLAY`（テイク）で**継ぎ目のない切替**を作る。

```python
PREROLL = 5000   # ms 先読み(デコード余裕)
TICK    = 100    # ms

def agent_loop(channel):
    buf = pull_events(channel)              # N時間分をローカル保持(WAN断耐性)
    while running:
        now = wall_clock()                  # NTP同期した壁時計
        # 1) 先読み：PREROLL以内のクリップを背面ロード
        for ev in due_within(buf, now, PREROLL):
            if ev.action in ("play_asset", "play_cm", "play_filler") and not ev.loaded:
                amcp_loadbg(ev)             # LOADBG layer <r2_local_path> SEEK/LENGTH=in/out
                ev.loaded = True
        # 2) 実行：scheduled_at 到来分
        for ev in due_now(buf, now):
            if executed(ev.idempotency_key):    # 冪等：再送/再起動でも二重実行しない
                continue
            try:
                amcp_take(ev)               # PLAY / cut_live / filler loop 開始
                commit(ev, status="done", actual_at=now)
            except Exception as e:
                commit(ev, status="failed", note=str(e))
                if critical(ev):
                    goto_slate(channel)     # 本線が落ちたら即スレート
            asrun_queue.put(ev)             # store-and-forward
        # 3) 割り込み
        handle_interrupts(channel)
        flush_asrun_if_online()             # WAN復帰時に制御プレーンへ返送
        sleep(TICK)
```

- **冪等 / resume**：`idempotency_key` を実行済としてローカル永続化（SQLite等）。再起動・WAN復帰後は
  再 pull → 実行済をスキップ。
- **store-and-forward**：as-run はローカルにキューし、オンライン時に制御プレーンへ返送（`aired_count` 確定もここ）。

## 割り込み（優先度：SLATE ＞ 生cut ＞ CM IN ＞ 通常）

- **CM IN（生）**：運用UI → agent が `cm_bundle` を再生 → 戻り（手動 or 残尺自動）→ 生入力へ復帰。
  実送出を `playout_event(action="play_cm_bundle", actual_at=now)` として as-run 記録。
- **feed断（生）**：MediaMTX のフレーム監視で検知 → 即 SLATE → Zabbix通報。復帰は運用ポリシー（自動 or 手動）。
- **緊急SLATE**：全状態から最優先で遷移（手動 or ウォッチドッグ）。
- **YouTube枠遷移（yt_transition）**：AMCP ではなく Data API。YouTube枠ワーカー（制御プレーン）が担当（#3）。
  as-run には観測用に記録。

## 前提・時刻

- 制御プレーン / 送出ノードとも **NTP 同期**。`playout_event` は絶対 `timestamptz`。
- 解決窓は常に「今 + 48h」を維持（agent のローカルバッファ寿命と整合）。

## 未確定・論点

- **PREROLL の長さ**：素材デコード時間・GPUロード時間に依存。実機で計測して決める。
- ~~**生番組の延長時の扱い**~~ → [operations.md](operations.md)（#7 決定 O1）で解決：**既定は強制カット**のまま、
  明示の押え操作で後続繰り下げ＋フィラー隙間吸収。
- ~~**CM IN のトリガ源**~~ → タイムキープ実装（決定#25、[timekeeper-live.md](timekeeper-live.md)）で
  解決：**手動発火が主＋cue 単位で任意に `auto_fire`（絶対時刻予約。手動が常に上書き）**。
- ~~**CM在庫不足時のハウス枠**~~ → 解決済：**埋まらない残尺は既定フィラー
  （`PLAY_FILLER`, `reason=cm_insufficient`）で充填。`house_cm` は不採用**（上記「CM枠の充填」）。
