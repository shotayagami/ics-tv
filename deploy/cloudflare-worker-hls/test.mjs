// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Worker の署名検証と manifest 書き換えの回帰テスト。
 *
 *   node test.mjs
 *
 * CI には載せていない (Worker は deploy/ 配下でフロント/サーバのワークスペース外)。
 * src/index.js を触ったら手元で実行すること。署名が Django 側 (server/core/hls_auth.py) と
 * 一致することは、同じ入力に対する hexdigest を突き合わせて検証する。
 */

import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import test from "node:test";

import worker, { __test__, rewriteManifest } from "./src/index.js";

const KEY = "test-shared-key-1234567890"; // pragma: allowlist secret - test only
const SLUG = "ch1";

/** Django の core.hls_auth._signature と同じ計算 (HMAC-SHA256 の hex)。 */
function djangoSignature(secret, channelSlug, expiresAt) {
  return createHmac("sha256", secret).update(`${channelSlug}:${expiresAt}`).digest("hex");
}

function token(expiresAt, { secret = KEY, slug = SLUG } = {}) {
  return `${expiresAt}.${djangoSignature(secret, slug, expiresAt)}`;
}

const future = Math.floor(Date.now() / 1000) + 3600;
const past = Math.floor(Date.now() / 1000) - 1;

// --- 署名検証 ---
assert.equal(await __test__.verifyToken(KEY, SLUG, token(future)), true, "正しい token は通る");
assert.equal(await __test__.verifyToken(KEY, "ch2", token(future)), false, "channel 不一致は拒否");
assert.equal(await __test__.verifyToken(KEY, SLUG, token(past)), false, "期限切れは拒否");
assert.equal(
  await __test__.verifyToken(KEY, SLUG, `${future}.${"0".repeat(64)}`),
  false,
  "署名改竄は拒否",
);
assert.equal(await __test__.verifyToken("other", SLUG, token(future)), false, "鍵違いは拒否");
assert.equal(await __test__.verifyToken(KEY, SLUG, ""), false, "token 空は拒否");
assert.equal(await __test__.verifyToken(KEY, SLUG, "no-dot"), false, "区切り無しは拒否");
assert.equal(await __test__.verifyToken("", SLUG, token(future)), false, "secret 未設定は拒否");

// --- WebCrypto の HMAC が Django と一致すること ---
assert.equal(
  await __test__.hmacHex(KEY, `${SLUG}:${future}`),
  djangoSignature(KEY, SLUG, future),
  "WebCrypto と Python の HMAC-SHA256 が一致する",
);

// --- manifest 書き換え ---
const master = [
  "#EXTM3U",
  "#EXT-X-STREAM-INF:BANDWIDTH=7290800,RESOLUTION=1280x720",
  "720p.m3u8",
  "",
  '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="a",URI="audio.m3u8"',
  "#EXT-X-STREAM-INF:BANDWIDTH=100000",
  "https://cdn.example/abs.m3u8",
].join("\n");

const rewritten = rewriteManifest(master, "tok");
assert.match(rewritten, /^720p\.m3u8\?token=tok$/m, "子プレイリストへ token を伝播する");
assert.match(rewritten, /URI="audio\.m3u8\?token=tok"/, "URI 属性へも伝播する");
assert.match(rewritten, /^https:\/\/cdn\.example\/abs\.m3u8$/m, "絶対 URL は書き換えない");
assert.equal(rewriteManifest(master, ""), master, "token 無し (監視モード) では書き換えない");

// --- パス正規化 (エンコードされた区切りでの channel 越え) ---
// enforce=true・ch1 の正当 token で叩き、origin へ何が渡ったかを見る。
async function callWorker(path) {
  let origin = null;
  const saved = globalThis.fetch;
  globalThis.fetch = async (req) => {
    origin = typeof req === "string" ? req : req.url;
    return new Response("x", { status: 200 });
  };
  try {
    const res = await worker.fetch(
      new Request(`https://tv.example${path}?token=${token(future)}`),
      { HLS_SIGNING_KEY: KEY, HLS_AUTH_ENFORCE: "true" },
    );
    return { status: res.status, origin };
  } finally {
    globalThis.fetch = saved;
  }
}

