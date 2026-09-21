// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** 時計スタイルプリセット管理ページ。
 * プリセットの CRUD + 静止プレビュー (実時刻を 1 秒ごとに描き直す。ClockEditorPage と共用)。 */

import { useEffect, useState } from "react";

import { Breadcrumb, EmptyState, Field, Notice, StudioPage } from "../atoms";
import { RouterLink } from "../links";
import { readCookie } from "../hooks";
import { ClockPreview, CLOCK_PREVIEW_DEFAULTS, type ClockPreviewProps } from "../ClockPreview";
import { CLOCK_FONT_GROUPS } from "../clockFonts";

// ---- 定数 ----

const TEXT_EFFECT_OPTIONS = [
  { value: "none",          label: "なし" },
  { value: "soft-shadow",   label: "ソフトシャドウ（既定）" },
  { value: "hard-outline",  label: "ハードアウトライン" },
  { value: "glow",          label: "グロー" },
] as const;

const BG_OPTIONS = [
  { value: "dark-gradient", label: "ダークグラデーション（既定）" },
  { value: "dark-solid",    label: "ダークソリッド" },
  { value: "dark-pill",     label: "ダークピル" },
  { value: "frosted",       label: "フロストガラス" },
  { value: "none",          label: "なし（透明）" },
] as const;

const SHADOW_OPTIONS = [
  { value: "none", label: "なし" },
  { value: "sm",   label: "小" },
  { value: "md",   label: "中（既定）" },
  { value: "lg",   label: "大" },
] as const;

const RADIUS_OPTIONS = [
  { value: "none", label: "なし" },
  { value: "sm",   label: "小" },
  { value: "md",   label: "中（既定）" },
  { value: "lg",   label: "大" },
  { value: "pill", label: "ピル（全丸）" },
] as const;

const ANIM_OPTIONS = [
  { value: "fade",       label: "フェード（既定）" },
  { value: "slide-down", label: "スライドダウン" },
  { value: "zoom",       label: "ズーム" },
  { value: "none",       label: "なし" },
] as const;

const CORNER_BUTTONS = [
  { value: "top-left",     label: "↖", row: 0, col: 0 },
  { value: "top-right",    label: "↗", row: 0, col: 1 },
  { value: "bottom-left",  label: "↙", row: 1, col: 0 },
  { value: "bottom-right", label: "↘", row: 1, col: 1 },
] as const;

const D = CLOCK_PREVIEW_DEFAULTS;
// entrance_anim はプレビューに含めない (静止画のため) が、フォームの既定値としては要る。
const DEFAULT_ENTRANCE_ANIM = "fade";

// ---- 型 ----

interface Preset {
  id: number;
  name: string;
  style: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

// ---- 色入力 ----

function ColorInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const hexOk = /^#[0-9A-Fa-f]{6}$/.test(value);
  return (
    <span style={{ display: "inline-flex", gap: ".3rem", alignItems: "center" }}>
      <input
        type="color"
        value={hexOk ? value : "#000000"}
        onChange={(e) => onChange(e.target.value)}
        style={{ width: 28, height: 24, padding: 0, border: "1px solid var(--line)", background: "none" }}
      />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="#rrggbb"
        style={{ width: "5.5rem" }}
      />
    </span>
  );
}

// ---- API helpers ----

async function apiFetch(path: string, opts: RequestInit = {}) {
  const res = await fetch(path, {
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": readCookie("csrftoken"),
      ...((opts.headers ?? {}) as Record<string, string>),
    },
    ...opts,
  });
  let data: unknown = null;
  try { data = await res.json(); } catch { /* ignore */ }
  return { status: res.status, data };
}

// ---- メインコンポーネント ----

