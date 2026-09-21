// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoot } from "react-dom/client";

import { ToastProvider } from "./toast";

import { ProgramActions } from "./ProgramActions";
import { VodPlayer } from "./VodPlayer";

// 番組詳細ページ: 操作バー島 (#program-actions-island)。トグル失敗通知のため ToastProvider でラップ。
const pa = document.getElementById("program-actions-island");
if (pa) {
  const id = Number(pa.dataset.programId ?? "0");
  createRoot(pa).render(
    <ToastProvider>
      <ProgramActions programId={id} base={pa.dataset.base ?? ""} />
    </ToastProvider>,
  );
}

// 見逃し再生ページ: 録画プレイヤー島 (#vod-player-island)。署名URL等は data 属性で渡る。
const vp = document.getElementById("vod-player-island");
if (vp) {
  createRoot(vp).render(
    <VodPlayer
      playUrl={vp.dataset.playUrl ?? ""}
      resumeMs={Number(vp.dataset.resume ?? "0")}
      progressUrl={vp.dataset.progress ?? ""}
      poster={vp.dataset.poster ?? ""}
      captionsUrl={vp.dataset.captions ?? ""}
    />,
  );
}
