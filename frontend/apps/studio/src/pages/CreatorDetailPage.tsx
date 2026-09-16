// SPDX-FileCopyrightText: 2026 アイシーエス
// SPDX-License-Identifier: AGPL-3.0-or-later
/** クリエイター詳細 (#27 ファンクラブ)。タブ: 基本情報 / 番組 / ティア / 招待 / 枠契約 / 会員・収益。
 *
 * URL: /creators/:creatorId?tab=basic|series|tiers|invitations|contracts|members
 */

import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api } from "@icstv/api";
import { Breadcrumb, EmptyState, Meter, Notice, StudioPage } from "../atoms";
import { money } from "../hooks";
import { RouterLink } from "../links";

import type { components } from "@icstv/api";

type CreatorAdminOut = components["schemas"]["CreatorAdminOut"];
type CreatorSeriesOut = components["schemas"]["CreatorSeriesOut"];
type SeriesOptionOut = components["schemas"]["SeriesOptionOut"];
type CreatorTierOut = components["schemas"]["CreatorTierOut"];
type CreatorInvitationOut = components["schemas"]["CreatorInvitationOut"];
type SlotContractOut = components["schemas"]["SlotContractOut"];
type CreatorMembersSummaryOut = components["schemas"]["CreatorMembersSummaryOut"];
type CreatorRevenueSummaryOut = components["schemas"]["CreatorRevenueSummaryOut"];
type FcSettlementSummaryOut = components["schemas"]["FcSettlementSummaryOut"];

/** 通貨別内訳に JPY 以外が含まれていれば注記を返す (合計は JPY 分のみのため)。
 *
 * 通貨をまたいだ合算はしない方針なので、合計値そのものは JPY 固定のまま
 * 「他通貨がある」ことだけを見せる。黙って落とすと過少に見えたまま気付けない。
 */
function otherCurrencyNote(rows: Array<Record<string, unknown>> | undefined): string | null {
  const others = [
    ...new Set(
      (rows ?? []).map((r) => String(r.currency ?? "").toLowerCase()).filter((c) => c && c !== "jpy"),
    ),
  ];
  return others.length ? `合計は JPY 分のみ (他に ${others.map((c) => c.toUpperCase()).join("/")} あり)` : null;
}

type Tab =
  | "basic"
  | "onboarding"
  | "series"
  | "tiers"
  | "invitations"
  | "contracts"
  | "members"
  | "settlements";
const TABS: { key: Tab; label: string }[] = [
  { key: "basic", label: "基本情報" },
  { key: "onboarding", label: "オンボーディング" },
  { key: "series", label: "番組" },
  { key: "tiers", label: "ティア" },
  { key: "invitations", label: "招待" },
  { key: "contracts", label: "枠契約" },
  { key: "members", label: "会員・収益" },
  { key: "settlements", label: "分配元帳" },
];

const inputStyle = { width: "100%", boxSizing: "border-box" as const, background: "#0a0f24", border: "1px solid #2a2a2a", borderRadius: 6, color: "inherit", padding: "6px 10px", fontSize: 13 };

// --------------------------------------------------------------------------- //
//  基本情報タブ                                                                //
// --------------------------------------------------------------------------- //

