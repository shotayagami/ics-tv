// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import assert from "node:assert/strict";
import { File } from "node:buffer";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  contentTypeFor,
  isUploadSessionLifetimeExceeded,
  resumeAfterVerifying,
  sha256File,
  shouldAutoResign,
  uploadMissingParts,
  uploadPartsWithAutoResign,
  UploadHttpError,
  UploadUrlExpiredError,
} from "../src/mediaUpload.ts";

function baseSession(overrides) {
  return {
    idempotencyKey: "idem-resign",
    uploadUuid: "00000000-0000-0000-0000-000000000099",
    title: "fixture",
    kind: "program",
    filename: "fixture.mp4",
    contentType: "video/mp4",
    size: 4,
    lastModified: 1,
    sha256: "0".repeat(64),
    etags: {},
    ...overrides,
  };
}

test("sha256File streams known content correctly", async () => {
  for (const bytes of [Buffer.alloc(0), Buffer.from("abc"), Buffer.alloc(1024 * 1024 + 17, 0xa5)]) {
    const file = new File([bytes], "fixture.mp4", { type: "video/mp4" });
    const actual = await sha256File(file, new AbortController().signal, () => {});
    assert.equal(actual, createHash("sha256").update(bytes).digest("hex"));
  }
});

test("contentTypeFor accepts declared types and infers Matroska", () => {
  assert.equal(contentTypeFor(new File(["x"], "clip.bin", { type: "video/mp4" })), "video/mp4");
  assert.equal(contentTypeFor(new File(["x"], "clip.MKV")), "video/x-matroska");
  assert.equal(contentTypeFor(new File(["x"], "clip.exe")), "");
});

test("uploadMissingParts skips persisted ETags and omits credentials", async (context) => {
  const writes = [];
  const calls = [];
  context.mock.method(globalThis, "fetch", async (url, init) => {
    calls.push({ url, init });
    return new Response(null, { status: 200, headers: { ETag: `etag-${url.split("/").pop()}` } });
  });
  globalThis.sessionStorage = { setItem: (...args) => writes.push(args) };
  const file = new File(["abcdefgh"], "fixture.mp4", { type: "video/mp4", lastModified: 1 });
  const session = {
    idempotencyKey: "idem",
    uploadUuid: "00000000-0000-0000-0000-000000000001",
    title: "fixture",
    kind: "program",
    filename: file.name,
    contentType: file.type,
    size: file.size,
    lastModified: file.lastModified,
    sha256: "0".repeat(64),
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
    etags: { "1": "etag-existing" },
  };
  const response = {
    upload_uuid: session.uploadUuid,
    status: "uploading",
    part_size_bytes: 2,
    parts: [1, 2, 3, 4].map((part_number) => ({ part_number, url: `https://r2.invalid/${part_number}` })),
    expires_at: session.expiresAt,
    asset_id: null,
    error_code: "",
  };

  const etags = await uploadMissingParts(file, response, session, new AbortController().signal, () => {});

  assert.deepEqual(calls.map(({ url }) => url).sort(), [
    "https://r2.invalid/2",
    "https://r2.invalid/3",
    "https://r2.invalid/4",
  ]);
  assert.ok(calls.every(({ init }) => init.credentials === "omit" && init.redirect === "error"));
  assert.equal(Object.keys(etags).length, 4);
  assert.equal(writes.length, 3);
  assert.ok(writes.every(([, value]) => !value.includes("r2.invalid")));
});

