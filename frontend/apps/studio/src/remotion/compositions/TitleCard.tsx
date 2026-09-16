// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export type TitleCardProps = {
  title: string;
  subtitle: string;
  date: string;
};

export const TITLE_CARD_DEFAULT_PROPS: TitleCardProps = {
  title: "番組タイトル",
  subtitle: "エピソード 1",
  date: "2026-07-01",
};

export function TitleCard({ title, subtitle, date }: TitleCardProps) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

  const titleOpacity = interpolate(frame, [0, 15], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const subtitleProgress = spring({ frame: frame - 10, fps, config: { damping: 80, stiffness: 200 } });
  const subtitleOpacity = interpolate(frame, [10, 25], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const accentWidth = interpolate(frame, [5, 30], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill
      style={{
        background: "#04101a",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        fontFamily: "'Noto Sans JP', sans-serif",
      }}
    >
      {/* アクセントバー */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: `${accentWidth * 100}%`,
          height: 4,
          background: "linear-gradient(90deg, #1a6fa8 0%, #3ab0e8 100%)",
        }}
      />

      <div
        style={{
          opacity: titleOpacity,
          fontSize: 72,
          fontWeight: 700,
          color: "#fff",
          textAlign: "center",
          letterSpacing: ".04em",
          lineHeight: 1.3,
          maxWidth: 960,
          padding: "0 80px",
        }}
      >
        {title}
      </div>

      {subtitle && (
        <div
          style={{
            opacity: subtitleOpacity,
            transform: `translateY(${(1 - subtitleProgress) * 20}px)`,
            fontSize: 32,
            color: "rgba(160, 210, 240, .85)",
            marginTop: 28,
            letterSpacing: ".06em",
          }}
        >
          {subtitle}
        </div>
      )}

      {date && (
        <div
          style={{
            position: "absolute",
            bottom: 48,
            right: 64,
            fontSize: 22,
            color: "rgba(255,255,255,.35)",
            fontFamily: "monospace",
            letterSpacing: ".1em",
          }}
        >
          {date}
        </div>
      )}

      {/* 下部アクセントライン */}
      <div
        style={{
          position: "absolute",
          bottom: 0,
          left: 0,
          width: `${accentWidth * 100}%`,
          height: 2,
          background: "rgba(58, 176, 232, .4)",
        }}
      />
    </AbsoluteFill>
  );
}
