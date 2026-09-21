// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { Button, Chip, Hero, LiveBadge } from "./atoms";
import { LiveBackground } from "./LiveBackground";
import { type HomeCard, useHomeData } from "./hooks";
import { Countdown, LiveProgress } from "./ticker";

/** 公開トップの主役ブロック。HLS(LiveBackground)/カウントダウン/進行バーのロジックは home 側に残す。 */
function FeaturedHero({ c, homeBase }: { c: HomeCard; homeBase: string }) {
  return (
    <Hero
      href={`/ch/${c.slug}/`}
      live={c.live}
      media={
        <LiveBackground
          hlsUrl={c.hls_url}
          online={c.live}
          poster={c.poster}
          logoSize="clamp(40px,9vw,120px)"
        />
      }
      bug={
        <>
          <i style={{ background: c.tint }} />
          {c.short}
        </>
      }
      chips={
        <>
          <Chip label={c.name} color={c.tint} />
          {c.is_rerun && <Chip label="再放送" />}
          {c.genre && <Chip label={c.genre} genre />}
        </>
      }
      title={c.nowtitle}
      meta={
        <>
          <span>放送中 ・ {c.time_range}</span>
          <span className="cd">
            次の番組まで{" "}
            <span className="tabnum" style={{ fontWeight: 800 }}>
              <Countdown start={c.cur_start_ts} end={c.cur_end_ts} />
            </span>
          </span>
        </>
      }
      progress={
        <>
          <LiveProgress start={c.cur_start_ts} end={c.cur_end_ts} />
          <div className="lbl">
            <span>{c.nowtitle}</span>
            <span>
              次：{c.next_title} <span style={{ color: "var(--bright2)" }}>{c.next_time}</span>
            </span>
          </div>
        </>
      }
      actions={
        <>
          <Button variant="play" as="span">
            <svg width="15" height="15" viewBox="0 0 12 12">
              <path d="M2.5 1.5 L10 6 L2.5 10.5 Z" fill="#04101a" />
            </svg>
            視聴する
          </Button>
          <Button
            variant="ghost"
            as="span"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              location.href = `${homeBase}/guide/`;
            }}
          >
            番組表を見る
          </Button>
        </>
      }
    />
  );
}

function Card({ c }: { c: HomeCard }) {
  return (
    <a className="chcard" href={`/ch/${c.slug}/`}>
      <div className="thumb">
        <LiveBackground hlsUrl={c.hls_url} online={c.live} poster={c.poster} />
        {c.live && <LiveBadge />}
        <div className="bug">
          <i style={{ background: c.tint }} />
          {c.short}
        </div>
        <LiveProgress start={c.cur_start_ts} end={c.cur_end_ts} />
      </div>
      <div className="body">
        <div className="row1">
          <span className="dot" style={{ background: c.tint }} />
          <span className="nm">{c.name}</span>
          {c.is_rerun && <span className="gtag">再放送</span>}
          {c.genre && <span className="gtag">{c.genre}</span>}
        </div>
        <div className="ttl">{c.nowtitle}</div>
        <div className="times">
          <span>{c.time_range}</span>
          <span className="rem">
            残り{" "}
            <span className="tabnum">
              <Countdown start={c.cur_start_ts} end={c.cur_end_ts} />
            </span>
          </span>
        </div>
        {c.next_title && (
          <div className="nextrow">
            <span className="ntag">NEXT</span>
            <span className="nt">{c.next_title}</span>
            <span className="ntime">{c.next_time}</span>
          </div>
        )}
      </div>
    </a>
  );
}

export function HomePage({ homeBase }: { homeBase: string }) {
  const data = useHomeData();
  if (!data) return null; // 初期ロード中は何も出さない (下部 SSR セクションは表示済み)

  return (
    <>
      {data.featured && <FeaturedHero c={data.featured} homeBase={homeBase} />}
      <div className="sec-head">
        <h2>チャンネル</h2>
        <a href={`${homeBase}/guide/`}>すべての番組表 ›</a>
      </div>
      {data.cards.length > 0 ? (
        <div className="ch-grid">
          {data.cards.map((c) => (
            <Card key={c.slug} c={c} />
          ))}
        </div>
      ) : (
        <p className="muted">公開中のチャンネルがありません。</p>
      )}
    </>
  );
}
