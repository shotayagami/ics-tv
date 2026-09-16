// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import "./studio.css";

import { App } from "./App";

// studio SPA マウント。Django が staff 限定で <div id="studio-root"> シェルを返す。
// React Router の basename=/studio で /studio/* をクライアントルーティング。
const el = document.getElementById("studio-root");
if (el) {
  createRoot(el).render(
    <BrowserRouter basename="/studio">
      <App />
    </BrowserRouter>,
  );
}
