// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { AbsoluteFill, interpolate, useCurrentFrame } from "remotion";

export type LowerThirdProps = {
  name: string;
  role: string;
  channel?: string;
};

export const LOWER_THIRD_DEFAULT_PROPS: LowerThirdProps = {
  name: "田中 太郎",
  role: "キャスター",
  channel: "ICS-TV",
};

export function LowerThird({ name, role, channel }: LowerThirdProps) {
  const frame = useCurrentFrame();

  // 0〜20フレームでスライドイン → 保持 → 120〜140フレームでスライドアウト
  const slideIn = interpolate(frame, [0, 20], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const slideOut = interpolate(frame, [120, 140], [1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const progress = Math.min(slideIn, slideOut);

  const textOpacity = interpolate(frame, [10, 25], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const barWidth = 480;

  return (
    <AbsoluteFill style={{ background: "transparent" }}>
      {/* テロップバー */}
      <div
        style={{
          position: "absolute",
          bottom: 80,
          left: 0,
          width: barWidth * progress,
          overflow: "hidden",
          fontFamily: "'Noto Sans JP', sans-serif",
        }}
      >
        <div
          style={{
            width: barWidth,
            background: "linear-gradient(90deg, rgba(10,60,130,.92) 0%, rgba(8,40,90,.88) 100%)",
            padding: "14px 28px 14px 36px",
            borderRight: "3px solid #3ab0e8",
          }}
        >
          <div
            style={{
              opacity: textOpacity,
              fontSize: 36,
              fontWeight: 700,
              color: "#fff",
              lineHeight: 1.2,
              letterSpacing: ".03em",
              whiteSpace: "nowrap",
            }}
          >
            {name}
          </div>
          <div
            style={{
              opacity: textOpacity,
              fontSize: 20,
              color: "rgba(200, 230, 255, .75)",
              marginTop: 5,
              letterSpacing: ".05em",
              whiteSpace: "nowrap",
            }}
          >
            {role}
          </div>
        </div>
      </div>

      {/* チャンネルバグ（局名表示） */}
      {channel && (
        <div
          style={{
            position: "absolute",
            bottom: 80,
            left: barWidth * progress + 8,
            opacity: progress > 0.95 ? textOpacity : 0,
            fontSize: 14,
            color: "rgba(160, 210, 255, .6)",
            fontFamily: "monospace",
            letterSpacing: ".08em",
            padding: "4px 8px",
          }}
        >
          {channel}
        </div>
      )}
    </AbsoluteFill>
  );
}
