// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 本線ライブ HLS のエッジ認証 (#27 / docs/site-only-broadcast.md §5 リスク#3)。
 *
 * tv.example.com/hls2/<slug>/... へのリクエストを Cloudflare 上で受け、URL クエリの ?token=
 * (server/core/hls_auth.py が発行する HMAC-SHA256 署名) を検証してからオリジン
 * (k8s ingress-nginx → 送出ノードの nginx ABR ladder) へ流す。
 *
 * 設計上の要点:
 *
 * 1. **オリジンへは token を外した URL で取りに行く**。Cloudflare のキャッシュキーはリクエスト
 *    URL なので、視聴者ごとに異なる token を含んだまま取りに行くとキャッシュが視聴者数だけ
 *    分裂し、セグメント配信がすべてオリジンへ落ちる。正規化することで従来どおり共有される。
 *
 * 2. **manifest は書き換えて token を伝播する**。HLS の manifest 内の相対 URI は親の
 *    クエリ文字列を引き継がないため、書き換えないと子プレイリスト/セグメントが token 無しで
 *    要求され 403 になる。
 *
 * 3. **トークンは channel と期限しか束縛しない**。エッジからは「その番組が今ゲート対象か」を
 *    判定できないため、エンタイトルメント変更が効くまでの最大遅延は TTL
 *    (server 側 ICSTV_HLS_TOKEN_TTL_SEC) そのものになる。TTL を短くするほど窓が狭まる。
 *
 * 4. **HLS_AUTH_ENFORCE=false の間は素通し**(検証結果をヘッダに出すだけ)。24/7 送出を止めない
 *    ための段階導入用で、監視モードで問題が無いことを確認してから true にする。
 */

const TOKEN_PARAM = "token";
const PATH_RE = /^\/hls2\/([^/]+)\/(.+)$/;
// パス区切りのエンコード形。`new URL()` は素の ".." を正規化するので `/hls2/ch1/../ch2/x.ts` は
// slug が ch2 になって弾かれるが、**%2F / %5C は解決しない**。そのまま origin へ流すと、
// origin (ingress-nginx → 送出ノードの nginx) 側が %2F を復号して dot-segment を解決した時点で、
// ch1 の token のまま別 channel のセグメントへ到達しうる。エッジで落とす。
const ENCODED_SEPARATOR_RE = /%2f|%5c/i;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    // route は /hls2/* だが、経路外は従来どおり素通しする (保険)。
    if (!url.pathname.startsWith("/hls2/")) return fetch(request);
    // slug 判定と実際に配信される経路がずれるパスは通さない。
    if (ENCODED_SEPARATOR_RE.test(url.pathname)) return forbidden();
    const matched = url.pathname.match(PATH_RE);
    // /hls2/ 配下で形が合わないものを素通しすると、検証を経ずに origin へ抜ける。
    if (!matched) return forbidden();

    const channelSlug = matched[1];
    const token = url.searchParams.get(TOKEN_PARAM) || "";
    const distribution = isDistributionMode(env.HLS_DISTRIBUTION_MODE);
    const enforce = isLegacyEnforcementEnabled(env.HLS_AUTH_ENFORCE);
    if (distribution && (!enforce || !hasDistributionSigningKey(env.HLS_SIGNING_KEY))) {
      return unavailable();
    }
    const ok = await verifyToken(env.HLS_SIGNING_KEY, channelSlug, token);

    if (!ok && enforce) return forbidden();

    // オリジンへは token を除いた URL で。CF のキャッシュキーを視聴者間で共有させる。
    const originUrl = new URL(url);
    originUrl.searchParams.delete(TOKEN_PARAM);
    const manifestWithToken = isManifest(url.pathname) && Boolean(token);
    const originRequest = requestWithoutClientConditionals(
      new Request(originUrl.toString(), request),
      distribution || manifestWithToken,
    );
    const response = await fetch(originRequest);

    // no-store応答で304を返すとクライアントの既存表現を再利用させるため、配布モードでは
    // conditional headerをoriginへ渡さず、それでも304なら利用不能なorigin応答として閉じる。
    if (distribution && response.status === 304) return unusableNotModified();

    if (!isManifest(url.pathname) || !response.ok) {
      return withAuthState(distribution ? privateNoStore(response) : response, ok);
    }
    // manifest 内の相対 URI へ token を伝播する (子プレイリスト・セグメント・EXT-X-MAP 等)。
    const body = rewriteManifest(await response.text(), token);
    const headers = new Headers(response.headers);
    headers.delete("content-length"); // 書き換えで長さが変わる
    let manifestResponse = new Response(body, { status: response.status, headers });
    if (distribution || manifestWithToken) manifestResponse = privateNoStore(manifestResponse);
    return withAuthState(manifestResponse, ok);
  },
};

function isManifest(pathname) {
  return pathname.endsWith(".m3u8");
}