function BasicTab({ creatorId, creator, onSaved }: { creatorId: number; creator: CreatorAdminOut; onSaved: () => void }) {
  const [name, setName] = useState(creator.name);
  const [slug, setSlug] = useState(creator.slug);
  const [description, setDescription] = useState(creator.description);
  const [status, setStatus] = useState(creator.status);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function save() {
    setBusy(true);
    setErr("");
    const { error } = await api.PUT("/api/v1/admin/fanclub/creators/{creator_id}", {
      params: { path: { creator_id: creatorId } },
      // PUT は全項目置換のため、このタブが扱わないオンボーディング/特商法表記は creator から素通しする
      // (でないと基本情報を保存するたびに他タブの入力がデフォルト値へ巻き戻ってしまう)。
      body: {
        name,
        slug,
        description,
        status,
        onboarding_status: creator.onboarding_status,
        legal_name: creator.legal_name,
        is_individual: creator.is_individual,
        representative_name: creator.representative_name,
        address: creator.address,
        phone: creator.phone,
        hide_contact_details: creator.hide_contact_details,
        contact_email: creator.contact_email,
        invoice_registration_number: creator.invoice_registration_number,
      },
    });
    setBusy(false);
    if (error) setErr((error as { detail?: string })?.detail || "保存に失敗しました");
    else onSaved();
  }

  const row = (label: string, el: React.ReactNode) => (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 4 }}>{label}</label>
      {el}
    </div>
  );

  return (
    <div className="card" style={{ padding: "1rem", maxWidth: 480 }}>
      {row("名前", <input style={inputStyle} value={name} onChange={(e) => setName(e.target.value)} />)}
      {row("slug", <input style={inputStyle} value={slug} onChange={(e) => setSlug(e.target.value)} />)}
      {row("説明", <textarea style={{ ...inputStyle, resize: "vertical", minHeight: 80 }} value={description} onChange={(e) => setDescription(e.target.value)} />)}
      {row(
        "状態",
        <select style={inputStyle} value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="active">有効</option>
          <option value="suspended">停止中(公開サイトでは非活性表示)</option>
        </select>
      )}
      {err && <div style={{ color: "#f87171", fontSize: 13, marginBottom: 10 }}>{err}</div>}
      <button className="btn" type="button" onClick={save} disabled={busy}>
        {busy ? "保存中…" : "保存"}
      </button>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  オンボーディング (審査/本人確認 + 特商法表記) タブ                          //
// --------------------------------------------------------------------------- //

function OnboardingTab({ creatorId, creator, onSaved }: { creatorId: number; creator: CreatorAdminOut; onSaved: () => void }) {
  const [onboardingStatus, setOnboardingStatus] = useState(creator.onboarding_status);
  const [legalName, setLegalName] = useState(creator.legal_name);
  const [isIndividual, setIsIndividual] = useState(creator.is_individual);
  const [representativeName, setRepresentativeName] = useState(creator.representative_name);
  const [address, setAddress] = useState(creator.address);
  const [phone, setPhone] = useState(creator.phone);
  const [hideContactDetails, setHideContactDetails] = useState(creator.hide_contact_details);
  const [contactEmail, setContactEmail] = useState(creator.contact_email);
  const [invoiceNumber, setInvoiceNumber] = useState(creator.invoice_registration_number);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function save() {
    setBusy(true);
    setErr("");
    const { error } = await api.PUT("/api/v1/admin/fanclub/creators/{creator_id}", {
      params: { path: { creator_id: creatorId } },
      body: {
        name: creator.name,
        slug: creator.slug,
        description: creator.description,
        status: creator.status,
        onboarding_status: onboardingStatus,
        legal_name: legalName,
        is_individual: isIndividual,
        representative_name: representativeName,
        address,
        phone,
        hide_contact_details: hideContactDetails,
        contact_email: contactEmail,
        invoice_registration_number: invoiceNumber,
      },
    });
    setBusy(false);
    if (error) setErr((error as { detail?: string })?.detail || "保存に失敗しました");
    else onSaved();
  }

  async function verifyIdentity() {
    setBusy(true);
    setErr("");
    const { error } = await api.POST("/api/v1/admin/fanclub/creators/{creator_id}/verify-identity", {
      params: { path: { creator_id: creatorId } },
    });
    setBusy(false);
    if (error) setErr("本人確認の記録に失敗しました");
    else onSaved();
  }

  const row = (label: string, el: React.ReactNode) => (
    <div style={{ marginBottom: 14 }}>
      <label style={{ display: "block", fontSize: 13, fontWeight: 700, marginBottom: 4 }}>{label}</label>
      {el}
    </div>
  );

  return (
    <div className="card" style={{ padding: "1rem", maxWidth: 560 }}>
      <h3 style={{ marginTop: 0 }}>審査・本人確認</h3>
      {row(
        "審査ステータス",
        <select style={inputStyle} value={onboardingStatus} onChange={(e) => setOnboardingStatus(e.target.value)}>
          <option value="pending">審査待ち</option>
          <option value="approved">承認済み</option>
          <option value="rejected">却下</option>
        </select>
      )}
      {row(
        "本人確認",
        <div style={{ display: "flex", alignItems: "center", gap: ".6rem" }}>
          <span>{creator.identity_verified_at ? `確認済み (${creator.identity_verified_at.slice(0, 10)})` : "未確認"}</span>
          {!creator.identity_verified_at && (
            <button className="btn" type="button" onClick={verifyIdentity} disabled={busy}>
              本人確認済みにする
            </button>
          )}
        </div>
      )}

      <h3>特定商取引法表記</h3>
      {row("事業者名(氏名・名称)", <input style={inputStyle} value={legalName} onChange={(e) => setLegalName(e.target.value)} />)}
      {row(
        "形態",
        <select
          style={inputStyle}
          value={isIndividual ? "individual" : "corporate"}
          onChange={(e) => setIsIndividual(e.target.value === "individual")}
        >
          <option value="individual">個人</option>
          <option value="corporate">法人</option>
        </select>
      )}
      {!isIndividual &&
        row("代表者名", <input style={inputStyle} value={representativeName} onChange={(e) => setRepresentativeName(e.target.value)} />)}
      {row("住所", <input style={inputStyle} value={address} onChange={(e) => setAddress(e.target.value)} />)}
      {row("電話番号", <input style={inputStyle} value={phone} onChange={(e) => setPhone(e.target.value)} />)}
      {row(
        "住所・電話番号の公開",
        <label style={{ display: "flex", alignItems: "center", gap: ".4rem", fontWeight: 400 }}>
          <input type="checkbox" checked={hideContactDetails} onChange={(e) => setHideContactDetails(e.target.checked)} />
          非公開にする(請求により遅滞なく開示する方式。下の問い合わせ先を公開表示に使う)
        </label>
      )}
      {row("問い合わせ先メールアドレス", <input style={inputStyle} value={contactEmail} onChange={(e) => setContactEmail(e.target.value)} />)}
      {row("インボイス登録番号(任意)", <input style={inputStyle} value={invoiceNumber} onChange={(e) => setInvoiceNumber(e.target.value)} />)}

      {err && <div style={{ color: "#f87171", fontSize: 13, marginBottom: 10 }}>{err}</div>}
      <button className="btn" type="button" onClick={save} disabled={busy}>
        {busy ? "保存中…" : "保存"}
      </button>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  番組 (Series紐付け) タブ                                                    //
// --------------------------------------------------------------------------- //

function SeriesTab({ creatorId }: { creatorId: number }) {
  const [links, setLinks] = useState<CreatorSeriesOut[]>([]);
  const [options, setOptions] = useState<SeriesOptionOut[]>([]);
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<number | "">("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}/series", { params: { path: { creator_id: creatorId } } })
      .then(({ data }) => setLinks((data as CreatorSeriesOut[]) ?? []));
  }, [creatorId]);
  useEffect(load, [load]);

  useEffect(() => {
    api
      .GET("/api/v1/admin/fanclub/series-options", { params: { query: { q } } })
      .then(({ data }) => setOptions((data as SeriesOptionOut[]) ?? []));
  }, [q]);

  async function link() {
    if (!selected) return;
    setErr("");
    const { error } = await api.POST("/api/v1/admin/fanclub/creators/{creator_id}/series", {
      params: { path: { creator_id: creatorId }, query: { series_id: Number(selected) } },
    });
    if (error) setErr((error as { detail?: string })?.detail || "紐付けに失敗しました");
    else {
      setMsg("紐付けました");
      setSelected("");
      load();
    }
  }

  async function unlink(seriesId: number) {
    if (!window.confirm("この番組の紐付けを解除しますか？")) return;
    await api.DELETE("/api/v1/admin/fanclub/creators/{creator_id}/series/{series_id}", {
      params: { path: { creator_id: creatorId, series_id: seriesId } },
    });
    setMsg("解除しました");
    load();
  }

  return (
    <div>
      {msg && <Notice variant="success">{msg}</Notice>}
      {err && <Notice variant="error">{err}</Notice>}
      <div className="card" style={{ padding: "1rem", marginBottom: "1rem" }}>
        <div style={{ display: "flex", gap: ".5rem", alignItems: "center", flexWrap: "wrap" }}>
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="番組名で検索" style={{ width: "12rem" }} />
          <select value={selected} onChange={(e) => setSelected(e.target.value ? Number(e.target.value) : "")} style={{ width: "20rem" }}>
            <option value="">-- 番組を選択 --</option>
            {options.map((o) => (
              <option key={o.id} value={o.id} disabled={!!o.linked_creator_name}>
                {o.title} ({o.channel_name}){o.linked_creator_name ? ` — 既に「${o.linked_creator_name}」に紐付け済み` : ""}
              </option>
            ))}
          </select>
          <button className="btn" type="button" onClick={link} disabled={!selected}>
            紐付ける
          </button>
        </div>
      </div>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>番組</th>
              <th>チャンネル</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {links.map((l) => (
              <tr key={l.series_id}>
                <td>{l.series_title}</td>
                <td className="muted">{l.channel_name}</td>
                <td>
                  <button className="btn" type="button" onClick={() => void unlink(l.series_id)} style={{ fontSize: ".78rem", padding: ".2rem .6rem", background: "#3a1a1a", color: "#f87171" }}>
                    解除
                  </button>
                </td>
              </tr>
            ))}
            {links.length === 0 && (
              <tr>
                <td colSpan={3} className="muted">
                  紐付けられた番組がありません。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  ティアタブ                                                                  //
// --------------------------------------------------------------------------- //

function StripePriceCell({ tier, onSave }: { tier: CreatorTierOut; onSave: (value: string) => void }) {
  const [value, setValue] = useState(tier.stripe_price_id);
  const dirty = value !== tier.stripe_price_id;
  return (
    <div style={{ display: "flex", gap: ".3rem", alignItems: "center" }}>
      <input
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="price_..."
        style={{ width: "9rem", fontSize: ".75rem" }}
      />
      {dirty && (
        <button className="btn" type="button" onClick={() => onSave(value)} style={{ fontSize: ".72rem", padding: ".15rem .5rem" }}>
          保存
        </button>
      )}
    </div>
  );
}

function TiersTab({ creatorId }: { creatorId: number }) {
  const [tiers, setTiers] = useState<CreatorTierOut[]>([]);
  const [newLevel, setNewLevel] = useState("1");
  const [newName, setNewName] = useState("");
  const [newPrice, setNewPrice] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}/tiers", { params: { path: { creator_id: creatorId } } })
      .then(({ data }) => setTiers((data as CreatorTierOut[]) ?? []));
  }, [creatorId]);
  useEffect(load, [load]);

  async function create() {
    setErr("");
    const { error } = await api.POST("/api/v1/admin/fanclub/creators/{creator_id}/tiers", {
      params: { path: { creator_id: creatorId } },
      body: {
        level: Number(newLevel),
        name: newName,
        description: "",
        price_minor: newPrice ? Number(newPrice) : null,
        is_active: true,
        stripe_price_id: "",
      },
    });
    if (error) setErr((error as { detail?: string })?.detail || "作成に失敗しました");
    else {
      setMsg("作成しました");
      setNewName("");
      setNewPrice("");
      load();
    }
  }

  async function toggleActive(t: CreatorTierOut) {
    await api.PUT("/api/v1/admin/fanclub/creators/{creator_id}/tiers/{tier_id}", {
      params: { path: { creator_id: creatorId, tier_id: t.id } },
      body: {
        level: t.level,
        name: t.name,
        description: t.description,
        price_minor: t.price_minor,
        is_active: !t.is_active,
        stripe_price_id: t.stripe_price_id,
      },
    });
    load();
  }

  async function saveStripePriceId(t: CreatorTierOut, stripePriceId: string) {
    const { error } = await api.PUT("/api/v1/admin/fanclub/creators/{creator_id}/tiers/{tier_id}", {
      params: { path: { creator_id: creatorId, tier_id: t.id } },
      body: {
        level: t.level,
        name: t.name,
        description: t.description,
        price_minor: t.price_minor,
        is_active: t.is_active,
        stripe_price_id: stripePriceId,
      },
    });
    if (error) setErr((error as { detail?: string })?.detail || "保存に失敗しました");
    else {
      setMsg("Stripe Price ID を保存しました");
      load();
    }
  }

  async function remove(t: CreatorTierOut) {
    if (t.level === 0) return;
    if (!window.confirm(`ティア「${t.name}」を削除しますか？`)) return;
    await api.DELETE("/api/v1/admin/fanclub/creators/{creator_id}/tiers/{tier_id}", {
      params: { path: { creator_id: creatorId, tier_id: t.id } },
    });
    setMsg("削除しました");
    load();
  }

  return (
    <div>
      {msg && <Notice variant="success">{msg}</Notice>}
      {err && <Notice variant="error">{err}</Notice>}
      <div className="card" style={{ padding: "1rem", marginBottom: "1rem" }}>
        <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 8 }}>有料ティアを追加</div>
        <p className="muted" style={{ fontSize: ".75rem", marginTop: 0 }}>
          作成後、Stripe ダッシュボードで Product/Price を作成し、下の表の「Stripe Price ID」に貼り付けてください。
          クリエイター側の Stripe Connect オンボーディングが完了すると「受付中」に切り替わります。
        </p>
        <div style={{ display: "flex", gap: ".5rem", alignItems: "center", flexWrap: "wrap" }}>
          <input type="number" min={1} value={newLevel} onChange={(e) => setNewLevel(e.target.value)} placeholder="level" style={{ width: "5rem" }} />
          <input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="ティア名" style={{ width: "10rem" }} />
          <input type="number" min={0} value={newPrice} onChange={(e) => setNewPrice(e.target.value)} placeholder="月額(円)" style={{ width: "8rem" }} />
          <button className="btn" type="button" onClick={create} disabled={!newName || !newPrice}>
            ＋ 追加
          </button>
        </div>
      </div>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>level</th>
              <th>名前</th>
              <th style={{ textAlign: "right" }}>月額</th>
              <th>Stripe Price ID</th>
              <th>加入</th>
              <th>状態</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {tiers.map((t) => (
              <tr key={t.id}>
                <td>{t.level}</td>
                <td>{t.name}</td>
                <td style={{ textAlign: "right" }}>{t.price_minor != null ? money(t.price_minor, t.currency) : "無料"}</td>
                <td>{t.level !== 0 && <StripePriceCell tier={t} onSave={(v) => void saveStripePriceId(t, v)} />}</td>
                <td>{t.joinable ? <span style={{ color: "#4ade80" }}>受付中</span> : <span className="muted">準備中</span>}</td>
                <td>
                  <button className="btn" type="button" onClick={() => void toggleActive(t)} style={{ fontSize: ".78rem", padding: ".2rem .6rem" }}>
                    {t.is_active ? "● 有効" : "○ 無効"}
                  </button>
                </td>
                <td>
                  {t.level !== 0 && (
                    <button className="btn" type="button" onClick={() => void remove(t)} style={{ fontSize: ".78rem", padding: ".2rem .6rem", background: "#3a1a1a", color: "#f87171" }}>
                      削除
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  招待タブ                                                                    //
// --------------------------------------------------------------------------- //

function InvitationsTab({ creatorId }: { creatorId: number }) {
  const [invitations, setInvitations] = useState<CreatorInvitationOut[]>([]);
  const [email, setEmail] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}/invitations", { params: { path: { creator_id: creatorId } } })
      .then(({ data }) => setInvitations((data as CreatorInvitationOut[]) ?? []));
  }, [creatorId]);
  useEffect(load, [load]);

  async function invite() {
    setErr("");
    const { error } = await api.POST("/api/v1/admin/fanclub/creators/{creator_id}/invitations", {
      params: { path: { creator_id: creatorId } },
      body: { email },
    });
    if (error) setErr((error as { detail?: string })?.detail || "招待に失敗しました");
    else {
      setMsg("招待を発行しました(可能ならメール送信済み)");
      setEmail("");
      load();
    }
  }

  async function revoke(id: number) {
    if (!window.confirm("この招待を取り消しますか？")) return;
    await api.DELETE("/api/v1/admin/fanclub/creators/{creator_id}/invitations/{invitation_id}", {
      params: { path: { creator_id: creatorId, invitation_id: id } },
    });
    setMsg("取り消しました");
    load();
  }

  return (
    <div>
      {msg && <Notice variant="success">{msg}</Notice>}
      {err && <Notice variant="error">{err}</Notice>}
      <div className="card" style={{ padding: "1rem", marginBottom: "1rem" }}>
        <div style={{ display: "flex", gap: ".5rem", alignItems: "center", flexWrap: "wrap" }}>
          <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="クリエイターのメールアドレス" style={{ width: "18rem" }} />
          <button className="btn" type="button" onClick={invite} disabled={!email}>
            招待を発行
          </button>
        </div>
        <p className="muted" style={{ fontSize: ".75rem", marginTop: ".4rem", marginBottom: 0 }}>
          メール送信に失敗しても招待自体は作成されます。下記の招待URLを直接共有することもできます。
        </p>
      </div>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>メール</th>
              <th>招待URL</th>
              <th>期限</th>
              <th>受諾</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {invitations.map((inv) => (
              <tr key={inv.id}>
                <td>{inv.email}</td>
                <td className="muted" style={{ fontSize: ".78rem", wordBreak: "break-all" }}>{inv.invite_url}</td>
                <td className="muted" style={{ fontSize: ".78rem" }}>{new Date(inv.expires_at).toLocaleString("ja-JP")}</td>
                <td>{inv.accepted_at ? <span style={{ color: "#4ade80" }}>済</span> : <span className="muted">未</span>}</td>
                <td>
                  {!inv.accepted_at && (
                    <button className="btn" type="button" onClick={() => void revoke(inv.id)} style={{ fontSize: ".78rem", padding: ".2rem .6rem", background: "#3a1a1a", color: "#f87171" }}>
                      取消
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {invitations.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  招待がまだありません。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  枠契約タブ                                                                  //
// --------------------------------------------------------------------------- //

const CONTRACT_STATUS_LABEL: Record<string, string> = { draft: "draft", active: "active", suspended: "suspended", ended: "ended" };
// #27 Part B: docs/fanclub.md §5.5・§6.5。creator_channel はクリエイター自身が創作者ポータルで
// 自分の YouTube ストリームキーを設定済みでないと実際には配信されない (fanclub.tasks の reconciler)。
const DESTINATION_LABEL: Record<string, string> = { operator_channel: "運営チャンネル", creator_channel: "クリエイター自チャンネル", none: "配信連携なし" };

function ContractsTab({ creatorId }: { creatorId: number }) {
  const [contracts, setContracts] = useState<SlotContractOut[]>([]);
  const [title, setTitle] = useState("");
  const [fee, setFee] = useState("");
  const [startsOn, setStartsOn] = useState("");
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}/contracts", { params: { path: { creator_id: creatorId } } })
      .then(({ data }) => setContracts((data as SlotContractOut[]) ?? []));
  }, [creatorId]);
  useEffect(load, [load]);

  async function create() {
    setErr("");
    const { error } = await api.POST("/api/v1/admin/fanclub/creators/{creator_id}/contracts", {
      params: { path: { creator_id: creatorId } },
      body: { title, monthly_fee_minor: Number(fee), starts_on: startsOn, ends_on: "", status: "draft", youtube_destination: "none", notes: "" },
    });
    if (error) setErr((error as { detail?: string })?.detail || "作成に失敗しました");
    else {
      setMsg("作成しました");
      setTitle("");
      setFee("");
      setStartsOn("");
      load();
    }
  }

  async function setStatus(c: SlotContractOut, status: string) {
    await api.PUT("/api/v1/admin/fanclub/creators/{creator_id}/contracts/{contract_id}", {
      params: { path: { creator_id: creatorId, contract_id: c.id } },
      body: { title: c.title, monthly_fee_minor: c.monthly_fee_minor, starts_on: c.starts_on, ends_on: c.ends_on, status, youtube_destination: c.youtube_destination, notes: c.notes },
    });
    load();
  }

  async function setDestination(c: SlotContractOut, youtube_destination: string) {
    await api.PUT("/api/v1/admin/fanclub/creators/{creator_id}/contracts/{contract_id}", {
      params: { path: { creator_id: creatorId, contract_id: c.id } },
      body: { title: c.title, monthly_fee_minor: c.monthly_fee_minor, starts_on: c.starts_on, ends_on: c.ends_on, status: c.status, youtube_destination, notes: c.notes },
    });
    load();
  }

  async function remove(id: number) {
    if (!window.confirm("この枠契約を削除しますか？")) return;
    await api.DELETE("/api/v1/admin/fanclub/creators/{creator_id}/contracts/{contract_id}", {
      params: { path: { creator_id: creatorId, contract_id: id } },
    });
    setMsg("削除しました");
    load();
  }

  return (
    <div>
      {msg && <Notice variant="success">{msg}</Notice>}
      {err && <Notice variant="error">{err}</Notice>}
      <p className="muted" style={{ fontSize: ".78rem" }}>
        Phase A は契約レコード + 手動請求のみです(Stripe Billing 連携は次フェーズ)。
      </p>
      <div className="card" style={{ padding: "1rem", marginBottom: "1rem" }}>
        <div style={{ display: "flex", gap: ".5rem", alignItems: "center", flexWrap: "wrap" }}>
          <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="枠名" style={{ width: "12rem" }} />
          <input type="number" min={0} value={fee} onChange={(e) => setFee(e.target.value)} placeholder="月額(円)" style={{ width: "8rem" }} />
          <input type="date" value={startsOn} onChange={(e) => setStartsOn(e.target.value)} style={{ width: "10rem" }} />
          <button className="btn" type="button" onClick={create} disabled={!title || !fee || !startsOn}>
            ＋ 契約を作成
          </button>
        </div>
      </div>
      <div className="card">
        <table>
          <thead>
            <tr>
              <th>枠名</th>
              <th style={{ textAlign: "right" }}>月額</th>
              <th>開始</th>
              <th>状態</th>
              <th>YouTube宛先</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {contracts.map((c) => (
              <tr key={c.id}>
                <td>{c.title}</td>
                <td style={{ textAlign: "right" }}>{money(c.monthly_fee_minor, c.currency)}</td>
                <td className="muted">{c.starts_on}</td>
                <td>
                  {c.stripe_active ? (
                    <span title="Stripe Billing連携済み。状態はWebhookが自動更新するため手動変更できません">
                      {CONTRACT_STATUS_LABEL[c.status] ?? c.status} <span className="muted" style={{ fontSize: ".72rem" }}>(Stripe連携)</span>
                    </span>
                  ) : (
                    <select value={c.status} onChange={(e) => void setStatus(c, e.target.value)} style={{ fontSize: ".78rem" }}>
                      {Object.entries(CONTRACT_STATUS_LABEL).map(([v, label]) => (
                        <option key={v} value={v}>
                          {label}
                        </option>
                      ))}
                    </select>
                  )}
                </td>
                <td>
                  <select value={c.youtube_destination} onChange={(e) => void setDestination(c, e.target.value)} style={{ fontSize: ".78rem" }}>
                    {Object.entries(DESTINATION_LABEL).map(([v, label]) => (
                      <option key={v} value={v}>
                        {label}
                      </option>
                    ))}
                  </select>
                  {c.youtube_destination === "creator_channel" && (
                    <p className="muted" style={{ fontSize: ".68rem", margin: ".2rem 0 0" }}>
                      クリエイター自身が創作者ポータルでYouTubeキーを設定済みの場合のみ配信されます。同一映像の二重配信は収益化リスクがあるため運用要件(サムネ/タイトル差異化)に注意。
                    </p>
                  )}
                </td>
                <td>
                  <button className="btn" type="button" onClick={() => void remove(c.id)} style={{ fontSize: ".78rem", padding: ".2rem .6rem", background: "#3a1a1a", color: "#f87171" }}>
                    削除
                  </button>
                </td>
              </tr>
            ))}
            {contracts.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  枠契約がまだありません。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  会員タブ (集計のみ、PII一覧は出さない)                                       //
// --------------------------------------------------------------------------- //

function RevenueSummaryCard({ creatorId }: { creatorId: number }) {
  const [rev, setRev] = useState<CreatorRevenueSummaryOut | null>(null);

  useEffect(() => {
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}/revenue-summary", { params: { path: { creator_id: creatorId } } })
      .then(({ data }) => setRev((data as CreatorRevenueSummaryOut) ?? null));
  }, [creatorId]);

  if (!rev) return <EmptyState loading>読み込み中…</EmptyState>;

  const maxCount = Math.max(1, ...rev.monthly.flatMap((m) => [m.joined, m.left]));
  const mrrNote = otherCurrencyNote(rev.slot_mrr_by_currency);

  return (
    <div className="card" style={{ padding: "1rem", marginBottom: "1rem" }}>
      <div style={{ display: "flex", gap: "1.5rem", flexWrap: "wrap", marginBottom: "1rem" }}>
        <div>
          <div style={{ fontSize: 22, fontWeight: 800 }}>{money(rev.slot_mrr_jpy, "jpy")}</div>
          <div className="muted" style={{ fontSize: ".75rem" }}>
            枠契約 MRR (有効{rev.active_slot_contracts}件・B2B手動請求)
            {mrrNote && ` ／ ${mrrNote}`}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 22, fontWeight: 800 }}>{rev.fc_active_members}</div>
          <div className="muted" style={{ fontSize: ".75rem" }}>FC在籍会員数</div>
        </div>
        <div>
          <div style={{ fontSize: 22, fontWeight: 800 }}>
            {rev.churn_rate == null ? "—" : `${(rev.churn_rate * 100).toFixed(1)}%`}
          </div>
          <div className="muted" style={{ fontSize: ".75rem" }}>直近完了月のチャーン率</div>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>月</th>
            <th style={{ textAlign: "right" }}>入会</th>
            <th style={{ textAlign: "right" }}>退会</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rev.monthly.map((m) => (
            <tr key={m.month}>
              <td className="tabnum">{m.month}</td>
              <td style={{ textAlign: "right" }}>{m.joined}</td>
              <td style={{ textAlign: "right" }}>{m.left}</td>
              <td style={{ minWidth: 120 }}>
                <Meter value={m.joined} max={maxCount} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted" style={{ fontSize: ".75rem", marginTop: ".75rem", marginBottom: 0 }}>
        当月は月初から現在時刻までの途中経過。チャーン率は有料ティア加入がまだ無いためFC無料会員の在籍動向を示す指標です。
      </p>
    </div>
  );
}

function MembersTab({ creatorId }: { creatorId: number }) {
  const [summary, setSummary] = useState<CreatorMembersSummaryOut | null>(null);

  useEffect(() => {
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}/members-summary", { params: { path: { creator_id: creatorId } } })
      .then(({ data }) => setSummary((data as CreatorMembersSummaryOut) ?? null));
  }, [creatorId]);

  return (
    <div>
      <RevenueSummaryCard creatorId={creatorId} />
      {!summary ? (
        <EmptyState loading>読み込み中…</EmptyState>
      ) : (
        <div className="card" style={{ padding: "1rem", maxWidth: 480 }}>
          <div style={{ fontSize: 24, fontWeight: 800, marginBottom: 12 }}>{summary.total} 名</div>
          <table>
            <thead>
              <tr>
                <th>level</th>
                <th style={{ textAlign: "right" }}>会員数</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(summary.by_level)
                .sort(([a], [b]) => Number(a) - Number(b))
                .map(([level, count]) => (
                  <tr key={level}>
                    <td>{level === "0" ? "無料会員" : `ティア${level}`}</td>
                    <td style={{ textAlign: "right" }}>{count}</td>
                  </tr>
                ))}
            </tbody>
          </table>
          <p className="muted" style={{ fontSize: ".75rem", marginTop: "1rem", marginBottom: 0 }}>
            個々の会員のメールアドレス等は表示しません(プライバシー配慮の意図的な設計)。
          </p>
        </div>
      )}
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  分配元帳タブ                                                                //
// --------------------------------------------------------------------------- //

function SettlementsTab({ creatorId }: { creatorId: number }) {
  const [summary, setSummary] = useState<FcSettlementSummaryOut | null>(null);

  useEffect(() => {
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}/settlements", { params: { path: { creator_id: creatorId } } })
      .then(({ data }) => setSummary((data as FcSettlementSummaryOut) ?? null));
  }, [creatorId]);

  if (!summary) return <EmptyState loading>読み込み中…</EmptyState>;

  const totalsNote = otherCurrencyNote(summary.totals_by_currency);

  return (
    <div>
      <div style={{ display: "flex", gap: "1rem", flexWrap: "wrap", marginBottom: "1rem" }}>
        <div className="card" style={{ flex: 1, minWidth: 160, padding: "1rem" }}>
          <div className="muted" style={{ fontSize: ".78rem" }}>累計売上(税込)</div>
          <div style={{ fontSize: 22, fontWeight: 800 }}>{money(summary.total_gross_jpy, "jpy")}</div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 160, padding: "1rem" }}>
          <div className="muted" style={{ fontSize: ".78rem" }}>累計手数料</div>
          <div style={{ fontSize: 22, fontWeight: 800 }}>{money(summary.total_fee_jpy, "jpy")}</div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 160, padding: "1rem" }}>
          <div className="muted" style={{ fontSize: ".78rem" }}>累計送金額</div>
          <div style={{ fontSize: 22, fontWeight: 800 }}>{money(summary.total_net_jpy, "jpy")}</div>
        </div>
        <div className="card" style={{ flex: 1, minWidth: 160, padding: "1rem" }}>
          <div className="muted" style={{ fontSize: ".78rem" }}>異議申立て中</div>
          <div style={{ fontSize: 22, fontWeight: 800, color: summary.disputed_count > 0 ? "#f87171" : undefined }}>
            {summary.disputed_count} 件
          </div>
        </div>
      </div>
      {totalsNote && (
        <p className="muted" style={{ fontSize: ".78rem", marginTop: "-.5rem" }}>{totalsNote}</p>
      )}
      <div className="card" style={{ padding: "1rem" }}>
        <table style={{ width: "100%" }}>
          <thead>
            <tr>
              <th>日時</th>
              <th>会員</th>
              <th>ティア</th>
              <th style={{ textAlign: "right" }}>売上</th>
              <th style={{ textAlign: "right" }}>手数料</th>
              <th style={{ textAlign: "right" }}>送金額</th>
              <th>状態</th>
            </tr>
          </thead>
          <tbody>
            {summary.items.map((s) => (
              <tr key={s.id}>
                <td className="muted">{s.created_at.slice(0, 16).replace("T", " ")}</td>
                <td>{s.member_email || "—"}</td>
                <td>{s.tier_name || "—"}</td>
                <td style={{ textAlign: "right" }}>{money(s.gross_amount_minor, s.currency)}</td>
                <td style={{ textAlign: "right" }}>{money(s.application_fee_minor, s.currency)}</td>
                <td style={{ textAlign: "right" }}>{money(s.net_amount_minor, s.currency)}</td>
                <td>{s.disputed ? <span style={{ color: "#f87171" }}>異議申立て中</span> : <span style={{ color: "#4ade80" }}>確定</span>}</td>
              </tr>
            ))}
            {summary.items.length === 0 && (
              <tr>
                <td colSpan={7} className="muted">
                  分配実績がまだありません。
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
//  ハブ                                                                        //
// --------------------------------------------------------------------------- //

export function CreatorDetailPage() {
  const { creatorId = "" } = useParams();
  const [sp] = useSearchParams();
  const navigate = useNavigate();
  const id = Number(creatorId);
  const [tab, setTab] = useState<Tab>((sp.get("tab") as Tab) || "basic");
  const [creator, setCreator] = useState<CreatorAdminOut | null>(null);
  const [msg, setMsg] = useState("");

  const load = useCallback(() => {
    api
      .GET("/api/v1/admin/fanclub/creators/{creator_id}", { params: { path: { creator_id: id } } })
      .then(({ data, error }) => {
        if (error) navigate("/creators");
        else setCreator((data as CreatorAdminOut) ?? null);
      });
  }, [id, navigate]);
  useEffect(load, [load]);

  if (!creator) return <EmptyState loading>読み込み中…</EmptyState>;

  return (
    <StudioPage
      title={creator.name}
      breadcrumb={<Breadcrumb items={[{ label: "クリエイター", href: "/creators" }]} linkComponent={RouterLink} />}
    >
      {msg && <Notice variant="success">{msg}</Notice>}
      <div style={{ display: "flex", gap: ".4rem", marginBottom: ".9rem", flexWrap: "wrap" }}>
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className="btn"
            onClick={() => setTab(t.key)}
            style={{ background: tab === t.key ? "var(--accent, #38bdf8)" : "#444", color: tab === t.key ? "var(--accent-on, #04101a)" : undefined }}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "basic" && (
        <BasicTab
          creatorId={id}
          creator={creator}
          onSaved={() => {
            setMsg("保存しました");
            load();
          }}
        />
      )}
      {tab === "onboarding" && (
        <OnboardingTab
          creatorId={id}
          creator={creator}
          onSaved={() => {
            setMsg("保存しました");
            load();
          }}
        />
      )}
      {tab === "series" && <SeriesTab creatorId={id} />}
      {tab === "tiers" && <TiersTab creatorId={id} />}
      {tab === "invitations" && <InvitationsTab creatorId={id} />}
      {tab === "contracts" && <ContractsTab creatorId={id} />}
      {tab === "members" && <MembersTab creatorId={id} />}
      {tab === "settlements" && <SettlementsTab creatorId={id} />}
    </StudioPage>
  );
}
