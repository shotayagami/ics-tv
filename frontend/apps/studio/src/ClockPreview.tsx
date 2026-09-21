// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** 時計オーバーレイの静止プレビュー。ClockEditorPage (チャンネル別編集) と
 * ClockPresetsPage (プリセット CRUD) の両方から使う共用コンポーネント。
 *
 * 実時刻を 1 秒ごとに描き直す (tick はこのコンポーネントの内部に持つので、
 * 呼び出し側はタイマーの管理をしなくてよい)。送出側の登場アニメーション
 * (entrance_anim) はここでは再生しない — 静止画のプレビューにとどめる。 */

import { useEffect, useState } from "react";

import { clockFontFamily, loadClockGoogleFont } from "./clockFonts";

export interface ClockPreviewProps {
  font_family: string;
  time_size: number;
  font_weight: number;
  time_color: string;
  date_color: string;
  text_effect: string;
  stroke_width: string;
  stroke_color: string;
  bg_preset: string;
  bg_opacity: number;
  box_shadow: string;
  border_radius: string;
  show_seconds: boolean;
  show_date: boolean;
  position: string;
}

export const CLOCK_PREVIEW_DEFAULTS: ClockPreviewProps = {
  font_family: "noto-sans-jp",
  time_size: 54,
  font_weight: 700,
  time_color: "#ffffff",
  date_color: "#cfe3ff",
  text_effect: "soft-shadow",
  stroke_width: "none",
  stroke_color: "#000000",
  bg_preset: "dark-gradient",
  bg_opacity: 0.8,
  box_shadow: "md",
  border_radius: "md",
  show_seconds: false,
  show_date: false,
  position: "top-left",
};

const TEXT_SHADOW_CSS: Record<string, string> = {
  "none":         "none",
  "soft-shadow":  "0 1px 3px rgba(0,0,0,.6)",
  "hard-outline": "-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000",
  "glow":         "0 0 8px rgba(255,255,255,.9),0 0 16px rgba(100,180,255,.7)",
};

// Bootstrap shadow クラスと等価な CSS 値（プレビュー用）
const BS_SHADOW_CSS: Record<string, string> = {
  "none": "none",
  "sm":   "0 .125rem .25rem rgba(0,0,0,.25)",
  "md":   "0 .5rem 1rem rgba(0,0,0,.4)",
  "lg":   "0 1rem 3rem rgba(0,0,0,.55)",
};

const BS_RADIUS_CSS: Record<string, string> = {
  "none": "0",
  "sm":   ".25rem",
  "md":   ".5rem",
  "lg":   "1rem",
  "pill": "50rem",
};

function getBgPreviewStyle(preset: string, opacity: number): string {
  const op = Math.min(1, Math.max(0, opacity));
  switch (preset) {
    case "dark-solid":  return `rgba(12,18,32,${op})`;
    case "dark-pill":   return `rgba(12,18,32,${op})`;
    case "frosted":     return `rgba(12,18,32,${op * 0.45})`;
    case "none":        return "transparent";
    default:
      return `linear-gradient(180deg,rgba(12,18,32,${op}),rgba(12,18,32,${op * 0.76}))`;
  }
}

export function ClockPreview(p: ClockPreviewProps) {
  // 1 秒ごとに再描画して実時刻を進める。値は使わず再レンダーの起点にするだけ。
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const SCALE = 560 / 1920;
  const ts = Math.round(p.time_size * SCALE);
  const ds = Math.round(p.time_size * 0.407 * SCALE);
  const pad = `${Math.round(10 * SCALE)}px ${Math.round(22 * SCALE)}px`;
  const strokeCss = p.stroke_width !== "none" ? `${p.stroke_width}px ${p.stroke_color}` : undefined;

  const now = new Date();
  const timeStr = now.toLocaleTimeString("ja-JP", {
    timeZone: "Asia/Tokyo", hour12: false,
    hour: "numeric", minute: "2-digit",
    ...(p.show_seconds ? { second: "2-digit" } : {}),
  });
  const dateStr = now.toLocaleDateString("ja-JP", {
    timeZone: "Asia/Tokyo", month: "numeric", day: "numeric", weekday: "short",
  });

  const posStyle: React.CSSProperties = {
    "top-left":     { left: "1.25%", top: "1.25%" },
    "top-right":    { right: "1.25%", top: "1.25%" },
    "bottom-left":  { left: "1.25%", bottom: "1.25%" },
    "bottom-right": { right: "1.25%", bottom: "1.25%" },
  }[p.position] ?? { left: "1.25%", top: "1.25%" };

  const ff = clockFontFamily(p.font_family);
  useEffect(() => { loadClockGoogleFont(p.font_family); }, [p.font_family]);

  return (
    <div style={{
      position: "relative", width: 560, height: 315,
      background: "#111827", borderRadius: 4, flexShrink: 0, overflow: "hidden",
    }}>
      {/* ARIB action-safe ガイド */}
      <div style={{
        position: "absolute", inset: "5.6%",
        border: "1px dashed rgba(255,255,255,.15)", pointerEvents: "none",
      }} />
      {/* 時計 */}
      <div style={{
        position: "absolute", ...posStyle,
        background: getBgPreviewStyle(p.bg_preset, p.bg_opacity),
        backdropFilter: p.bg_preset === "frosted" ? "blur(6px) saturate(1.4)" : undefined,
        borderRadius: BS_RADIUS_CSS[p.border_radius] ?? BS_RADIUS_CSS["md"],
        boxShadow: BS_SHADOW_CSS[p.box_shadow] ?? BS_SHADOW_CSS["md"],
        padding: pad, boxSizing: "border-box", textAlign: "center",
        fontFamily: ff,
      }}>
        <div style={{
          fontVariantNumeric: "tabular-nums",
          fontWeight: p.font_weight,
          fontSize: ts,
          lineHeight: 1.05,
          letterSpacing: ".03em",
          color: p.time_color,
          textShadow: TEXT_SHADOW_CSS[p.text_effect] ?? TEXT_SHADOW_CSS["soft-shadow"],
          WebkitTextStroke: strokeCss,
        }}>
          {timeStr}
        </div>
        {p.show_date && (
          <div style={{ fontSize: ds, letterSpacing: ".06em", color: p.date_color, marginTop: 1, WebkitTextStroke: strokeCss }}>
            {dateStr}
          </div>
        )}
      </div>
    </div>
  );
}
