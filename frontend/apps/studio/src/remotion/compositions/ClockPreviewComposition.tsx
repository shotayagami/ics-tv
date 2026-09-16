// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect } from "react";
import { AbsoluteFill, spring, useCurrentFrame, useVideoConfig } from "remotion";

import { clockFontFamily, loadClockGoogleFont } from "../../clockFonts";

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
  entrance_anim: string;
  show_seconds: boolean;
  show_date: boolean;
  position: string;
  sample_time: string;
  sample_date: string;
}

export const CLOCK_PREVIEW_DEFAULT_PROPS: ClockPreviewProps = {
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
  entrance_anim: "fade",
  show_seconds: false,
  show_date: false,
  position: "top-left",
  sample_time: "12:34",
  sample_date: "7月1日(火)",
};

const TEXT_SHADOW_CSS: Record<string, string> = {
  "none":         "none",
  "soft-shadow":  "0 1px 3px rgba(0,0,0,.6)",
  "hard-outline": "-1px -1px 0 #000,1px -1px 0 #000,-1px 1px 0 #000,1px 1px 0 #000",
  "glow":         "0 0 8px rgba(255,255,255,.9),0 0 16px rgba(100,180,255,.7)",
};

const BOX_SHADOW_CSS: Record<string, string> = {
  "none": "none",
  "sm":   "0 2px 4px rgba(0,0,0,.25)",
  "md":   "0 8px 16px rgba(0,0,0,.4)",
  "lg":   "0 16px 48px rgba(0,0,0,.55)",
};

const RADIUS_CSS: Record<string, string> = {
  "none": "0",
  "sm":   "4px",
  "md":   "8px",
  "lg":   "16px",
  "pill": "9999px",
};

function getBg(preset: string, opacity: number): string {
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

function getPositionStyle(pos: string): React.CSSProperties {
  switch (pos) {
    case "top-right":    return { right: 36, top: 36 };
    case "bottom-left":  return { left: 36, bottom: 36 };
    case "bottom-right": return { right: 36, bottom: 36 };
    default:             return { left: 36, top: 36 };
  }
}

export function ClockPreviewComposition({
  font_family,
  time_size,
  font_weight,
  time_color,
  date_color,
  text_effect,
  stroke_width,
  stroke_color,
  bg_preset,
  bg_opacity,
  box_shadow,
  border_radius,
  entrance_anim,
  show_seconds,
  show_date,
  position,
  sample_time,
  sample_date,
}: ClockPreviewProps) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  let opacity = 1;
  let transform = "none";

  if (entrance_anim === "fade") {
    opacity = spring({ frame, fps, from: 0, to: 1, durationInFrames: 30 });
  } else if (entrance_anim === "slide-down") {
    opacity = spring({ frame, fps, from: 0, to: 1, durationInFrames: 25 });
    const y = spring({ frame, fps, from: -30, to: 0, durationInFrames: 30 });
    transform = `translateY(${y}px)`;
  } else if (entrance_anim === "zoom") {
    opacity = spring({ frame, fps, from: 0, to: 1, durationInFrames: 25 });
    const scale = spring({ frame, fps, from: 0.7, to: 1, durationInFrames: 30 });
    transform = `scale(${scale})`;
  }

  useEffect(() => { loadClockGoogleFont(font_family); }, [font_family]);

  const ff = clockFontFamily(font_family);
  const dateSize = Math.round(time_size * 0.407);
  const timeStr = show_seconds ? `${sample_time}:00` : sample_time;
  const strokeCss = stroke_width !== "none" ? `${stroke_width}px ${stroke_color}` : undefined;

  return (
    <AbsoluteFill style={{ background: "linear-gradient(135deg,#0a0f1e 0%,#111827 100%)" }}>
      {/* ARIB action-safe ガイド (2.5% inset = 97.5% action-safe) */}
      <div style={{
        position: "absolute",
        inset: "2.5%",
        border: "1px dashed rgba(255,255,255,.1)",
        pointerEvents: "none",
      }} />

      {/* 時計ボックス */}
      <div style={{
        position: "absolute",
        ...getPositionStyle(position),
        opacity,
        transform,
        background: getBg(bg_preset, bg_opacity),
        backdropFilter: bg_preset === "frosted" ? "blur(12px) saturate(1.4)" : undefined,
        borderRadius: RADIUS_CSS[border_radius] ?? RADIUS_CSS["md"],
        boxShadow: BOX_SHADOW_CSS[box_shadow] ?? BOX_SHADOW_CSS["md"],
        padding: "10px 22px",
        boxSizing: "border-box",
        textAlign: "center",
        fontFamily: ff,
      }}>
        <div style={{
          fontVariantNumeric: "tabular-nums",
          fontWeight: font_weight,
          fontSize: time_size,
          lineHeight: 1.05,
          letterSpacing: ".03em",
          color: time_color,
          textShadow: TEXT_SHADOW_CSS[text_effect] ?? TEXT_SHADOW_CSS["soft-shadow"],
          WebkitTextStroke: strokeCss,
        }}>
          {timeStr}
        </div>
        {show_date && (
          <div style={{
            fontSize: dateSize,
            letterSpacing: ".06em",
            color: date_color,
            marginTop: 1,
            WebkitTextStroke: strokeCss,
          }}>
            {sample_date}
          </div>
        )}
      </div>
    </AbsoluteFill>
  );
}