test("uploadMissingParts retries a transient provider response", async (context) => {
  let attempts = 0;
  context.mock.method(globalThis, "fetch", async () => {
    attempts += 1;
    return attempts === 1
      ? new Response(null, { status: 503 })
      : new Response(null, { status: 200, headers: { ETag: "etag-retried" } });
  });
  context.mock.method(globalThis, "setTimeout", (callback) => {
    queueMicrotask(callback);
    return 1;
  });
  globalThis.window = globalThis;
  globalThis.sessionStorage = { setItem: () => {} };
  const file = new File(["ab"], "fixture.mp4", { type: "video/mp4", lastModified: 1 });
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const session = {
    idempotencyKey: "idem-retry",
    uploadUuid: "00000000-0000-0000-0000-000000000002",
    title: "fixture",
    kind: "program",
    filename: file.name,
    contentType: file.type,
    size: file.size,
    lastModified: file.lastModified,
    sha256: "0".repeat(64),
    expiresAt,
    etags: {},
  };
  const response = {
    upload_uuid: session.uploadUuid,
    status: "uploading",
    part_size_bytes: 2,
    parts: [{ part_number: 1, url: "https://r2.invalid/1" }],
    expires_at: expiresAt,
    asset_id: null,
    error_code: "",
  };

  const etags = await uploadMissingParts(file, response, session, new AbortController().signal, () => {});

  assert.equal(attempts, 2);
  assert.equal(etags["1"], "etag-retried");
});

test("one permanent part failure cancels peer PUT requests", async (context) => {
  let cancelledPeers = 0;
  context.mock.method(globalThis, "fetch", async (url, init) => {
    if (url.endsWith("/1")) return new Response(null, { status: 400 });
    return new Promise((resolve, reject) => {
      init.signal.addEventListener("abort", () => {
        cancelledPeers += 1;
        reject(new DOMException("Aborted", "AbortError"));
      }, { once: true });
    });
  });
  globalThis.sessionStorage = { setItem: () => {} };
  const file = new File(["abcdef"], "fixture.mp4", { type: "video/mp4", lastModified: 1 });
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const session = {
    idempotencyKey: "idem-cancel",
    uploadUuid: "00000000-0000-0000-0000-000000000003",
    title: "fixture",
    kind: "program",
    filename: file.name,
    contentType: file.type,
    size: file.size,
    lastModified: file.lastModified,
    sha256: "0".repeat(64),
    expiresAt,
    etags: {},
  };
  const response = {
    upload_uuid: session.uploadUuid,
    status: "uploading",
    part_size_bytes: 2,
    parts: [1, 2, 3].map((part_number) => ({ part_number, url: `https://r2.invalid/${part_number}` })),
    expires_at: expiresAt,
    asset_id: null,
    error_code: "",
  };

  await assert.rejects(
    uploadMissingParts(file, response, session, new AbortController().signal, () => {}),
    (error) => error instanceof UploadHttpError && error.status === 400,
  );
  assert.equal(cancelledPeers, 2);
});

test("resumeAfterVerifying skips the request when no etags were saved locally", async (context) => {
  const calls = [];
  context.mock.method(globalThis, "fetch", async (...args) => {
    calls.push(args);
    return new Response(null, { status: 200 });
  });
  globalThis.document = { cookie: "" };

  const result = await resumeAfterVerifying(
    "00000000-0000-0000-0000-000000000010",
    null,
    new AbortController().signal,
  );

  assert.equal(result, null);
  assert.equal(calls.length, 0);
});

