// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { BigCountdown, ChannelPicker, EmptyState, StatusBadge, remainingTone } from "../atoms";

import { RouterLink } from "../links";
import { postForm, type TimekeeperOut } from "../hooks";

const TIMEKEEPER_POLL_MS = 2500;

// now/next の kind → 色トーン (line=本線/cm=CM/filler=フィラー/slate=SLATE/none=無し)。
// ops.css の --warn は実際には赤系エイリアスなので使わず、Bootstrap 5 の意味色変数を直接使う。
const KIND_DOT_CLASS: Record<string, string> = {
  line: "ops-tk-dot--line",
  cm: "ops-tk-dot--cm",
  filler: "ops-tk-dot--filler",
  slate: "ops-tk-dot--slate",
  none: "ops-tk-dot--none",
  vt: "ops-tk-dot--vt",
};

function KindDot({ kind }: { kind: string }) {
  return <span className={`ops-tk-dot ${KIND_DOT_CLASS[kind] ?? "ops-tk-dot--none"}`} />;
}

function pad2(n: number): string {
  return n < 10 ? "0" + n : String(n);
}

// 秒 → m:ss / h:mm:ss。予定尺/押し巻き/残尺を studio 進行表エディタと同じ mm:ss 精度で出す
// (Phase 3 B: 分丸めだと押し 1:30 が「2分」に見え、30 秒が効くタイムキーパーで過大表示になる)。
function fmtClock(totalSec: number): string {
  const s = Math.max(0, Math.round(totalSec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0 ? `${h}:${pad2(m)}:${pad2(sec)}` : `${m}:${pad2(sec)}`;
}

/** 🔴 タイムキーパー (docs/timekeeper-live.md 決定#25・Phase 0)。既存データだけを集約した
 * 読み取り専用の壁時計 + 放送残り時間 + 今/次 + (録画番組のみ) CM残数画面。現場スマホ1台での
 * 進行監視を想定。CM入り/本線復帰/押え/巻きは OpsConsole.tsx と同じ既存操作 (/ops/ch/<slug>/...)
 * をそのまま呼ぶ (新規書き込みエンドポイントは無し)。Phase 1 (LiveRundown/LiveCue 進行表モデル) は
 * 別途、この画面のモジュール境界を拡張する形で実装する。 */
export function Timekeeper() {
  const { slug = "" } = useParams();
  const [tk, setTk] = useState<TimekeeperOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [bundleId, setBundleId] = useState("");
  const [wallClock, setWallClock] = useState(() => new Date());
  // サーバ時計とのズレ (秒。サーバ進み=正)。ポール毎に server_now から更新し、BigCountdown の
  // targetSec を補正することで現場端末の時計ズレによるカウントダウン誤差を防ぐ。
  const [serverOffset, setServerOffset] = useState(0);
  const busyRef = useRef(false);

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/ops/{slug}/timekeeper", { params: { path: { slug } } })
      .then(({ data, error }) => {
        if (error) {
          setErr("読み込み失敗 (staff 権限が必要)");
          return;
        }
        if (data) {
          setTk(data);
          setErr("");
          setServerOffset(data.server_now - Date.now() / 1000);
        }
      })
      .catch(() => setErr("通信に失敗しました"));
  }, [slug]);

  useEffect(() => {
    load();
    const id = window.setInterval(load, TIMEKEEPER_POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  // WS 受信: 他オペレータ端末の送出操作/自動発火を即座に反映 (#25 Phase2 D7)。ペイロードは
  // 軽量な invalidate 信号 ({type:"playout.update"}) のみなので受信したら load() を即時呼ぶだけ。
  // Comments.tsx (frontend/apps/player) の WS 接続と同型 (proto検出/3秒後再接続/unmount時close)。
  // 上の 2.5 秒ポーリングは fallback として残す (WS 再接続中の欠落に強い、D7 により置き換えない)。
  useEffect(() => {
    if (!slug || !window.WebSocket) return;
    let ws: WebSocket | null = null;
    let closed = false;
    let timer = 0;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const connect = () => {
      try {
        ws = new WebSocket(`${proto}//${location.host}/ws/playout/${slug}/`);
      } catch {
        return;
      }
      ws.onmessage = (ev) => {
        let d: { type?: string };
        try {
          d = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (d.type === "playout.update") load();
      };
      ws.onclose = () => {
        if (!closed) timer = window.setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      ws?.close();
    };
  }, [slug, load]);

  // 壁時計は 1 秒毎のローカル tick (useNowTick 系の BigCountdown とは独立: 表示は Date そのもの)。
  useEffect(() => {
    const id = window.setInterval(() => setWallClock(new Date()), 1000);
    return () => window.clearInterval(id);
  }, []);

  // 送出操作: OpsConsole.tsx の op() と同型 (意図的にコピー。Phase 0 はゼロリスクを優先し
  // OpsConsole.tsx には触れない — 計画書 §6 の推奨どおり)。
  async function op(path: string, okMsg: string, fields?: Record<string, string>, confirmMsg?: string) {
    if (busyRef.current) return;
    if (confirmMsg && !window.confirm(confirmMsg)) return;
    busyRef.current = true;
    setMsg("");
    setErr("");
    const { status, text } = await postForm(`/ops/ch/${slug}/${path}`, fields);
    busyRef.current = false;
    if (status === 200 || status === 204) {
      setMsg(okMsg);
      load();
    } else {
      // 409 (押え吸収不能 等) は本文に理由 → そのまま表示。
      setErr(text || "操作に失敗しました");
    }
  }

  if (err && !tk) return <EmptyState loading>{err}</EmptyState>;
  if (!tk) return <EmptyState loading>読み込み中…</EmptyState>;

  const b = tk.broadcast;
  const clock = `${pad2(wallClock.getHours())}:${pad2(wallClock.getMinutes())}:${pad2(wallClock.getSeconds())}`;
  // BigCountdown は端末のローカル epoch (useNowTick) で残り時間を計算するため、渡す targetSec を
  // サーバとのズレ分だけ補正する。トーン計算 (何色にするか) も同じ補正済み「今」を使う。
  const adj = (t: number | null | undefined): number | null => (t == null ? null : t - serverOffset);
  const trueNow = () => Date.now() / 1000 + serverOffset;

  // CM/VT 送出中は全画面カウントダウンへ切替 (docs/timekeeper-live.md §7・Phase 3 A)。現場は
  // 「あと何秒で本線へ戻すか」だけを見たいので、通常カード群を伏せて「◯明けまで」大カウントダウン +
  // 「今すぐ本線復帰」大ボタンに集約する。footer の操作列は常設のまま残す。
  const cmMode = tk.now.kind === "cm" || tk.now.kind === "vt";

  return (
    <div className="ops">
      <header className="ops-top">
        <span className="ops-brand">
          <span className="ops-dot" /> タイムキープ
        </span>
        <ChannelPicker
          items={tk.channels.map((c) => ({ slug: c.slug, name: c.name, href: `/${c.slug}/timekeeper` }))}
          activeSlug={slug}
          linkComponent={RouterLink}
        />
        <StatusBadge label={tk.health.online ? "ONLINE" : "OFFLINE"} tone={tk.health.online ? "ok" : "danger"} />
        <span className="ops-poll">2.5秒更新</span>
      </header>

      {msg && <div className="ops-toast">{msg}</div>}
      {err && <div className="ops-toast ops-toast--err">{err}</div>}

      {cmMode ? (
        /* CM/VT 中: 全画面カウントダウン (§7)。壁時計 + 「◯明けまで」 + 今すぐ本線復帰。 */
        <main className="ops-main ops-tk-cmmode">
          <section className="ops-card ops-tk-cmfull">
            <div className="ops-tk-clock">{clock}</div>
            <p className="ops-tk-cmlabel">
              <KindDot kind={tk.now.kind} /> {tk.now.label || (tk.now.kind === "cm" ? "CM" : "VT")}
            </p>
            <p className="ops-muted">{tk.now.kind === "cm" ? "CM明けまで" : "VT明けまで"}</p>
            {tk.now.segment_end_at != null ? (
              <BigCountdown
                targetSec={adj(tk.now.segment_end_at)}
                tone={remainingTone(tk.now.segment_end_at - trueNow())}
              />
            ) : (
              <div className="ops-tk-big ops-tk-big--neutral">--:--</div>
            )}
            <button type="button" className="ops-btn ops-btn--xl" onClick={() => op("cm-return/", "本線へ復帰しました")}>
              今すぐ本線復帰
            </button>
            {tk.cm_remaining.tracked && tk.cm_remaining.count > 0 && (
              <p className="ops-muted ops-tk-cmrem">
                この後 残 CM {tk.cm_remaining.count}本 / {fmtClock(tk.cm_remaining.seconds)}
              </p>
            )}
          </section>
        </main>
      ) : (
      <main className="ops-main">
        {/* ヒーロー: 壁時計 + 放送残り時間 (onair) / オンエアまで (pre) */}
        <section className="ops-card ops-tk-hero">
          <div className="ops-tk-clock">{clock}</div>
          {b.state === "onair" && (
            <>
              <p className="ops-muted">時間終了まで — {b.title}</p>
              <BigCountdown
                targetSec={adj(b.end_at)}
                tone={b.end_at != null ? remainingTone(b.end_at - trueNow()) : "ok"}
              />
            </>
          )}
          {b.state === "pre" && (
            <>
              <p className="ops-muted">オンエアまで</p>
              <BigCountdown targetSec={adj(b.next_on_air ?? b.next_program_at)} tone="ok" />
            </>
          )}
          {b.state === "post" &&
            (tk.now.kind !== "none" && tk.now.kind !== "slate" ? (
              // Program 行が無いだけで、フィラー等は継続送出中のことが多い (フィラーループは
              // 常時ギャップを埋める設計) — 「放送終了」と誤解させない表現にする。
              <p className="ops-muted ops-tk-post">編成番組の予定なし（送出は継続中）</p>
            ) : (
              <p className="ops-muted ops-tk-post">本日の放送は終了しました</p>
            ))}
        </section>

        {/* 今 / 次 */}
        <section className="ops-card">
          <h2>今 / 次</h2>
          <dl className="ops-kv">
            <div>
              <dt>
                <KindDot kind={tk.now.kind} /> 今
              </dt>
              <dd>{tk.now.label || "—"}</dd>
            </div>
            {tk.now.segment_end_at != null && (
              <div>
                <dt>{tk.now.kind === "cm" ? "CM明けまで" : "区切りまで"}</dt>
                <dd>
                  <BigCountdown
                    targetSec={adj(tk.now.segment_end_at)}
                    tone={remainingTone(tk.now.segment_end_at - trueNow())}
                  />
                </dd>
              </div>
            )}
            <div>
              <dt>
                <KindDot kind={tk.next.kind} /> 次
              </dt>
              <dd>{tk.next.label || "—"}</dd>
            </div>
          </dl>
        </section>

        {/* CM/VT 残数 (LiveRundown があるチャンネルのみ追跡・録画番組は cue-sheet 由来の実績集計)。 */}
        <section className="ops-card">
          <h2>CM 残数</h2>
          {tk.cm_remaining.tracked ? (
            <p className={tk.cm_remaining.count > 0 && tk.cm_remaining.seconds < 60 ? "ops-live" : ""}>
              残 CM: {tk.cm_remaining.count}本 / {fmtClock(tk.cm_remaining.seconds)}
            </p>
          ) : (
            <p className="ops-muted">CM 進行表未設定</p>
          )}
          {tk.vt_remaining.tracked && (
            <p>残 VT: {tk.vt_remaining.count}本 / {fmtClock(tk.vt_remaining.seconds)}</p>
          )}
          {tk.rundown && (
            <p className={tk.rundown.over_under_ms > 0 ? "ops-live" : ""}>
              予定尺 合計 {fmtClock(tk.rundown.planned_total_ms / 1000)}
              （{tk.rundown.over_under_ms >= 0 ? "押し" : "巻き"} {fmtClock(Math.abs(tk.rundown.over_under_ms) / 1000)}）
            </p>
          )}
        </section>

        {/* 次のCM/次のセクションまで (LiveRundown のある生番組のみ・どちらも無ければカード自体を出さない)。
            今/次カードと同じ dl.ops-kv + dt/dd + BigCountdown の並びをそのまま踏襲する。 */}
        {(tk.next_cm_at != null || tk.next_section_at != null) && (
          <section className="ops-card">
            <h2>次まで</h2>
            <dl className="ops-kv">
              {tk.next_cm_at != null && (
                <div>
                  <dt>次のCMまで</dt>
                  <dd>
                    <BigCountdown targetSec={adj(tk.next_cm_at)} tone={remainingTone(tk.next_cm_at - trueNow())} />
                  </dd>
                </div>
              )}
              {tk.next_section_at != null && (
                <div>
                  <dt>次のセクションまで</dt>
                  <dd>
                    <BigCountdown targetSec={adj(tk.next_section_at)} tone="ok" />
                  </dd>
                </div>
              )}
            </dl>
          </section>
        )}

        {/* 進行表 (LiveRundown/LiveCue・生番組のみ)。各 pending cue に種別別の送出ボタン
            (cm→CM入り/vt→VT送出/共通→スキップ) を出す。既存の OpsConsole.tsx の配信枠リスト
            (ul.ops-list + div.ops-actions + button.ops-btn--sm) と同じ「一覧+行内アクション」の
            慣習を踏襲する (このファイル自体には ul/li の前例が無いため、同じ ops app 内の
            既存パターンに合わせた)。 */}
        {tk.rundown && (
          <section className="ops-card">
            <h2>進行表</h2>
            <ul className="ops-rundown">
              {tk.rundown.cues.map((c) => (
                <li
                  key={c.id}
                  className={
                    c.state === "aired" ? "ops-rundown--aired" : c.state === "skipped" ? "ops-rundown--skipped" : ""
                  }
                >
                  <KindDot kind={c.kind === "cm" ? "cm" : c.kind === "vt" ? "vt" : "line"} />
                  {c.label || c.kind}
                  {c.auto_fire && <span className="ops-chip">自動</span>}
                  {c.state === "pending" && (
                    <div className="ops-actions">
                      {c.kind === "cm" && (
                        <button
                          type="button"
                          className="ops-btn ops-btn--sm"
                          onClick={() => op(`cue/${c.id}/cm-now/`, "CMを発火しました")}
                        >
                          CM入り
                        </button>
                      )}
                      {c.kind === "vt" && (
                        <button
                          type="button"
                          className="ops-btn ops-btn--sm"
                          onClick={() => op(`cue/${c.id}/roll-vt/`, "VTを送出しました", undefined, "VTを送出しますか？")}
                        >
                          VT送出
                        </button>
                      )}
                      <button
                        type="button"
                        className="ops-btn ops-btn--sm"
                        onClick={() =>
                          op(
                            `cue/${c.id}/skip/`,
                            "スキップしました",
                            undefined,
                            "この cue をスキップしますか？(元に戻せません)",
                          )
                        }
                      >
                        スキップ
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        <p className="ops-foot">現場スマホ1台での進行監視向け。詳細な送出操作は放送コンソールへ。</p>
      </main>
      )}

      {/* 送出操作 (footer 固定・OpsConsole.tsx と同じ既存エンドポイントを叩く) */}
      <div className="ops-tk-footer">
        <select value={bundleId} onChange={(e) => setBundleId(e.target.value)} disabled={!tk.bundles.length}>
          <option value="">CMバンドル…</option>
          {tk.bundles.map((bd) => (
            <option key={bd.id} value={bd.id}>
              {bd.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="ops-btn"
          disabled={!bundleId}
          onClick={() => op("cm-in/", "CM を割り込みました", { bundle_id: bundleId })}
        >
          CM入り
        </button>
        <button type="button" className="ops-btn" onClick={() => op("cm-return/", "本線へ復帰しました")}>
          本線復帰
        </button>
        {b.program_type === "live" && b.program_id != null && (
          <>
            <button
              type="button"
              className="ops-btn"
              onClick={() =>
                op(
                  `program/${b.program_id}/extend/`,
                  "押え (+5分) しました",
                  { delta_ms: "300000" },
                  "生番組を +5分 押えますか？",
                )
              }
            >
              押え +5分
            </button>
            <button
              type="button"
              className="ops-btn"
              onClick={() =>
                op(
                  `program/${b.program_id}/shorten/`,
                  "巻き (-5分) しました",
                  { delta_ms: "300000" },
                  "生番組を −5分 巻きますか？",
                )
              }
            >
              巻き −5分
            </button>
          </>
        )}
      </div>
    </div>
  );
}
