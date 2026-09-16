// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { ChannelPicker, EmptyState, StatusBadge, type StatusTone } from "../atoms";

import { RouterLink } from "../links";
import {
  postForm,
  postMultipart,
  type LiveSourceRow,
  type LiveSourcesOut,
  type OpsStatusOut,
  type YtSlotRow,
} from "../hooks";

const POLL_MS = 5000;

// 配信枠 (YT 配信状態) のステータス→トーン (studio SlotsPage と揃える)。
const SLOT_TONE: Record<string, StatusTone> = {
  live: "ok",
  ready: "ok",
  testing: "warn",
  error: "danger",
  complete: "neutral",
};

// 配信枠の現状態から実行できる遷移 (testing/live/complete) と表示ラベル (P1.3 go-live)。
function slotTargets(status: string): { target: string; label: string; warn?: boolean }[] {
  switch (status) {
    case "ready":
      return [
        { target: "testing", label: "テスト" },
        { target: "live", label: "配信開始", warn: true },
      ];
    case "testing":
      return [
        { target: "live", label: "配信開始", warn: true },
        { target: "complete", label: "完了" },
      ];
    case "live":
      return [{ target: "complete", label: "完了" }];
    default:
      return [];
  }
}

/** クリップボードへコピー (コピー後 1.2s だけ ✓ 表示)。生入力 URL を現場へ渡す用。 */
function CopyBtn({ value }: { value: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      className="ops-btn ops-btn--sm ops-copy"
      onClick={() =>
        navigator.clipboard?.writeText(value).then(
          () => {
            setDone(true);
            window.setTimeout(() => setDone(false), 1200);
          },
          () => undefined,
        )
      }
    >
      {done ? "✓" : "コピー"}
    </button>
  );
}

/** 生入力 1 ソース: SRT (推奨) と RTMP (Server/key 分割) を OBS に貼れる形で読み取り表示。 */
function LiveSourceItem({ s }: { s: LiveSourceRow }) {
  // OBS の RTMP は Server と Stream key に分かれる (studio LiveSourcesPage と同じ剥がし方)。
  const cut = s.rtmp_url.lastIndexOf("/");
  return (
    <div className="ops-src">
      <div className="ops-src-name">{s.name}</div>
      <div className="ops-src-row">
        <span className="ops-src-k">SRT</span>
        <code>{s.srt_url}</code>
        <CopyBtn value={s.srt_url} />
      </div>
      <div className="ops-src-row">
        <span className="ops-src-k">RTMP</span>
        <code>{s.rtmp_url.slice(0, cut)}</code>
        <CopyBtn value={s.rtmp_url.slice(0, cut)} />
      </div>
      <div className="ops-src-row">
        <span className="ops-src-k">key</span>
        <code>{s.rtmp_url.slice(cut + 1)}</code>
        <CopyBtn value={s.rtmp_url.slice(cut + 1)} />
      </div>
    </div>
  );
}

/** フル CG op パネル (P1.3): フリーグラフィック (画像+文字+位置) を任意 overlay レイヤへ
 * 即時 show/clear。studio 旧 ops の「フリーグラフィック」を放送コンソールへ移設。画像アップロード
 * は postMultipart (op_overlay kind=graphic)。レイヤは安全な overlay 帯のみ select で選ばせ、
 * 本線(10)/スレート(90) の誤爆を防ぐ (40 速報は専用カードで扱うため除外)。 */