const okPath = await callWorker("/hls2/ch1/seg.ts");
assert.equal(okPath.status, 200, "正当な token と経路は通る");
assert.equal(okPath.origin, "https://tv.example/hls2/ch1/seg.ts", "origin へは token を外して渡す");

for (const bad of ["/hls2/ch1/..%2Fch2/seg.ts", "/hls2/ch1/..%5Cch2/seg.ts", "/hls2/ch1/%2fx.ts"]) {
  const r = await callWorker(bad);
  assert.equal(r.status, 403, `エンコードされた区切りは拒否する: ${bad}`);
  assert.equal(r.origin, null, `拒否したら origin へ渡さない: ${bad}`);
}

const noRest = await callWorker("/hls2/ch1/");
assert.equal(noRest.status, 403, "/hls2/ 配下で形が合わないものは素通ししない");
assert.equal(noRest.origin, null, "形が合わないものを origin へ渡さない");

const dotdot = await callWorker("/hls2/ch1/%2e%2e/ch2/seg.ts");
assert.equal(dotdot.status, 403, "URL 正規化後に別 channel になるものは token 不一致で拒否");

const media = ["#EXTM3U", '#EXT-X-MAP:URI="init.mp4"', "#EXTINF:4.0,", "seg1.m4s?x=1"].join("\n");
const mediaOut = rewriteManifest(media, "tok");
assert.match(mediaOut, /URI="init\.mp4\?token=tok"/, "EXT-X-MAP へも伝播する");
assert.match(mediaOut, /^seg1\.m4s\?x=1&token=tok$/m, "既存クエリがあれば & で連結する");

// --- 判定ヘルパ ---
assert.equal(__test__.isManifest("/hls2/ch1/master.m3u8"), true);
assert.equal(__test__.isManifest("/hls2/ch1/seg1.ts"), false);

const DISTRIBUTION_KEY = "0123456789abcdef0123456789abcdef"; // pragma: allowlist secret -- 検証用の合成HMAC鍵。実在の資格情報ではない (gitleaks:allow)
const CONDITIONAL_HEADERS = {
  "if-match": '"current"',
  "if-none-match": '"shared"',
  "if-modified-since": "Wed, 01 Jan 2025 00:00:00 GMT",
  "if-unmodified-since": "Wed, 01 Jan 2030 00:00:00 GMT",
  "if-range": '"range"',
};
const REMOVED_CACHE_HEADERS = [
  "age",
  "cache-tag",
  "cdn-cache-control",
  "cloudflare-cdn-cache-control",
  "etag",
  "expires",
  "last-modified",
  "surrogate-control",
];

function distributionToken() {
  const expiresAt = Math.floor(Date.now() / 1000) + 3600;
  return token(expiresAt, { secret: DISTRIBUTION_KEY });
}

function env(overrides = {}) {
  return {
    HLS_DISTRIBUTION_MODE: "true",
    HLS_AUTH_ENFORCE: "true",
    HLS_SIGNING_KEY: DISTRIBUTION_KEY,
    ...overrides,
  };
}

