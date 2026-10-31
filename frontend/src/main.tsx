import React, { useEffect, useState, useCallback } from "react";
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
};
type Item = {
  id: string;
  domain: string;
  row_number: number;
  current: Record<string, string>;
  version: number;
};
type Result = {
  id: string;
  item_id: string;
  domain: string;
  row_number: number;
  fields: Record<string, string>;
  status: string;
  explanation: string;
  evidence: string[];
  suggestion: string;
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
};
const labels: Record<string, string> = {
  queued: "排队中",
  running: "审查中",
  completed: "已完成",
  partial_failed: "部分执行失败",
  passed: "通过",
  failed: "不通过",
  needs_review: "待人工确认",
  error: "执行失败",
  pending: "待处理",
  approved: "已批准",
  rejected: "已驳回",
  create: "新增",
  update: "修改",
  delete: "删除",
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
function App() {
  const [user, setUser] = useState<User | null>(null),
    [mode, setMode] = useState("");
  const [name, setName] = useState("demo"),
    [password, setPassword] = useState("");
  const [tab, setTab] = useState("reports"),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [reports, setReports] = useState<Report[]>([]),
    [selected, setSelected] = useState<Report | null>(null);
  const [items, setItems] = useState<Item[]>([]),
    [runs, setRuns] = useState<Run[]>([]),
    [run, setRun] = useState<Run | null>(null),
    [results, setResults] = useState<Result[]>([]);
  const [itemPage, setItemPage] = useState(0),
    [resultPage, setResultPage] = useState(0),
    [filter, setFilter] = useState("");
  const [reportPage, setReportPage] = useState(0),
    [runPage, setRunPage] = useState(0),
    [changePage, setChangePage] = useState(0),
    [auditPage, setAuditPage] = useState(0);
  const [keywords, setKeywords] = useState<Keyword[]>([]),
    [changes, setChanges] = useState<Change[]>([]),
    [users, setUsers] = useState<User[]>([]),
    [audit, setAudit] = useState<Audit[]>([]);
  const [edit, setEdit] = useState<Item | null>(null),
    [editFields, setEditFields] = useState<Record<string, string>>({}),
    [reason, setReason] = useState("");
  const [action, setAction] = useState("create"),
    [domain, setDomain] = useState(""),
    [key, setKey] = useState(""),
    [payload, setPayload] = useState("{}"),
    [kwReason, setKwReason] = useState(""),
    [linkResult, setLinkResult] = useState<string | null>(null);
  const [decision, setDecision] = useState<{
      id: string;
      approve: boolean;
    } | null>(null),
    [decisionNote, setDecisionNote] = useState("");
  const [permission, setPermission] = useState<User | null>(null),
    [permissionReason, setPermissionReason] = useState("");
  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError("");
    setNotice("");
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
    if (!selected) return;
    let cancelled = false;
    setItems([]);
    api<Item[]>(
      "/reports/" +
        selected.id +
        "/items?limit=" +
        PAGE +
        "&offset=" +
        itemPage * PAGE,
    )
      .then((v) => {
        if (!cancelled) setItems(v);
      })
      .catch((e) => setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [selected, itemPage]);
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
    if (!run) return;
    let cancelled = false;
    setResults([]);
    const poll = async () => {
      try {
        const [r, rows] = await Promise.all([
          api<Run>("/runs/" + run.id),
          api<Result[]>(
            "/runs/" +
              run.id +
              "/results?limit=" +
              PAGE +
              "&offset=" +
              resultPage * PAGE +
              (filter ? "&status=" + filter : ""),
          ),
        ]);
        if (!cancelled) {
          setRun(r);
          setResults(rows);
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      }
    };
    void poll();
    const timer = ["queued", "running"].includes(run.state)
      ? window.setInterval(poll, 2000)
      : undefined;
    return () => {
      cancelled = true;
      if (timer) clearInterval(timer);
    };
  }, [run?.id, run?.state, resultPage, filter]);
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
    setItemPage(0);
    setRunPage(0);
    setResultPage(0);
    setFilter("");
  };
  const chooseRun = (r: Run) => {
    setRun(r);
    setResultPage(0);
    setFilter("");
  };
  const kwFrom = (k: Keyword) => {
    setDomain(k.domain);
    setKey(k.key);
    setPayload(JSON.stringify(k.payload, null, 2));
    setAction("update");
    setLinkResult(null);
  };
  const linkedKeyword = keywords.find(
    (k) => k.domain === domain && k.key === key,
  );
  const submitKeyword = () =>
    act(async () => {
      const parsed = JSON.parse(payload);
      if (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
        throw new Error("关键词内容必须是 JSON 对象");
      await api("/keyword-requests", {
        method: "POST",
        body: json({
          action,
          domain,
          key,
          payload: parsed,
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
      setNotice("关键词申请已保存");
    });
  if (!user)
    return (
      <main className="login">
        <div className="mark">审</div>
        <p className="eyebrow">TEST REPORT REVIEW</p>
        <h1>单元测试报告 AI 审查</h1>
        <p className="muted">统一审查、领域关键词与全过程追溯</p>
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
        <p className="muted small">
          演示环境使用 demo / admin，密码见本机 .env。
        </p>
      </main>
    );
  return (
    <div className="layout">
      <aside>
        <div className="brand">
          <span className="mark">审</span>
          <b>报告审查工作台</b>
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
            onClick={() => {
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
              setEdit(null);
              setPermission(null);
              setDecision(null);
            }}
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
            {mode === "demo" ? "演示环境" : "内网环境"}
          </span>
        </header>
        {mode === "demo" && (
          <div className="banner">
            演示模式 · 当前审查结果统一标记“待人工确认”，尚未调用内网 AI
            和附件。
          </div>
        )}
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
                <p>每个 Sheet 对应一个领域，仅审查附件链接有值的测试项。</p>
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
                      });
                  }}
                />
              </label>
            </section>
            <div className="workspace">
              <section className="card report-list">
                <h2>{user.role === "admin" ? "全部报告" : "我的报告"}</h2>
                {!reports.length && (
                  <p className="empty">暂无报告，上传 Excel 开始审查。</p>
                )}
                {reports.map((r) => (
                  <button
                    className={
                      "report " + (selected?.id === r.id ? "selected" : "")
                    }
                    key={r.id}
                    onClick={() => chooseReport(r)}
                  >
                    <strong>{r.filename}</strong>
                    <span>
                      {r.item_count} 个测试项 · 版本 {r.revision}
                    </span>
                    <small>
                      {new Date(r.created_at + "Z").toLocaleString()}
                    </small>
                  </button>
                ))}
                {pager(reportPage, setReportPage, reports.length)}
              </section>
              <section className="card detail">
                {!selected ? (
                  <div className="empty">
                    <h2>选择一份报告</h2>
                    <p>查看测试项、发起审查或查询历史批次。</p>
                  </div>
                ) : (
                  <>
                    <div className="section-head">
                      <div>
                        <h2>{selected.filename}</h2>
                        <p>{selected.item_count} 个待审查测试项</p>
                      </div>
                      <div className="actions">
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
                          <button
                            className="primary"
                            disabled={busy}
                            onClick={() =>
                              void act(async () => {
                                const r = await api<Run>(
                                  "/reports/" + selected.id + "/runs",
                                  { method: "POST" },
                                );
                                chooseRun(r);
                                setNotice("审查已提交，等待后台进程执行");
                              })
                            }
                          >
                            AI 审查
                          </button>
                        )}
                      </div>
                    </div>
                    <h3>历史审查批次</h3>
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
                    {run ? (
                      <>
                        <div className="progress-head">
                          <b>
                            {labels[run.state]} · {run.completed}/{run.total}
                          </b>
                          <span>{run.failed} 项执行失败</span>
                        </div>
                        <progress max={run.total || 1} value={run.completed} />
                        <p className="muted small">
                          批次版本 {run.revision} · 模型 {run.model} ·{" "}
                          {run.mode}
                        </p>
                        <div className="section-head">
                          <select
                            aria-label="筛选审查状态"
                            value={filter}
                            onChange={(e) => {
                              setFilter(e.target.value);
                              setResultPage(0);
                            }}
                          >
                            <option value="">全部审查结果</option>
                            {[
                              "passed",
                              "failed",
                              "needs_review",
                              "error",
                              "pending",
                            ].map((s) => (
                              <option key={s} value={s}>
                                {labels[s]}
                              </option>
                            ))}
                          </select>
                          <div className="actions">
                            <button onClick={() => setRun(null)}>
                              查看并修订测试项
                            </button>
                            <button
                              disabled={
                                ["queued", "running"].includes(run.state) ||
                                busy
                              }
                              onClick={() =>
                                void act(async () => {
                                  await download(
                                    "/runs/" + run.id + "/export",
                                    "审查结果-" + run.id.slice(0, 8) + ".xlsx",
                                  );
                                })
                              }
                            >
                              导出 Excel
                            </button>
                          </div>
                        </div>
                        {results.map((r) => (
                          <details
                            className={"result " + r.status}
                            key={r.id}
                            open={r.status !== "passed"}
                          >
                            <summary>
                              <span className="status">{labels[r.status]}</span>
                              <strong>
                                {r.fields["用例名称"] || r.fields["用例编号"]}
                              </strong>
                              <span>
                                {r.domain} · 第 {r.row_number} 行
                              </span>
                            </summary>
                            <dl>
                              {Object.entries(r.fields).map(([k, v]) => (
                                <React.Fragment key={k}>
                                  <dt>{k}</dt>
                                  <dd>{v || "—"}</dd>
                                </React.Fragment>
                              ))}
                            </dl>
                            <p>
                              <b>问题说明：</b>
                              {r.explanation || "等待执行"}
                            </p>
                            {r.evidence.length > 0 && (
                              <p>
                                <b>判断依据：</b>
                                {r.evidence.join("；")}
                              </p>
                            )}
                            {r.suggestion && (
                              <p>
                                <b>建议：</b>
                                {r.suggestion}
                              </p>
                            )}
                            {selected.owner_id === user.id &&
                              ["failed", "needs_review", "error"].includes(
                                r.status,
                              ) && (
                                <button
                                  onClick={() => {
                                    setDomain(r.domain);
                                    setLinkResult(r.id);
                                    setKey("");
                                    setPayload("{}");
                                    setAction("create");
                                    setTab("keywords");
                                  }}
                                >
                                  申请关键词变更
                                </button>
                              )}
                          </details>
                        ))}
                        {!results.length && (
                          <p className="empty">当前筛选暂无结果。</p>
                        )}
                        {pager(resultPage, setResultPage, results.length)}
                      </>
                    ) : (
                      <>
                        <h3>解析后的测试项</h3>
                        <p className="muted">
                          修订原始字段会保存修改记录；已完成批次保持原样，新建批次使用修订后的数据。
                        </p>
                        <div className="table-scroll">
                          <table>
                            <thead>
                              <tr>
                                <th>领域 / 行号</th>
                                <th>用例名称</th>
                                <th>指标 / 值</th>
                                <th>操作</th>
                              </tr>
                            </thead>
                            <tbody>
                              {items.map((i) => (
                                <tr key={i.id}>
                                  <td>
                                    {i.domain} / {i.row_number}
                                  </td>
                                  <td>{i.current["用例名称"]}</td>
                                  <td>
                                    {i.current["指标名称"]} / {i.current["值"]}
                                  </td>
                                  <td>
                                    {selected.owner_id === user.id ? (
                                      <button
                                        onClick={() => {
                                          setEdit(i);
                                          setEditFields({ ...i.current });
                                          setReason("");
                                        }}
                                      >
                                        修订字段
                                      </button>
                                    ) : (
                                      "只读"
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                        {pager(itemPage, setItemPage, items.length)}
                      </>
                    )}
                  </>
                )}
              </section>
            </div>
          </>
        )}
        {tab === "keywords" && (
          <div className="columns">
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
              <form
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
                <label>
                  领域（Sheet 名称）
                  <input
                    required
                    value={domain}
                    onChange={(e) => setDomain(e.target.value)}
                  />
                </label>
                <label>
                  关键词 key
                  <input
                    required
                    value={key}
                    onChange={(e) => setKey(e.target.value)}
                  />
                </label>
                <label>
                  关键词内容（JSON 对象）
                  <textarea
                    rows={7}
                    required
                    value={payload}
                    onChange={(e) => setPayload(e.target.value)}
                    disabled={action === "delete"}
                  />
                </label>
                <label>
                  申请原因
                  <textarea
                    required
                    value={kwReason}
                    onChange={(e) => setKwReason(e.target.value)}
                  />
                </label>
                <p className="muted small">
                  当前版本：{linkedKeyword?.version ?? "不存在"}
                  。修改及删除需匹配当前版本。
                </p>
                <button className="primary" disabled={busy}>
                  提交变更
                </button>
              </form>
            </section>
            <section className="card">
              <h2>当前领域关键词</h2>
              {keywords.length === 0 && (
                <p className="empty">尚未配置关键词。</p>
              )}
              {keywords.map((k) => (
                <article key={k.id} className="keyword">
                  <div className="section-head">
                    <b>
                      {k.domain} / {k.key}
                    </b>
                    <button onClick={() => kwFrom(k)}>选择修改</button>
                  </div>
                  <pre>{JSON.stringify(k.payload, null, 2)}</pre>
                  <small>版本 {k.version}</small>
                </article>
              ))}
            </section>
            <section className="card full">
              <h2>{user.role === "admin" ? "关键词审批记录" : "我的申请"}</h2>
              {changes.map((c) => (
                <article key={c.id} className="keyword">
                  <div className="section-head">
                    <b>
                      {labels[c.action]} · {c.domain} / {c.key}
                    </b>
                    <span className="pill">{labels[c.state]}</span>
                  </div>
                  <p>{c.reason}</p>
                  <pre>{JSON.stringify(c.payload, null, 2)}</pre>
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
          <section className="card">
            <h2>
              {user.role === "admin" ? "全体用户操作记录" : "我的操作记录"}
            </h2>
            {audit.map((a) => (
              <details className="audit" key={a.id}>
                <summary>
                  {new Date(a.created_at + "Z").toLocaleString()} · {a.action} ·{" "}
                  {a.object_id.slice(0, 8)}
                </summary>
                <p>操作者：{a.actor_id}</p>
                <p>原因：{a.reason || "—"}</p>
                <pre>
                  {JSON.stringify(
                    { before: a.before, after: a.after },
                    null,
                    2,
                  )}
                </pre>
              </details>
            ))}
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
      {edit && (
        <div className="overlay">
          <section
            role="dialog"
            aria-modal="true"
            aria-label="修订测试项"
            className="modal card"
          >
            <h2>修订测试项 · 版本 {edit.version}</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void act(async () => {
                  await api("/items/" + edit.id, {
                    method: "PATCH",
                    body: json({
                      version: edit.version,
                      fields: editFields,
                      reason,
                    }),
                  });
                  setItems(
                    await api<Item[]>(
                      "/reports/" +
                        selected!.id +
                        "/items?limit=" +
                        PAGE +
                        "&offset=" +
                        itemPage * PAGE,
                    ),
                  );
                  await loadReports();
                  setEdit(null);
                  setNotice("字段已修订，历史审查快照保持不变");
                });
              }}
            >
              {Object.entries(editFields).map(([k, v]) => (
                <label key={k}>
                  {k}
                  <textarea
                    rows={2}
                    value={v}
                    onChange={(e) =>
                      setEditFields({ ...editFields, [k]: e.target.value })
                    }
                  />
                </label>
              ))}
              <label>
                修改原因
                <textarea
                  required
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </label>
              <div className="actions">
                <button type="button" onClick={() => setEdit(null)}>
                  取消
                </button>
                <button className="primary" disabled={busy}>
                  保存修订
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
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
