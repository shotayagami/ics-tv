// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { useMemo, useRef, useState } from "react";

import { Notice } from "../atoms";
import {
  abortUpload,
  clearUploadSession,
  completeUpload,
  contentTypeFor,
  isUploadSessionLifetimeExceeded,
  loadUploadSession,
  resumeAfterVerifying,
  saveUploadSession,
  sessionMatchesFile,
  sha256File,
  startUpload,
  uploadPartsWithAutoResign,
  UploadHttpError,
  UploadUrlExpiredError,
  waitForCompletion,
  type UploadKind,
  type UploadSession,
} from "../mediaUpload";

type Props = { onCompleted: () => void };

function percent(done: number, total: number): number {
  return total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
}

function errorMessage(error: unknown): string {
  if (isUploadSessionLifetimeExceeded(error)) {
    return "アップロードセッションの通算有効期限(24時間)に達しました。最初からやり直してください";
  }
  if (error instanceof UploadUrlExpiredError) return `${error.message}。再開すると署名URLを更新します`;
  if (error instanceof UploadHttpError) return error.message;
  if (error instanceof Error && error.name === "AbortError") return "一時停止しました";
  return error instanceof Error ? error.message : "アップロードに失敗しました";
}

export function MediaUploadPanel({ onCompleted }: Props) {
  const initial = useMemo(loadUploadSession, []);
  const [session, setSession] = useState<UploadSession | null>(initial);
  const [title, setTitle] = useState(initial?.title ?? "");
  const [kind, setKind] = useState<UploadKind>(initial?.kind ?? "program");
  const [file, setFile] = useState<File | null>(null);
  const [stage, setStage] = useState(initial ? "再開するファイルを選択してください" : "");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);
  const controller = useRef<AbortController | null>(null);

  async function run() {
    if (!file || !title.trim() || running) return;
    const contentType = contentTypeFor(file);
    if (!contentType) {
      setError("MP4、MOV、WebM、MKVの動画ファイルを選択してください");
      return;
    }
    if (session && !sessionMatchesFile(session, file)) {
      setError(`再開対象は「${session.filename}」です。同じファイルを選択するか、セッションを破棄してください`);
      return;
    }

    const abortController = new AbortController();
    controller.current = abortController;
    setRunning(true);
    setError("");
    try {
      setStage("SHA-256を計算中");
      const sha256 = await sha256File(file, abortController.signal, (done, total) => {
        setProgress(percent(done, total));
      });
      if (session && sha256 !== session.sha256) {
        throw new Error("選択したファイルの内容が再開対象と一致しません");
      }

      const idempotencyKey = session?.idempotencyKey ?? crypto.randomUUID();
      setStage(session ? "署名URLを更新中" : "アップロードを開始中");
      const started = await startUpload(
        file,
        session?.title ?? title.trim(),
        session?.kind ?? kind,
        sha256,
        idempotencyKey,
        abortController.signal,
      );
      if (started.status === "completed") {
        clearUploadSession();
        setSession(null);
        setStage("完了");
        setProgress(100);
        onCompleted();
        return;
      }
      if (started.status === "verifying") {
        // 前回のcomplete要求がサーバ側の一時失敗(503)で終わっていた場合、同じsessionのetagsで
        // 再送すると復旧できることがある(P6-fixes B)。失敗しても無視して通常のpollingへ進む。
        const retried = await resumeAfterVerifying(started.upload_uuid, session, abortController.signal);
        if (retried?.status === "completed") {
          clearUploadSession();
          setSession(null);
          setStage("完了");
          setProgress(100);
          onCompleted();
          return;
        }
        setStage("サーバで検証中");
        const completed = await waitForCompletion(started.upload_uuid, abortController.signal, setStage);
        if (completed.status !== "completed") throw new Error(completed.error_code || `検証結果: ${completed.status}`);
        clearUploadSession();
        setSession(null);
        setStage("完了");
        setProgress(100);
        onCompleted();
        return;
      }
      if (started.status !== "uploading" || started.parts.length === 0) {
        throw new Error(`再開できないセッション状態です: ${started.status}`);
      }

      const active: UploadSession = session ?? {
        idempotencyKey,
        uploadUuid: started.upload_uuid,
        title: title.trim(),
        kind,
        filename: file.name,
        contentType,
        size: file.size,
        lastModified: file.lastModified,
        sha256,
        expiresAt: started.expires_at,
        etags: {},
      };
      active.expiresAt = started.expires_at;
      saveUploadSession(active);
      setSession({ ...active });

      setStage("R2へ送信中");
      const etags = await uploadPartsWithAutoResign(
        file,
        started,
        active,
        () => startUpload(file, active.title, active.kind, sha256, idempotencyKey, abortController.signal),
        abortController.signal,
        (done, total) => setProgress(percent(done, total)),
      );
      setSession({ ...active, etags: { ...etags } });
      setStage("アップロードを確定中");
      const verifying = await completeUpload(active.uploadUuid, etags, abortController.signal);
      if (verifying.status === "completed") {
        clearUploadSession();
        setSession(null);
        setStage("完了");
        onCompleted();
        return;
      }
      setStage("サーバで検証中");
      const completed = await waitForCompletion(active.uploadUuid, abortController.signal, (value) => {
        setStage(value === "verifying" ? "サーバで検証中" : value);
      });
      if (completed.status !== "completed") throw new Error(completed.error_code || `検証結果: ${completed.status}`);
      clearUploadSession();
      setSession(null);
      setStage("完了");
      setProgress(100);
      setFile(null);
      onCompleted();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      controller.current = null;
      setRunning(false);
    }
  }

  function pause() {
    controller.current?.abort();
  }

  async function discard() {
    controller.current?.abort();
    setRunning(false);
    setError("");
    try {
      if (session?.uploadUuid) await abortUpload(session.uploadUuid);
    } catch (caught) {
      setError(errorMessage(caught));
      return;
    }
    clearUploadSession();
    setSession(null);
    setFile(null);
    setTitle("");
    setProgress(0);
    setStage("破棄しました");
  }

  const locked = Boolean(session);
  return (
    <section className="card" style={{ marginBottom: "1rem" }}>
      <h2>動画を追加</h2>
      {error && <Notice variant="error">{error}</Notice>}
      {session && (
        <p className="muted" style={{ fontSize: ".8rem" }}>
          中断した「{session.filename}」を再開できます。ページを再読み込みした場合は同じファイルを再選択してください。
        </p>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(12rem, 2fr) minmax(8rem, 1fr)", gap: ".6rem", marginBottom: ".6rem" }}>
        <label>
          <span className="muted" style={{ display: "block", fontSize: ".75rem" }}>タイトル</span>
          <input value={title} disabled={locked || running} onChange={(event) => setTitle(event.target.value)} style={{ width: "100%" }} maxLength={300} />
        </label>
        <label>
          <span className="muted" style={{ display: "block", fontSize: ".75rem" }}>種別</span>
          <select value={kind} disabled={locked || running} onChange={(event) => setKind(event.target.value as UploadKind)} style={{ width: "100%" }}>
            <option value="program">番組</option>
            <option value="cm">CM</option>
            <option value="filler">フィラー</option>
            <option value="bumper">バンパー</option>
            <option value="slate">スレート</option>
          </select>
        </label>
      </div>
      <input
        type="file"
        accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mkv"
        disabled={running}
        onChange={(event) => setFile(event.target.files?.[0] ?? null)}
      />
      {(stage || running) && (
        <div style={{ marginTop: ".7rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: ".78rem" }}><span>{stage}</span><span>{progress}%</span></div>
          <progress value={progress} max={100} style={{ width: "100%" }} />
        </div>
      )}
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".7rem" }}>
        <button className="btn" type="button" disabled={running || !file || !title.trim()} onClick={() => void run()}>
          {session ? "再開" : "アップロード"}
        </button>
        {running && <button className="btn" type="button" onClick={pause}>一時停止</button>}
        {session && <button className="btn" type="button" disabled={running} onClick={() => void discard()} style={{ background: "var(--warn)" }}>破棄</button>}
      </div>
      <p className="muted" style={{ fontSize: ".72rem", marginBottom: 0 }}>
        パートは最大3本を並列送信し、一時障害を最大4回試行します。署名URLはブラウザに保存しません。
      </p>
    </section>
  );
}
