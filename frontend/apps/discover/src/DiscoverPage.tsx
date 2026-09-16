// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useEffect, useState } from "react";

import { api } from "@icstv/api";
import { Button, Chip, Hero, SearchResultList, SegmentedNav, TextField } from "./atoms";

import type { BrowseOut, CardItem, SearchOut, VodOut } from "./hooks";

/** 取得待ちのカード骨組み (SSR shell と同形・public_base の sk-* グローバル CSS を使う)。 */
function SkelGrid({ n = 6 }: { n?: number }) {
  return (
    <div className="sk-grid" aria-hidden="true">
      {Array.from({ length: n }, (_, i) => (
        <div className="sk-card" key={i}>
          <div className="sk sk-thumb" />
          <div className="sk-body">
            <div className="sk sk-line" style={{ width: "42%" }} />
            <div className="sk sk-line" style={{ width: "88%" }} />
            <div className="sk sk-line" style={{ width: "60%" }} />
          </div>
        </div>
      ))}
    </div>
  );
}

/** 見逃し/ブラウズ共用カード。バッジ (ゲート/ロック/尺/YouTube) は public_base のグローバル CSS。 */
function VodCard({ c, href }: { c: CardItem; href: string }) {
  return (
    <a className="vodcard" href={href}>
      <div className="vthumb">
        {c.thumb_url ? <img src={c.thumb_url} alt="" loading="lazy" /> : <span className="ph">ICS-TV</span>}
        {c.badge === "yt" && <span className="gate yt">YouTube</span>}
        {c.vod_visibility === "members" && <span className="gate members">会員限定</span>}
        {c.vod_visibility === "subscribers" && <span className="gate subscribers">サブスク限定</span>}
        {c.can_watch === false && <span className="lock">🔒</span>}
        {c.duration && <span className="dur tabnum">{c.duration}</span>}
      </div>
      <div className="vbody">
        <div className="vrow">
          <span className="nm">{c.channel_name}</span>
          {c.genre && <Chip label={c.genre} genre className="g" />}
        </div>
        <div className="vttl">{c.title}</div>
        <div className="vmeta tabnum">{c.start_display}</div>
      </div>
    </a>
  );
}

/** 検索: 入力に応じて 250ms デバウンスで /api/v1/search、?q= を URL に反映 (live search)。 */
function SearchView({ homeBase, initialQ }: { homeBase: string; initialQ: string }) {
  const [q, setQ] = useState(initialQ);
  const [data, setData] = useState<SearchOut | null>(null);

  useEffect(() => {
    const h = window.setTimeout(() => {
      api
        .GET("/api/v1/search", { params: { query: { q } } })
        .then(({ data }) => {
          if (data) setData(data);
        })
        .catch(() => {});
      history.replaceState(
        null,
        "",
        q.trim() ? `${location.pathname}?q=${encodeURIComponent(q)}` : location.pathname,
      );
    }, 250);
    return () => window.clearTimeout(h);
  }, [q]);

  const trimmed = q.trim();
  return (
    <section className="sr">
      <h1>{trimmed ? `「${trimmed}」の検索結果` : "検索"}</h1>
      <TextField
        value={q}
        onChange={setQ}
        placeholder="番組名・あらすじ・出演者・ジャンルで検索"
        ariaLabel="番組を検索"
        autoFocus
      />
      {!trimmed ? (
        <p className="lead">番組名・あらすじ・出演者・ジャンルから検索できます。</p>
      ) : !data ? (
        <p className="lead">検索中…</p>
      ) : (
        <>
          <p className="lead">
            {data.programs.length} 件の番組
            {data.channels.length ? ` ・ ${data.channels.length} 件のチャンネル` : ""}
          </p>
          {data.channels.length > 0 && (
            <>
              <h2>チャンネル</h2>
              <div className="sr-chs">
                {data.channels.map((c) => (
                  <a key={c.slug} href={`${homeBase}/ch/${c.slug}/`}>
                    <span className="dot" style={{ background: c.tint }} />
                    {c.name}
                  </a>
                ))}
              </div>
            </>
          )}
          {data.programs.length > 0 ? (
            <>
              <h2>番組</h2>
              <SearchResultList
                items={data.programs.map((p) => ({
                  id: p.id,
                  time: p.start_display,
                  title: p.title,
                  channel: p.channel_name,
                  genre: p.genre ?? undefined,
                  href: `${homeBase}/program/${p.id}/`,
                }))}
              />
            </>
          ) : data.channels.length === 0 ? (
            <div className="sr-empty">
              「{trimmed}」に一致する番組・チャンネルは見つかりませんでした。
            </div>
          ) : null}
        </>
      )}
    </section>
  );
}