function CgOpPanel({ slug, onMsg, onErr }: { slug: string; onMsg: (m: string) => void; onErr: (e: string) => void }) {
  const [layer, setLayer] = useState("45");
  const [text, setText] = useState("");
  const [x, setX] = useState("5");
  const [y, setY] = useState("78");
  const [w, setW] = useState("25");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  async function send(op: "show" | "clear") {
    if (busy) return;
    const file = fileRef.current?.files?.[0];
    if (op === "show" && !file && !text.trim()) {
      onErr("画像か文字のどちらかを指定してください");
      return;
    }
    if (!window.confirm(op === "show" ? `L${layer} にグラフィックを送出しますか？` : `L${layer} のグラフィックを消去しますか？`)) return;
    setBusy(true);
    onErr("");
    const fd = new FormData();
    fd.set("op", op);
    fd.set("layer", layer);
    fd.set("kind", "graphic");
    if (op === "show") {
      if (text.trim()) fd.set("text", text.trim());
      if (file) fd.set("image", file);
      fd.set("x", x);
      fd.set("y", y);
      fd.set("w", w);
    }
    const { status, text: body } = await postMultipart(`/ops/ch/${slug}/overlay/`, fd);
    setBusy(false);
    if (status === 200 || status === 204) {
      onMsg(op === "show" ? "グラフィックを送出しました" : "グラフィックを消去しました");
      if (op === "show") {
        setText("");
        if (fileRef.current) fileRef.current.value = "";
      }
    } else onErr(body || "操作に失敗しました");
  }

  return (
    <section className="ops-card">
      <h2>フル CG (フリーグラフィック)</h2>
      <div className="ops-field">
        <select value={layer} onChange={(e) => setLayer(e.target.value)}>
          <option value="30">L30 Lバー</option>
          <option value="35">L35 予告</option>
          <option value="45">L45 フリー</option>
          <option value="50">L50 提供</option>
        </select>
      </div>
      <div className="ops-field">
        <input type="text" value={text} onChange={(e) => setText(e.target.value)} placeholder="文字 (任意)" maxLength={120} />
      </div>
      <div className="ops-field">
        <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp,image/gif" />
      </div>
      <div className="ops-cg-pos">
        <label>
          x% <input type="number" value={x} onChange={(e) => setX(e.target.value)} />
        </label>
        <label>
          y% <input type="number" value={y} onChange={(e) => setY(e.target.value)} />
        </label>
        <label>
          w% <input type="number" value={w} onChange={(e) => setW(e.target.value)} />
        </label>
      </div>
      <div className="ops-actions">
        <button type="button" className="ops-btn ops-btn--warn" disabled={busy} onClick={() => send("show")}>
          出す
        </button>
        <button type="button" className="ops-btn" disabled={busy} onClick={() => send("clear")}>
          消す
        </button>
      </div>
    </section>
  );
}

/** 🔴 放送コンソール (リファクタ Phase 1.2)。外出先スマホからの即応面。送出健全性 / on-air /
 * 通知 / as-run を 5s ポーリングで監視 (P1.1) しつつ、主要な送出操作 (緊急SLATE/SLATE解除/本線
 * リロード/CM割込・復帰/自動復帰/手動速報/通知 ack) を実行する (P1.2)。監視は GET status を集約、
 * 操作は studio.* と共有する既存 /ops/ch/<slug>/... へ form-POST (core.urls.OPS_OPERATIONS)。
 * モバイルファースト: 健全性を先頭に縦 1 カラム、操作は親指で押せる大きなボタン。 */