// Django側と共通: trim後のnamed true、または符号付きASCII十進整数の非zeroだけをtrueとする。
function isDistributionMode(value) {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (["true", "on", "ok", "y", "yes", "1"].includes(normalized)) return true;
  if (!/^[+-]?[0-9]+$/.test(normalized)) return false;
  return /[1-9]/.test(normalized);
}

// P2以前との互換: legacy enforcementはcase-insensitiveな厳密文字列 "true" だけ。
function isLegacyEnforcementEnabled(value) {
  return String(value ?? "").toLowerCase() === "true";
}

function hasDistributionSigningKey(value) {
  // 文字数は設定漏れ・既知の短い値を弾く最低条件であり、鍵のエントロピー保証ではない。
  return typeof value === "string" && value.length >= 32;
}

function unavailable() {
  return privateNoStore(
    new Response("service unavailable\n", {
      status: 503,
      headers: {
        "content-type": "text/plain; charset=utf-8",
        "access-control-allow-origin": "*",
      },
    }),
  );
}

function unusableNotModified() {
  return privateNoStore(
    new Response("bad gateway\n", {
      status: 502,
      headers: {
        "content-type": "text/plain; charset=utf-8",
        "access-control-allow-origin": "*",
      },
    }),
  );
}

function requestWithoutClientConditionals(request, strip) {
  if (!strip) return request;
  const headers = new Headers(request.headers);
  for (const name of [
    "if-match",
    "if-none-match",
    "if-modified-since",
    "if-unmodified-since",
    "if-range",
  ]) {
    headers.delete(name);
  }
  return new Request(request, { headers });
}

function privateNoStore(response) {
  const headers = new Headers(response.headers);
  headers.set("cache-control", "private, no-store");
  for (const name of [
    "age",
    "cache-tag",
    "cdn-cache-control",
    "cloudflare-cdn-cache-control",
    "etag",
    "expires",
    "last-modified",
    "surrogate-control",
  ]) {
    headers.delete(name);
  }
  return new Response(response.body, { status: response.status, headers });
}

function forbidden() {
  return new Response("forbidden\n", {
    status: 403,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      // ゲート判定の結果を共有キャッシュに残さない。
      "cache-control": "private, no-store",
      // プレイヤー側でエラーを識別できるよう CORS は付ける。
      "access-control-allow-origin": "*",
    },
  });
}

/** 監視モード時の可視化用。enforce 前に「どれだけ弾かれるはずだったか」をログで見るため。 */
function withAuthState(response, ok) {
  const headers = new Headers(response.headers);
  headers.set("x-icstv-hls-auth", ok ? "ok" : "invalid");
  return new Response(response.body, { status: response.status, headers });
}

/**
 * token = "<expires_at>.<hex hmac-sha256>"。
 * 署名対象は server/core/hls_auth.py::_signature と同じ `${slug}:${expires_at}` 文字列。
 * expires_at はトークンに入っていた文字列をそのまま使う (数値整形の差で不一致になるのを防ぐ)。
 */
async function verifyToken(secret, channelSlug, token) {
  if (!secret || !token) return false;
  const dot = token.indexOf(".");
  if (dot < 1) return false;
  const expiresStr = token.slice(0, dot);
  const signature = token.slice(dot + 1);
  const expiresAt = Number(expiresStr);
  if (!Number.isFinite(expiresAt) || Date.now() / 1000 >= expiresAt) return false;
  const expected = await hmacHex(secret, `${channelSlug}:${expiresStr}`);
  return timingSafeEqual(expected, signature);
}

async function hmacHex(secret, message) {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signed = await crypto.subtle.sign("HMAC", key, encoder.encode(message));
  return [...new Uint8Array(signed)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function rewriteManifest(text, token) {
  // token が無いときは書き換えない。HLS_AUTH_ENFORCE=false の監視モードでは token 無しの
  // リクエストも素通しするため、ここで空の `?token=` を全 URI へ付けると無意味に
  // キャッシュキーを分裂させてしまう。
  if (!token) return text;
  const query = `${TOKEN_PARAM}=${encodeURIComponent(token)}`;
  const addToken = (uri) => {
    if (/^[a-z]+:\/\//i.test(uri)) return uri; // 絶対 URL は書き換えない
    return uri.includes("?") ? `${uri}&${query}` : `${uri}?${query}`;
  };
  return text
    .split("\n")
    .map((line) => {
      const trimmed = line.trim();
      if (!trimmed) return line;
      // URI 行 (子プレイリスト / セグメント)
      if (!trimmed.startsWith("#")) return addToken(trimmed);
      // タグ属性内の URI="..." (EXT-X-MEDIA / EXT-X-MAP / EXT-X-I-FRAME-STREAM-INF)
      return line.replace(/URI="([^"]+)"/g, (_, uri) => `URI="${addToken(uri)}"`);
    })
    .join("\n");
}

export const __test__ = {
  verifyToken,
  hmacHex,
  timingSafeEqual,
  isManifest,
  hasDistributionSigningKey,
  isDistributionMode,
  isLegacyEnforcementEnabled,
};
