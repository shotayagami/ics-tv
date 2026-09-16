// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, StudioPage } from "../atoms";

import type { LiveRundownOut } from "../hooks";
import { RouterLink } from "../links";
import { RundownEditor } from "./RundownEditor";

/** 生キューシート エディタ (タイムキープ Phase1 §2.3)。生番組の進行表 (LiveRundown/LiveCue) を
 * 順序リストで編集。読み込み (typed api.GET) と StudioPage シェルだけを持ち、テーブル/フォーム/
 * 書き込みは RundownEditor へ委譲する (SeriesSlot の定番進行表テンプレと共通・Phase3 C)。 */
export function LiveRundownPage() {
  const { slug = "", programId = "" } = useParams();
  const [d, setD] = useState<LiveRundownOut | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    if (!slug || !programId) return;
    api
      .GET("/api/v1/admin/scheduling/{slug}/live-rundown/{program_id}", {
        params: { path: { slug, program_id: Number(programId) } },
      })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [slug, programId]);
  useEffect(load, [load]);

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title={`生キューシート — ${d.program_title}`}
      breadcrumb={
        <Breadcrumb
          items={[{ label: "番組編集", href: `/program-form/${slug}?program_id=${programId}` }]}
          linkComponent={RouterLink}
        />
      }
      actions={<span className="muted">{d.channel.name}</span>}
    >
      <RundownEditor
        data={d}
        reload={load}
        addUrl={`/scheduling/ch/${slug}/programs/${programId}/cues/add/`}
        cueUrl={(cueId, action) => `/scheduling/ch/${slug}/cues/${cueId}/${action}/`}
      />
    </StudioPage>
  );
}
