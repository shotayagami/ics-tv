// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { createRoot } from "react-dom/client";

import { AudienceFormIsland } from "./AudienceForm";

// 番組ページ フォーム島マウント (series_detail.html の #series-form-island)
const el = document.getElementById("series-form-island");
if (el) {
  const seriesId = Number(el.dataset.seriesId ?? "0");
  const csrf = el.dataset.csrf ?? "";
  createRoot(el).render(<AudienceFormIsland seriesId={seriesId} csrf={csrf} />);
}
