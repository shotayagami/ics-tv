// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, Field, Notice, StudioPage, SubNav } from "../atoms";

import type { components } from "@icstv/api";
import { postForm } from "../hooks";
import { RouterLink } from "../links";
import { CLOCK_FONT_GROUPS } from "../clockFonts";
import { ClockPreview } from "../ClockPreview";

type ClockStyleOut = components["schemas"]["ClockStyleOut"];

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

// ---- 色ピッカー ----

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

// ---- 時間帯リスト ----

interface Window_ { start: string; end: string }

function windowsToText(windows: unknown[]): string {
  return (windows as Window_[])
    .filter((w) => w && typeof w === "object" && w.start && w.end)
    .map((w) => `${w.start}-${w.end}`)
    .join("\n");
}

// ---- メインコンポーネント ----

export function ClockEditorPage() {
  const { slug = "" } = useParams();
  const [d, setD] = useState<ClockStyleOut | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  // フォームステート
  const [fontFamily,     setFontFamily]     = useState("noto-sans-jp");
  const [timeSize,       setTimeSize]        = useState(54);
  const [fontWeight,     setFontWeight]      = useState(700);
  const [timeColor,      setTimeColor]       = useState("#ffffff");
  const [dateColor,      setDateColor]       = useState("#cfe3ff");
  const [textEffect,     setTextEffect]      = useState("soft-shadow");
  const [strokeWidth,    setStrokeWidth]     = useState("none");
  const [strokeColor,    setStrokeColor]     = useState("#000000");
  const [bgPreset,       setBgPreset]        = useState("dark-gradient");
  const [bgOpacity,      setBgOpacity]       = useState(0.8);
  const [boxShadow,      setBoxShadow]       = useState("md");
  const [borderRadius,   setBorderRadius]    = useState("md");
  const [entranceAnim,   setEntranceAnim]    = useState("fade");
  const [position,       setPosition]        = useState("top-left");
  const [showSeconds,    setShowSeconds]     = useState(false);
  const [showDate,       setShowDate]        = useState(false);
  const [clockEnabled,   setClockEnabled]    = useState(false);
  const [windowsText,    setWindowsText]     = useState("");

  const load = useCallback(() => {
    if (!slug) return;
    api
      .GET("/api/v1/admin/channels/{slug}/clock", { params: { path: { slug } } })
      .then(({ data, error }) => {
        if (error || !data) return setErr("読み込み失敗 (staff 権限が必要)");
        setD(data);
        setFontFamily(data.font_family);
        setTimeSize(data.time_size);
        setFontWeight(data.font_weight);
        setTimeColor(data.time_color);
        setDateColor(data.date_color);
        setTextEffect(data.text_effect);
        setStrokeWidth(data.stroke_width ?? "none");
        setStrokeColor(data.stroke_color ?? "#000000");
        setBgPreset(data.bg_preset);
        setBgOpacity(data.bg_opacity);
        setBoxShadow(data.box_shadow);
        setBorderRadius(data.border_radius);
        setEntranceAnim(data.entrance_anim);
        setPosition(data.position);
        setShowSeconds(data.show_seconds);
        setShowDate(data.show_date);
        setClockEnabled(data.clock_overlay_enabled);
        setWindowsText(windowsToText(data.clock_windows));
      })
      .catch(() => setErr("読み込み失敗"));
  }, [slug]);
  useEffect(load, [load]);

  async function save() {
    if (busy || !d) return;
    setBusy(true);
    setErr(""); setMsg("");
    const fields: Record<string, string> = {
      font_family:   fontFamily,
      time_size:     String(timeSize),
      font_weight:   String(fontWeight),
      time_color:    timeColor,
      date_color:    dateColor,
      text_effect:   textEffect,
      stroke_width:  strokeWidth,
      stroke_color:  strokeColor,
      bg_preset:     bgPreset,
      bg_opacity:    String(bgOpacity),
      box_shadow:    boxShadow,
      border_radius: borderRadius,
      entrance_anim: entranceAnim,
      position,
      clock_windows: windowsText,
    };
    if (showSeconds)  fields.show_seconds          = "1";
    if (showDate)     fields.show_date             = "1";
    if (clockEnabled) fields.clock_overlay_enabled = "1";

    const { status, data } = await postForm(d.save_url, fields);
    setBusy(false);
    if (status === 200) {
      setMsg("時計スタイルを保存しました");
      load();
    } else {
      setErr(typeof data === "string" ? data : "保存に失敗しました");
    }
  }

  return (
    <StudioPage
      title={d ? `${d.clock_overlay_enabled ? "🟢" : "⚫"} 時計オーバーレイ` : "時計オーバーレイ"}
      breadcrumb={
        <Breadcrumb
          items={[{ label: "チャンネル", href: `/channels` }, { label: slug }]}
          linkComponent={RouterLink}
        />
      }
    >
      <SubNav
        items={[
          { label: "詳細設定", href: `/channels/${slug}/settings`, active: false },
          { label: "時計エディタ", href: `/channels/${slug}/clock`, active: true },
        ]}
        linkComponent={RouterLink}
      />

      {(msg || err) && (
        <Notice variant={err ? "error" : "success"}>{msg || err}</Notice>
      )}

      {!d ? (
        <EmptyState>読み込み中…</EmptyState>
      ) : (
        <div style={{ display: "grid", gridTemplateColumns: "1fr auto", gap: "1.5rem", alignItems: "start" }}>

          {/* 左: フォームカード群 */}
          <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>

            {/* フォント */}
            <div className="card">
              <div className="card-head"><strong>フォント</strong></div>
              <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
                <Field label="フォントファミリ">
                  <select value={fontFamily} onChange={(e) => setFontFamily(e.target.value)}>
                    {CLOCK_FONT_GROUPS.map((g) => (
                      <optgroup key={g.group} label={g.group}>
                        {g.options.map((o) => (
                          <option key={o.value} value={o.value}>{o.label}</option>
                        ))}
                      </optgroup>
                    ))}
                  </select>
                </Field>
                <Field label="時刻サイズ (px)">
                  <input
                    type="number" min={36} max={80} value={timeSize}
                    onChange={(e) => setTimeSize(Number(e.target.value))}
                    style={{ width: "5rem" }}
                  />
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

            {/* エフェクト・背景・シャドウ・角丸 */}
            <div className="card">
              <div className="card-head"><strong>エフェクト / 背景</strong></div>
              <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
                <Field label="テキストエフェクト">
                  <select value={textEffect} onChange={(e) => setTextEffect(e.target.value)}>
                    {TEXT_EFFECT_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
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
                    {BG_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </Field>
                <Field label="背景の不透明度">
                  <input
                    type="range" min={0} max={1} step={0.05} value={bgOpacity}
                    onChange={(e) => setBgOpacity(Number(e.target.value))}
                    style={{ width: "10rem" }}
                  />
                  <span className="muted" style={{ marginLeft: ".4rem" }}>{Math.round(bgOpacity * 100)}%</span>
                </Field>
                <Field label="シャドウ (Bootstrap)">
                  <select value={boxShadow} onChange={(e) => setBoxShadow(e.target.value)}>
                    {SHADOW_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </Field>
                <Field label="角丸 (Bootstrap)">
                  <select value={borderRadius} onChange={(e) => setBorderRadius(e.target.value)}>
                    {RADIUS_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
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
                    {ANIM_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                </Field>
                <Field label="表示コーナー">
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: ".3rem", width: "fit-content" }}>
                    {CORNER_BUTTONS.map(({ value, label }) => (
                      <button
                        key={value}
                        type="button"
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

            {/* 表示オプション・時間帯 */}
            <div className="card">
              <div className="card-head"><strong>表示オプション / 時間帯</strong></div>
              <div className="card-body" style={{ display: "flex", flexDirection: "column", gap: ".75rem" }}>
                <label style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}>
                  <input type="checkbox" checked={showSeconds} onChange={(e) => setShowSeconds(e.target.checked)} />
                  秒を表示
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}>
                  <input type="checkbox" checked={showDate} onChange={(e) => setShowDate(e.target.checked)} />
                  日付を表示
                </label>
                <label style={{ display: "flex", alignItems: "center", gap: ".5rem", cursor: "pointer" }}>
                  <input type="checkbox" checked={clockEnabled} onChange={(e) => setClockEnabled(e.target.checked)} />
                  時計オーバーレイを有効にする
                </label>
                <Field label="表示時間帯 (HH:MM-HH:MM、1行1窓)">
                  <textarea
                    value={windowsText}
                    onChange={(e) => setWindowsText(e.target.value)}
                    rows={4}
                    placeholder={"04:30-08:00\n15:00-19:00"}
                    style={{ fontFamily: "monospace", width: "100%" }}
                  />
                </Field>
              </div>
            </div>
          </div>

          {/* 右: プレビュー + 保存 */}
          <div style={{ position: "sticky", top: "1rem", display: "flex", flexDirection: "column", gap: "1rem" }}>
            <div className="card">
              <div className="card-head"><strong>プレビュー</strong></div>
              <div className="card-body" style={{ padding: "0" }}>
                <ClockPreview
                  font_family={fontFamily}
                  time_size={timeSize}
                  font_weight={fontWeight}
                  time_color={timeColor}
                  date_color={dateColor}
                  text_effect={textEffect}
                  stroke_width={strokeWidth}
                  stroke_color={strokeColor}
                  bg_preset={bgPreset}
                  bg_opacity={bgOpacity}
                  box_shadow={boxShadow}
                  border_radius={borderRadius}
                  show_seconds={showSeconds}
                  show_date={showDate}
                  position={position}
                />
              </div>
            </div>
            <button className="btn" onClick={save} disabled={busy} style={{ width: "100%" }}>
              {busy ? "保存中…" : "保存"}
            </button>
          </div>
        </div>
      )}
    </StudioPage>
  );
}
