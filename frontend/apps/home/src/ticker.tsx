// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
import { ProgressBar } from "./atoms";

import { useNowTick } from "./hooks";

function pad(n: number): string {
  return (n < 10 ? "0" : "") + n;
}

type Ts = number | null | undefined;

/** 進行バー (cur_start_ts/cur_end_ts = epoch)。独立 leaf で毎秒。video を再レンダさせない。 */
export function LiveProgress({ start, end }: { start: Ts; end: Ts }) {
  const now = useNowTick();
  let pct = 0;
  if (start && end && end > start) pct = Math.max(0, Math.min(100, ((now - start) / (end - start)) * 100));
  return <ProgressBar pct={pct} />;
}

/** カウントダウン MM:SS。番組残り。start/end 無しは "--:--"。 */
export function Countdown({ start, end }: { start: Ts; end: Ts }) {
  const now = useNowTick();
  if (!start || !end || end <= start) return <>--:--</>;
  const rem = Math.max(0, Math.floor(end - now));
  return (
    <>
      {pad(Math.floor(rem / 60))}:{pad(rem % 60)}
    </>
  );
}
