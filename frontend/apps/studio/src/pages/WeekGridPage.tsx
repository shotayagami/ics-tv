// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { ChannelPicker, EmptyState, Notice, StudioPage } from "../atoms";

import { RouterLink } from "../links";

import type { ProgramFormOut, SeriesOut, WeekOut, WeekProgram, WeekSlotOccurrence } from "../hooks";
import { postForm, postJson } from "../hooks";

const DOW_JA = ["月", "火", "水", "木", "金", "土", "日"];
const DAY_MIN = 1440;
const SNAP_MIN = 5; // 移動/尺/新規ともに 5 分スナップ (週は密度が低いので粗め)
const HEADER_H = 24; // 曜日ヘッダ行の固定高さ (px) — 時間軸スペーサーと一致させる

type Layer = "base" | "diff";

type DragInfo =
  | { kind: "create"; layer: Layer; dow: number; startClientY: number }
  | { kind: "move-prog" | "resize-prog"; id: number; dow: number; startClientY: number; origStartMin: number; origDurMin: number }
  | { kind: "move-slot" | "resize-slot"; slotId: number; dow: number; startClientY: number; origStartMin: number; origDurMin: number };

type Ghost = { dow: number; topMin: number; heightMin: number; label: string };

type CreateDraft = { layer: Layer; dow: number; date: string; startMin: number; endMin: number };

