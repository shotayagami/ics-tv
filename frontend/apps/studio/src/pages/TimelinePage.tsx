// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { ChannelPicker, EmptyState, Notice, StudioPage } from "../atoms";

import { RouterLink } from "../links";

import type { TimelineOut, TimelineProgram } from "../hooks";
import { postJson } from "../hooks";

const SNAP_MIN = 1; // 移動/リサイズの分スナップ
const SNAP5_MS = 5 * 60000; // 新規作成は壁時計5分スナップ

type DragInfo =
  | { mode: "move" | "resize"; id: number; startClientY: number; origStartMs: number; origEndMs: number }
  | {
      mode: "break";
      programId: number;
      breakId: number;
      startClientY: number;
      origOffsetMs: number;
      assetDurationMs: number;
      blockHeightPx: number;
      blockTopPx: number;
      gridMs: number;
    }
  | { mode: "create"; trackTop: number; startClientY: number };

function pad(n: number): string {
  return String(n).padStart(2, "0");
}
function fmtTime(ms: number): string {
  const t = new Date(ms);
  return `${pad(t.getHours())}:${pad(t.getMinutes())}`;
}
/** datetime-local 値 (YYYY-MM-DDTHH:MM, ローカル時刻)。program_create の prefill 用。 */
function toLocalInput(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function snap5(ms: number): number {
  return Math.round(ms / SNAP5_MS) * SNAP5_MS;
}

/** 編成タイムライン (#Phase2d-3/4 本丸)。グリッド + ドラッグ移動/尺変更 +
 * 空き領域ドラッグで新規作成 (既存フォームへ prefill) + CM枠の追加/移動/削除/キューシート適用。
 * 更新は既存 JSON エンドポイント (move/resize/adbreak*) を再利用。 */
export function TimelinePage() {
  const { slug = "" } = useParams();
  const navigate = useNavigate();
  const [d, setD] = useState<TimelineOut | null>(null);
  const [pxPerMin, setPxPerMin] = useState(1.6);
  const [ghostDelta, setGhostDelta] = useState<number | null>(null); // 移動/尺/枠: 分 or px 差。create: 現在Y(track相対)
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");

  const dragRef = useRef<DragInfo | null>(null);
  const valRef = useRef(0);
  const pxRef = useRef(pxPerMin);
  pxRef.current = pxPerMin;
  const slugRef = useRef(slug);
  slugRef.current = slug;

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/scheduling/{slug}/timeline", { params: { path: { slug } } })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [slug]);
  useEffect(load, [load]);

  const originMs = d ? Date.parse(d.now) - 60 * 60000 : 0;
  const horizonMs = d ? Date.parse(d.horizon) : 0;
  const trackMin = d ? (horizonMs - originMs) / 60000 : 0;
  const topPx = (ms: number) => ((ms - originMs) / 60000) * pxPerMin;
  const durPx = (deltaMs: number) => (deltaMs / 60000) * pxPerMin;

  function applyTimes(id: number, start: string | null, end: string) {
    setD(
      (prev) =>
        prev && {
          ...prev,
          programs: prev.programs.map((p) =>
            p.id === id ? { ...p, start_at: start ?? p.start_at, end_at: end } : p,
          ),
        },
    );
  }

  function startBlockDrag(e: React.PointerEvent, p: TimelineProgram, mode: "move" | "resize") {
    e.preventDefault();
    e.stopPropagation();
    dragRef.current = { mode, id: p.id, startClientY: e.clientY, origStartMs: Date.parse(p.start_at), origEndMs: Date.parse(p.end_at) };
    valRef.current = 0;
    setGhostDelta(0);
  }

  function startBreakDrag(e: React.PointerEvent, p: TimelineProgram, breakId: number, origOffsetMs: number, gridMs: number) {
    e.preventDefault();
    e.stopPropagation();
    const sMs = Date.parse(p.start_at);
    const eMs = Date.parse(p.end_at);
    dragRef.current = {
      mode: "break",
      programId: p.id,
      breakId,
      startClientY: e.clientY,
      origOffsetMs,
      assetDurationMs: p.asset_duration_ms || 1,
      blockHeightPx: Math.max(durPx(eMs - sMs), 14),
      blockTopPx: topPx(sMs),
      gridMs,
    };
    valRef.current = 0;
    setGhostDelta(0);
  }

  function startCreate(e: React.PointerEvent) {
    if (e.target !== e.currentTarget) return; // ブロック/枠の上では作成しない
    const rect = e.currentTarget.getBoundingClientRect();
    dragRef.current = { mode: "create", trackTop: rect.top, startClientY: e.clientY };
    valRef.current = e.clientY - rect.top;
    setGhostDelta(e.clientY - rect.top);
  }

  useEffect(() => {
    if (ghostDelta === null) return;
    function onMove(e: PointerEvent) {
      const dr = dragRef.current;
      if (!dr) return;
      if (dr.mode === "create") {
        const cur = Math.max(0, e.clientY - dr.trackTop);
        valRef.current = cur;
        setGhostDelta(cur);
      } else {
        const raw = (e.clientY - dr.startClientY) / pxRef.current;
        const snapped = Math.round(raw / SNAP_MIN) * SNAP_MIN;
        valRef.current = snapped;
        setGhostDelta(snapped);
      }
    }
    async function onUp() {
      const dr = dragRef.current;
      const v = valRef.current;
      dragRef.current = null;
      setGhostDelta(null);
      if (!dr) return;
      const s = slugRef.current;
      setErr("");
      setMsg("");
      if (dr.mode === "move") {
        if (v === 0) return;
        const newStart = new Date(dr.origStartMs + v * 60000).toISOString();
        const { status, data } = await postJson(`/scheduling/ch/${s}/programs/${dr.id}/move/`, { start_at: newStart });
        if (status === 200 && data?.ok) {
          applyTimes(dr.id, data.start_at, data.end_at);
          setMsg("移動しました");
        } else setErr(data?.earliest ? `重複: 最早 ${fmtTime(Date.parse(data.earliest))} まで空き無し` : data?.error || "移動できません");
      } else if (dr.mode === "resize") {
        if (v === 0) return;
        const newEnd = new Date(dr.origEndMs + v * 60000).toISOString();
        const { status, data } = await postJson(`/scheduling/ch/${s}/programs/${dr.id}/resize/`, { end_at: newEnd });
        if (status === 200 && data?.ok) {
          applyTimes(dr.id, null, data.end_at);
          setMsg("尺を変更しました");
        } else setErr(data?.error || "リサイズできません");
      } else if (dr.mode === "break") {
        // v は分換算 → px に戻し、ブロック高に対する比率で素材尺内の offset へ。grid スナップ。
        const px = v * pxRef.current;
        let off = dr.origOffsetMs + (px / dr.blockHeightPx) * dr.assetDurationMs;
        off = Math.round(off / dr.gridMs) * dr.gridMs;
        off = Math.max(0, Math.min(dr.assetDurationMs - dr.gridMs, off));
        if (off === dr.origOffsetMs) return;
        const { status, data } = await postJson(`/scheduling/ch/${s}/adbreaks/${dr.breakId}/move/`, { offset_ms: off });
        if (status === 200 && data?.ok) {
          load();
          setMsg("CM枠を移動しました");
        } else setErr(data?.error || "CM枠を移動できません");
      } else if (dr.mode === "create") {
        const top = Math.min(dr.startClientY - dr.trackTop, v);
        const bottom = Math.max(dr.startClientY - dr.trackTop, v);
        if (bottom - top < 6) return; // ほぼクリック
        const startMs = snap5(originMs + (top / pxRef.current) * 60000);
        const endMs = snap5(originMs + (bottom / pxRef.current) * 60000);
        if (endMs <= startMs) return;
        navigate(`/program-form/${s}?start=${toLocalInput(startMs)}&end=${toLocalInput(endMs)}`);
      }
    }
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ghostDelta === null]);

  async function addBreak(p: TimelineProgram) {
    if (!p.asset_duration_ms) return;
    setErr("");
    setMsg("");
    const { status, data } = await postJson(`/scheduling/ch/${slug}/programs/${p.id}/adbreaks/add/`, {
      offset_ms: Math.floor(p.asset_duration_ms / 3),
      grid: "15s",
      duration_ms: 30000,
    });
    if (status === 200 && data?.ok) {
      load();
      setMsg("CM枠を追加しました");
    } else setErr(data?.error || "CM枠を追加できません");
  }

  async function delBreak(breakId: number) {
    setErr("");
    setMsg("");
    const { status } = await postJson(`/scheduling/ch/${slug}/adbreaks/${breakId}/delete/`, {});
    if (status === 204 || status === 200) {
      load();
      setMsg("CM枠を削除しました");
    } else setErr("CM枠を削除できません");
  }

  async function applyCuesheet(p: TimelineProgram) {
    setErr("");
    setMsg("");
    const { status, data } = await postJson(`/scheduling/ch/${slug}/programs/${p.id}/adbreaks/apply-cuesheet/`, {});
    if (status === 200) {
      load();
      setMsg("キューシートを適用しました");
    } else setErr(data?.error || "キューシートを適用できません");
  }

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  const hourLines: { ms: number; label: string }[] = [];
  const firstHour = Math.ceil(originMs / 3600000) * 3600000;
  for (let t = firstHour; t <= horizonMs; t += 3600000) hourLines.push({ ms: t, label: fmtTime(t) });
  const dragId = dragRef.current && "id" in dragRef.current ? dragRef.current.id : null;

  return (
    <StudioPage
      title="編成 タイムライン"
      channel={
        <ChannelPicker
          items={d.channels.map((c) => ({ slug: c.slug, name: c.name, href: `/scheduling/${c.slug}` }))}
          activeSlug={slug}
          linkComponent={RouterLink}
        />
      }
      actions={
        <>
          <button className="btn" type="button" onClick={() => setPxPerMin((v) => Math.max(0.8, v - 0.8))} style={{ padding: ".05rem .5rem" }}>−</button>
          <span className="muted" style={{ fontSize: ".8rem", minWidth: "3rem", textAlign: "center" }}>{(pxPerMin / 1.6).toFixed(1)}x</span>
          <button className="btn" type="button" onClick={() => setPxPerMin((v) => Math.min(8, v + 0.8))} style={{ padding: ".05rem .5rem" }}>＋</button>
        </>
      }
    >
      {(msg || err) && <Notice variant={err ? "error" : "success"}>{err || msg}</Notice>}

      <div style={{ display: "flex", gap: "8px", alignItems: "flex-start" }}>
        <div style={{ position: "relative", width: 56, height: trackMin * pxPerMin, flex: "none" }}>
          {hourLines.map((h) => (
            <div key={h.ms} style={{ position: "absolute", top: topPx(h.ms), right: 4, fontSize: ".72rem", color: "#888" }}>{h.label}</div>
          ))}
        </div>
        <div data-track="" onPointerDown={startCreate} style={{ position: "relative", flex: 1, height: trackMin * pxPerMin, borderLeft: "1px solid #2a2a2a", background: "#141414", touchAction: "none", cursor: "crosshair" }}>
          {hourLines.map((h) => (
            <div key={h.ms} style={{ position: "absolute", left: 0, right: 0, top: topPx(h.ms), borderTop: "1px solid #222", pointerEvents: "none" }} />
          ))}
          <div style={{ position: "absolute", left: 0, right: 0, top: topPx(Date.parse(d.now)), borderTop: "2px solid var(--live)", pointerEvents: "none", zIndex: 3 }} />

          {d.programs.map((p) => {
            const sMs = Date.parse(p.start_at);
            const eMs = Date.parse(p.end_at);
            const top = topPx(sMs);
            const height = Math.max(durPx(eMs - sMs), 14);
            const isLive = p.type === "live";
            const recorded = p.type === "recorded" && !!p.asset_duration_ms;
            return (
              <div
                key={p.id}
                onPointerDown={(e) => startBlockDrag(e, p, "move")}
                title={`${fmtTime(sMs)}–${fmtTime(eMs)} ${p.title}`}
                style={{ position: "absolute", left: 6, right: 10, top, height, boxSizing: "border-box", borderRadius: 5, padding: "3px 6px", overflow: "hidden", cursor: "grab", color: "#eee", background: isLive ? "rgba(90,50,40,.85)" : "rgba(40,60,90,.85)", border: `1px solid ${isLive ? "#a05a3a" : "#3a5a82"}`, opacity: dragId === p.id ? 0.5 : 1, touchAction: "none", zIndex: 2 }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", gap: 4, fontSize: ".74rem" }}>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{p.title}</span>
                  <span style={{ display: "flex", gap: 4, flex: "none" }} onPointerDown={(e) => e.stopPropagation()}>
                    {recorded && height >= 30 && (
                      <button type="button" onClick={() => addBreak(p)} title="CM枠を追加" style={{ background: "none", border: 0, color: "#e0c070", cursor: "pointer", fontSize: ".68rem", padding: 0 }}>CM+</button>
                    )}
                    {recorded && p.has_cuesheet && height >= 30 && (
                      <button type="button" onClick={() => applyCuesheet(p)} title="キューシートを適用" style={{ background: "none", border: 0, color: "#9bd", cursor: "pointer", fontSize: ".68rem", padding: 0 }}>Cue適用</button>
                    )}
                    <Link to={`/program-form/${slug}?program_id=${p.id}`} onPointerDown={(e) => e.stopPropagation()} style={{ color: "#bcd", fontSize: ".68rem" }}>編集</Link>
                  </span>
                </div>
                {height >= 38 && (
                  <div className="muted" style={{ fontSize: ".7rem" }}>
                    {(isLive ? "◆生 " : "◇録画 ") + (p.source || "")}
                    {!p.public_visible && " ·非公開"}
                  </div>
                )}
                {recorded
                  ? p.breaks.map((b) => {
                      const bTop = (b.offset_ms / p.asset_duration_ms!) * height;
                      return (
                        <div key={b.id} onPointerDown={(e) => startBreakDrag(e, p, b.id, b.offset_ms, b.grid === "15s" ? 15000 : 20000)} title={`CM枠 ${Math.round(b.offset_ms / 1000)}s / ${b.duration_ms / 1000}s`} style={{ position: "absolute", left: 0, right: 0, top: bTop, height: 6, background: "rgba(217,177,90,.55)", borderTop: "2px solid #d9b15a", cursor: "ns-resize", display: "flex", justifyContent: "flex-end", alignItems: "center", touchAction: "none", zIndex: 3 }}>
                          <button type="button" onPointerDown={(e) => e.stopPropagation()} onClick={() => delBreak(b.id)} title="CM枠を削除" style={{ background: "none", border: 0, color: "#1a1a1a", cursor: "pointer", fontSize: ".62rem", lineHeight: 1, padding: "0 3px" }}>×</button>
                        </div>
                      );
                    })
                  : null}
                {isLive && (
                  <div onPointerDown={(e) => startBlockDrag(e, p, "resize")} style={{ position: "absolute", left: 0, right: 0, bottom: 0, height: 7, cursor: "ns-resize", background: "repeating-linear-gradient(90deg,#a05a3a,#a05a3a 4px,transparent 4px,transparent 8px)" }} />
                )}
              </div>
            );
          })}

          {ghostDelta !== null && dragRef.current && (() => {
            const dr = dragRef.current;
            if (dr.mode === "create") {
              const top = Math.min(dr.startClientY - dr.trackTop, ghostDelta);
              const height = Math.abs(ghostDelta - (dr.startClientY - dr.trackTop));
              if (height < 4) return null;
              const startMs = snap5(originMs + (top / pxPerMin) * 60000);
              const endMs = snap5(originMs + ((top + height) / pxPerMin) * 60000);
              return (
                <div style={{ position: "absolute", left: 6, right: 10, top, height, borderRadius: 5, pointerEvents: "none", zIndex: 4, background: "repeating-linear-gradient(45deg,rgba(127,255,180,.18),rgba(127,255,180,.18) 6px,transparent 6px,transparent 12px)", border: "1px dashed #7fffb4" }}>
                  <span style={{ fontSize: ".68rem", color: "#7fffb4", padding: "0 4px" }}>新規 {fmtTime(startMs)}–{fmtTime(endMs)}</span>
                </div>
              );
            }
            if (dr.mode === "break") {
              const px = ghostDelta * pxPerMin;
              const gTop = dr.blockTopPx + Math.max(0, Math.min(dr.blockHeightPx - 2, (dr.origOffsetMs / dr.assetDurationMs) * dr.blockHeightPx + px));
              return <div style={{ position: "absolute", left: 6, right: 10, top: gTop, borderTop: "2px dashed #e0c070", pointerEvents: "none", zIndex: 5 }} />;
            }
            const gTop = dr.mode === "move" ? topPx(dr.origStartMs) + ghostDelta * pxPerMin : topPx(dr.origStartMs);
            const gHeight = dr.mode === "move" ? durPx(dr.origEndMs - dr.origStartMs) : Math.max(durPx(dr.origEndMs - dr.origStartMs) + ghostDelta * pxPerMin, 6);
            const ns = dr.mode === "move" ? dr.origStartMs + ghostDelta * 60000 : dr.origStartMs;
            const ne = dr.origEndMs + ghostDelta * 60000;
            return (
              <div style={{ position: "absolute", left: 6, right: 10, top: gTop, height: gHeight, borderRadius: 5, pointerEvents: "none", zIndex: 4, background: "repeating-linear-gradient(45deg,rgba(127,208,255,.18),rgba(127,208,255,.18) 6px,transparent 6px,transparent 12px)", border: "1px dashed #7fd0ff" }}>
                <span style={{ fontSize: ".68rem", color: "#7fd0ff", padding: "0 4px" }}>{fmtTime(ns)}–{fmtTime(ne)}</span>
              </div>
            );
          })()}
        </div>
      </div>
      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".6rem" }}>
        空き領域ドラッグ=新規番組(作成フォームへ) / ブロックドラッグ=移動 / live 下端=尺変更 / 録画は CM+ で枠追加・帯ドラッグで移動・× で削除。
      </p>
    </StudioPage>
  );
}
