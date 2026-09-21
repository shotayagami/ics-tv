// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * 本線ライブ HLS の署名トークン (#27) をフロント側で扱うユーティリティ。
 *
 * サーバは完全公開の番組を含め、すべての再生 URL に `?token=` を付けて返す
 * (server/scheduling/exposure_gate.py::hls_url_for)。トークンは有効期限付きで、
 * API を再取得するたびに新しい値になる。そのため:
 *
 * - **同一性の判定にはトークンを除いた URL を使う** (stripHlsToken)。そうしないと
 *   ポーリングのたびに URL が変わり、`useEffect([hlsUrl])` が毎回プレイヤーを作り直して
 *   再バッファリングが起きる。
 * - **リクエスト直前に最新トークンへ差し替える** (createHlsTokenLoader)。トークンの TTL は
 *   エンタイトルメント変更がエッジへ効くまでの遅延そのものなので短く保ちたい一方、
 *   manifest に焼かれた古いトークンのままではその TTL で視聴が切れてしまう。
 */

const TOKEN_PARAM = "token";
const FALLBACK_BASE = "https://invalid.local";

function parse(url: string): URL | null {
  try {
    return new URL(url, FALLBACK_BASE);
  } catch {
    return null;
  }
}

function serialize(url: URL, original: string): string {
  const s = url.toString();
  return s.startsWith(FALLBACK_BASE) && !original.startsWith(FALLBACK_BASE)
    ? s.slice(FALLBACK_BASE.length)
    : s;
}

/** token を除いた URL。値が変わらない限りプレイヤーを作り直さないための同一性キー。 */
export function stripHlsToken(url: string): string {
  const u = parse(url);
  if (!u) return url;
  u.searchParams.delete(TOKEN_PARAM);
  return serialize(u, url);
}

/** URL に埋まっている token (無ければ空文字)。 */
export function hlsTokenOf(url: string): string {
  const u = parse(url);
  return u?.searchParams.get(TOKEN_PARAM) ?? "";
}

/** URL の token を差し替える (無ければ付ける)。token が空なら URL をそのまま返す。 */
export function withHlsToken(url: string, token: string): string {
  if (!token) return url;
  const u = parse(url);
  if (!u) return url;
  u.searchParams.set(TOKEN_PARAM, token);
  return serialize(u, url);
}

type HlsLoaderContext = { url: string };
// hls.js の Loader<LoaderContext> 型をそのまま受けて同じ型を返すため、コンストラクタ型を
// 素通しするジェネリックにする (packages/api に hls.js への依存を持ち込まないための措置)。
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type AnyLoaderCtor = new (...args: any[]) => any;

/**
 * hls.js のローダーを包み、プレイリスト/セグメントの取得直前に URL の token を最新へ差し替える。
 *
 * hls.js は manifest に書かれた URI をそのまま使い続けるため、これが無いと最初に読み込んだ
 * トークンの期限切れと同時に再生が止まる。Safari のネイティブ HLS (`<video src>`) には
 * この介入点が無く、TTL を超える連続視聴で切れる (deploy/cloudflare-worker-hls/README.md 参照)。
 *
 * 使い方: `new Hls({ loader: createHlsTokenLoader(Hls.DefaultConfig.loader, () => tokenRef.current) })`
 */
export function createHlsTokenLoader<T extends AnyLoaderCtor>(Base: T, getToken: () => string): T {
  const HlsTokenLoader = class extends Base {
    load(context: HlsLoaderContext, config: unknown, callbacks: unknown): void {
      const token = getToken();
      if (token) context.url = withHlsToken(context.url, token);
      super.load(context, config, callbacks);
    }
  };
  return HlsTokenLoader as unknown as T;
}
