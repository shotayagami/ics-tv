// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
const API_ROOT = "/api/v1/admin/media-uploads";
const SESSION_KEY = "icstv.studio.media-upload.v1";

export type UploadKind = "program" | "cm" | "filler" | "bumper" | "slate";

export type UploadPart = {
  part_number: number;
  url: string;
};

export type UploadResponse = {
  upload_uuid: string;
  status: string;
  part_size_bytes: number;
  parts: UploadPart[];
  expires_at: string;
  asset_id: number | null;
  error_code: string;
};

export type UploadSession = {
  idempotencyKey: string;
  uploadUuid: string;
  title: string;
  kind: UploadKind;
  filename: string;
  contentType: string;
  size: number;
  lastModified: number;
  sha256: string;
  expiresAt: string;
  etags: Record<string, string>;
};

export class UploadHttpError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export class UploadUrlExpiredError extends Error {}

function readCookie(name: string): string {
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${name}=([^;]+)`));
  return match ? decodeURIComponent(match[1]) : "";
}

async function requestJson<T>(url: string, init: RequestInit): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Content-Type", "application/json");
  const csrf = readCookie("csrftoken");
  if (csrf) headers.set("X-CSRFToken", csrf);
  const response = await fetch(url, { ...init, headers, credentials: "same-origin" });
  const body = (await response.json().catch(() => ({}))) as { detail?: string; error?: string } & T;
  if (!response.ok) {
    throw new UploadHttpError(body.detail || body.error || `HTTP ${response.status}`, response.status);
  }
  return body;
}

export function loadUploadSession(): UploadSession | null {
  try {
    const value = sessionStorage.getItem(SESSION_KEY);
    if (!value) return null;
    const parsed = JSON.parse(value) as UploadSession;
    if (!parsed.idempotencyKey || !parsed.uploadUuid || !parsed.sha256 || !parsed.etags) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveUploadSession(session: UploadSession): void {
  sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
}

export function clearUploadSession(): void {
  sessionStorage.removeItem(SESSION_KEY);
}

export function contentTypeFor(file: File): string {
  const declared = file.type.trim().toLowerCase();
  if (["video/mp4", "video/quicktime", "video/webm", "video/x-matroska"].includes(declared)) {
    return declared;
  }
  const ext = file.name.toLowerCase().split(".").pop();
  const inferred: Record<string, string> = {
    mp4: "video/mp4",
    mov: "video/quicktime",
    webm: "video/webm",
    mkv: "video/x-matroska",
  };
  return (ext && inferred[ext]) || "";
}

export function sessionMatchesFile(session: UploadSession, file: File): boolean {
  return (
    session.filename === file.name &&
    session.size === file.size &&
    session.lastModified === file.lastModified &&
    session.contentType === contentTypeFor(file)
  );
}

export async function startUpload(
  file: File,
  title: string,
  kind: UploadKind,
  sha256: string,
  idempotencyKey: string,
  signal: AbortSignal,
): Promise<UploadResponse> {
  return requestJson(`${API_ROOT}/start`, {
    method: "POST",
    signal,
    headers: { "Idempotency-Key": idempotencyKey },
    body: JSON.stringify({
      kind,
      title,
      original_filename: file.name,
      content_type: contentTypeFor(file),
      expected_size_bytes: file.size,
      expected_sha256: sha256,
    }),
  });
}

export function getUploadStatus(uploadUuid: string, signal: AbortSignal): Promise<UploadResponse> {
  return requestJson(`${API_ROOT}/${uploadUuid}`, { method: "GET", signal });
}

export function completeUpload(
  uploadUuid: string,
  etags: Record<string, string>,
  signal: AbortSignal,
): Promise<UploadResponse> {
  const parts = Object.entries(etags)
    .map(([partNumber, etag]) => ({ part_number: Number(partNumber), etag }))
    .sort((a, b) => a.part_number - b.part_number);
  return requestJson(`${API_ROOT}/${uploadUuid}/complete`, {
    method: "POST",
    signal,
    body: JSON.stringify({ parts }),
  });
}

// P6-fixes B: サーバ側のcomplete_multipart一時失敗はverifyingのまま残り(object_etag_or_version未
// 設定)、同じsessionへの再complete要求で復旧できる。だが再開時にstartUploadが返す"verifying"は
// 既にcomplete受付済みの状態と区別できず、呼び出し側は素朴にはpollingへ直行してしまう
// (サーバ側は前回のcompleteが本当に失敗していたかを知っているが、クライアントは知らない)。
// ローカルsessionに全part分のetagsが残っていれば無害に再送できるので先に試す。失敗しても
// nullを返すだけにし、呼び出し側は既存のpollingへフォールバックする(最終的にはcleanup beatが
// 滞留したsessionを回収する)。
export async function resumeAfterVerifying(
  uploadUuid: string,
  session: UploadSession | null,
  signal: AbortSignal,
): Promise<UploadResponse | null> {
  if (!session?.etags || Object.keys(session.etags).length === 0) return null;
  try {
    return await completeUpload(uploadUuid, session.etags, signal);
  } catch {
    return null;
  }
}

export function abortUpload(uploadUuid: string): Promise<UploadResponse> {
  return requestJson(`${API_ROOT}/${uploadUuid}/abort`, {
    method: "POST",
    body: "{}",
  });
}

const RETRYABLE_STATUS = new Set([408, 425, 429, 500, 502, 503, 504]);

function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

async function putPart(url: string, body: Blob, signal: AbortSignal): Promise<string> {
  const maxAttempts = 4;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      const response = await fetch(url, {
        method: "PUT",
        body,
        credentials: "omit",
        redirect: "error",
        signal,
      });
      if (response.status === 401 || response.status === 403) {
        throw new UploadUrlExpiredError("アップロードURLの有効期限が切れました");
      }
      if (!response.ok) {
        if (!RETRYABLE_STATUS.has(response.status) || attempt === maxAttempts) {
          throw new UploadHttpError(`パート送信に失敗しました (HTTP ${response.status})`, response.status);
        }
        await response.body?.cancel();
      } else {
        const etag = response.headers.get("ETag");
        if (!etag) throw new Error("R2 CORSでETagヘッダーが公開されていません");
        return etag;
      }
    } catch (error) {
      if (error instanceof UploadUrlExpiredError || error instanceof UploadHttpError) throw error;
      if (signal.aborted) throw error;
      if (attempt === maxAttempts) throw error;
    }
    await abortableDelay(500 * 2 ** (attempt - 1), signal);
  }
  throw new Error("パート送信に失敗しました");
}

export async function uploadMissingParts(
  file: File,
  response: UploadResponse,
  session: UploadSession,
  signal: AbortSignal,
  onProgress: (completedBytes: number, totalBytes: number) => void,
): Promise<Record<string, string>> {
  if (Date.parse(response.expires_at) <= Date.now() + 5_000) {
    throw new UploadUrlExpiredError("アップロードセッションの有効期限が切れました");
  }
  const urls = new Map(response.parts.map((part) => [part.part_number, part.url]));
  const partCount = Math.ceil(file.size / response.part_size_bytes);
  const pending = Array.from({ length: partCount }, (_, index) => index + 1).filter(
    (partNumber) => !session.etags[String(partNumber)],
  );
  let completedBytes = Object.keys(session.etags).reduce((sum, raw) => {
    const partNumber = Number(raw);
    const start = (partNumber - 1) * response.part_size_bytes;
    return sum + Math.max(0, Math.min(response.part_size_bytes, file.size - start));
  }, 0);
  onProgress(completedBytes, file.size);

  const partController = new AbortController();
  const relayAbort = () => partController.abort();
  if (signal.aborted) relayAbort();
  else signal.addEventListener("abort", relayAbort, { once: true });
  let next = 0;
  const worker = async () => {
    while (next < pending.length) {
      const partNumber = pending[next++];
      const url = urls.get(partNumber);
      if (!url) throw new Error(`パート${partNumber}の署名URLがありません`);
      const start = (partNumber - 1) * response.part_size_bytes;
      const end = Math.min(start + response.part_size_bytes, file.size);
      const etag = await putPart(url, file.slice(start, end), partController.signal);
      session.etags[String(partNumber)] = etag;
      saveUploadSession(session);
      completedBytes += end - start;
      onProgress(completedBytes, file.size);
    }
  };
  try {
    await Promise.all(Array.from({ length: Math.min(3, pending.length) }, worker));
    return session.etags;
  } catch (error) {
    partController.abort();
    throw error;
  } finally {
    signal.removeEventListener("abort", relayAbort);
  }
}

// D-auto-resign: 署名URL自体の寿命(最大1時間、サーバのpart_urlsが刻むmin(3600,remaining))は、
// セッション通算上限(既定24時間、MEDIA_UPLOAD_SESSION_MAX_SECONDS)より先に切れうる。失効時は
// 同じidempotencyKeyでstartUploadを呼び直せば新しいパートURLを取得できるが、サーバの
// _maybe_extend_ttl()は前回チェック以降にproviderへ新規完了パートが届いていない限り期限を
// 延長しない。無進捗のまま再署名を繰り返すと、延びない期限に向かって空回りするだけになる。
//   - 最初の再署名は常に許可する(サーバのlast_progress_part_count初期値0と同じ発想。ゼロから
//     でも新しい署名URL自体は取得できる)。
//   - 2回目以降は、直前の再署名以降に新たに完了したパートが無ければ許可しない(利用者に委ねる)。
//   - 無進捗ガードをすり抜け続けるケース(順調に進捗しつつ延々と再署名が必要になる異常系)への
//     二次防御として、通算の再署名回数にも上限を設ける。
// 上限値の根拠: 個々の署名URLの寿命は最大1時間、セッション通算上限は既定24時間なので、
// 進捗を伴う再署名サイクルは最大でも24回程度で足りる。この比をそのまま上限に採用した。
export const MAX_AUTO_RESIGN_ATTEMPTS = 24;

export function shouldAutoResign(
  resignCount: number,
  completedAtLastResign: number,
  completedNow: number,
  maxAttempts: number = MAX_AUTO_RESIGN_ATTEMPTS,
): boolean {
  if (resignCount >= maxAttempts) return false;
  if (resignCount === 0) return true;
  return completedNow > completedAtLastResign;
}

const SESSION_LIFETIME_EXCEEDED_MESSAGE = "upload session has expired";

// start_upload再呼び出し(_part_urls)がセッション通算上限到達で返す409は、署名URLの単純な失効
// と違い再署名しても回復しない。呼び出し側はこれを区別して利用者に案内する(要件6)。
export function isUploadSessionLifetimeExceeded(error: unknown): boolean {
  return (
    error instanceof UploadHttpError &&
    error.status === 409 &&
    error.message === SESSION_LIFETIME_EXCEEDED_MESSAGE
  );
}

// uploadMissingPartsをラップし、署名URL失効(UploadUrlExpiredError)を利用者操作なしに
// startUpload再呼び出し(resign)で回復して送信を継続する。同じsession(=同じidempotencyKey経由で
// 得たsession)を使い続けるので、完了済みパートの再送はuploadMissingPartsの既存フィルタが防ぐ
// (要件2)。resignが投げるエラー(セッション通算上限到達の409やAbort)はそのまま呼び出し元へ
// 伝播させ、既存のエラー表示経路に委ねる(要件6・7)。
export async function uploadPartsWithAutoResign(
  file: File,
  initial: UploadResponse,
  session: UploadSession,
  resign: () => Promise<UploadResponse>,
  signal: AbortSignal,
  onProgress: (completedBytes: number, totalBytes: number) => void,
  maxAttempts: number = MAX_AUTO_RESIGN_ATTEMPTS,
): Promise<Record<string, string>> {
  let response = initial;
  let resignCount = 0;
  let completedAtLastResign = Object.keys(session.etags).length;
  for (;;) {
    try {
      return await uploadMissingParts(file, response, session, signal, onProgress);
    } catch (error) {
      if (!(error instanceof UploadUrlExpiredError)) throw error;
      const completedNow = Object.keys(session.etags).length;
      if (!shouldAutoResign(resignCount, completedAtLastResign, completedNow, maxAttempts)) {
        throw error;
      }
      resignCount += 1;
      completedAtLastResign = completedNow;
      response = await resign();
      if (response.status !== "uploading" || response.parts.length === 0) {
        throw new Error(`再開できないセッション状態です: ${response.status}`);
      }
      session.expiresAt = response.expires_at;
      saveUploadSession(session);
    }
  }
}

export async function waitForCompletion(
  uploadUuid: string,
  signal: AbortSignal,
  onStatus: (status: string) => void,
): Promise<UploadResponse> {
  const deadline = Date.now() + 10 * 60_000;
  while (Date.now() < deadline) {
    const response = await getUploadStatus(uploadUuid, signal);
    onStatus(response.status);
    if (["completed", "failed", "aborted", "expired"].includes(response.status)) return response;
    await abortableDelay(2_000, signal);
  }
  throw new Error("検証完了の待機がタイムアウトしました。状態は後から再確認できます");
}

// Streaming SHA-256 avoids retaining a large media file in browser memory.
const SHA256_K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4,
  0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe,
  0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f,
  0x4a7484aa, 0x5cb0a9dc, 0x76f988da, 0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
  0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc,
  0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070, 0x19a4c116,
  0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7,
  0xc67178f2,
]);

class Sha256 {
  private readonly state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  private readonly block = new Uint8Array(64);
  private readonly words = new Uint32Array(64);
  private blockLength = 0;
  private bytes = 0;

  update(data: Uint8Array): void {
    this.bytes += data.length;
    let offset = 0;
    while (offset < data.length) {
      const take = Math.min(64 - this.blockLength, data.length - offset);
      this.block.set(data.subarray(offset, offset + take), this.blockLength);
      this.blockLength += take;
      offset += take;
      if (this.blockLength === 64) {
        this.transform();
        this.blockLength = 0;
      }
    }
  }

  digestHex(): string {
    const high = Math.floor(this.bytes / 0x20000000) >>> 0;
    const low = (this.bytes * 8) >>> 0;
    this.block[this.blockLength++] = 0x80;
    if (this.blockLength > 56) {
      this.block.fill(0, this.blockLength);
      this.transform();
      this.blockLength = 0;
    }
    this.block.fill(0, this.blockLength, 56);
    const view = new DataView(this.block.buffer);
    view.setUint32(56, high);
    view.setUint32(60, low);
    this.transform();
    return Array.from(this.state, (word) => word.toString(16).padStart(8, "0")).join("");
  }

  private transform(): void {
    const view = new DataView(this.block.buffer);
    for (let i = 0; i < 16; i += 1) this.words[i] = view.getUint32(i * 4);
    for (let i = 16; i < 64; i += 1) {
      const x = this.words[i - 15];
      const y = this.words[i - 2];
      const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
      const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
      this.words[i] = (this.words[i - 16] + s0 + this.words[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = this.state;
    for (let i = 0; i < 64; i += 1) {
      const s1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + s1 + ch + SHA256_K[i] + this.words[i]) >>> 0;
      const s0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (s0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    this.state[0] = (this.state[0] + a) >>> 0;
    this.state[1] = (this.state[1] + b) >>> 0;
    this.state[2] = (this.state[2] + c) >>> 0;
    this.state[3] = (this.state[3] + d) >>> 0;
    this.state[4] = (this.state[4] + e) >>> 0;
    this.state[5] = (this.state[5] + f) >>> 0;
    this.state[6] = (this.state[6] + g) >>> 0;
    this.state[7] = (this.state[7] + h) >>> 0;
  }
}

export async function sha256File(
  file: File,
  signal: AbortSignal,
  onProgress: (readBytes: number, totalBytes: number) => void,
): Promise<string> {
  const hash = new Sha256();
  const reader = file.stream().getReader();
  let readBytes = 0;
  try {
    while (true) {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const { done, value } = await reader.read();
      if (done) break;
      hash.update(value);
      readBytes += value.length;
      onProgress(readBytes, file.size);
    }
    return hash.digestHex();
  } finally {
    reader.releaseLock();
  }
}
