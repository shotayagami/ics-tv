// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import createClient, { type Middleware } from "openapi-fetch";

import type { paths } from "./schema";

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp("(?:^|;\\s*)" + name + "=([^;]+)"));
  return match ? decodeURIComponent(match[1]) : "";
}

// 既存サイトと同じ session cookie + CSRF を踏襲。非 GET には csrftoken cookie を
// X-CSRFToken ヘッダで付与する (ninja の APIKeyCookie が検証する)。
const csrf: Middleware = {
  onRequest({ request }) {
    if (!["GET", "HEAD", "OPTIONS", "TRACE"].includes(request.method.toUpperCase())) {
      const token = readCookie("csrftoken");
      if (token) request.headers.set("X-CSRFToken", token);
    }
    return request;
  },
};

// Option B は同一オリジン配信なので baseUrl は相対。将来フロント Pod を分離したら env 化。
export const api = createClient<paths>({ baseUrl: "/", credentials: "same-origin" });
api.use(csrf);
