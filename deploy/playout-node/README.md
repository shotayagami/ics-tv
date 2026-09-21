# 送出ノード (playout node) — 特権 LXC + Intel iGPU 共有

送出系3点 ＝ **CasparCG / MediaMTX / icstv-agent** を、導入者の仮想化基盤の **特権 LXC** に
同居させる構成・手順。

> 本書の「構築手順」節は新規ノードを 1 から作る際の一般手順で、`CTID` 以外はスクリプトの既定値
>（`CT_HOSTNAME=icstv-playout` 等）をそのまま使っている。
> 操作は `ssh root@<Proxmox ホスト>` + `pct exec <CTID> -- <cmd>`（ノードへの直接 SSH は不可）。
> 本書のコマンド例の `<CTID>` は `provision-lxc.sh` で作成した CT の id を表す。

> 本書内の IP (`192.0.2.x`) は RFC 5737 TEST-NET-1 の例示アドレス。実際の値は導入者の LAN ごとに
> 異なるため、構築時は自分の環境の値に置き換えること。

## なぜこの構成か (元設計からの逸脱点)

`docs/overview.md` は送出を「クラウド GPU ノード + NVENC + WireGuard」と想定していたが、参照構成の
基盤には **NVIDIA GPU が無く Intel UHD630 (iGPU) のみ**、仮想化ホストの容量も逼迫していた。
オンプレに置く前提で現実的な構成に置き換えた:

- **VM 全 passthrough ではなく特権 LXC + `/dev/dri` 共有**。ホストは iGPU を保持し、複数コンシューマで共有。
- **CasparCG の ffmpeg consumer は VAAPI HW エンコード不可** ([CasparCG #1300](https://github.com/CasparCG/server/issues/1300))。
  → CasparCG は localhost UDP に安価な intra(mpegts) を吐き、**別プロセス `icstv-encoder` が
  `/dev/dri/renderD128` 上の h264_vaapi でエンコード**して `tee` で **YouTube ingest** ＋ ローカル
  MediaMTX へ二重送出（[docs/casparcg.md](../../docs/casparcg.md) §6.4）。**Cloudflare Live Input はこの経路には無い**
  （studio の手動操作 / fanclub simulcast 用に別途残るのみ）。
- **ヘッドレス(非X11)は CasparCG 2.5.0+ が必要** (EGL 化)。実機は 2.5.0 PPA を採用（保険は 2.3.3+Xvfb だが未使用）。
- **同一 LAN** のため WireGuard 不要。agent は制御プレーンの gRPC エンドポイント
  `icstv-grpc:50051` (例 `192.0.2.91`。導入者の基盤側で固定する) へ直結。
- 目標解像度は **720p60 既定** (`ICSTV_FPS=60` / casparcg.config `720p6000` と一致)。1080p30 も可（GPU/CPU と容量の見積もりが別途必要）。
- **既定は 1ch (ch1) 単独構成**（GPU/CPU 負荷削減のため casparcg.config から ch2 以降を外している）。
  下記の構成図・マルチチャンネル節は 2ch 以上へ増やす場合の設計として残す。

```
[送出ノード LXC / 192.0.2.20]                      [制御プレーン]            [YouTube]
 OBS ──RTMP→ MediaMTX(:1935) ─┐                     icstv-grpc(.91:50051)    ingest (live2)
                              ↓ (127.0.0.1)              ▲ gRPC                  ▲
 CasparCG(EGL/CEF) ─UDP:5004→ icstv-encoder@ch1 ─h264_vaapi─tee→ YouTube ingest(ch1)
        ▲ AMCP 5250                            └─tee→ ローカル MediaMTX /hls/ch1 (自前 HLS)
        └──────────── icstv-agent@ch1 ──gRPC(SubscribeEvents/ReportResult)─────────┘
                       (systemd template, SQLite store-and-forward / queue-ch1.db)

 マルチチャンネル (将来案・現行は 1ch): 1 CasparCG サーバ (AMCP 5250 共有) に <channel> を並べ、
 agent/encoder をチャンネル別インスタンス (@ch1/@ch2/@ch3) で同居。CasparCG channel N → UDP 5004+(N-1) →
 icstv-encoder@<slug> → YouTube ingest(<slug>) + ローカル MediaMTX /hls/<slug>。iGPU は全 ch 共有。
```

## ディレクトリ構成

| パス | 役割 |
|---|---|
| `lxc/playout-node.conf.example` | LXC 定義 + iGPU 共有 (dev0/dev1) の参考 |
| `casparcg/casparcg.config` | CasparCG 設定 (`<channel>` ごとに UDP 5004+(N-1) 出力, AMCP 5250 共有, CEF は enable-gpu=false=CPU描画。現行は 1ch。マルチch は将来案) |
| `casparcg/template/` | CEF CG テンプレ (提供 credit/sponsor・Lバー lbar/standard・テロップ telop/lower-third・バンパー bumper/cm-in・地図CG map/\*・時計 clock/corner 等)。`install.sh` は**初回プロビジョニング時にのみ** `template-path`（`/opt/casparcg/template/`）へ配置する。**以後のテンプレ更新はノードへの手動配備が必要**（`sync-node.sh` の差分レポートが未配置/差分を検出するので、§ノードの更新 の `template:` 手順で個別 `install`）。各 `?test=1` でブラウザ単体確認可 |
| `mediamtx/mediamtx.yml` | 生入力 RTMP 終端 (:1935) + API (feed-drop 検知) |
| `systemd/*.service` | mediamtx / casparcg-scanner / casparcg-server / **icstv-encoder@** / **icstv-agent@** (後 2 つは `@<slug>` テンプレートユニット)。**規約**: unit 先頭コメントの「`# 未配備の理由: …`」は意図的な未配備の宣言で、`install.sh` はこのヘッダ持ち unit を既定では配置せず (opt-in 時のみ)、`sync-node.sh` は `[未配備]` として理由を表示する |
| `systemd/icstv-yt-mirror-encoder@.service` | exposure_policy (#27) 公開ミラー M の VAAPI サイドカー (YouTube ingest 単一出力)。opt-in・**本番未適用**。GPU 容量実測前は start しない (`docs/site-only-broadcast.md` §5-1) |
| `systemd/icstv-yt-members-encoder@.service` | 同メンバーミラー P (YouTube members-only ingest 単一出力)。同上・**本番未適用** |
| `systemd/icstv-encoder@ch1.service.d.example/` | 本線 tee から YouTube leg を外す (exposure_policy カットオーバー) systemd drop-in の例。ch1 だけに適用、他チャンネルの共有テンプレートは変えない。**本番未適用** |
| `env/agent.env.example` | agent 環境の例 (env 契約は `agent/icstv_agent/config.py`)。本番は ch 別に `/etc/icstv/agent-<slug>.env`。exposure_policy のミラー channel 番号/案内フィラー clip も含む (既定コメントアウト) |
| `env/encoder.env.example` | サイドカーエンコーダ環境の例 (UDP_PORT/slug/CF ingest URL/ビットレート)。本番は ch 別に `/etc/icstv/encoder-<slug>.env` |
| `env/yt-mirror-encoder.env.example` / `env/yt-members-encoder.env.example` | exposure_policy YTミラー2種の環境の例。本番は ch 別に `/etc/icstv/yt-mirror-encoder-<slug>.env` / `yt-members-encoder-<slug>.env` |
| `scripts/provision-lxc.sh` | Proxmox ホストへ SSH して CT 作成 + iGPU 共有 |
| `scripts/install.sh` | CT 内で VAAPI/CasparCG/MediaMTX/agent 導入 + systemd 配備。**プロビジョニング専用**で apt/venv まで触るため、稼働中のノードに流してはいけない (更新は `sync-node.sh` + 個別配置) |
| `scripts/sync-node.sh` | ノードの `/opt/icstv` を git チェックアウトとして同期し、配備済み成果物との差分を報告する。稼働サービスには触らない (fetch/checkout と差分表示のみ) |
| `scripts/validate.sh` | 段階検証ゲート (vainfo → EGL → AMCP → agent → HLS → watchdog) |
| `scripts/amcp-smoke.py` | AMCP 接続 + 提供 CG テンプレの ADD/PLAY/UPDATE/STOP スモーク (agent venv で実行)。`/opt/icstv/agent/.venv/bin/python …/amcp-smoke.py` |
| `scripts/watchdog.sh` | 送出ヘルス監視 → 緊急スレート/再起動。install.sh が `/usr/local/bin/icstv-watchdog.sh` へ配置し `icstv-watchdog.timer` が 30s 周期で起動する。**単体では動かないので timer の稼働を validate.sh Gate 10 で確認すること**。YouTube 枝 bytes_sent 監視を含む — 後述 §送出ヘルス監視: YouTube 枝 bytes_sent |
| `systemd/icstv-watchdog.service` / `.timer` | 上記 watchdog の周期起動 (30s)。timer を配置しないと watchdog は周期実行されない |

### 監視・アラート経路の外部依存 (ログ転送)

watchdog.sh の検知結果は `logger -t icstv-watchdog` → journal に出るだけで、そこから先の転送は
**ノード共通プロビジョニングのログ転送エージェント**（導入者の配備基盤側の管理物で、この
リポジトリの範囲外）が journal をログ基盤へ送ることで初めてアラート化できる。
この README の手順（install.sh / sync-node.sh）はログ転送エージェントを配備**しない**。
journal→ログ基盤の転送を別途用意するか、この経路が無い前提で
`journalctl -t icstv-watchdog` を直接見る運用にすること。

### 送出ヘルス監視: YouTube 枝 bytes_sent

tee の YouTube 枝だけが凍結することがある (ffmpeg active・ローカル MediaMTX 枝正常・
HLS 鮮度正常のまま、`ss` で YouTube 宛ソケットの bytes_sent が停滞)。encoder unit の
fifo 自動復帰は「エラーが出れば」再接続する仕組みなので、エラーにならない凍結では発火しない。
watchdog.sh の `check_yt_bytes` がこれを検出する:

- 30s tick ごとに `ss -tinp 'dport = :1935'` を見て、encoder MainPID が持つ**非 loopback 宛**
  (= YouTube ingest) ソケットの bytes_sent を前回値と比較。`YT_BYTES_STALL_SEC` (既定 300s)
  進まなければ priority=crit で journal へ出し (転送先のログ基盤では重大度で絞れる)、
  `icstv-encoder@<slug>` を自動 restart する
- **ソケット不在は正常扱い** (放送休止帯で YouTube 出力が無い形・YTミラーへのカットオーバー
  drop-in で encoder が MediaMTX 枝のみの形)。「存在するのに進まない」だけを異常とする
- 安全弁: restart は最短 `YT_RESTART_COOLDOWN_SEC` (既定 30 分) 間隔。復旧が
  `YT_BYTES_STALL_SEC` 持続しないまま `YT_RESTART_MAX_ATTEMPTS` (既定 3) 回 restart したら
  諦めて CRIT ログのみ (= 原因が encoder の外にある形。手動対応)

パーサ (`parse_yt_bytes`) は実ノードで採取した ss 出力をフィクスチャにした
self-test を内蔵する: `bash deploy/playout-node/scripts/watchdog.sh --self-test`
(root 不要・システム無変更。validate.sh Gate 10 も配備済みスクリプトに対して実行する)。

## 構築手順（新規ノードを 1 から作る場合）

> ⚠️ `provision-lxc.sh` は `CTID` の指定が必須（既定値なし）。その他の既定値は `CT_HOSTNAME=icstv-playout` / **`IP_CIDR=192.0.2.20/24`**
> （`scripts/provision-lxc.sh`）。**既に送出ノードを動かしている環境で既定値のまま実行すると、
> ホスト名・IP が衝突する CT が生える**。同一の基盤で別ノードを試すときは必ず
> `CTID` には既存と衝突しない値を指定し、`CT_HOSTNAME` / `IP_CIDR` を明示的に変えること（下記コマンド例は `CTID` 以外は既定値のまま。実行前に上書きすること）。

```bash
# 1. Proxmox ホスト (ssh root@<Proxmox ホスト>) 上で LXC 作成 + iGPU 共有 (gid は自動実測)。
#    CTID は必須。既存と衝突しない値を指定する (以下の <CTID> はこの値)
CTID=<CTID> deploy/playout-node/scripts/provision-lxc.sh

# 2. CT にリポジトリを clone して install (このノードに置くチャンネルを ICSTV_CHANNELS で指定)
pct exec <CTID> -- bash -lc 'apt-get update && apt-get install -y git ca-certificates'
pct exec <CTID> -- git clone https://<git-host>/<your-org>/icstv.git /opt/icstv
pct exec <CTID> -- bash -lc 'bash /opt/icstv/deploy/playout-node/scripts/install.sh'

# 3. 実値を投入 (mode 600。チャンネルごとに)
#    /etc/icstv/agent-<slug>.env   : ICSTV_AGENT_TOKEN=channel.agent_token / ICSTV_CHANNEL_SLUG /
#                                    ICSTV_CASPAR_CHANNEL / ICSTV_QUEUE_DB=queue-<slug>.db
#    /etc/icstv/encoder-<slug>.env : UDP_PORT=5004+(N-1) / ICSTV_CHANNEL_SLUG / CF_INGEST_URL
#                                    (変数名は historical。実体は YouTube ingest URL + 永続キー)

# 4. 段階検証 → 起動
pct exec <CTID> -- bash /opt/icstv/deploy/playout-node/scripts/validate.sh
pct exec <CTID> -- systemctl start mediamtx casparcg-scanner casparcg-server
pct exec <CTID> -- bash -lc 'for ch in ch1; do systemctl start icstv-encoder@$ch icstv-agent@$ch; done'
```

> 上記コマンド中の `<CTID>` は手順 1 で `provision-lxc.sh` に指定した CTID を表す。実際に作成した
> CTID に読み替えること。**既に稼働しているノード**への操作は本節ではなく
> 「ノードの更新」節（下記）を参照する。

## ノードの更新 (プロビジョニング後の日常運用)

`install.sh` は**プロビジョニング専用**。apt でのパッケージ再取得や venv 再構築まで行うため、
稼働中の送出ノードに流すとオンエアが飛ぶ。更新は次の 2 段で行う。

```bash
# 1. ソースを git で同期し、配備済みとの差分を確認する (無停止・冪等)
pct exec <CTID> -- bash /opt/icstv/deploy/playout-node/scripts/sync-node.sh        # 既定 ref=main
pct exec <CTID> -- bash /opt/icstv/deploy/playout-node/scripts/sync-node.sh dev    # ref 指定

# 2. 差分のあるものだけを手で配置する (断が出る操作を暗黙に走らせないため自動化しない)
#    unit:     install -m644 <src> /etc/systemd/system/<name> && systemctl daemon-reload
#    watchdog: install -m755 <src> /usr/local/bin/icstv-watchdog.sh
#              配置後 bash /usr/local/bin/icstv-watchdog.sh --self-test でパーサ検証 (無停止)
#    config:   install -m644 <src> <dst> の後、該当サービスの restart が要る = 断が出る
#    template: install -m644 -o casparcg -g casparcg <src> /opt/casparcg/template/<rel>
#              (CasparCG の再起動は不要。CEF は CG ADD 時にファイルを読む。owner は
#              install.sh が配置する既存テンプレと揃える。反映確認は §CG テンプレの手動配備 参照)
```

> **CG テンプレ（`casparcg/template/`）も sync-node.sh の差分レポート対象**（未配置は [差分] と同格で
> 検出する。新規追加したテンプレが検査対象外のまま数か月ノードへ配備されずに残り、実運用で
> CG ADD が File not found になった事故を受けて追加）。検出後の配備は
> 上記 `template:` の手順で個別に行う。テンプレはノードへの手動配備が必要な成果物である、という一般則
> として覚えておくこと（配備の自動同期はされない）。

> **sync-node.sh 自体も自動実行されない**（ノードに sync 系 timer は無く、開発側 CI の
> 自動デプロイ（このツリーには含まれない）が反映するのは agent パッケージ
> （`update-agent.sh` の sha256 差分転送 + restart）のみで、テンプレも config も配備しない）。
> `deploy/playout-node/` を触るリリースでは必ず手動で `sync-node.sh` を実行して差分を確認すること。
> ドリフト検査の自動化（差分レポート専用の日次 timer で DRIFT>0 を journal→ログ基盤へ流す案）と
> テンプレ配備の自動化（workflow で sync-node.sh を回す / `update-agent.sh` 相当のテンプレ配備
> ステップ新設）は**未決**（「断が出る操作を暗黙に走らせない」現設計との整合をユーザ判断で決める）。

`/opt/icstv` が git チェックアウトでない旧ノードでも `sync-node.sh` がその場で移行する。
`.venv` や `*.egg-info` は `.gitignore` 対象なので保持される。ただし checkout は追跡外ファイルを
削除しないため、スナップショット時代の残骸が残る。**掃除対象は `deploy/` 配下だけではない**：
実機には `agent/icstv_agent/*.bak` や `agent-code.bak*.tgz` などツリー全域に残骸があり、さらに
git 外の `/etc/systemd/system` には旧非テンプレート unit（`icstv-agent.service` /
`icstv-encoder.service`）や unit の `*.bak`、`/etc/icstv` には config の `*.bak` が残っている
（実機で確認した例）。`sync-node.sh` は `/opt/icstv` 全ツリーの追跡外と `/etc/systemd/system` の
git 外 icstv 系 unit/`*.bak` を警告として列挙するので、`git clean -fd`（追跡外のみ削除）と
`rm` + `systemctl daemon-reload` で掃除すること（実施の判断はオペレータ）。
**残骸を放置すると、削除済みの古い unit を現役のソースと取り違えて配備する事故になる。**

agent コードは `/opt/icstv/agent` の editable install なので、`sync-node.sh` の checkout で
更新され、`systemctl restart 'icstv-agent@*'` で反映される (配置作業は不要)。
コミットしていない手元の変更を試すときだけ `update-agent.sh` を使う。

## 実証結果 (2026-06-10, 実証用 LXC / Ubuntu 24.04 / Intel UHD630)

低スペック CT (mem2560/720p) で Gate1-7 を全通過。常駐 (24/7) は host 容量の都合で保留。

| Gate | 内容 | 結果 |
|---|---|---|
| 1 | iGPU `/dev/dri` 共有 (dev0 renderD128 gid993 / dev1 card0 gid44) | ✅ card0=root:video, renderD128=root:render |
| 2 | VAAPI H264 encode (casparcg ユーザで vainfo) | ✅ iHD 24.1.0, `VAEntrypointEncSlice`/`EncSliceLP` |
| 3 | ffmpeg `h264_vaapi` 単体エンコード | ✅ h264 1280x720@60 出力 |
| 4 | **CasparCG 2.5 headless EGL** | ✅ **OpenGL 4.6 Compatibility (Mesa Intel)** + Initialized channels |
| 5 | AMCP 制御 (VERSION/PLAY/INFO) | ✅ 201/202、filler 再生、720p6000 |
| 6 | CasparCG→UDP→サイドカー `h264_vaapi`→出力 (E2E) | ✅ mpeg2/mp2 stereo→h264 出力 |
| 7 | MediaMTX RTMP ingest + API | ✅ v1.19.0、:1935 / 127.0.0.1:9997 |

**実証で確定した必須設定 (install.sh / casparcg.config / unit に反映済み)**:
- CasparCG は **PPA** (`ppa:casparcg/ppa`) で導入。binary=`/usr/bin/casparcg-server`、scanner=`/usr/bin/casparcg-scanner`。
- 起動 env **`EGL_PLATFORM=surfaceless`** が必須 (無いと `eglChooseConfig` 失敗。device platform は不可)。
  併せて `LC_ALL=C.UTF-8 LANG=C.UTF-8` (locale abort 回避)、`LIBVA_DRIVER_NAME=iHD`。
  → 旧情報「Intel/Linux は core-only で CasparCG 不可」は Mesa 25.2 では**覆り、iGPU で 4.6 Compat 取得**。
- CEF は `<enable-gpu>false>` (headless で CEF GPU プロセス segfault 回避)。
- consumer 音声は **`-filter:a pan=stereo|c0=c0|c1=c1`** で 16ch→stereo ダウンミックス (mp2 は 2ch まで)。

## 検証ゲート (各層を実証してから次へ)

1. `/dev/dri` と gid 一致 → 2. `vainfo` に H264 `VAEntrypointEncSlice` → 3. CasparCG 抜きで testsrc を
VAAPI→YouTube ingest (egress 単独実証) → 4. CasparCG 2.5 EGL でチャンネル green (**最大リスク**) → 5. AMCP
`PLAY 1-10 filler LOOP` → 5.5 **CG テンプレ** (`amcp-smoke.py` で提供 ADD/PLAY/UPDATE/STOP、出力に数秒映る)
→ 6. CasparCG→UDP→encoder→YouTube にフィラー到達 → 7. OBS→MediaMTX→`PLAY 1-10 rtmp://127.0.0.1:1935/...`
→ 8. agent が gRPC 接続 (Bearer = channel.agent_token) → 9. Django で play イベント投入 → preroll
LOADBG→PLAY 切替 → as-run `DONE` 返送。

`scripts/validate.sh` が Gate 1-10 (CG スモーク=Gate 6、HLS ABR=Gate 9、送出ヘルス監視=Gate 10) を自動チェックする。AMCP/CG プロトコル解釈は
実機なしでも agent の `tests/test_caspar_client.py` (fake AMCP サーバ) で回帰検証済。

## リスク / フォールバック

- ~~**CasparCG EGL on UHD630**~~ → **2026-06-10 に解決済み** (`EGL_PLATFORM=surfaceless` で 4.6 Compat)。
  保険: 不調時は llvmpipe (`LIBGL_ALWAYS_SOFTWARE=1`、検証で起動確認済) → 最終手段 2.3.3 + Xvfb。
- **VAAPI エンコード不安定**なら encoder を **x264 veryfast / 720p** ソフトに切替 (720p 既定の理由)。
- **容量**: `cores=3/cpulimit=3/cpuunits=50` で他 VM を侵食させない。host CPU steal / mem>30GiB /
  `intel_gpu_top` を監視。超過時は 1080p30 へ。
- **media cache**: rootfs と別の mount (`provision-lxc.sh` の `MEDIA_STORE`、既定 `local-lvm`)。同じ
  仮想化ホストに同居する他ゲスト (制御プレーンの etcd 等) が使うディスクを避けるため、別ディスクの
  ストレージを指定する。LRU は prefetch agent。
- **exposure_policy (#27) の YTミラー**: 本線 + 公開ミラーの2本で UHD630 の RCS が既に ~90% (実測)。
  メンバーミラーまで含めた3本同時稼働は専用 GPU 増設が実質前提 (`docs/site-only-broadcast.md` §5-1)。
  `ICSTV_YT_MIRROR_CHANNELS` は既定空 (opt-in) で、GPU 容量実測が終わるまで対象チャンネルへ追加しないこと。

## ノード再構築時の注意 (このリポの手順だけでは完結しない)

- 再構築した CT はこのリポの手順だけでは**監視/バックアップの対象にならない**。監視エージェント
  (同梱の `zabbix/icstv-playout.conf` は zabbix-agent2 の UserParameter として install.sh が
  配置する) の登録・バックアップジョブの対象追加・共有 DB への接続許可リストへの登録は、導入者の配備基盤側で
  別途必要 (手順はこのリポジトリの範囲外)。
- `/etc/icstv/*.env` (`agent-<slug>.env` / `encoder-<slug>.env` 等の実値) は**このリポに無い**。
  失うと復元手段はノードのバックアップからのリストアのみ。
