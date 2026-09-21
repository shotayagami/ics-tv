# ICS-TV playout agent

クラウド送出ノード上で動く独立プロセス（[../docs/overview.md](../docs/overview.md) 意思決定
#8/#13/#14）。Django コントロールプレーンから gRPC で `PlayoutEvent` を受信し、ローカル
AMCP（CasparCG）を駆動。as-run 実績を返送する。

契約は [../proto/icstv/v1/playout.proto](../proto/icstv/v1/playout.proto) （言語非依存）。
将来ホットループのみ Go で部分置換する余地を残すため、本実装は薄く保ち、状態は SQLite に
寄せている。

## 構成

```
agent/
├── pyproject.toml
├── icstv_proto/              # buf generate 出力 (proto/icstv/v1/playout.proto から)
└── icstv_agent/
    ├── config.py             # 環境変数読み込み
    ├── server_client.py      # gRPC client (SubscribeEvents / ReportResult / Heartbeat)
    ├── queue_db.py           # SQLite ローカルキュー (数十時間分。WAN 断耐性)
    ├── caspar.py             # CasparCG AMCP クライアント (Phase 1 後半に実装)
    └── main.py               # asyncio エントリ (subscribe/dispatch/heartbeat の 3 ループ)
```

## 環境変数

| 変数 | 例 | 説明 |
|---|---|---|
| `ICSTV_SERVER_GRPC` | `server.icstv.local:50051` | コントロールプレーンの gRPC エンドポイント (WireGuard 越し) |
| `ICSTV_AGENT_TOKEN` | (シークレット) | gRPC metadata `authorization: Bearer <token>` |
| `ICSTV_CHANNEL_SLUG` | `ch1` | 担当チャンネルの slug |
| `ICSTV_QUEUE_DB` | `~/.cache/icstv-agent/queue.db` | SQLite キュー path |
| `ICSTV_CASPAR_HOST` | `127.0.0.1` | AMCP 接続先 |
| `ICSTV_CASPAR_PORT` | `5250` | AMCP ポート |
| `ICSTV_HEARTBEAT_SEC` | `30` | Heartbeat 周期 |

## gRPC の障害検出

agent は unary RPC（Heartbeat / ReportResult / ReportInterrupt /
RequestRecordingUpload）に 10 秒の deadline を設定する。長時間接続の SubscribeEvents には
有限 deadline を付けず、20 秒間隔・10 秒 timeout の HTTP/2 keepalive で half-open 接続を
検出する。送信できなかった実績と割り込みは SQLite outbox に残り、既存の再試行ループで再送する。

server 側はこの keepalive 間隔を許可する設定が必要である。server の既定値のまま agent だけを
先に更新すると GOAWAY の原因になりうるため、変更時の本番展開順序は次を守る。

1. server image をリリースし、production の `icstv-grpc` を更新する
2. rollout 完了後に agent を更新する

`agent/icstv_agent/**` の main push は `deploy-agent` workflow で送出ノードへ自動反映されるため、
server と agent の keepalive 設定を同じ変更に含める場合は、agent 自動反映より前に server が
更新済みとなるようリリースを分ける。

## 起動

```
cd agent
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
# 環境変数をセットして
python -m icstv_agent
```

## proto 再生成

リポジトリルートで:

```
buf generate
python3 tools/spdx_headers.py --fix
```

`agent/icstv_proto/icstv/v1/{playout_pb2.py,playout_pb2_grpc.py}` が更新される。buf の生成物には
SPDX ヘッダが無いので、続く `spdx_headers.py --fix` で付与する。