test("resumeAfterVerifying re-sends the saved etags and returns the response on success", async (context) => {
  context.mock.method(globalThis, "fetch", async () => {
    return new Response(JSON.stringify({ status: "completed", upload_uuid: "u1" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
  globalThis.document = { cookie: "" };
  const session = { etags: { 1: "etag-1", 2: "etag-2" } };

  const result = await resumeAfterVerifying(
    "00000000-0000-0000-0000-000000000011",
    session,
    new AbortController().signal,
  );

  assert.deepEqual(result, { status: "completed", upload_uuid: "u1" });
});

test("resumeAfterVerifying swallows a repeated provider failure and returns null", async (context) => {
  context.mock.method(globalThis, "fetch", async () => new Response(null, { status: 503 }));
  globalThis.document = { cookie: "" };
  const session = { etags: { 1: "etag-1" } };

  const result = await resumeAfterVerifying(
    "00000000-0000-0000-0000-000000000012",
    session,
    new AbortController().signal,
  );

  assert.equal(result, null);
});

test("shouldAutoResign: first attempt always allowed, later attempts need new progress, cap wins even with progress", () => {
  assert.equal(shouldAutoResign(0, 0, 0, 24), true, "first ever resign has nothing to compare against");
  assert.equal(shouldAutoResign(1, 2, 2, 24), false, "no new completed parts since the last resign");
  assert.equal(shouldAutoResign(1, 2, 3, 24), true, "a new part completed since the last resign");
  assert.equal(shouldAutoResign(24, 2, 3, 24), false, "cap reached even though progress happened");
  assert.equal(shouldAutoResign(23, 2, 3, 24), true, "one below the cap, with progress, is still allowed");
});

test("isUploadSessionLifetimeExceeded matches only the session-lifetime 409, not other errors", () => {
  assert.equal(isUploadSessionLifetimeExceeded(new UploadHttpError("upload session has expired", 409)), true);
  assert.equal(isUploadSessionLifetimeExceeded(new UploadHttpError("upload session has expired", 403)), false);
  assert.equal(isUploadSessionLifetimeExceeded(new UploadHttpError("some other conflict", 409)), false);
  assert.equal(isUploadSessionLifetimeExceeded(new UploadUrlExpiredError("expired")), false);
});

test("uploadPartsWithAutoResign(a): auto-resigns once on a expired URL and finishes without user input", async (context) => {
  const calls = [];
  context.mock.method(globalThis, "fetch", async (url) => {
    calls.push(url);
    if (url === "https://r2.invalid/2-v0") return new Response(null, { status: 403 });
    return new Response(null, { status: 200, headers: { ETag: `etag-${url}` } });
  });
  globalThis.sessionStorage = { setItem: () => {} };
  const file = new File(["abcd"], "fixture.mp4", { type: "video/mp4", lastModified: 1 });
  const session = baseSession({ size: file.size });
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const initial = {
    upload_uuid: session.uploadUuid,
    status: "uploading",
    part_size_bytes: 2,
    parts: [
      { part_number: 1, url: "https://r2.invalid/1-v0" },
      { part_number: 2, url: "https://r2.invalid/2-v0" },
    ],
    expires_at: expiresAt,
    asset_id: null,
    error_code: "",
  };
  let resignCalls = 0;
  const resign = async () => {
    resignCalls += 1;
    return {
      upload_uuid: session.uploadUuid,
      status: "uploading",
      part_size_bytes: 2,
      parts: [{ part_number: 2, url: "https://r2.invalid/2-v1" }],
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      asset_id: null,
      error_code: "",
    };
  };

  const etags = await uploadPartsWithAutoResign(
    file,
    initial,
    session,
    resign,
    new AbortController().signal,
    () => {},
  );

  assert.equal(resignCalls, 1);
  assert.equal(Object.keys(etags).length, 2);
  assert.deepEqual(calls, ["https://r2.invalid/1-v0", "https://r2.invalid/2-v0", "https://r2.invalid/2-v1"]);
});

test("uploadPartsWithAutoResign(c): does not auto-resign again when no new part completed since the last resign", async (context) => {
  context.mock.method(globalThis, "fetch", async (url) => {
    if (url.endsWith("-sentinel")) return new Response(null, { status: 403 });
    return new Response(null, { status: 200, headers: { ETag: `etag-${url}` } });
  });
  globalThis.sessionStorage = { setItem: () => {} };
  const file = new File(["abcd"], "fixture.mp4", { type: "video/mp4", lastModified: 1 });
  const session = baseSession({ size: file.size });
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const initial = {
    upload_uuid: session.uploadUuid,
    status: "uploading",
    part_size_bytes: 2,
    parts: [
      { part_number: 1, url: "https://r2.invalid/1-ok" },
      { part_number: 2, url: "https://r2.invalid/2-sentinel" },
    ],
    expires_at: expiresAt,
    asset_id: null,
    error_code: "",
  };
  let resignCalls = 0;
  const resign = async () => {
    resignCalls += 1;
    return {
      upload_uuid: session.uploadUuid,
      status: "uploading",
      part_size_bytes: 2,
      // part 2's URL keeps expiring on every re-issue: no forward progress is possible.
      parts: [{ part_number: 2, url: "https://r2.invalid/2-sentinel" }],
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      asset_id: null,
      error_code: "",
    };
  };

  await assert.rejects(
    uploadPartsWithAutoResign(file, initial, session, resign, new AbortController().signal, () => {}),
    (error) => error instanceof UploadUrlExpiredError,
  );

  assert.equal(resignCalls, 1, "the first resign is allowed, a second one is refused for lack of progress");
  assert.deepEqual(Object.keys(session.etags), ["1"]);
});

test("uploadPartsWithAutoResign(b): stops once the automatic resign cap is reached", async (context) => {
  let sentinelAttempts = 0;
  context.mock.method(globalThis, "fetch", async (url) => {
    if (url.startsWith("https://r2.invalid/sentinel-")) {
      sentinelAttempts += 1;
      return new Response(null, { status: 403 });
    }
    return new Response(null, { status: 200, headers: { ETag: `etag-${url}` } });
  });
  globalThis.sessionStorage = { setItem: () => {} };
  const file = new File(["abcd"], "fixture.mp4", { type: "video/mp4", lastModified: 1 });
  const session = baseSession({ size: file.size });
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const initial = {
    upload_uuid: session.uploadUuid,
    status: "uploading",
    part_size_bytes: 2,
    parts: [
      { part_number: 1, url: "https://r2.invalid/1-ok" },
      { part_number: 2, url: "https://r2.invalid/sentinel-v0" },
    ],
    expires_at: expiresAt,
    asset_id: null,
    error_code: "",
  };
  let resignCalls = 0;
  const resign = async () => {
    resignCalls += 1;
    return {
      upload_uuid: session.uploadUuid,
      status: "uploading",
      part_size_bytes: 2,
      parts: [{ part_number: 2, url: `https://r2.invalid/sentinel-v${resignCalls}` }],
      expires_at: new Date(Date.now() + 60_000).toISOString(),
      asset_id: null,
      error_code: "",
    };
  };

  await assert.rejects(
    uploadPartsWithAutoResign(file, initial, session, resign, new AbortController().signal, () => {}, 1),
    (error) => error instanceof UploadUrlExpiredError,
  );

  assert.equal(resignCalls, 1, "maxAttempts=1 permits exactly one automatic resign");
  assert.equal(sentinelAttempts, 2, "the still-failing part is retried once more with the resigned URL, then it stops");
});

test("uploadPartsWithAutoResign(d): an abort while waiting for the auto-resign stops the upload", async (context) => {
  context.mock.method(globalThis, "fetch", async () => new Response(null, { status: 403 }));
  globalThis.sessionStorage = { setItem: () => {} };
  const file = new File(["ab"], "fixture.mp4", { type: "video/mp4", lastModified: 1 });
  const session = baseSession({ size: file.size });
  const expiresAt = new Date(Date.now() + 60_000).toISOString();
  const initial = {
    upload_uuid: session.uploadUuid,
    status: "uploading",
    part_size_bytes: 2,
    parts: [{ part_number: 1, url: "https://r2.invalid/1" }],
    expires_at: expiresAt,
    asset_id: null,
    error_code: "",
  };
  const abortController = new AbortController();
  let resignInvoked = false;
  const resign = () => {
    resignInvoked = true;
    return new Promise((_resolve, reject) => {
      abortController.signal.addEventListener(
        "abort",
        () => reject(new DOMException("Aborted", "AbortError")),
        { once: true },
      );
      // Simulates the user hitting "pause" while the resign network call is in flight.
      abortController.abort();
    });
  };

  await assert.rejects(
    uploadPartsWithAutoResign(file, initial, session, resign, abortController.signal, () => {}),
    (error) => error.name === "AbortError",
  );

  assert.equal(resignInvoked, true);
});
