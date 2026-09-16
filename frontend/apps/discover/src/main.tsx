// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoot } from "react-dom/client";

import "./discover.css";

import { DiscoverPage } from "./DiscoverPage";

// 島マウント: Django が <div id="discover-island" data-mode="search|browse|vod" data-home-base=...> を置く。
const el = document.getElementById("discover-island");
if (el) {
  const mode = (el.dataset.mode ?? "search") as "search" | "browse" | "vod";
  createRoot(el).render(<DiscoverPage mode={mode} homeBase={el.dataset.homeBase ?? ""} />);
}