async function withFetch(mock, callback) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = mock;
  try {
    return await callback();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("distribution mode parser follows the shared ASCII contract", () => {
  const longNonzero = `${"0".repeat(4500)}1`;
  for (const value of [
    "true",
    "TRUE",
    "on",
    "OK",
    "y",
    "YES",
    "1",
    "2",
    "007",
    "+1",
    "-1",
    "  +2  ",
    longNonzero,
  ]) {
    assert.equal(__test__.isDistributionMode(value), true, `${value.slice(0, 40)} must be true`);
  }
  for (const value of [
    undefined,
    "false",
    "t",
    "0",
    "+0",
    "-000",
    "1_0",
    "１",
    "١",
    "1.0",
    "0x1",
    "1e3",
    "+",
    "--1",
    "1 2",
    "   ",
  ]) {
    assert.equal(__test__.isDistributionMode(value), false, `${String(value)} must be false`);
  }
});

test("legacy enforcement accepts only case-insensitive exact true", async () => {
  for (const value of ["1", "yes", "on", " true "]) {
    let originCalls = 0;
    const response = await withFetch(
      async () => {
        originCalls += 1;
        return new Response("origin");
      },
      () =>
        worker.fetch(new Request("https://tv.example/hls2/ch1/seg.ts"), {
          HLS_AUTH_ENFORCE: value,
          HLS_SIGNING_KEY: DISTRIBUTION_KEY,
        }),
    );
    assert.equal(response.status, 200);
    assert.equal(originCalls, 1);
  }
  for (const value of ["true", "TRUE"]) {
    let originCalls = 0;
    const response = await withFetch(
      async () => {
        originCalls += 1;
        return new Response("origin");
      },
      () =>
        worker.fetch(new Request("https://tv.example/hls2/ch1/seg.ts"), {
          HLS_AUTH_ENFORCE: value,
          HLS_SIGNING_KEY: DISTRIBUTION_KEY,
        }),
    );
    assert.equal(response.status, 403);
    assert.equal(originCalls, 0);
  }
});

test("django-environ true spellings all enable distribution fail-closed", async () => {
  for (const value of [
    "true",
    "TRUE",
    "on",
    "ON",
    "ok",
    "OK",
    "y",
    "Y",
    "yes",
    "YES",
    "1",
  ]) {
    let originCalls = 0;
    const response = await withFetch(
      async () => {
        originCalls += 1;
        return new Response("origin");
      },
      () =>
        worker.fetch(new Request("https://tv.example/hls2/ch1/master.m3u8"), {
          HLS_DISTRIBUTION_MODE: value,
          HLS_AUTH_ENFORCE: "false",
          HLS_SIGNING_KEY: DISTRIBUTION_KEY,
        }),
    );
    assert.equal(response.status, 503, `${value} must enable distribution mode`);
    assert.equal(originCalls, 0);
  }
});

test("false, t, and unset distribution mode remain off", async () => {
  for (const value of ["false", "t", "T", undefined]) {
    let originCalls = 0;
    const response = await withFetch(
      async () => {
        originCalls += 1;
        return new Response("origin");
      },
      () =>
        worker.fetch(new Request("https://tv.example/hls2/ch1/master.m3u8"), {
          HLS_DISTRIBUTION_MODE: value,
          HLS_AUTH_ENFORCE: "false",
          HLS_SIGNING_KEY: "",
        }),
    );
    assert.equal(response.status, 200);
    assert.equal(originCalls, 1);
  }
});

test("signed decimal integers follow django-environ truth parsing", async () => {
  const longNonzero = "9".repeat(1000);
  for (const value of ["2", "007", "+1", "-1", "01", "-007", "  +2  ", longNonzero]) {
    let originCalls = 0;
    const response = await withFetch(
      async () => {
        originCalls += 1;
        return new Response("origin");
      },
      () =>
        worker.fetch(new Request("https://tv.example/hls2/ch1/master.m3u8"), {
          HLS_DISTRIBUTION_MODE: value,
          HLS_AUTH_ENFORCE: "false",
          HLS_SIGNING_KEY: DISTRIBUTION_KEY,
        }),
    );
    assert.equal(response.status, 503, `${value} must enable distribution mode`);
    assert.equal(originCalls, 0);
  }

  for (const value of ["0", "+0", "-0", "1.0", "0x1", "1e3"]) {
    let originCalls = 0;
    const response = await withFetch(
      async () => {
        originCalls += 1;
        return new Response("origin");
      },
      () =>
        worker.fetch(new Request("https://tv.example/hls2/ch1/master.m3u8"), {
          HLS_DISTRIBUTION_MODE: value,
          HLS_AUTH_ENFORCE: "false",
          HLS_SIGNING_KEY: "",
        }),
    );
    assert.equal(response.status, 200, `${value} must leave distribution mode off`);
    assert.equal(originCalls, 1);
  }
});

test("distribution mode rejects misconfiguration before origin", async () => {
  for (const overrides of [
    { HLS_AUTH_ENFORCE: "false" },
    { HLS_SIGNING_KEY: "" },
    { HLS_SIGNING_KEY: "short" },
  ]) {
    let originCalls = 0;
    const response = await withFetch(
      async () => {
        originCalls += 1;
        return new Response("origin");
      },
      () =>
        worker.fetch(
          new Request("https://tv.example/hls2/ch1/master.m3u8"),
          env(overrides),
        ),
    );
    assert.equal(response.status, 503);
    assert.equal(response.headers.get("cache-control"), "private, no-store");
    assert.equal(response.headers.get("access-control-allow-origin"), "*");
    assert.equal(originCalls, 0);
    assert.doesNotMatch(await response.text(), /short|012345/);
  }
});

test("legacy monitoring mode remains pass-through for an invalid token", async () => {
  let originCalls = 0;
  const response = await withFetch(
    async () => {
      originCalls += 1;
      return new Response("segment", { headers: { "cache-control": "public, max-age=60" } });
    },
    () =>
      worker.fetch(new Request("https://tv.example/hls2/ch1/seg.ts"), {
        HLS_AUTH_ENFORCE: "false",
        HLS_SIGNING_KEY: "",
      }),
  );
  assert.equal(response.status, 200);
  assert.equal(originCalls, 1);
  assert.equal(response.headers.get("cache-control"), "public, max-age=60");
  assert.equal(response.headers.get("x-icstv-hls-auth"), "invalid");
});

test("invalid and expired tokens are 403 without an origin fetch", async () => {
  const expiredAt = Math.floor(Date.now() / 1000) - 1;
  const candidates = [
    `${Math.floor(Date.now() / 1000) + 3600}.${"0".repeat(64)}`,
    token(expiredAt, { secret: DISTRIBUTION_KEY }),
  ];
  for (const candidate of candidates) {
    let originCalls = 0;
    const response = await withFetch(
      async () => {
        originCalls += 1;
        return new Response("origin");
      },
      () =>
        worker.fetch(
          new Request(`https://tv.example/hls2/ch1/seg.ts?token=${candidate}`),
          env(),
        ),
    );
    assert.equal(response.status, 403);
    assert.equal(response.headers.get("cache-control"), "private, no-store");
    assert.equal(originCalls, 0);
  }
});

test("tokenized manifest is rewritten and protected from downstream caches", async () => {
  let upstreamRequest;
  const signed = distributionToken();
  const response = await withFetch(
    async (request) => {
      upstreamRequest = request;
      return new Response("#EXTM3U\nseg.ts\n", {
        headers: {
          "cache-control": "public, max-age=60",
          age: "12",
          "cache-tag": "shared-manifest",
          "cdn-cache-control": "public, max-age=120",
          "cloudflare-cdn-cache-control": "public, max-age=180",
          etag: '"shared"',
          expires: "Wed, 01 Jan 2030 00:00:00 GMT",
          "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT",
          "surrogate-control": "public, max-age=240",
        },
      });
    },
    () =>
      worker.fetch(
        new Request(`https://tv.example/hls2/ch1/master.m3u8?token=${signed}`, {
          headers: CONDITIONAL_HEADERS,
        }),
        env(),
      ),
  );
  assert.equal(new URL(upstreamRequest.url).searchParams.has("token"), false);
  for (const header of Object.keys(CONDITIONAL_HEADERS)) {
    assert.equal(upstreamRequest.headers.has(header), false, `${header} must be removed upstream`);
  }
  assert.match(await response.text(), new RegExp(`seg\\.ts\\?token=${signed.replace(".", "\\.")}`));
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  for (const header of REMOVED_CACHE_HEADERS) {
    assert.equal(response.headers.has(header), false, `${header} must be removed`);
  }
});

test("legacy token-bearing manifest is private and strips conditionals", async () => {
  let upstreamRequest;
  const signed = distributionToken();
  const response = await withFetch(
    async (request) => {
      upstreamRequest = request;
      return new Response("#EXTM3U\nseg.ts\n", {
        headers: {
          "cache-control": "public, max-age=60",
          age: "12",
          "cache-tag": "legacy",
          "cdn-cache-control": "public, max-age=120",
          "cloudflare-cdn-cache-control": "public, max-age=180",
          etag: '"legacy"',
          expires: "Wed, 01 Jan 2030 00:00:00 GMT",
          "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT",
          "surrogate-control": "public, max-age=240",
        },
      });
    },
    () =>
      worker.fetch(
        new Request(`https://tv.example/hls2/ch1/master.m3u8?token=${signed}`, {
          headers: CONDITIONAL_HEADERS,
        }),
        { HLS_AUTH_ENFORCE: "true", HLS_SIGNING_KEY: DISTRIBUTION_KEY },
      ),
  );
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  for (const header of REMOVED_CACHE_HEADERS) assert.equal(response.headers.has(header), false);
  for (const header of Object.keys(CONDITIONAL_HEADERS)) {
    assert.equal(upstreamRequest.headers.has(header), false);
  }
  assert.match(await response.text(), /seg\.ts\?token=/);
});

test("legacy token-bearing segment retains cache and conditionals", async () => {
  let upstreamRequest;
  const response = await withFetch(
    async (request) => {
      upstreamRequest = request;
      return new Response("segment", {
        headers: {
          "cache-control": "public, max-age=60",
          etag: '"segment"',
          "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT",
        },
      });
    },
    () =>
      worker.fetch(
        new Request(`https://tv.example/hls2/ch1/seg.ts?token=${distributionToken()}`, {
          headers: CONDITIONAL_HEADERS,
        }),
        { HLS_AUTH_ENFORCE: "true", HLS_SIGNING_KEY: DISTRIBUTION_KEY },
      ),
  );
  assert.equal(new URL(upstreamRequest.url).searchParams.has("token"), false);
  for (const header of Object.keys(CONDITIONAL_HEADERS)) {
    assert.equal(upstreamRequest.headers.get(header), CONDITIONAL_HEADERS[header]);
  }
  assert.equal(response.headers.get("cache-control"), "public, max-age=60");
  assert.equal(response.headers.get("etag"), '"segment"');
  assert.equal(response.headers.has("last-modified"), true);
});

test("distribution mode rejects an unexpected origin 304", async () => {
  const response = await withFetch(
    async () => new Response(null, { status: 304, headers: { etag: '"shared"' } }),
    () =>
      worker.fetch(
        new Request(`https://tv.example/hls2/ch1/master.m3u8?token=${distributionToken()}`),
        env(),
      ),
  );
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  assert.equal(response.headers.get("access-control-allow-origin"), "*");
  assert.equal(response.headers.has("etag"), false);
});

test("distribution segment also rejects an unexpected origin 304", async () => {
  const response = await withFetch(
    async () => new Response(null, { status: 304, headers: { etag: '"segment"' } }),
    () =>
      worker.fetch(
        new Request(`https://tv.example/hls2/ch1/seg.ts?token=${distributionToken()}`),
        env(),
      ),
  );
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  assert.equal(response.headers.get("access-control-allow-origin"), "*");
  assert.equal(response.headers.has("etag"), false);
});

test("distribution mode preserves origin errors but prevents downstream caching", async () => {
  const response = await withFetch(
    async () =>
      new Response("missing", {
        status: 404,
        headers: { "cache-control": "public, max-age=60", etag: '"missing"' },
      }),
    () =>
      worker.fetch(
        new Request(`https://tv.example/hls2/ch1/master.m3u8?token=${distributionToken()}`),
        env(),
      ),
  );
  assert.equal(response.status, 404);
  assert.equal(await response.text(), "missing");
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  assert.equal(response.headers.has("etag"), false);
});

test("distribution segment strips token upstream and is private downstream", async () => {
  let upstreamRequest;
  const response = await withFetch(
    async (request) => {
      upstreamRequest = request;
      return new Response("segment", {
        headers: { "cache-control": "public, max-age=60", etag: '"segment"' },
      });
    },
    () =>
      worker.fetch(
        new Request(`https://tv.example/hls2/ch1/seg.ts?token=${distributionToken()}`),
        env(),
      ),
  );
  assert.equal(new URL(upstreamRequest.url).searchParams.has("token"), false);
  assert.equal(await response.text(), "segment");
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  assert.equal(response.headers.has("etag"), false);
});

test("paths outside the configured HLS route keep their legacy behavior", async () => {
  let originCalls = 0;
  const response = await withFetch(
    async () => {
      originCalls += 1;
      return new Response("outside", { headers: { "cache-control": "public" } });
    },
    () => worker.fetch(new Request("https://tv.example/other/file.ts"), env()),
  );
  assert.equal(originCalls, 1);
  assert.equal(response.headers.get("cache-control"), "public");
});