function pad(n: number): string {
  return String(n).padStart(2, "0");
}
function fmtMin(min: number): string {
  const m = ((Math.round(min) % DAY_MIN) + DAY_MIN) % DAY_MIN;
  return `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;
}
function fmtMD(dateStr: string): string {
  const [, mo, d] = dateStr.split("-");
  return `${Number(mo)}/${Number(d)}`;
}
function snap(min: number): number {
  return Math.round(min / SNAP_MIN) * SNAP_MIN;
}
/** ローカル日付 (YYYY-MM-DD) + 分 → datetime-local 文字列 (YYYY-MM-DDTHH:MM:SS, ローカル時刻)。 */
function localStamp(dateStr: string, minutes: number): string {
  const base = new Date(`${dateStr}T00:00:00`);
  base.setMinutes(base.getMinutes() + Math.round(minutes));
  return `${base.getFullYear()}-${pad(base.getMonth() + 1)}-${pad(base.getDate())}T${pad(base.getHours())}:${pad(base.getMinutes())}:${pad(base.getSeconds())}`;
}
/** Program の開始ローカル分 (0-1439)。 */
function progStartMin(p: WeekProgram): number {
  const dt = new Date(p.start_at);
  return dt.getHours() * 60 + dt.getMinutes() + dt.getSeconds() / 60;
}
function progDurMin(p: WeekProgram): number {
  return (Date.parse(p.end_at) - Date.parse(p.start_at)) / 60000;
}
function slotStartMin(s: WeekSlotOccurrence): number {
  const [h, m] = s.start_time.split(":").map(Number);
  return h * 60 + m;
}

/** 週間グリッド編成 (曜日×タイムライン D&D)。下地=基本編成スロット(SeriesSlot, 全週に効く)、
 * 上=当週の実 Program(差分/変則)。空きセルドラッグ→モーダルで番組/素材/繰り返しを選んで作成。
 * 更新は既存 JSON エンドポイント (slots/* と programs/move|resize) を再利用。 */
export function WeekGridPage() {
  const { slug = "" } = useParams();
  const [d, setD] = useState<WeekOut | null>(null);
  const [pxPerMin, setPxPerMin] = useState(0.7);
  const [weekStart, setWeekStart] = useState(""); // YYYY-MM-DD 月曜 ("" = 今週)
  const [showBase, setShowBase] = useState(true);
  const [showDiff, setShowDiff] = useState(true);
  const [createLayer, setCreateLayer] = useState<Layer>("base");
  const [ghost, setGhost] = useState<Ghost | null>(null);
  const [modal, setModal] = useState<CreateDraft | null>(null);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  // モーダルの選択肢 (series + asset/live)。マウント時/ch 変更時に一度取得。
  const [series, setSeries] = useState<SeriesOut["series"]>([]);
  const [opts, setOpts] = useState<{ assets: ProgramFormOut["assets"]; live: ProgramFormOut["live_sources"] }>({ assets: [], live: [] });

  const dragRef = useRef<DragInfo | null>(null);
  const ghostRef = useRef<Ghost | null>(null);
  const colRefs = useRef<(HTMLDivElement | null)[]>([]);
  const pxRef = useRef(pxPerMin);
  pxRef.current = pxPerMin;
  const slugRef = useRef(slug);
  slugRef.current = slug;

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/scheduling/{slug}/week", { params: { path: { slug }, query: weekStart ? { start: weekStart } : {} } })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [slug, weekStart]);
  useEffect(load, [load]);

  // モーダル用の series + 素材/生ソース一覧 (作成時のみ使用)。
  useEffect(() => {
    if (!slug) return;
    api.GET("/api/v1/admin/scheduling/{slug}/series", { params: { path: { slug } } }).then(({ data }) => data && setSeries(data.series));
    api.GET("/api/v1/admin/scheduling/{slug}/program-form", { params: { path: { slug }, query: {} } }).then(({ data }) => data && setOpts({ assets: data.assets, live: data.live_sources }));
  }, [slug]);

  // colRefs は data-track (ヘッダ除くトラック域) を指す → ヘッダ分のずれが生じない。
  const gridTop = () => {
    const el = colRefs.current.find((c) => c != null);
    return el ? el.getBoundingClientRect().top : 0;
  };
  const minuteAt = (clientY: number) => Math.max(0, Math.min(DAY_MIN, (clientY - gridTop()) / pxRef.current));
  const dowAt = (clientX: number): number | null => {
    for (let i = 0; i < colRefs.current.length; i++) {
      const el = colRefs.current[i];
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (clientX >= r.left && clientX < r.right) return i;
    }
    return null;
  };

  function startCreate(e: React.PointerEvent, dow: number) {
    if (e.target !== e.currentTarget) return; // ブロックの上では作成しない
    if (createLayer === "base" ? !showBase : !showDiff) return;
    e.preventDefault();
    dragRef.current = { kind: "create", layer: createLayer, dow, startClientY: e.clientY };
    const m = snap(minuteAt(e.clientY));
    ghostRef.current = { dow, topMin: m, heightMin: 0, label: "" };
    setGhost(ghostRef.current);
  }
  function startProgDrag(e: React.PointerEvent, p: WeekProgram, kind: "move-prog" | "resize-prog") {
    e.preventDefault();
    e.stopPropagation();
    dragRef.current = { kind, id: p.id, dow: p.dow, startClientY: e.clientY, origStartMin: progStartMin(p), origDurMin: progDurMin(p) };
    setGhost(null);
  }
  function startSlotDrag(e: React.PointerEvent, s: WeekSlotOccurrence, kind: "move-slot" | "resize-slot") {
    e.preventDefault();
    e.stopPropagation();
    dragRef.current = { kind, slotId: s.slot_id, dow: s.dow, startClientY: e.clientY, origStartMin: slotStartMin(s), origDurMin: s.duration_ms / 60000 };
    setGhost(null);
  }

  useEffect(() => {
    function onMove(e: PointerEvent) {
      const dr = dragRef.current;
      if (!dr) return;
      if (dr.kind === "create") {
        const a = snap(minuteAt(dr.startClientY));
        const b = snap(minuteAt(e.clientY));
        const topMin = Math.min(a, b);
        const heightMin = Math.abs(b - a);
        ghostRef.current = { dow: dr.dow, topMin, heightMin, label: `新規 ${fmtMin(topMin)}–${fmtMin(topMin + heightMin)}` };
      } else if (dr.kind === "resize-prog" || dr.kind === "resize-slot") {
        const deltaMin = (e.clientY - dr.startClientY) / pxRef.current;
        const heightMin = Math.max(SNAP_MIN, snap(dr.origDurMin + deltaMin));
        ghostRef.current = { dow: dr.dow, topMin: dr.origStartMin, heightMin, label: `${fmtMin(dr.origStartMin)}–${fmtMin(dr.origStartMin + heightMin)}` };
      } else {
        // move-prog / move-slot: 縦=時刻、横=曜日列。
        const deltaMin = (e.clientY - dr.startClientY) / pxRef.current;
        const startMin = Math.max(0, Math.min(DAY_MIN - dr.origDurMin, snap(dr.origStartMin + deltaMin)));
        const dow = dowAt(e.clientX) ?? dr.dow;
        ghostRef.current = { dow, topMin: startMin, heightMin: dr.origDurMin, label: `${DOW_JA[dow]} ${fmtMin(startMin)}` };
      }
      setGhost(ghostRef.current);
    }
    async function onUp() {
      const dr = dragRef.current;
      const g = ghostRef.current;
      dragRef.current = null;
      ghostRef.current = null;
      setGhost(null);
      if (!dr || !g || !d) return;
      const s = slugRef.current;
      setErr("");
      setMsg("");
      if (dr.kind === "create") {
        if (g.heightMin < SNAP_MIN) return; // ほぼクリック
        setModal({ layer: dr.layer, dow: g.dow, date: d.days[g.dow], startMin: g.topMin, endMin: g.topMin + g.heightMin });
        return;
      }
      if (dr.kind === "move-prog") {
        if (g.dow === dr.dow && g.topMin === dr.origStartMin) return;
        const startAt = new Date(localStamp(d.days[g.dow], g.topMin)).toISOString();
        const { status, data } = await postJson(`/scheduling/ch/${s}/programs/${dr.id}/move/`, { start_at: startAt });
        if (status === 200 && data?.ok) { setMsg("番組を移動しました"); load(); }
        else setErr(data?.earliest ? `重複: 最早 ${fmtMin(new Date(data.earliest).getHours() * 60 + new Date(data.earliest).getMinutes())} まで空き無し` : data?.error || "移動できません");
      } else if (dr.kind === "resize-prog") {
        if (g.heightMin === dr.origDurMin) return;
        const endAt = new Date(localStamp(d.days[dr.dow], dr.origStartMin + g.heightMin)).toISOString();
        const { status, data } = await postJson(`/scheduling/ch/${s}/programs/${dr.id}/resize/`, { end_at: endAt });
        if (status === 200 && data?.ok) { setMsg("尺を変更しました"); load(); }
        else setErr(data?.error || "リサイズできません (録画の尺は固定)");
      } else if (dr.kind === "move-slot") {
        if (g.dow === dr.dow && g.topMin === dr.origStartMin) return;
        const { status, data } = await postJson(`/scheduling/ch/${s}/slots/${dr.slotId}/move/`, { dow: g.dow, start_time: fmtMin(g.topMin) });
        if (status === 200 && data?.ok) { setMsg("スロットを移動しました (全週に反映)"); load(); }
        else setErr(data?.error || "スロットを移動できません");
      } else if (dr.kind === "resize-slot") {
        if (g.heightMin === dr.origDurMin) return;
        const { status, data } = await postJson(`/scheduling/ch/${s}/slots/${dr.slotId}/resize/`, { duration_ms: Math.round(g.heightMin) * 60000 });
        if (status === 200 && data?.ok) { setMsg("スロットの尺を変更しました (全週に反映)"); load(); }
        else setErr(data?.error || "尺を変更できません");
      }
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [d, load]);

  async function expand() {
    setErr("");
    setMsg("");
    const { status } = await postForm(`/scheduling/ch/${slug}/series/expand/`);
    if (status === 200 || status === 204) { setMsg("4週先まで展開しました"); load(); }
    else setErr("展開に失敗しました");
  }

  function shiftWeek(deltaDays: number) {
    if (!d) return;
    const base = new Date(`${d.week_start}T00:00:00`);
    base.setDate(base.getDate() + deltaDays);
    setWeekStart(`${base.getFullYear()}-${pad(base.getMonth() + 1)}-${pad(base.getDate())}`);
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  const hours = Array.from({ length: 25 }, (_, h) => h);
  const topPx = (min: number) => min * pxPerMin;
  const colHeight = DAY_MIN * pxPerMin;
  const progByDow = (i: number) => d.programs.filter((p) => p.dow === i);
  const slotByDow = (i: number) => d.slots.filter((s) => s.dow === i);

  return (
    <StudioPage
      title="週間編成 (グリッド)"
      channel={
        <ChannelPicker
          items={d.channels.map((c) => ({ slug: c.slug, name: c.name, href: `/week/${c.slug}` }))}
          activeSlug={slug}
          linkComponent={RouterLink}
        />
      }
      actions={
        <>
          <button className="btn" type="button" onClick={() => setPxPerMin((v) => Math.max(0.4, v - 0.3))} style={{ padding: ".05rem .5rem" }}>−</button>
          <span className="muted" style={{ fontSize: ".8rem", minWidth: "2.5rem", textAlign: "center" }}>{(pxPerMin / 0.7).toFixed(1)}x</span>
          <button className="btn" type="button" onClick={() => setPxPerMin((v) => Math.min(4, v + 0.3))} style={{ padding: ".05rem .5rem" }}>＋</button>
          <button className="btn" type="button" onClick={expand} title="基本編成スロットを4週先まで実番組へ展開" style={{ marginLeft: ".4rem" }}>展開 (4週)</button>
          <Link to={`/series/${slug}`} className="btn secondary" style={{ marginLeft: ".4rem" }}>シリーズ管理</Link>
        </>
      }
    >
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      <div style={{ display: "flex", gap: ".8rem", alignItems: "center", flexWrap: "wrap", marginBottom: ".5rem" }}>
        <div style={{ display: "flex", gap: ".3rem", alignItems: "center" }}>
          <button className="btn" type="button" onClick={() => shiftWeek(-7)} style={{ padding: ".05rem .5rem" }}>◀ 前週</button>
          <button className="btn" type="button" onClick={() => setWeekStart("")} style={{ padding: ".05rem .5rem" }}>今週</button>
          <button className="btn" type="button" onClick={() => shiftWeek(7)} style={{ padding: ".05rem .5rem" }}>翌週 ▶</button>
          <input type="date" value={d.week_start} onChange={(e) => setWeekStart(e.target.value)} style={{ marginLeft: ".3rem" }} />
        </div>
        <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", fontSize: ".82rem" }}>
          <input type="checkbox" checked={showBase} onChange={(e) => setShowBase(e.target.checked)} /> 基本編成 (下地)
        </label>
        <label style={{ display: "inline-flex", gap: ".3rem", alignItems: "center", fontSize: ".82rem" }}>
          <input type="checkbox" checked={showDiff} onChange={(e) => setShowDiff(e.target.checked)} /> 当週の番組
        </label>
        <div style={{ display: "inline-flex", gap: ".2rem", alignItems: "center", fontSize: ".82rem", marginLeft: "auto" }}>
          <span className="muted">作成:</span>
          {(["base", "diff"] as Layer[]).map((l) => (
            <button key={l} type="button" className="btn" onClick={() => setCreateLayer(l)} style={{ padding: ".05rem .55rem", background: createLayer === l ? "var(--accent, #3a5a82)" : "#333" }}>
              {l === "base" ? "基本編成" : "当週"}
            </button>
          ))}
        </div>
      </div>

      {/* 外側ラッパーを scroll コンテナにして overflow-x/y 両方 auto → sticky が確実に効く。
          maxHeight でビューポート内に収め、ヘッダ/時間軸スペーサーを top:0 でピン留め。 */}
      <div style={{ overflowX: "auto", overflowY: "auto", maxHeight: "calc(100dvh - 200px)" }}>
        <div style={{ display: "flex", gap: 0, alignItems: "flex-start" }}>
        {/* 時間軸: スペーサーを sticky にしてヘッダと高さを揃え左端を固定 */}
        <div style={{ flex: "none", width: 44, position: "sticky", left: 0, zIndex: 12, background: "#111" }}>
          <div style={{ height: HEADER_H, position: "sticky", top: 0, background: "#111", zIndex: 13 }} />
          <div style={{ position: "relative", height: colHeight }}>
            {hours.map((h) => (
              <div key={h} style={{ position: "absolute", top: topPx(h * 60), right: 4, fontSize: ".68rem", color: "#888", transform: "translateY(-50%)" }}>{pad(h)}:00</div>
            ))}
          </div>
        </div>
        {/* 7 曜日列 */}
        <div style={{ display: "flex", flex: 1, minWidth: 7 * 110 }}>
          {d.days.map((date, i) => (
            <div key={date} style={{ flex: 1, minWidth: 110, borderLeft: "1px solid #2a2a2a" }}>
              {/* ヘッダを sticky: top:0 でスクロール中も上端固定 */}
              <div style={{ height: HEADER_H, boxSizing: "border-box", display: "flex", alignItems: "center", justifyContent: "center", fontSize: ".78rem", color: i >= 5 ? "#d99" : "#bcd", borderBottom: "1px solid #2a2a2a", position: "sticky", top: 0, zIndex: 10, background: "#1c1c1c" }}>
                {DOW_JA[i]} <span className="muted" style={{ marginLeft: 3 }}>{fmtMD(date)}</span>
              </div>
              <div
                ref={(el) => { colRefs.current[i] = el; }}
                data-track=""
                onPointerDown={(e) => startCreate(e, i)}
                style={{ position: "relative", height: colHeight, background: "#141414", touchAction: "none", cursor: "crosshair" }}
              >
                {hours.map((h) => (
                  <div key={h} style={{ position: "absolute", left: 0, right: 0, top: topPx(h * 60), borderTop: "1px solid #222", pointerEvents: "none" }} />
                ))}

                {/* 下地: 基本編成スロット投影 */}
                {showBase && slotByDow(i).map((sl) => {
                  const top = topPx(slotStartMin(sl));
                  const height = Math.max((sl.duration_ms / 60000) * pxPerMin, 12);
                  if (sl.covered) {
                    // 既に実番組が占有 → ごく薄く下地のみ (二重表示防止)
                    return <div key={`s${sl.slot_id}-${sl.date}`} title={`${sl.series_title} (展開済)`} style={{ position: "absolute", left: 2, right: 2, top, height, borderRadius: 4, border: "1px dashed #555", background: "rgba(217,177,90,.06)", pointerEvents: "none" }} />;
                  }
                  return (
                    <div
                      key={`s${sl.slot_id}-${sl.date}`}
                      onPointerDown={(e) => startSlotDrag(e, sl, "move-slot")}
                      title={`基本編成: ${sl.series_title} ${sl.recurrence_label} ${sl.start_time}`}
                      style={{ position: "absolute", left: 2, right: 2, top, height, boxSizing: "border-box", borderRadius: 4, padding: "1px 4px", overflow: "hidden", cursor: "grab", color: "#f0d9a0", background: "rgba(217,177,90,.14)", border: "1px dashed #d9b15a", touchAction: "none", zIndex: 1 }}
                    >
                      <div style={{ fontSize: ".66rem", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        <span style={{ background: "rgba(217,177,90,.3)", borderRadius: 2, padding: "0 2px", marginRight: 2 }}>基</span>{sl.series_title}
                      </div>
                      <div onPointerDown={(e) => startSlotDrag(e, sl, "resize-slot")} style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: 6, cursor: "ns-resize" }} />
                    </div>
                  );
                })}

                {/* 上: 当週の実 Program */}
                {showDiff && progByDow(i).map((p) => {
                  const sMin = progStartMin(p);
                  const top = topPx(sMin);
                  const height = Math.max(Math.min(progDurMin(p), DAY_MIN - sMin) * pxPerMin, 12);
                  const isLive = p.type === "live";
                  return (
                    <div
                      key={p.id}
                      onPointerDown={(e) => startProgDrag(e, p, "move-prog")}
                      title={`${fmtMin(sMin)} ${p.title}`}
                      style={{ position: "absolute", left: 3, right: 3, top, height, boxSizing: "border-box", borderRadius: 4, padding: "1px 4px", overflow: "hidden", cursor: "grab", color: "#eee", background: isLive ? "rgba(90,50,40,.92)" : "rgba(40,60,90,.92)", border: `1px solid ${isLive ? "#a05a3a" : "#3a5a82"}`, touchAction: "none", zIndex: 2 }}
                    >
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 2, fontSize: ".68rem" }}>
                        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{isLive ? "◆" : "◇"}{p.title}</span>
                        <Link to={`/program-form/${slug}?program_id=${p.id}`} onPointerDown={(e) => e.stopPropagation()} style={{ color: "#bcd", fontSize: ".64rem", flex: "none" }}>編集</Link>
                      </div>
                      {!p.public_visible && height >= 24 && <div className="muted" style={{ fontSize: ".62rem" }}>非公開</div>}
                      {isLive && <div onPointerDown={(e) => startProgDrag(e, p, "resize-prog")} style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: 6, cursor: "ns-resize", background: "repeating-linear-gradient(90deg,#a05a3a,#a05a3a 4px,transparent 4px,transparent 8px)" }} />}
                    </div>
                  );
                })}

                {/* ghost プレビュー (この列のみ) */}
                {ghost && ghost.dow === i && ghost.heightMin >= 1 && (
                  <div style={{ position: "absolute", left: 3, right: 3, top: topPx(ghost.topMin), height: Math.max(ghost.heightMin * pxPerMin, 4), borderRadius: 4, pointerEvents: "none", zIndex: 5, background: "repeating-linear-gradient(45deg,rgba(127,255,180,.18),rgba(127,255,180,.18) 6px,transparent 6px,transparent 12px)", border: "1px dashed #7fffb4" }}>
                    <span style={{ fontSize: ".64rem", color: "#7fffb4", padding: "0 3px" }}>{ghost.label}</span>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
        </div>
      </div>

      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".6rem" }}>
        下地(琥珀/破線)=基本編成スロット (全週に効く・ドラッグで移動/尺変更は全週へ反映)。濃いブロック=当週の実番組。
        空きセルをドラッグ→「作成: {createLayer === "base" ? "基本編成" : "当週"}」のモーダルで確定。基本編成の変更は「展開」で実番組に反映。
      </p>

      {modal && (
        <WeekCreateModal
          slug={slug}
          draft={modal}
          series={series}
          opts={opts}
          onClose={() => setModal(null)}
          onDone={(m) => { setModal(null); setMsg(m); load(); }}
          onError={(e) => setErr(e)}
        />
      )}
    </StudioPage>
  );
}

const RECURRENCE_OPTS = [
  { v: "weekly", label: "毎週(曜日)" },
  { v: "monthly_nth_dow", label: "第N曜" },
  { v: "days_of_month", label: "毎月の指定日" },
  { v: "days_ending", label: "末尾が指定の日" },
  { v: "daily", label: "毎日" },
];

function WeekCreateModal({
  slug, draft, series: initSeries, opts, onClose, onDone, onError,
}: {
  slug: string;
  draft: CreateDraft;
  series: Array<{ id: number; title: string }>;
  opts: { assets: ProgramFormOut["assets"]; live: ProgramFormOut["live_sources"] };
  onClose: () => void;
  onDone: (msg: string) => void;
  onError: (err: string) => void;
}) {
  const isBase = draft.layer === "base";
  const durMin = Math.max(1, Math.round(draft.endMin - draft.startMin));
  // モーダル内でシリーズをインライン作成できるようローカルコピーを保持。
  const [localSeries, setLocalSeries] = useState(initSeries);
  const [seriesId, setSeriesId] = useState<string>(initSeries[0] ? String(initSeries[0].id) : "");
  const [showNewSeries, setShowNewSeries] = useState(initSeries.length === 0);
  const [newSeriesTitle, setNewSeriesTitle] = useState("");
  const [creatingNew, setCreatingNew] = useState(false);
  const [title, setTitle] = useState("");
  const [ptype, setPtype] = useState<"recorded" | "live">("recorded");
  const [assetId, setAssetId] = useState("");
  const [liveId, setLiveId] = useState("");
  const [duration, setDuration] = useState(durMin);
  const [kind, setKind] = useState("weekly");
  const [weeksCsv, setWeeksCsv] = useState("");
  const [daysCsv, setDaysCsv] = useState("");
  const [endingCsv, setEndingCsv] = useState("");
  const [effFrom, setEffFrom] = useState(draft.date);
  const [effTo, setEffTo] = useState("");
  const [busy, setBusy] = useState(false);

  const selectedSeries = localSeries.find((s) => String(s.id) === seriesId);

  async function createSeries() {
    const t = newSeriesTitle.trim();
    if (!t) return;
    setCreatingNew(true);
    const { data, error } = await api.POST("/api/v1/admin/scheduling/{slug}/series", {
      params: { path: { slug } },
      body: { title: t },
    });
    setCreatingNew(false);
    if (error || !data) { onError("シリーズの作成に失敗しました"); return; }
    setLocalSeries((prev) => [...prev, data]);
    setSeriesId(String(data.id));
    setShowNewSeries(false);
    setNewSeriesTitle("");
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    if (isBase) {
      if (!seriesId) { onError("番組(シリーズ)を選択してください"); setBusy(false); return; }
      const { status, data } = await postJson(`/scheduling/ch/${slug}/slots/create/`, {
        series_id: Number(seriesId), dow: draft.dow, start_time: fmtMin(draft.startMin), duration_min: duration,
        program_type: ptype, default_asset_id: ptype === "recorded" ? Number(assetId) || null : null,
        live_source_id: ptype === "live" ? Number(liveId) || null : null,
        recurrence_kind: kind, weeks_csv: weeksCsv, days_csv: daysCsv, ending_csv: endingCsv,
        effective_from: effFrom, effective_to: effTo,
      });
      setBusy(false);
      if (status === 200 && data?.ok) onDone("基本編成スロットを作成しました (全週に反映)");
      else onError(data?.error || "スロットを作成できません");
    } else {
      const start_at = localStamp(draft.date, draft.startMin);
      const end_at = localStamp(draft.date, draft.startMin + duration);
      const { error } = await api.POST("/api/v1/admin/scheduling/{slug}/programs", {
        params: { path: { slug } },
        body: {
          title: title || (selectedSeries?.title ?? "新規番組"), type: ptype, genre: "", exposure_policy: "", start_at, end_at,
          asset_id: ptype === "recorded" && assetId ? Number(assetId) : null,
          live_source_id: ptype === "live" && liveId ? Number(liveId) : null,
          cast: "", description: "", public_visible: true, vod_visibility: "off",
          record_live: false,
        },
      });
      setBusy(false);
      if (error) onError((error as { detail?: string })?.detail || "番組を作成できません (時間重複の可能性)");
      else onDone("当週の番組を作成しました");
    }
  }

  return (
    <div onPointerDown={onClose} style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.55)", display: "flex", alignItems: "center", justifyContent: "center", zIndex: 100 }}>
      <form onSubmit={submit} onPointerDown={(e) => e.stopPropagation()} className="card" style={{ width: "min(30rem, 92vw)", maxHeight: "88vh", overflowY: "auto", background: "#1c1c1c" }}>
        <h2 style={{ margin: "0 0 .5rem", fontSize: "1rem" }}>{isBase ? "基本編成スロットを作成" : "当週の番組を作成"}</h2>
        <p className="muted" style={{ fontSize: ".76rem" }}>{DOW_JA[draft.dow]} {fmtMD(draft.date)} {fmtMin(draft.startMin)}–{fmtMin(draft.endMin)}</p>
        {isBase && <Notice variant="error">基本編成スロットは全週に適用されます（この週だけではありません）。</Notice>}

        {isBase ? (
          <div className="field">
            <span>番組 (シリーズ)</span>
            <div style={{ display: "flex", gap: ".3rem", alignItems: "center" }}>
              <select value={seriesId} onChange={(e) => setSeriesId(e.target.value)} required disabled={showNewSeries} style={{ flex: 1 }}>
                {localSeries.length === 0 && <option value="">— シリーズがありません —</option>}
                {localSeries.map((s) => <option key={s.id} value={s.id}>{s.title}</option>)}
              </select>
              <button type="button" className="btn" onClick={() => setShowNewSeries((v) => !v)} style={{ padding: ".1rem .5rem", fontSize: ".8rem", flex: "none" }}>
                {showNewSeries ? "キャンセル" : "＋ 新規"}
              </button>
            </div>
            {showNewSeries && (
              <div style={{ display: "flex", gap: ".3rem", marginTop: ".4rem" }}>
                <input
                  value={newSeriesTitle}
                  onChange={(e) => setNewSeriesTitle(e.target.value)}
                  placeholder="シリーズタイトル"
                  // eslint-disable-next-line jsx-a11y/no-autofocus
                  autoFocus
                  onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void createSeries(); } }}
                  style={{ flex: 1 }}
                />
                <button type="button" className="btn" onClick={() => void createSeries()} disabled={creatingNew || !newSeriesTitle.trim()} style={{ flex: "none" }}>
                  {creatingNew ? "作成中…" : "作成して選択"}
                </button>
              </div>
            )}
          </div>
        ) : (
          <label className="field" style={{ display: "block" }}><span>タイトル</span>
            <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="番組タイトル" style={{ width: "100%" }} />
          </label>
        )}

        <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
          <label className="field"><span>種別</span>
            <select value={ptype} onChange={(e) => setPtype(e.target.value as "recorded" | "live")}><option value="recorded">◇ 録画</option><option value="live">◆ 生</option></select>
          </label>
          <label className="field"><span>尺(分)</span>
            <input type="number" min={1} value={duration} onChange={(e) => setDuration(Number(e.target.value))} style={{ width: "5rem" }} />
          </label>
        </div>

        {ptype === "recorded" ? (
          <label className="field" style={{ display: "block" }}><span>{isBase ? "既定素材 (再放送/汎用)" : "素材"}</span>
            <select value={assetId} onChange={(e) => setAssetId(e.target.value)} required={isBase} style={{ width: "100%" }}>
              <option value="">— 素材を選択 —</option>
              {opts.assets.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </select>
          </label>
        ) : (
          <label className="field" style={{ display: "block" }}><span>ライブソース</span>
            <select value={liveId} onChange={(e) => setLiveId(e.target.value)} required style={{ width: "100%" }}>
              <option value="">— ソースを選択 —</option>
              {opts.live.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </label>
        )}

        {isBase && (
          <>
            <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap", alignItems: "flex-end" }}>
              <label className="field"><span>繰り返し</span>
                <select value={kind} onChange={(e) => setKind(e.target.value)}>{RECURRENCE_OPTS.map((o) => <option key={o.v} value={o.v}>{o.label}</option>)}</select>
              </label>
              {kind === "monthly_nth_dow" && <label className="field"><span>第N (例 2,4)</span><input value={weeksCsv} onChange={(e) => setWeeksCsv(e.target.value)} style={{ width: "6rem" }} /></label>}
              {kind === "days_of_month" && <label className="field"><span>日 (例 1,15)</span><input value={daysCsv} onChange={(e) => setDaysCsv(e.target.value)} style={{ width: "6rem" }} /></label>}
              {kind === "days_ending" && <label className="field"><span>末尾 (例 5)</span><input value={endingCsv} onChange={(e) => setEndingCsv(e.target.value)} style={{ width: "6rem" }} /></label>}
            </div>
            <div style={{ display: "flex", gap: ".8rem", flexWrap: "wrap" }}>
              <label className="field"><span>適用開始</span><input type="date" value={effFrom} onChange={(e) => setEffFrom(e.target.value)} required /></label>
              <label className="field"><span>適用終了 (任意)</span><input type="date" value={effTo} onChange={(e) => setEffTo(e.target.value)} /></label>
            </div>
          </>
        )}

        <div style={{ marginTop: ".8rem", display: "flex", gap: ".5rem", justifyContent: "flex-end" }}>
          <button className="btn secondary" type="button" onClick={onClose} disabled={busy}>キャンセル</button>
          <button className="btn" type="submit" disabled={busy}>{isBase ? "スロットを作成" : "番組を作成"}</button>
        </div>
      </form>
    </div>
  );
}