/** ジャンルブラウズ: タブ選択で /api/v1/browse?genre=、?genre= を pushState で反映。 */
function BrowseView({ homeBase, initialGenre }: { homeBase: string; initialGenre: string }) {
  const [genre, setGenre] = useState(initialGenre);
  const [data, setData] = useState<BrowseOut | null>(null);

  useEffect(() => {
    api
      .GET("/api/v1/browse", { params: { query: { genre } } })
      .then(({ data }) => {
        if (data) setData(data);
      })
      .catch(() => {});
  }, [genre]);

  function pick(g: string) {
    setGenre(g);
    history.pushState(null, "", `${location.pathname}?genre=${encodeURIComponent(g)}`);
  }

  if (!data) {
    return (
      <section className="br">
        <h1>ジャンルから探す</h1>
        <p className="lead">放送予定・放送済みの番組をジャンル別に一覧できます。</p>
        <div className="sk-chips" aria-hidden="true">
          {Array.from({ length: 5 }, (_, i) => (
            <div className="sk sk-chip" key={i} />
          ))}
        </div>
        <SkelGrid />
      </section>
    );
  }
  return (
    <section className="br">
      <h1>ジャンルから探す</h1>
      <p className="lead">放送予定・放送済みの番組をジャンル別に一覧できます。</p>
      {data.genres.length > 0 && (
        <SegmentedNav
          variant="chips"
          ariaLabel="ジャンル"
          items={data.genres.map((g) => ({
            key: g,
            label: g,
            selected: g === data.genre,
            href: `?genre=${encodeURIComponent(g)}`,
            onClick: (e) => {
              e.preventDefault();
              pick(g);
            },
          }))}
        />
      )}
      {data.genre ? (
        data.programs.length > 0 ? (
          <div className="vod-grid">
            {data.programs.map((c) => (
              <VodCard
                key={c.series_id ?? c.id}
                c={c}
                href={c.series_id ? `${homeBase}/series/${c.series_id}/` : `${homeBase}/program/${c.id}/`}
              />
            ))}
          </div>
        ) : (
          <div className="br-empty">「{data.genre}」の番組は見つかりませんでした。</div>
        )
      ) : data.genres.length === 0 ? (
        <div className="br-empty">ジャンルの付いた公開番組がまだありません。</div>
      ) : (
        <div className="br-empty">上のジャンルを選んでください。</div>
      )}
    </section>
  );
}

/** 見逃しトップの featured Hero(編集選択 is_featured・slot 式 Hero アトムを消費)。VOD はライブでない
 * ため live/進行/カウントダウンは無し。ポスター + チャンネル/ジャンル + タイトル + 再生 CTA。 */
function FeaturedVodHero({ c, homeBase }: { c: CardItem; homeBase: string }) {
  return (
    <Hero
      href={`${homeBase}/vod/${c.id}/`}
      media={c.thumb_url ? <img className="hero-poster" src={c.thumb_url} alt="" /> : undefined}
      chips={c.genre ? <Chip label={c.genre} genre /> : undefined}
      title={c.title}
      meta={
        <span>
          {c.channel_name} ・ {c.start_display}
          {c.duration ? ` ・ ${c.duration}` : ""}
        </span>
      }
      actions={
        <Button variant="play" as="span">
          ▶ 再生
        </Button>
      }
    />
  );
}

/** 見逃し一覧: /api/v1/vod (録画 + YouTube アーカイブ)。 */
function VodView({ homeBase }: { homeBase: string }) {
  const [data, setData] = useState<VodOut | null>(null);
  useEffect(() => {
    api
      .GET("/api/v1/vod")
      .then(({ data }) => {
        if (data) setData(data);
      })
      .catch(() => {});
  }, []);

  return (
    <section className="vod">
      <h1>見逃し配信</h1>
      <p className="lead">放送が終わった番組をオンデマンドで視聴できます。</p>
      {!data && <SkelGrid />}
      {data?.featured && <FeaturedVodHero c={data.featured} homeBase={homeBase} />}
      {data && (
        <>
          {data.items.length > 0 && (
            <div className="vod-grid">
              {data.items.map((c) => (
                <VodCard key={c.id} c={c} href={`${homeBase}/vod/${c.id}/`} />
              ))}
            </div>
          )}
          {data.live_archives.length > 0 && (
            <>
              <h2 className="vod-h2">ライブ配信の見逃し（YouTube）</h2>
              <div className="vod-grid">
                {data.live_archives.map((c) => (
                  <VodCard key={c.id} c={c} href={`${homeBase}/program/${c.id}/`} />
                ))}
              </div>
            </>
          )}
          {data.items.length === 0 && data.live_archives.length === 0 && (
            <div className="vod-empty">現在、見逃し配信できる番組はありません。</div>
          )}
        </>
      )}
    </section>
  );
}

export function DiscoverPage({
  mode,
  homeBase,
}: {
  mode: "search" | "browse" | "vod";
  homeBase: string;
}) {
  const params = new URLSearchParams(location.search);
  if (mode === "search") return <SearchView homeBase={homeBase} initialQ={params.get("q") ?? ""} />;
  if (mode === "browse")
    return <BrowseView homeBase={homeBase} initialGenre={params.get("genre") ?? ""} />;
  return <VodView homeBase={homeBase} />;
}
