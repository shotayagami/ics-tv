// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import "./ops.css";

import { App } from "./App";

// 🔴 放送コンソール マウント。Django が ops.* ホストで staff 限定 <div id="ops-root"> を返す。
// ops.* は放送コンソール専用ホストなので basename 無し (ルート直下に SPA)。
const el = document.getElementById("ops-root");
if (el) {
  createRoot(el).render(
    <BrowserRouter>
      <App />
    </BrowserRouter>,
  );
}
