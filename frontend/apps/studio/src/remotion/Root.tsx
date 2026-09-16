// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Composition } from "remotion";
import { ClockPreviewComposition, CLOCK_PREVIEW_DEFAULT_PROPS } from "./compositions/ClockPreviewComposition";
import { LowerThird, LOWER_THIRD_DEFAULT_PROPS } from "./compositions/LowerThird";
import { TitleCard, TITLE_CARD_DEFAULT_PROPS } from "./compositions/TitleCard";

const FPS = 30;

export function StudioRemotionRoot() {
  return (
    <>
      <Composition
        id="title-card"
        component={TitleCard}
        durationInFrames={FPS * 5}
        fps={FPS}
        width={1280}
        height={720}
        defaultProps={TITLE_CARD_DEFAULT_PROPS}
      />
      <Composition
        id="lower-third"
        component={LowerThird}
        durationInFrames={FPS * 5}
        fps={FPS}
        width={1280}
        height={720}
        defaultProps={LOWER_THIRD_DEFAULT_PROPS}
      />
      <Composition
        id="clock-preview"
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        component={ClockPreviewComposition as any}
        durationInFrames={FPS * 5}
        fps={FPS}
        width={1920}
        height={1080}
        defaultProps={CLOCK_PREVIEW_DEFAULT_PROPS}
      />
    </>
  );
}