export function OpsConsole() {
  const { slug = "" } = useParams();
  const [d, setD] = useState<OpsStatusOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [bundleId, setBundleId] = useState("");
  const [telop, setTelop] = useState("");
  const [chime, setChime] = useState("none");
  const [slots, setSlots] = useState<YtSlotRow[]>([]);
  const [sources, setSources] = useState<LiveSourcesOut | null>(null);
  const busyRef = useRef(false);

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/ops/{slug}/status", { params: { path: { slug } } })
      .then(({ data, error }) =>
        error ? setErr("読み込み失敗 (staff 権限が必要)") : data && (setD(data), setErr("")),
      )
      .catch(() => setErr("通信に失敗しました"));
    // 配信枠 (YT 配信状態) は best-effort の監視 (失敗しても本体は動く)。
    api
      .GET("/api/v1/admin/youtube/{slug}/slots", { params: { path: { slug } } })
      .then(({ data }) => data && setSlots(data.slots))
      .catch(() => {});
  }, [slug]);

  useEffect(() => {
    load();
    const id = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  // 生入力 ingest URL は滅多に変わらないので 5s ポーリングには載せず、ch 切替時に 1 度だけ取得。
  useEffect(() => {
    api
      .GET("/api/v1/admin/live-sources")
      .then(({ data }) => data && setSources(data))
      .catch(() => {});
  }, [slug]);

  // 送出操作: studio OpsPage と同型。/ops/ch/<slug>/<path> へ form-POST → 成功で再ポーリング。
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

  async function sendTelop(kind: "show" | "clear") {
    const text = telop.trim();
    if (kind === "show" && !text) return;
    await op(
      "overlay/",
      kind === "show" ? "速報を送出しました" : "速報を消去しました",
      { layer: "40", op: kind, kind: "text", text, ...(kind === "show" ? { chime } : {}) },
      kind === "show" ? `速報を送出しますか？\n「${text}」` : undefined,
    );
    if (kind === "show") setTelop("");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;
  const h = d.health;

  return (
    <div className="ops">
      <header className="ops-top">
        <span className="ops-brand">
          <span className="ops-dot" /> 放送
        </span>
        <ChannelPicker
          items={d.channels.map((c) => ({ slug: c.slug, name: c.name, href: `/${c.slug}` }))}
          activeSlug={slug}
          linkComponent={RouterLink}
        />
        <span className="ops-poll">5秒更新</span>
      </header>

      {msg && <div className="ops-toast">{msg}</div>}
      {err && <div className="ops-toast ops-toast--err">{err}</div>}

      <main className="ops-main">
        {/* 送出健全性 (最重要・先頭) */}
        <section className="ops-card">
          <h2>
            送出健全性{" "}
            <StatusBadge label={h.online ? "ONLINE" : "OFFLINE"} tone={h.online ? "ok" : "danger"} />
          </h2>
          <dl className="ops-kv">
            <div>
              <dt>最終 heartbeat</dt>
              <dd>{h.last_heartbeat || "—"}</dd>
            </div>
            <div>
              <dt>CasparCG / feed</dt>
              <dd>
                {h.caspar_health || "—"} / {h.feed_state || "—"}
              </dd>
            </div>
            <div>
              <dt>SLATE</dt>
              <dd>{h.slate_active ? <span className="ops-warn">● 表示中</span> : "—"}</dd>
            </div>
            <div>
              <dt>queue / seq</dt>
              <dd>
                {h.queue_depth ?? "—"} / {h.last_seq ?? "—"}
              </dd>
            </div>
            <div>
              <dt>YT slot</dt>
              <dd>{h.yt_slot_status || "—"}</dd>
            </div>
          </dl>
        </section>

        {/* 配信枠 (YT 配信状態・稼働監視。読み取り) */}
        <section className="ops-card">
          <h2>配信枠 (YouTube)</h2>
          {slots.filter((s) => s.status !== "complete").length === 0 ? (
            <p className="ops-muted">稼働中の配信枠はありません。</p>
          ) : (
            <ul className="ops-list">
              {slots
                .filter((s) => s.status !== "complete")
                .map((s) => (
                  <li key={s.id}>
                    <StatusBadge label={s.status} tone={SLOT_TONE[s.status] ?? "neutral"} />{" "}
                    <span className="ops-time">{s.window}</span> {s.title || "—"}
                    {s.error && <span className="ops-sev"> · {s.error}</span>}
                    {s.broadcast_id && slotTargets(s.status).length > 0 && (
                      <div className="ops-actions ops-slot-ops">
                        {slotTargets(s.status).map((t) => (
                          <button
                            key={t.target}
                            type="button"
                            className={`ops-btn ops-btn--sm${t.warn ? " ops-btn--warn" : ""}`}
                            onClick={() =>
                              op(
                                `slot/${s.id}/transition/`,
                                `配信枠を ${t.label} にしました`,
                                { target: t.target },
                                `配信枠「${s.title || s.window}」を ${t.label} (${t.target}) にしますか？`,
                              )
                            }
                          >
                            {t.label}
                          </button>
                        ))}
                      </div>
                    )}
                  </li>
                ))}
            </ul>
          )}
        </section>

        {/* 送出操作 (親指リーチの大ボタン) */}
        <section className="ops-card">
          <h2>送出操作</h2>
          <div className="ops-actions">
            <button
              type="button"
              className="ops-btn ops-btn--warn"
              onClick={() => op("slate/", "緊急SLATE を発火しました", undefined, `${slug} に緊急SLATE を発火しますか？`)}
            >
              緊急SLATE
            </button>
            <button type="button" className="ops-btn" onClick={() => op("clear-slate/", "SLATE を解除しました")}>
              SLATE解除
            </button>
            <button
              type="button"
              className="ops-btn"
              onClick={() => op("reload-main/", "本線をリロードしました", undefined, "本線をリロードしますか？")}
            >
              本線リロード
            </button>
            <button
              type="button"
              className="ops-btn"
              onClick={() => op("cm-return/", "本線へ復帰しました")}
            >
              CM復帰
            </button>
          </div>
          <div className="ops-cm">
            <select value={bundleId} onChange={(e) => setBundleId(e.target.value)} disabled={!d.bundles.length}>
              <option value="">CMバンドル…</option>
              {d.bundles.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                </option>
              ))}
            </select>
            <button
              type="button"
              className="ops-btn"
              disabled={!bundleId}
              onClick={() => op("cm-in/", "CM を割り込みました", { bundle_id: bundleId })}
            >
              CM割込
            </button>
          </div>
          <button
            type="button"
            className={`ops-btn ops-btn--toggle${h.auto_return ? " is-on" : ""}`}
            onClick={() => op("auto-return/", "自動復帰を切替えました", { auto_return: h.auto_return ? "off" : "on" })}
          >
            自動復帰: {h.auto_return ? "ON" : "OFF"}
            {h.auto_return_suspended ? " (一時停止)" : ""}
          </button>
        </section>

        {/* 手動速報 (layer40・外出先即応の要・決定⑥) */}
        <section className="ops-card">
          <h2>手動速報</h2>
          <div className="ops-field">
            <input
              type="text"
              value={telop}
              onChange={(e) => setTelop(e.target.value)}
              placeholder="速報テロップ本文"
              maxLength={120}
            />
          </div>
          <div className="ops-field">
            <label className="ops-muted" htmlFor="ops-chime">
              チャイム
            </label>
            <select id="ops-chime" value={chime} onChange={(e) => setChime(e.target.value)}>
              <option value="none">なし</option>
              <optgroup label="カテゴリ既定">
                {d.chime_choices.filter((c) => c.group === "category").map((c) => (
                  <option key={c.value} value={c.value}>{c.label.replace("カテゴリ既定: ", "")}</option>
                ))}
              </optgroup>
              {d.chime_choices.some((c) => c.group === "library") && (
                <optgroup label="ライブラリ">
                  {d.chime_choices.filter((c) => c.group === "library").map((c) => (
                    <option key={c.value} value={c.value}>{c.label.replace("ライブラリ: ", "")}</option>
                  ))}
                </optgroup>
              )}
            </select>
          </div>
          <div className="ops-actions">
            <button type="button" className="ops-btn ops-btn--warn" disabled={!telop.trim()} onClick={() => sendTelop("show")}>
              速報を送出
            </button>
            <button type="button" className="ops-btn" onClick={() => sendTelop("clear")}>
              速報を消去
            </button>
          </div>
        </section>

        {/* フル CG op パネル (P1.3・グラフィック overlay の手動 show/clear) */}
        <CgOpPanel slug={slug} onMsg={setMsg} onErr={setErr} />

        {/* on air */}
        <section className="ops-card">
          <h2>On Air</h2>
          {d.on_air ? (
            <p className="ops-onair">
              <strong>{d.on_air.program || d.on_air.action || "—"}</strong>
              <span className="ops-muted">
                {" "}
                · {d.on_air.status} · {d.on_air.time}
              </span>
            </p>
          ) : (
            <p className="ops-muted">on-air イベントなし</p>
          )}
          {d.live_program && <p className="ops-live">◆ 生: {d.live_program}</p>}
          {d.live_program_id != null && (
            <div className="ops-actions ops-timing">
              <button
                type="button"
                className="ops-btn"
                onClick={() =>
                  op(
                    `program/${d.live_program_id}/extend/`,
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
                    `program/${d.live_program_id}/shorten/`,
                    "巻き (-5分) しました",
                    { delta_ms: "300000" },
                    "生番組を −5分 巻きますか？",
                  )
                }
              >
                巻き −5分
              </button>
            </div>
          )}
          {d.upcoming.length > 0 && (
            <ul className="ops-list">
              {d.upcoming.map((e, i) => (
                <li key={i}>
                  <span className="ops-time">{e.time}</span> {e.title || e.action}{" "}
                  <span className="ops-muted">{e.status}</span>
                </li>
              ))}
            </ul>
          )}
        </section>

        {/* 通知 (ack 付き) */}
        <section className="ops-card">
          <h2>
            通知{" "}
            {d.notifications_count > 0 ? (
              <span className="ops-badge">{d.notifications_count}</span>
            ) : (
              <span className="ops-muted">0</span>
            )}
            {d.notifications_count > 0 && (
              <button type="button" className="ops-btn ops-btn--sm" onClick={() => op("notifications/ack-all/", "全通知を確認しました")}>
                全確認
              </button>
            )}
          </h2>
          <ul className="ops-list">
            {d.notifications.map((n) => (
              <li key={n.id} className={n.severity === "error" || n.severity === "critical" ? "ops-sev" : ""}>
                <span className="ops-time">{n.created}</span> {n.severity} {n.kind} — {n.message}
                <button
                  type="button"
                  className="ops-btn ops-btn--sm ops-ack"
                  onClick={() => op(`notifications/${n.id}/ack/`, "確認しました")}
                >
                  確認
                </button>
              </li>
            ))}
            {d.notifications.length === 0 && <li className="ops-muted">未確認の通知はありません。</li>}
          </ul>
        </section>

        {/* as-run */}
        <section className="ops-card">
          <h2>
            As-Run <span className="ops-muted">直近</span>
          </h2>
          <ul className="ops-list">
            {d.asrun.map((e, i) => (
              <li key={i}>
                <span className="ops-time">{e.time}</span> {e.action} <span className="ops-muted">{e.status}</span>{" "}
                {e.title}
              </li>
            ))}
            {d.asrun.length === 0 && <li className="ops-muted">ログなし</li>}
          </ul>
        </section>

        {/* 生入力 ingest URL (読み取り・現場 OBS へ渡す。P1.3) */}
        {sources && (
          <section className="ops-card">
            <h2>生入力 (OBS ingest)</h2>
            {!sources.host_configured && (
              <p className="ops-warn">送出ノード host 未設定 (ICSTV_INGEST_NODE_HOST)。URL は雛形のままです。</p>
            )}
            {sources.sources.length === 0 ? (
              <p className="ops-muted">生入力ソースがありません。</p>
            ) : (
              sources.sources.map((s) => <LiveSourceItem key={s.id} s={s} />)
            )}
          </section>
        )}

        <p className="ops-foot">
          編成編集 (timeline 等) は PC の studio へ。本コンソールは放送当直の即応操作に絞っています。
        </p>
      </main>
    </div>
  );
}
