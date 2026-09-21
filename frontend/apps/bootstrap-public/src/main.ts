// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
// Phase 0: 公開 SSR 向け Bootstrap テーマ (cyan/dark) + Bootstrap JS。
// SCSS → bootstrap-public.css、この JS → bootstrap-public.js。public_base.html (SSR) が読み込む。
// React 島/SPA は react-bootstrap を使うためこの JS は読み込まない (Phase 4 以降)。
import "@icstv/theme/public.scss";
import * as bootstrap from "bootstrap";

// Bootstrap を import した時点で data-bs-* (dropdown/modal/offcanvas/collapse) の data-API は自動登録される。
// htmx 等で後から差し込まれた要素を手動初期化できるよう window.bootstrap も露出する。
declare global {
  interface Window {
    bootstrap?: typeof bootstrap;
  }
}
window.bootstrap = bootstrap;