export function ClockPresetsPage() {
  const [presets, setPresets] = useState<Preset[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [presetName, setPresetName] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);

  // スタイルステート
  const [fontFamily,    setFontFamily]   = useState(D.font_family);
  const [timeSize,      setTimeSize]     = useState(D.time_size);
  const [fontWeight,    setFontWeight]   = useState(D.font_weight);
  const [timeColor,     setTimeColor]    = useState(D.time_color);
  const [dateColor,     setDateColor]    = useState(D.date_color);
  const [textEffect,    setTextEffect]   = useState(D.text_effect);
  const [strokeWidth,   setStrokeWidth]  = useState(D.stroke_width);
  const [strokeColor,   setStrokeColor]  = useState(D.stroke_color);
  const [bgPreset,      setBgPreset]     = useState(D.bg_preset);
  const [bgOpacity,     setBgOpacity]    = useState(D.bg_opacity);
  const [boxShadow,     setBoxShadow]    = useState(D.box_shadow);
  const [borderRadius,  setBorderRadius] = useState(D.border_radius);
  const [entranceAnim,  setEntranceAnim] = useState(DEFAULT_ENTRANCE_ANIM);
  const [position,      setPosition]     = useState(D.position);
  const [showSeconds,   setShowSeconds]  = useState(D.show_seconds);
  const [showDate,      setShowDate]     = useState(D.show_date);

  // プレビューに渡す props (entrance_anim は静止プレビューでは使わない)
  const previewProps: ClockPreviewProps = {
    font_family: fontFamily,
    time_size: timeSize,
    font_weight: fontWeight,
    time_color: timeColor,
    date_color: dateColor,
    text_effect: textEffect,
    stroke_width: strokeWidth,
    stroke_color: strokeColor,
    bg_preset: bgPreset,
    bg_opacity: bgOpacity,
    box_shadow: boxShadow,
    border_radius: borderRadius,
    show_seconds: showSeconds,
    show_date: showDate,
    position,
  };

  function loadStyle(style: Record<string, unknown>) {
    const s = { ...D, ...style };
    setFontFamily(String(s.font_family));
    setTimeSize(Number(s.time_size));
    setFontWeight(Number(s.font_weight));
    setTimeColor(String(s.time_color));
    setDateColor(String(s.date_color));
    setTextEffect(String(s.text_effect));
    setStrokeWidth(String(s.stroke_width ?? "none"));
    setStrokeColor(String(s.stroke_color ?? "#000000"));
    setBgPreset(String(s.bg_preset));
    setBgOpacity(Number(s.bg_opacity));
    setBoxShadow(String(s.box_shadow));
    setBorderRadius(String(s.border_radius));
    setEntranceAnim(String(style.entrance_anim ?? DEFAULT_ENTRANCE_ANIM));
    setPosition(String(s.position));
    setShowSeconds(Boolean(s.show_seconds));
    setShowDate(Boolean(s.show_date));
  }

  async function fetchPresets() {
    const { data, status } = await apiFetch("/api/v1/admin/clock-presets", { method: "GET" });
    if (status === 200 && Array.isArray(data)) {
      setPresets(data as Preset[]);
    }
    setLoading(false);
  }
  useEffect(() => { fetchPresets(); }, []);

  function selectPreset(p: Preset) {
    setSelectedId(p.id);
    setPresetName(p.name);
    loadStyle(p.style);
    setErr(""); setMsg("");
  }

  function newPreset() {
    setSelectedId(null);
    setPresetName("");
    loadStyle({});
    setErr(""); setMsg("");
  }

  function currentStyle(): Record<string, unknown> {
    return {
      font_family: fontFamily, time_size: timeSize, font_weight: fontWeight,
      time_color: timeColor, date_color: dateColor, text_effect: textEffect,
      stroke_width: strokeWidth, stroke_color: strokeColor,
      bg_preset: bgPreset, bg_opacity: bgOpacity, box_shadow: boxShadow,
      border_radius: borderRadius, entrance_anim: entranceAnim,
      position, show_seconds: showSeconds, show_date: showDate,
    };
  }

  async function save() {
    if (busy) return;
    const name = presetName.trim();
    if (!name) { setErr("プリセット名を入力してください"); return; }
    setBusy(true); setErr(""); setMsg("");
    const body = JSON.stringify({ name, style: currentStyle() });
    const { status, data } = selectedId === null
      ? await apiFetch("/api/v1/admin/clock-presets", { method: "POST", body })
      : await apiFetch(`/api/v1/admin/clock-presets/${selectedId}`, { method: "PUT", body });
    setBusy(false);
    if (status === 200 || status === 201) {
      const saved = data as Preset;
      setMsg(`「${saved.name}」を保存しました`);
      setSelectedId(saved.id);
      await fetchPresets();
    } else {
      const detail = (data as { detail?: string })?.detail ?? "保存に失敗しました";
      setErr(detail);
    }
  }

  async function remove() {
    if (!selectedId || busy) return;
    if (!confirm(`「${presetName}」を削除しますか？`)) return;
    setBusy(true); setErr(""); setMsg("");
    const { status } = await apiFetch(`/api/v1/admin/clock-presets/${selectedId}`, { method: "DELETE" });
    setBusy(false);
    if (status === 200) {
      setMsg("削除しました");
      newPreset();
      await fetchPresets();
    } else {
      setErr("削除に失敗しました");
    }
  }

  return (
    <StudioPage
      title="時計スタイルプリセット"
      breadcrumb={
        <Breadcrumb
          items={[{ label: "設定", href: "/channels" }, { label: "時計プリセット" }]}
          linkComponent={RouterLink}
        />
      }
    >
      {(msg || err) && (
        <div style={{ marginBottom: "1rem" }}>
          <Notice variant={err ? "error" : "success"}>{msg || err}</Notice>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "200px 1fr auto", gap: "1.5rem", alignItems: "start" }}>

        {/* 左: プリセット一覧 */}
        <div>
          <button className="btn" onClick={newPreset} style={{ width: "100%", marginBottom: ".75rem" }}>
            + 新規プリセット
          </button>
          {loading ? (
            <EmptyState>読み込み中…</EmptyState>
          ) : presets.length === 0 ? (
            <p className="muted" style={{ fontSize: ".82rem" }}>プリセットがありません</p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "flex", flexDirection: "column", gap: ".3rem" }}>
              {presets.map((p) => (
                <li key={p.id}>
                  <button
                    type="button"
                    onClick={() => selectPreset(p)}
                    style={{
                      width: "100%", textAlign: "left", padding: ".45rem .7rem",
                      borderRadius: ".35rem", border: "none", cursor: "pointer",
                      background: selectedId === p.id ? "var(--accent)" : "transparent",
                      color: selectedId === p.id ? "#fff" : "var(--fg)",
                      fontSize: ".85rem",
                    }}
                  >
                    {p.name}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* 中: スタイルエディタ */}
        <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>

          {/* フォント */}
          <div className="card">
            <div className="card-head"><strong>フォント</strong></div>
            <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
              <Field label="フォントファミリ">
                <select value={fontFamily} onChange={(e) => setFontFamily(e.target.value)}>
                  {CLOCK_FONT_GROUPS.map((g) => (
                    <optgroup key={g.group} label={g.group}>
                      {g.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </optgroup>
                  ))}
                </select>
              </Field>
              <Field label="時刻サイズ (px)">
                <input type="number" min={36} max={80} value={timeSize}
                  onChange={(e) => setTimeSize(Number(e.target.value))}
                  style={{ width: "5rem" }} />
              </Field>
              <Field label="ウェイト">
                <select value={fontWeight} onChange={(e) => setFontWeight(Number(e.target.value))}>
                  <option value={400}>400 (Regular)</option>
                  <option value={700}>700 (Bold / 既定)</option>
                  <option value={900}>900 (Black)</option>
                </select>
              </Field>
            </div>
          </div>

          {/* 色 */}
          <div className="card">
            <div className="card-head"><strong>色</strong></div>
            <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
              <Field label="時刻の色">
                <ColorInput value={timeColor} onChange={setTimeColor} />
              </Field>
              <Field label="日付の色">
                <ColorInput value={dateColor} onChange={setDateColor} />
              </Field>
            </div>
          </div>

          {/* エフェクト・背景 */}
          <div className="card">
            <div className="card-head"><strong>エフェクト / 背景</strong></div>
            <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
              <Field label="テキストエフェクト">
                <select value={textEffect} onChange={(e) => setTextEffect(e.target.value)}>
                  {TEXT_EFFECT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="縁取り幅">
                <select value={strokeWidth} onChange={(e) => setStrokeWidth(e.target.value)}>
                  <option value="none">なし（既定）</option>
                  <option value="1">細 (1px)</option>
                  <option value="2">中 (2px)</option>
                  <option value="4">太 (4px)</option>
                </select>
              </Field>
              {strokeWidth !== "none" && (
                <Field label="縁取りの色">
                  <ColorInput value={strokeColor} onChange={setStrokeColor} />
                </Field>
              )}
              <Field label="背景">
                <select value={bgPreset} onChange={(e) => setBgPreset(e.target.value)}>
                  {BG_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="背景の不透明度">
                <input type="range" min={0} max={1} step={0.05} value={bgOpacity}
                  onChange={(e) => setBgOpacity(Number(e.target.value))}
                  style={{ width: "10rem" }} />
                <span className="muted" style={{ marginLeft: ".4rem" }}>{Math.round(bgOpacity * 100)}%</span>
              </Field>
              <Field label="シャドウ">
                <select value={boxShadow} onChange={(e) => setBoxShadow(e.target.value)}>
                  {SHADOW_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="角丸">
                <select value={borderRadius} onChange={(e) => setBorderRadius(e.target.value)}>
                  {RADIUS_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
            </div>
          </div>

          {/* アニメーション・位置 */}
          <div className="card">
            <div className="card-head"><strong>アニメーション / 位置</strong></div>
            <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
              <Field label="登場アニメーション">
                <select value={entranceAnim} onChange={(e) => setEntranceAnim(e.target.value)}>
                  {ANIM_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
              </Field>
              <Field label="表示コーナー">
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: ".3rem", width: "fit-content" }}>
                  {CORNER_BUTTONS.map(({ value, label }) => (
                    <button key={value} type="button"
                      className={position === value ? "btn" : "btn ghost"}
                      onClick={() => setPosition(value)}
                      style={{ fontSize: "1.4rem", width: 48, height: 48 }}
                      title={value}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </Field>
            </div>
          </div>

          {/* 表示オプション */}
          <div className="card">
            <div className="card-head"><strong>表示オプション</strong></div>
            <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
              <label style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}>
                <input type="checkbox" checked={showSeconds} onChange={(e) => setShowSeconds(e.target.checked)} />
                秒を表示
              </label>
              <label style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}>
                <input type="checkbox" checked={showDate} onChange={(e) => setShowDate(e.target.checked)} />
                日付を表示
              </label>
            </div>
          </div>
        </div>

        {/* 右: プレビュー + 保存 */}
        <div style={{ position: "sticky", top: "1rem", display: "flex", flexDirection: "column", gap: "1rem", minWidth: 560 }}>
          <div className="card">
            <div className="card-head"><strong>プレビュー</strong></div>
            <div className="card-body" style={{ padding: 0, overflow: "hidden", borderRadius: "0 0 .5rem .5rem" }}>
              <ClockPreview {...previewProps} />
            </div>
          </div>

          <div className="card">
            <div className="card-head"><strong>{selectedId === null ? "新規プリセット" : "プリセット編集"}</strong></div>
            <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
              <Field label="プリセット名">
                <input
                  type="text"
                  value={presetName}
                  onChange={(e) => setPresetName(e.target.value)}
                  placeholder="例: 朝の時計スタイル"
                  style={{ width: "100%" }}
                />
              </Field>
              <button className="btn" onClick={save} disabled={busy} style={{ width: "100%" }}>
                {busy ? "保存中…" : selectedId === null ? "プリセットを作成" : "プリセットを更新"}
              </button>
              {selectedId !== null && (
                <button className="btn ghost" onClick={remove} disabled={busy}
                  style={{ width: "100%", color: "var(--danger, #e53)" }}>
                  削除
                </button>
              )}
            </div>
          </div>

          <p className="muted" style={{ fontSize: ".75rem" }}>
            プリセットはチャンネル時計エディタや番組・シリーズフォームから読み込めます。
          </p>
        </div>
      </div>
    </StudioPage>
  );
}
