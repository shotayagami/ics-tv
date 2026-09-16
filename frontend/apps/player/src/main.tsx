// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoot } from "react-dom/client";

import "./player.css";

import { PlayerPage } from "./PlayerPage";

// 島マウント: Django の epg.html が <div id="player-island" data-slug=... data-home-base=...> を置く。
// :root のデザイントークンは public_base.html が供給するため tokens.css は import しない。
// StrictMode は使わない (動画エンジンの命令的 effect を二重起動させないため)。
const el = document.getElementById("player-island");
if (el) {
  createRoot(el).render(
    <PlayerPage slug={el.dataset.slug ?? ""} homeBase={el.dataset.homeBase ?? ""} />,
  );
}
