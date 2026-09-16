// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoot } from "react-dom/client";

import "./home.css";

import { HomePage } from "./HomePage";

// 島マウント: Django home.html が <div id="home-island" data-home-base=...> を置く。
// hero + チャンネルカード (ライブ部分) のみ島化。これからの番組/VOD/ジャンルは SSR のまま。
const el = document.getElementById("home-island");
if (el) {
  createRoot(el).render(<HomePage homeBase={el.dataset.homeBase ?? ""} />);
}
