// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, StudioPage } from "../atoms";

import type { RundownTemplateOut } from "../hooks";
import { RouterLink } from "../links";
import { RundownEditor } from "./RundownEditor";

/** 定番進行表テンプレ エディタ (タイムキープ Phase3 C / #25 §9)。SeriesSlot の雛形
 * (LiveRundownTemplate/…Cue) を編集する。expand_series_slots が展開時にこの雛形を各回の
 * LiveRundown/LiveCue へ複製する。UI は生キューシートと同じ RundownEditor を共有し、雛形は
 * state を持たないため 状態 列を出さない (showState=false)。 */
export function RundownTemplatePage() {
  const { slug = "", slotId = "" } = useParams();
  const [d, setD] = useState<RundownTemplateOut | null>(null);
  const [err, setErr] = useState("");

  const load = useCallback(() => {
    if (!slug || !slotId) return;
    api
      .GET("/api/v1/admin/scheduling/{slug}/rundown-template/{slot_id}", {
        params: { path: { slug, slot_id: Number(slotId) } },
      })
      .then(({ data, error }) => (error ? setErr("読み込み失敗 (staff 権限が必要)") : data && setD(data)))
      .catch(() => setErr("読み込み失敗"));
  }, [slug, slotId]);
  useEffect(load, [load]);

  if (err && !d) return <EmptyState loading>{err}</EmptyState>;
  if (!d) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title={`定番進行表 — ${d.slot_label}`}
      breadcrumb={
        <Breadcrumb items={[{ label: "週間編成", href: `/series` }]} linkComponent={RouterLink} />
      }
      actions={<span className="muted">{d.channel.name}</span>}
    >
      <RundownEditor
        data={d}
        reload={load}
        addUrl={`/scheduling/ch/${slug}/slots/${slotId}/template-cues/add/`}
        cueUrl={(cueId, action) => `/scheduling/ch/${slug}/template-cues/${cueId}/${action}/`}
        showState={false}
      />
    </StudioPage>
  );
}
