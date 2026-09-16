// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
// Phase 0: 管理 (studio/ops/旧admin) 向け Bootstrap テーマ (green/等幅/dark) + Bootstrap JS。
// SCSS → bootstrap-admin.css、この JS → bootstrap-admin.js。
// studio/ops の SPA は react-bootstrap を使うため CSS のみリンクし、この JS は読み込まない。
// 旧 admin / delivery の SSR ページ (htmx) では data-bs-* 用にこの JS を読み込む。
import "@icstv/theme/admin.scss";
import * as bootstrap from "bootstrap";

declare global {
  interface Window {
    bootstrap?: typeof bootstrap;
  }
}
window.bootstrap = bootstrap;
