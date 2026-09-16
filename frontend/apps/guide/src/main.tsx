// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoot } from "react-dom/client";

import "./guide.css";

import { GuidePage } from "./GuidePage";

// 島マウント: Django guide.html が <div id="guide-island" data-home-base=...> を置く。
// 表示日は GuidePage が URL の ?date= から取り、無ければサーバの当日に委ねる (data-base-date 非依存)。
const el = document.getElementById("guide-island");
if (el) {
  createRoot(el).render(<GuidePage homeBase={el.dataset.homeBase ?? ""} />);
}
