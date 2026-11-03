import React, { useEffect, useState, useCallback, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { createRoot } from "react-dom/client";
import "./style.css";

type User = { id: string; username: string; role: string; active: boolean };
type Report = {
  id: string;
  owner_id: string;
  filename: string;
  revision: number;
  item_count: number;
  created_at: string;
};
type Run = {
  id: string;
  report_id: string;
  state: string;
  total: number;
  completed: number;
  failed: number;
  mode: string;
  model: string;
  revision: number;
  selection_mode: "full" | "missing";
  error_code: string;
  error_message: string;
};
type Item = {
  id: string;
  domain: string;
  row_number: number;
  current: Record<string, string>;
  version: number;
  note: string;
  note_version: number;
  pending_review: boolean;
  review_revision: number;
  reviewed_revision: number;
  review: Result | null;
};
type Result = {
  id: string;
  item_id: string;
  domain: string;
  row_number: number;
  fields: Record<string, string>;
  status: string;
  result_label?: string;
  explanation: string;
  evidence: string[];
  suggestion: string;
  ai_threshold: string;
  ai_value: string;
  note: string;
  note_version: number;
  pending_review?: boolean;
  review_revision?: number;
  reviewed_revision?: number;
  stale?: boolean;
};
type ReviewRow = {
  itemId: string;
  domain: string;
  fields: Record<string, string>;
  pendingReview: boolean;
  reviewRevision: number;
  reviewedRevision: number;
  note: string;
  noteVersion: number;
  item?: Item;
  result?: Result;
};
type Keyword = {
  id: string;
  domain: string;
  key: string;
  payload: Record<string, unknown>;
  version: number;
};
type Change = {
  id: string;
  domain: string;
  key: string;
  action: string;
  reason: string;
  state: string;
  payload: Record<string, unknown>;
  review_note: string;
};
type Audit = {
  id: string;
  actor_id: string;
  action: string;
  object_id: string;
  created_at: string;
  reason: string;
  before: unknown;
  after: unknown;
  actor_username: string;
  action_label: string;
  category: string;
  target_label: string;
};
const labels: Record<string, string> = {
  queued: "排队中",
  running: "审查中",
  completed: "已完成",
  partial_failed: "部分执行失败",
  passed: "通过",
  failed: "未通过",
  needs_review: "待人工确认",
  missing_keyword: "缺少关键词",
  error: "执行失败",
  pending: "待处理",
  approved: "已批准",
  rejected: "已驳回",
  create: "新增",
  update: "修改",
  delete: "删除",
  paused: "已暂停",
  pausing: "正在暂停",
  blocked: "审查已停止",
  cancelled: "已重新开始",
};
let token = ""; // 仅保存在当前页面内存，关闭/刷新后重新登录。
async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", "Bearer " + token);
  if (init.body && !(init.body instanceof FormData))
    headers.set("Content-Type", "application/json");
  const res = await fetch("/api" + path, { ...init, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: "请求失败" }));
    if (res.status === 401) window.dispatchEvent(new Event("session-expired"));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail),
    );
  }
  return res.json() as Promise<T>;
}
async function download(path: string, name: string) {
  const res = await fetch("/api" + path, {
    headers: { Authorization: "Bearer " + token },
  });
  if (!res.ok) throw new Error("下载失败，请等待审查完成或重新登录");
  const url = URL.createObjectURL(await res.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const json = (value: unknown) => JSON.stringify(value);
const PAGE = 100;
const keywordFields = { a: "测试对象", b: "用例编号", c: "用例名称", d: "测试小项", e: "关键词" };
const entryDomain = "__five_column_keywords__";
const emptyEntry = () => ({ a: "", b: "", c: "", d: "", e: "" });
const keywordText = (payload: Record<string, unknown>) =>
  Object.entries(keywordFields).map(([key, label]) => `${label}：${String(payload[key] ?? "")}`).join("\n");
const reportColumns = ["测试对象", "用例编号", "用例名称", "测试小项", "指标名称", "测试条件", "判据门限", "单位", "门限", "值", "备注", "附件链接"];
const columnWeights = [4, 5, 6, 9, 7, 6, 7, 5, 3, 5, 5, 7, 4, 4, 4, 5, 6, 8, 9];
const columnWidths = columnWeights.map(weight => `${weight / columnWeights.reduce((sum, n) => sum + n, 0) * 100}%`);
const keywordIdentity = (values: unknown[]) => JSON.stringify(values);

function AttachmentLinks({ value }: { value: string }) {
  const links = value.match(/https?:\/\/[^\s<>"，,;；]+/gi) ?? [];
  if (!links.length) return <span className="muted" title={value}>{value}</span>;
  return <div className="attachment-links">{links.map((url, index) =>
    <a key={`${index}-${url}`} href={url} target="_blank" rel="noopener noreferrer" title={url}>
      {links.length === 1 ? "附件" : `附件${index + 1}`}
    </a>)}</div>;
}

const NoteCell = React.memo(function NoteCell({ itemId, note, version, editable }: { itemId: string; note: string; version: number; editable: boolean }) {
  const [draft, setDraft] = useState(note);
  const [saved, setSaved] = useState(note);
  const [baseVersion, setBaseVersion] = useState(version);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [editing, setEditing] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const anchor = useRef<HTMLButtonElement>(null);
  const editor = useRef<HTMLTextAreaElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  const dirty = draft !== saved;
  useEffect(() => {
    if (!editing && !dirty && version > baseVersion) {
      setDraft(note); setSaved(note); setBaseVersion(version);
    }
  }, [note, version, dirty, baseVersion, editing]);
  useEffect(() => {
    if (editing) editor.current?.focus({ preventScroll: true });
  }, [editing]);
  useEffect(() => {
    if (!editing || saving) return;
    const close = (event: PointerEvent) => {
      if (popover.current?.contains(event.target as Node) || anchor.current?.contains(event.target as Node)) return;
      setDraft(saved); setEditing(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [editing, saving, saved]);
  const openEditor = () => {
    const rect = anchor.current?.getBoundingClientRect();
    if (!rect) return;
    setPosition({ left: Math.max(8, Math.min(rect.right - 260, window.innerWidth - 268)),
      top: rect.bottom + 150 < window.innerHeight ? rect.bottom + 4 : Math.max(8, rect.top - 145) });
    setDraft(saved); setError(""); setEditing(true);
  };
  if (!editable) return <span className="note-text">{note}</span>;
  const save = async (value: string) => {
      setSaving(true); setError("");
      try {
        const updated = await api<{ note: string; version: number }>(`/items/${itemId}/note`, {
          method: "PATCH", body: json({ note: value, version: baseVersion }),
        });
        setSaved(updated.note); setDraft(updated.note); setBaseVersion(updated.version); setEditing(false);
      } catch (e) { setError((e as Error).message); }
      finally { setSaving(false); }
  };
  return <>
    <button ref={anchor} className="note-display" title={saved || "点击编辑备注"} onClick={openEditor}>
      {saved || <span className="muted">点击添加</span>}
    </button>
    {editing && createPortal(<div ref={popover} className="note-editor note-popover" role="dialog" aria-label="编辑备注"
      style={position}>
    <textarea ref={editor} aria-label="用户备注" rows={3} maxLength={16000} value={draft}
      disabled={saving} onChange={e => setDraft(e.target.value)} onKeyDown={e => {
        if (e.key === "Escape" && !saving) { setDraft(saved); setEditing(false); }
      }} />
    <div className="note-actions">
      <button className="primary" disabled={saving} title="保存备注" onClick={() => void save(draft)}>确认</button>
      <button disabled={saving} title="清空并保存备注" onClick={() => void save("")}>清除</button>
    </div>
    {error && <span role="alert" className="note-error">{error}</span>}
  </div>, document.body)}
  </>;
});
function MultiSelect({ label, allLabel, options, value, onChange }: {
  label: string; allLabel: string; options: string[]; value: string[]; onChange: (value: string[]) => void;
}) {
  const choices = Array.from(new Set([...options, ...value]));
  const summary = value.length ? value.length <= 2 ? value.join("、") : `已选 ${value.length} 项` : allLabel;
  return <details className="multi-select">
    <summary aria-label={label} title={value.length ? value.join("、") : allLabel}>{summary}</summary>
    <div className="multi-select-panel" role="group" aria-label={label + "选项"}>
      <button type="button" onClick={() => onChange([])}>清除选择（显示全部）</button>
      {choices.map(option => <label key={option}>
        <input type="checkbox" checked={value.includes(option)} onChange={event =>
          onChange(event.target.checked ? [...value, option] : value.filter(item => item !== option))} />
        <span>{option}</span>
      </label>)}
      {!choices.length && <span className="muted small">暂无选项</span>}
    </div>
  </details>;
}

function filterQuery(domains: string[], results: string[] = []) {
  const params = new URLSearchParams();
  domains.forEach(domain => params.append("domain", domain));
  results.forEach(result => params.append("result", result));
  return params.size ? "&" + params.toString() : "";
}

function App() {
  const [user, setUser] = useState<User | null>(null),
    [mode, setMode] = useState("");
  const [name, setName] = useState(""),
    [password, setPassword] = useState("");
  const [tab, setTab] = useState("reports"),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [reports, setReports] = useState<Report[]>([]),
    [selected, setSelected] = useState<Report | null>(null);
  const [items, setItems] = useState<Item[]>([]),
    [runs, setRuns] = useState<Run[]>([]),
    [run, setRun] = useState<Run | null>(null),
    [results, setResults] = useState<Result[]>([]);
  const [itemPage, setItemPage] = useState(0),
    [resultPage, setResultPage] = useState(0),
    [filter, setFilter] = useState<string[]>([]);
  const [resultOptions, setResultOptions] = useState<string[]>([]);
  const [reportPage, setReportPage] = useState(0),
    [runPage, setRunPage] = useState(0),
    [changePage, setChangePage] = useState(0),
    [auditPage, setAuditPage] = useState(0);
  const [keywords, setKeywords] = useState<Keyword[]>([]),
    [changes, setChanges] = useState<Change[]>([]),
    [users, setUsers] = useState<User[]>([]),
    [audit, setAudit] = useState<Audit[]>([]);
  const [domains, setDomains] = useState<string[]>([]),
    [domainFilter, setDomainFilter] = useState<string[]>([]),
    [reviewMode, setReviewMode] = useState<"full" | "missing">("full"),
    [authAlert, setAuthAlert] = useState("");
  const [verdictDetail, setVerdictDetail] = useState<Result | null>(null);
  const [keywordDialog, setKeywordDialog] = useState(false);
  const [reviewRefresh, setReviewRefresh] = useState(0);
  const itemsQuery = useRef("");
  const keywordIndex = useMemo(() => new Map(keywords.filter(k => k.domain === entryDomain).map(k =>
    [keywordIdentity([k.payload.a, k.payload.b, k.payload.c, k.payload.d]), k])), [keywords]);
  useEffect(() => {
    if (!uploading) return;
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => { document.body.style.overflow = previous; };
  }, [uploading]);
  const [action, setAction] = useState("create"),
    [entry, setEntry] = useState<Record<string, string>>(emptyEntry),
    [kwReason, setKwReason] = useState(""),
    [linkResult, setLinkResult] = useState<string | null>(null);
  const [decision, setDecision] = useState<{
      id: string;
      approve: boolean;
    } | null>(null),
    [decisionNote, setDecisionNote] = useState("");
  const [permission, setPermission] = useState<User | null>(null),
    [permissionReason, setPermissionReason] = useState("");
  const act = async (fn: () => Promise<void>, quiet = false) => {
    setBusy(true);
    setError("");
    if (!quiet) setNotice("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    const expire = () => {
      token = "";
      setUser(null);
      setPassword("");
      setSelected(null);
      setRun(null);
      setReports([]);
      setAuthAlert("");
    };
    window.addEventListener("session-expired", expire);
    return () => window.removeEventListener("session-expired", expire);
  }, []);
  const loadReports = useCallback(async () => {
    setReports(
      await api<Report[]>(
        "/reports?limit=" + PAGE + "&offset=" + reportPage * PAGE,
      ),
    );
  }, [reportPage]);
  useEffect(() => {
    if (user && tab === "reports")
      loadReports().catch((e) => setError(e.message));
  }, [user, tab, loadReports]);
  useEffect(() => {
    if (!user) return;
    const load = async () => {
      if (tab === "reports") setKeywords(await api<Keyword[]>("/keywords"));
      if (tab === "keywords") {
        const [k, c] = await Promise.all([
          api<Keyword[]>("/keywords"),
          api<Change[]>(
            "/keyword-requests?limit=" + PAGE + "&offset=" + changePage * PAGE,
          ),
        ]);
        setKeywords(k);
        setChanges(c);
      }
      if (tab === "users" && user.role === "admin")
        setUsers(await api<User[]>("/users"));
      if (tab === "audit")
        setAudit(
          await api<Audit[]>(
            "/audit?limit=" + PAGE + "&offset=" + auditPage * PAGE,
          ),
        );
    };
    load().catch((e) => setError(e.message));
  }, [user, tab, changePage, auditPage]);
  useEffect(() => {
    if (!selected || tab !== "reports") return;
    let cancelled = false;
    // Background refreshes must keep the existing rows mounted and preserve scroll.
    const query = json([selected.id, itemPage, domainFilter]);
    if (itemsQuery.current !== query) setItems([]);
    itemsQuery.current = query;
    api<Item[]>(
      "/reports/" +
        selected.id +
        "/items?limit=" +
        PAGE +
        "&offset=" +
        itemPage * PAGE + filterQuery(domainFilter),
    )
      .then((v) => {
        if (!cancelled) setItems(v);
      })
      .catch((e) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [selected, itemPage, domainFilter, tab, run?.state, reviewRefresh]);
  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    api<string[]>(`/reports/${selected.id}/domains`).then(v => { if (!cancelled) setDomains(v); })
      .catch(e => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [selected?.id]);
  useEffect(() => {
    if (run?.error_code === "auth_session_failed") setAuthAlert(run.error_message);
  }, [run?.id, run?.error_code, run?.error_message]);
  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    api<Run[]>(
      "/reports/" +
        selected.id +
        "/runs?limit=" +
        PAGE +
        "&offset=" +
        runPage * PAGE,
    )
      .then((v) => {
        if (!cancelled) setRuns(v);
      })
      .catch((e) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [selected, runPage, run?.id, run?.state]);
  useEffect(() => {
    if (!run || tab !== "reports") return;
    let cancelled = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const [r, rows, options] = await Promise.all([
          api<Run>("/runs/" + run.id),
          api<Result[]>(
            "/runs/" +
              run.id +
              "/results?limit=" +
              PAGE +
              "&offset=" +
              resultPage * PAGE +
              filterQuery(domainFilter, filter),
          ),
          api<string[]>("/runs/" + run.id + "/result-options"),
        ]);
        if (!cancelled) {
          setRun(r);
          setResults(rows);
          setResultOptions(options);
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
      if (!cancelled && ["queued", "running", "pausing"].includes(run.state)) {
        timer = window.setTimeout(poll, 1000);
      }
    };
    void poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [run?.id, run?.state, resultPage, filter, domainFilter, tab, reviewRefresh]);
  const pager = (page: number, setPage: (n: number) => void, count: number) => (
    <div className="pager">
      <button disabled={page === 0} onClick={() => setPage(page - 1)}>
        上一页
      </button>
      <span>第 {page + 1} 页</span>
      <button disabled={count < PAGE} onClick={() => setPage(page + 1)}>
        下一页
      </button>
    </div>
  );
  const chooseReport = (r: Report) => {
    setSelected(r);
    setRun(null);
    setResults([]);
    setDomainFilter([]); setDomains([]); setAuthAlert(""); setResultOptions([]);
    setItemPage(0);
    setRunPage(0);
    setResultPage(0);
    setFilter([]);
  };
  const chooseRun = (r: Run) => {
    setResults([]);
    setRun(r);
    setReviewMode(r.selection_mode ?? "full");
    setResultPage(0);
    setFilter([]); setResultOptions([]);
  };
  const controlRun = (operation: "pause" | "resume" | "restart") => act(async () => {
    if (!run) return;
    const updated = await api<Run>(`/runs/${run.id}/${operation}`, {
      method: "POST", ...(operation === "restart" ? { body: json({ selection_mode: reviewMode }) } : {}),
    });
    if (operation === "restart") chooseRun(updated);
    else setRun(updated);
    setNotice(operation === "restart" ? `已按${updated.selection_mode === "missing" ? "查漏补缺" : "全量审查"}重新开始，旧批次保留。` : operation === "pause" ? "已请求暂停，将在当前项结束后生效。" : "已继续审查。");
  });
  const kwFrom = (k: Keyword) => {
    setEntry(Object.fromEntries(Object.keys(keywordFields).map(key => [key, String(k.payload[key] ?? "")])));
    setAction("update");
    setLinkResult(null);
    document.querySelector(".keyword-change-form")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const linkedKeyword = keywords.find(
    (k) => k.domain === entryDomain && ["a", "b", "c", "d"].every(key => k.payload[key] === entry[key]),
  );
  const submitKeyword = () =>
    act(async () => {
      await api("/keyword-entry-requests", {
        method: "POST",
        body: json({
          action,
          entry,
          base_version: action === "create" ? 0 : (linkedKeyword?.version ?? 0),
          reason: kwReason,
          result_id: linkResult,
        }),
      });
      setChanges(
        await api<Change[]>(
          "/keyword-requests?limit=" + PAGE + "&offset=" + changePage * PAGE,
        ),
      );
      setKeywords(await api<Keyword[]>("/keywords"));
      setKwReason("");
      setLinkResult(null);
      setKeywordDialog(false);
      setReviewRefresh(value => value + 1);
      setNotice(user?.role === "admin" ? "关键词已生效，相关项已恢复待审查" : "关键词变更已提交审批");
    });

  const adjustKeyword = (row: ReviewRow) => act(async () => {
    const entries = await api<Keyword[]>("/keywords");
    setKeywords(entries);
    const next = {
      a: row.fields["测试对象"] ?? "", b: row.fields["用例编号"] ?? "",
      c: row.fields["用例名称"] ?? "", d: row.fields["测试小项"] ?? "", e: "",
    };
    const existing = entries.find(k => k.domain === entryDomain &&
      ["a", "b", "c", "d"].every(key => k.payload[key] === next[key as keyof typeof next]));
    if (existing) kwFrom(existing);
    else { setEntry(next); setAction("create"); }
    setLinkResult(row.result?.id ?? null);
    setKwReason("审查页面调整关键词");
    setKeywordDialog(true);
  });
  const rowKeyword = (row: ReviewRow) => keywordIndex.get(keywordIdentity(
    ["测试对象", "用例编号", "用例名称", "测试小项"].map(label => row.fields[label] ?? "")));
  const changeReviewState = (row: ReviewRow, state: string) => act(async () => {
    await api(`/items/${row.itemId}/review-state`, { method: "PATCH",
      body: json({ state, revision: row.reviewRevision, reviewed_revision: row.reviewedRevision }) });
    setReviewRefresh(value => value + 1);
  }, true);
  const tableRows: ReviewRow[] = run
    ? results.map(r => ({ itemId: r.item_id, domain: r.domain, fields: r.fields, note: r.note,
        pendingReview: r.pending_review ?? true, reviewRevision: r.review_revision ?? 1, reviewedRevision: r.reviewed_revision ?? 0,
        noteVersion: r.note_version, result: r }))
    : items.map(i => ({ itemId: i.id, domain: i.domain, fields: i.current, note: i.note,
        pendingReview: i.pending_review, reviewRevision: i.review_revision, reviewedRevision: i.reviewed_revision,
        noteVersion: i.note_version, item: i, result: i.review ? { ...i.review, pending_review: i.pending_review } : undefined }));

  const reviewTable = () => <div className="review-scroll" aria-label="测试详情表格">
    <table className="review-table">
      <colgroup>
        {columnWidths.map((width, index) => <col key={index} style={{ width }} />)}
      </colgroup>
      <thead>
        <tr className="review-groups">
          <th rowSpan={2} className="domain-cell">领域</th>
          <th colSpan={reportColumns.length}>原始测试详情</th>
          <th colSpan={4} className="ai-group">AI 审查</th>
          <th rowSpan={2}>当前状态</th><th rowSpan={2} className="row-actions">关键词</th>
        </tr>
        <tr>{reportColumns.map(c => <th key={c}>{c}</th>)}
          <th className="ai-cell">门限</th><th className="ai-cell">值</th>
          <th className="ai-cell">结果</th><th className="ai-cell">备注</th>
        </tr>
      </thead>
      <tbody>{tableRows.map(row => {
        const r = row.result;
        const pending = !!r?.pending_review && (!["error", "missing_keyword"].includes(r.status) || !!r.stale);
        const status = pending ? "pending" : r?.status ?? "pending";
        const canEdit = selected?.owner_id === user?.id;
        return <tr key={row.itemId} className={`review-row review-${status}`}>
          <td className="domain-cell">{row.domain}</td>
          {reportColumns.map(c => <td key={c}>{c === "附件链接"
            ? <AttachmentLinks value={row.fields[c] ?? ""} /> : row.fields[c] ?? ""}</td>)}
          <td className="ai-cell">{pending ? "" : r?.ai_threshold ?? ""}</td>
          <td className="ai-cell">{pending ? "" : r?.ai_value ?? ""}</td>
          <td className="ai-cell">{r && status !== "pending" && <button
            className={`verdict-button verdict-${status}`} onClick={() => setVerdictDetail(r)}
            title="查看审查依据和建议">{r.result_label || (status === "needs_review" ? "待确认" : labels[status])}</button>}</td>
          <td className="ai-cell"><NoteCell itemId={row.itemId} note={row.note ?? ""}
            version={row.noteVersion ?? 0} editable={canEdit && !uploading} /></td>
          <td>{canEdit ? <select className={`review-state-select ${row.pendingReview ? "review-pending" : "review-done"}`} aria-label="修改审查状态"
            value={row.pendingReview ? "pending" : "reviewed"}
            disabled={busy || !!run && ["queued", "running", "pausing", "paused"].includes(run.state)}
            onChange={event => void changeReviewState(row, event.target.value)}>
            <option value="pending">待审查</option><option value="reviewed">已审查</option>
          </select> : <span className={`review-state ${row.pendingReview ? "review-pending" : "review-done"}`}>{row.pendingReview ? "待审查" : "已审查"}</span>}</td>
          <td className="row-actions keyword-cell"><div className="keyword-inline"><span>{String(rowKeyword(row)?.payload.e ?? "未配置")}</span>
            {canEdit && <button disabled={busy} aria-label="编辑当前项关键词" title="编辑关键词"
              onClick={() => void adjustKeyword(row)}>✎</button>}</div></td>
        </tr>;
      })}</tbody>
    </table>
    {!tableRows.length && <p className="empty">{run ? "当前筛选暂无结果。" : "暂无测试项。"}</p>}
  </div>;
  if (!user)
    return (
      <main className="login">
        <div className="mark">审</div>
        <p className="eyebrow">TEST REPORT REVIEW</p>
        <h1>单元测试报告 AI 审查</h1>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void act(async () => {
              const res = await api<{
                access_token: string;
                user: User;
                mode: string;
              }>("/auth/login", {
                method: "POST",
                body: json({ username: name, password }),
              });
              token = res.access_token;
              setUser(res.user);
              setMode(res.mode);
              setPassword("");
              setTab("reports");
            });
          }}
        >
          <label>
            内网账号
            <input
              required
              autoComplete="username"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            密码
            <input
              required
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          <button className="primary" disabled={busy}>
            登录系统
          </button>
        </form>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
      </main>
    );
  return (
    <div className="layout" inert={uploading} aria-busy={uploading}>
      <aside>
        <div className="brand">
          <span className="mark">审</span>
          <b>审查工作台</b>
        </div>
        <p className="eyebrow">WORKSPACE</p>
        <nav>
          {[
            ["reports", "报告与审查"],
            ["keywords", "领域关键词"],
            ["audit", "操作记录"],
            ...(user.role === "admin" ? [["users", "用户权限"]] : []),
          ].map(([id, title]) => (
            <button
              key={id}
              className={tab === id ? "active" : ""}
              onClick={() => {
                setTab(id);
                setError("");
                setNotice("");
              }}
            >
              {title}
            </button>
          ))}
        </nav>
        <div className="account">
          <b>{user.username}</b>
          <p>{user.role === "admin" ? "管理员" : "普通用户"}</p>
          <button
            onClick={() => void act(async () => {
              await api("/auth/logout", { method: "POST" });
              token = "";
              setUser(null);
              setSelected(null);
              setRun(null);
              setReports([]);
              setItems([]);
              setResults([]);
              setKeywords([]);
              setChanges([]);
              setUsers([]);
              setAudit([]);
              setAuthAlert("");
              setPermission(null);
              setDecision(null);
            })}
          >
            退出登录
          </button>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <p className="eyebrow">QUALITY / REVIEW</p>
            <h1>
              {
                {
                  reports: "报告与审查",
                  keywords: "领域关键词",
                  audit: "操作记录",
                  users: "用户权限",
                }[tab]
              }
            </h1>
          </div>
          <span className="pill">
            {mode === "demo" ? "演示环境" : mode === "local" ? "本地接入" : "正式环境"}
          </span>
        </header>
        {error && (
          <div role="alert" className="error">
            {error}
            <button onClick={() => setError("")}>关闭</button>
          </div>
        )}
        {notice && (
          <div role="status" className="success">
            {notice}
          </div>
        )}
        {tab === "reports" && (
          <>
            <section className="card upload">
              <div>
                <h2>上传单元测试报告</h2>
              </div>
              <label className="button primary">
                {busy ? "正在处理…" : "选择 Excel 上传"}
                <input
                  className="file-input"
                  aria-label="上传 Excel"
                  type="file"
                  accept=".xlsx"
                  disabled={busy}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    e.target.value = "";
                    if (file)
                      void act(async () => {
                        setUploading(true);
                        try {
                        const form = new FormData();
                        form.append("file", file);
                        const report = await api<Report>("/reports", {
                          method: "POST",
                          body: form,
                        });
                        setReportPage(0);
                        await loadReports();
                        chooseReport(report);
                        setNotice(
                          "解析完成，已筛选 " + report.item_count + " 个测试项",
                        );
                        } finally {
                          setUploading(false);
                        }
                      });
                  }}
                />
              </label>
            </section>
            <div className="workspace">
              <section className="card report-list">
                <label className="report-picker">
                  <span>{user.role === "admin" ? "全部报告" : "我的报告"}</span>
                  <select aria-label="选择报告" value={reports.some(r => r.id === selected?.id) ? selected!.id : ""}
                    onChange={e => { const report = reports.find(r => r.id === e.target.value); if (report) chooseReport(report); }}>
                    <option value="">{reports.length ? "选择一份报告" : "暂无报告，请先上传"}</option>
                    {reports.map(r => <option key={r.id} value={r.id}>{r.filename} · {r.item_count} 项 · {new Date(r.created_at + "Z").toLocaleDateString()}</option>)}
                  </select>
                </label>
                <span className="report-picker-meta">{selected ? `${selected.item_count} 个测试项 · 版本 ${selected.revision}` : ""}</span>
                {pager(reportPage, setReportPage, reports.length)}
              </section>
              <section className="card detail">
                {!selected ? (
                  <div className="empty">
                    <h2>选择一份报告</h2>
                  </div>
                ) : (
                  <>
                    <div className="section-head">
                      <div>
                        <h2>{selected.filename}</h2>
                        <p>{selected.item_count} 个测试项</p>
                      </div>
                      <div className="actions">
                        {selected.owner_id === user.id && <button disabled={busy}
                          onClick={() => {
                            const report = selected;
                            if (!window.confirm(`清除“${report.filename}”的历史？将永久删除该报告、原始 Excel、全部审查批次、结果和备注。关键词及审批记录会保留。此操作无法撤销。`)) return;
                            void act(async () => {
                              const response = await api<{ warning: string }>("/reports/" + report.id, { method: "DELETE" });
                              setSelected(null); setRun(null); setRuns([]); setItems([]); setResults([]);
                              await loadReports();
                              setNotice(response.warning || "已清除该报告及全部审查历史");
                            });
                          }}>清除历史</button>}
                        <button
                          disabled={busy}
                          onClick={() =>
                            void act(async () => {
                              await download(
                                "/reports/" + selected.id + "/original",
                                selected.filename,
                              );
                            })
                          }
                        >
                          原始文件
                        </button>
                        {selected.owner_id === user.id && (
                          <>
                          <select className="review-mode" aria-label="审查模式" value={reviewMode}
                            onChange={e => setReviewMode(e.target.value as "full" | "missing")}>
                            <option value="full">全量审查</option><option value="missing">查漏补缺</option>
                          </select>
                          <button
                            className="primary"
                            disabled={busy || !!run && ["queued", "running", "pausing", "paused"].includes(run.state)}
                            onClick={() =>
                              void act(async () => {
                                const r = await api<Run>(
                                  "/reports/" + selected.id + "/runs",
                                  { method: "POST", body: json({ selection_mode: reviewMode }) },
                                );
                                chooseRun(r);
                                setNotice("审查已提交，等待后台进程执行");
                              })
                            }
                          >
                            AI 审查
                          </button>
                          </>
                        )}
                      </div>
                    </div>
                    <details className="history-panel">
                    <summary>审查历史 <span>{runs.length} 个批次</span></summary>
                    <div className="run-list">
                      {runs.map((r) => (
                        <button
                          key={r.id}
                          className={run?.id === r.id ? "selected" : ""}
                          onClick={() => chooseRun(r)}
                        >
                          {r.id.slice(0, 8)} · {labels[r.state]} · {r.completed}
                          /{r.total}
                        </button>
                      ))}
                    </div>
                    {pager(runPage, setRunPage, runs.length)}
                    </details>
                    {run && <>
                      <div className="progress-head">
                        <b>{labels[run.state]} · {run.completed}/{run.total}</b>
                        <span>{run.failed} 项执行失败</span>
                      </div>
                      <progress max={run.total || 1} value={run.completed} />
                      <div className="section-head">
                        <MultiSelect label="筛选审查结果" allLabel="全部审查结果" options={resultOptions}
                          value={filter} onChange={value => { setFilter(value); setResultPage(0); }} />
                        <div className="actions">
                          <button onClick={() => { setItemPage(0); setRun(null); }}>查看全部测试项</button>
                          {selected.owner_id === user.id && <>
                            {["queued", "running", "pausing"].includes(run.state) && <button disabled={busy || run.state === "pausing"}
                              onClick={() => void controlRun("pause")}>{run.state === "pausing" ? "正在暂停…" : "暂停"}</button>}
                            {["paused", "blocked"].includes(run.state) && <button disabled={busy} onClick={() => void controlRun("resume")}>继续</button>}
                            <button disabled={busy} onClick={() => void controlRun("restart")}>重新开始（{reviewMode === "missing" ? "查漏补缺" : "全量"}）</button>
                          </>}
                          <button disabled={["queued", "running", "pausing", "paused"].includes(run.state) || busy}
                            onClick={() => void act(async () => {
                              await download("/runs/" + run.id + "/export", "审查结果-" + run.id.slice(0, 8) + ".xlsx");
                            })}>导出 Excel</button>
                        </div>
                      </div>
                    </>}
                    <div className="domain-filter">
                      <span>领域</span><MultiSelect label="领域筛选" allLabel="全部领域" options={domains}
                        value={domainFilter} onChange={value => { setDomainFilter(value); setItemPage(0); setResultPage(0); }} />
                    </div>
                    <h3>{run ? "审查结果" : "解析后的测试项"}</h3>
                    {reviewTable()}
                    {pager(run ? resultPage : itemPage, run ? setResultPage : setItemPage, tableRows.length)}
                  </>
                )}
              </section>
            </div>
          </>
        )}
        {tab === "keywords" && (
          <div className="columns keyword-workspace">
            <section className="card">
              <h2>
                {user.role === "admin"
                  ? "关键词变更（直接生效）"
                  : "提交关键词变更申请"}
              </h2>
              {linkResult && (
                <p className="banner">
                  已关联审查项 {linkResult.slice(0, 8)}
                  <button onClick={() => setLinkResult(null)}>取消关联</button>
                </p>
              )}
              <form className="keyword-change-form"
                onSubmit={(e) => {
                  e.preventDefault();
                  void submitKeyword();
                }}
              >
                <label>
                  操作
                  <select
                    value={action}
                    onChange={(e) => setAction(e.target.value)}
                  >
                    {["create", "update", "delete"].map((a) => (
                      <option key={a} value={a}>
                        {labels[a]}
                      </option>
                    ))}
                  </select>
                </label>
                {Object.entries(keywordFields).map(([key, label]) => (
                  <label key={key}>
                    {label}
                    <input
                      required={key === "e"}
                      value={entry[key]}
                      disabled={key === "e" ? action === "delete" : action !== "create"}
                      onChange={(e) => setEntry({ ...entry, [key]: e.target.value })}
                    />
                  </label>
                ))}
                <label>
                  申请原因
                  <textarea
                    required
                    value={kwReason}
                    onChange={(e) => setKwReason(e.target.value)}
                  />
                </label>
                <button className="primary" disabled={busy}>
                  提交变更
                </button>
              </form>
            </section>
            <section className="card">
              <h2>当前关键词</h2>
              {keywords.length === 0 && (
                <p className="empty">尚未配置关键词。</p>
              )}
              {keywords.length > 0 && <div className="keyword-table-scroll"><table className="keyword-table">
                <thead><tr>{Object.values(keywordFields).map(label => <th key={label}>{label}</th>)}<th>版本</th><th>操作</th></tr></thead>
                <tbody>{keywords.map(k => <tr key={k.id}>
                  {Object.keys(keywordFields).map(key => <td key={key}>{k.domain === entryDomain ? String(k.payload[key] ?? "") : key === "a" ? k.domain : key === "e" ? JSON.stringify(k.payload) : k.key}</td>)}
                  <td>{k.version}</td><td>{k.domain === entryDomain && <button onClick={() => kwFrom(k)}>编辑</button>}</td>
                </tr>)}</tbody>
              </table></div>}
            </section>
            <section className="card full">
              <h2>{user.role === "admin" ? "关键词审批记录" : "我的申请"}</h2>
              {changes.map((c) => (
                <article key={c.id} className="keyword">
                  <div className="section-head">
                    <b>
                      {labels[c.action]} · {c.domain === entryDomain ? String(c.payload.c) : `${c.domain} / ${c.key}`}
                    </b>
                    <span className="pill">{labels[c.state]}</span>
                  </div>
                  <p>{c.reason}</p>
                  <pre>{c.domain === entryDomain ? keywordText(c.payload) : JSON.stringify(c.payload, null, 2)}</pre>
                  {c.review_note && <p>审批说明：{c.review_note}</p>}
                  {user.role === "admin" && c.state === "pending" && (
                    <div className="actions">
                      <button
                        onClick={() => {
                          setDecision({ id: c.id, approve: true });
                          setDecisionNote("");
                        }}
                      >
                        批准
                      </button>
                      <button
                        onClick={() => {
                          setDecision({ id: c.id, approve: false });
                          setDecisionNote("");
                        }}
                      >
                        驳回
                      </button>
                    </div>
                  )}
                </article>
              ))}
              {pager(changePage, setChangePage, changes.length)}
            </section>
          </div>
        )}
        {tab === "audit" && (
          <section className="card activity-card">
            <h2>
              {user.role === "admin" ? "全体用户操作记录" : "我的操作记录"}
            </h2>
            <div className="activity-table-wrap"><table className="activity-table">
              <thead><tr><th>操作时间</th><th>操作人</th><th>操作</th><th>报告 / 关键词</th><th>说明</th></tr></thead>
              <tbody>{audit.map(a => <tr key={a.id}>
                <td className="activity-time"><span>{new Date(a.created_at + "Z").toLocaleDateString()}</span>
                  <small>{new Date(a.created_at + "Z").toLocaleTimeString()}</small></td>
                <td><div className="actor-name"><span className="actor-avatar" aria-hidden="true">{a.actor_username.slice(0, 1).toUpperCase()}</span>{a.actor_username}</div></td>
                <td><span className={`activity-tag activity-${a.category}`}>{a.action_label}</span></td>
                <td className="activity-target">{a.target_label}</td><td className="activity-reason">{a.reason || "—"}</td>
              </tr>)}</tbody>
            </table></div>
            {!audit.length && <p className="empty">暂无操作记录。</p>}
            {pager(auditPage, setAuditPage, audit.length)}
          </section>
        )}
        {tab === "users" && user.role === "admin" && (
          <section className="card">
            <h2>内网账号与系统权限</h2>
            <p className="muted">
              账号通过内网鉴权首次登录后登记；本系统管理角色与启用状态。
            </p>
            <table>
              <thead>
                <tr>
                  <th>账号</th>
                  <th>角色</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id}>
                    <td>{u.username}</td>
                    <td>{u.role === "admin" ? "管理员" : "普通用户"}</td>
                    <td>{u.active ? "启用" : "停用"}</td>
                    <td>
                      <button
                        disabled={u.id === user.id}
                        onClick={() => {
                          setPermission({ ...u });
                          setPermissionReason("");
                        }}
                      >
                        配置权限
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}
      </main>
      {authAlert && <div className="overlay"><section role="alertdialog" aria-modal="true" aria-label="审查已停止" className="modal card">
        <h2>审查已停止</h2><p>{authAlert}</p><p className="muted">请联系管理员处理，重新登录后可继续，或按所选模式重新开始。</p>
        <button className="primary" onClick={() => setAuthAlert("")}>我知道了</button>
      </section></div>}
      {uploading && createPortal(<div className="upload-overlay">
        <section role="dialog" aria-modal="true" aria-label="正在解析 Excel" className="upload-progress card">
          <div className="loading-spinner" aria-hidden="true" />
          <h2>正在上传并解析 Excel</h2><p role="status">请稍候…</p>
        </section>
      </div>, document.body)}
      {keywordDialog && <div className="overlay">
        <section role="dialog" aria-modal="true" aria-label="编辑当前项关键词" className="modal card keyword-modal"
          onKeyDown={event => { if (event.key === "Escape" && !busy) setKeywordDialog(false); }}>
          <div className="section-head"><h2>{action === "create" ? "新增当前项关键词" : "编辑当前项关键词"}</h2>
            <button disabled={busy} onClick={() => setKeywordDialog(false)}>关闭</button></div>
          {error && <p role="alert" className="error">{error}</p>}
          <form onSubmit={event => { event.preventDefault(); void submitKeyword(); }}>
            <div className="keyword-identity">{Object.entries(keywordFields).filter(([key]) => key !== "e").map(([key, label]) =>
              <label key={key}>{label}<input readOnly value={entry[key]} /></label>)}</div>
            <label>关键词<textarea autoFocus required maxLength={32767} rows={5} value={entry.e}
              onChange={event => setEntry({ ...entry, e: event.target.value })} /></label>
            <label>变更原因<textarea required value={kwReason} onChange={event => setKwReason(event.target.value)} /></label>
            <div className="actions"><button type="button" disabled={busy} onClick={() => setKeywordDialog(false)}>取消</button>
              <button className="primary" disabled={busy}>{user.role === "admin" ? "保存并生效" : "提交审批"}</button></div>
          </form>
        </section>
      </div>}
      {verdictDetail && <div className="overlay" onClick={() => setVerdictDetail(null)}>
        <section role="dialog" aria-modal="true" aria-label="AI 审查详情" className="modal card verdict-modal"
          onClick={e => e.stopPropagation()}>
          <div className="section-head"><h2>AI 审查详情</h2><button onClick={() => setVerdictDetail(null)}>关闭</button></div>
          <p className="muted">{verdictDetail.domain} · {verdictDetail.fields["用例名称"]}</p>
          <span className={`activity-tag verdict-${verdictDetail.status}`}>{verdictDetail.result_label || labels[verdictDetail.status]}</span>
          <dl><dt>识别门限</dt><dd>{verdictDetail.ai_threshold || "—"}</dd>
            <dt>识别值</dt><dd>{verdictDetail.ai_value || "—"}</dd>
            <dt>审查说明</dt><dd>{verdictDetail.explanation}</dd>
            <dt>判断依据</dt><dd>{verdictDetail.evidence.join("；") || "—"}</dd>
            <dt>修改建议</dt><dd>{verdictDetail.suggestion || "—"}</dd></dl>
        </section>
      </div>}
      {decision && (
        <div className="overlay">
          <section
            role="dialog"
            aria-modal="true"
            aria-label="关键词审批"
            className="modal card"
          >
            <h2>{decision.approve ? "批准" : "驳回"}关键词申请</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void act(async () => {
                  await api("/keyword-requests/" + decision.id + "/decision", {
                    method: "POST",
                    body: json({
                      approve: decision.approve,
                      note: decisionNote,
                    }),
                  });
                  setDecision(null);
                  setKeywords(await api<Keyword[]>("/keywords"));
                  setChanges(
                    await api<Change[]>(
                      "/keyword-requests?limit=" +
                        PAGE +
                        "&offset=" +
                        changePage * PAGE,
                    ),
                  );
                  setNotice("审批已保存");
                });
              }}
            >
              <label>
                审批说明
                <textarea
                  required
                  value={decisionNote}
                  onChange={(e) => setDecisionNote(e.target.value)}
                />
              </label>
              <div className="actions">
                <button type="button" onClick={() => setDecision(null)}>
                  取消
                </button>
                <button className="primary" disabled={busy}>
                  确认{decision.approve ? "批准" : "驳回"}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
      {permission && (
        <div className="overlay">
          <section
            role="dialog"
            aria-modal="true"
            aria-label="配置用户权限"
            className="modal card"
          >
            <h2>配置权限 · {permission.username}</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void act(async () => {
                  await api("/users/" + permission.id, {
                    method: "PATCH",
                    body: json({
                      role: permission.role,
                      active: permission.active,
                      reason: permissionReason,
                    }),
                  });
                  setPermission(null);
                  setUsers(await api<User[]>("/users"));
                  setNotice("用户权限已更新");
                });
              }}
            >
              <label>
                角色
                <select
                  value={permission.role}
                  onChange={(e) =>
                    setPermission({ ...permission, role: e.target.value })
                  }
                >
                  <option value="user">普通用户</option>
                  <option value="admin">管理员</option>
                </select>
              </label>
              <label>
                状态
                <select
                  value={String(permission.active)}
                  onChange={(e) =>
                    setPermission({
                      ...permission,
                      active: e.target.value === "true",
                    })
                  }
                >
                  <option value="true">启用</option>
                  <option value="false">停用</option>
                </select>
              </label>
              <label>
                变更原因
                <textarea
                  required
                  value={permissionReason}
                  onChange={(e) => setPermissionReason(e.target.value)}
                />
              </label>
              <div className="actions">
                <button type="button" onClick={() => setPermission(null)}>
                  取消
                </button>
                <button className="primary" disabled={busy}>
                  保存权限
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
