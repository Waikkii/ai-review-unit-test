from contextlib import asynccontextmanager
from hashlib import sha256
import json
from fastapi import FastAPI, Depends, File, UploadFile, HTTPException, Query
from fastapi.responses import Response, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from .config import settings, ROOT
from .db import get_db, uid
from .models import User, Report, Item, Run, Result, Keyword, ChangeRequest, Audit
from .schemas import Login, ItemEdit, KeywordChange, Decision, UserChange
from .auth import authenticate, issue_token, current_user, admin_user
from .excel import parse_excel, export_excel, attachment_urls
from .reviewer import rule_version
from .services import audit, visible_report, visible_run, keyword_dict, apply_change

@asynccontextmanager
async def lifespan(app):
    settings.validate_runtime()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # 初始化由 python -m app.init_db 显式执行，API 不自动改数据库。
    yield

app = FastAPI(title="单元测试报告 AI 审查", version="0.1.0", lifespan=lifespan)

@app.exception_handler(IntegrityError)
async def conflict_handler(request, exc):
    return JSONResponse(status_code=409, content={"detail": "存在并发修改或重复记录，请刷新后重试"})

@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store"
    return response

def serial(obj, names):
    return {name: getattr(obj, name) for name in names.split()}

def report_json(r):
    return serial(r, "id owner_id filename sha256 revision item_count created_at")

def run_json(r):
    return serial(r, "id report_id actor_id revision state total completed failed model rule_version mode created_at")

@app.get("/api/health")
def health():
    return {"status": "ok", "mode": settings.app_mode}

@app.post("/api/auth/login")
def login(data: Login, db=Depends(get_db)):
    try:
        identity = authenticate(data.username, data.password)
        name = identity["username"]
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError()
    except Exception:
        raise HTTPException(401, "用户名或密码错误，或内网认证服务不可用")
    user = db.scalar(select(User).where(User.username == name))
    if user is None:
        admins = {x.strip() for x in settings.bootstrap_admins.split(",") if x.strip()}
        role = "admin" if name in admins or (settings.app_mode == "demo" and name == "admin") else "user"
        user = User(username=name, role=role)
        db.add(user)
        db.flush()
    if not user.active:
        raise HTTPException(403, "账号已停用")
    audit(db, user, "session.login", user.id)
    db.commit()
    return {"access_token": issue_token(user), "token_type": "bearer",
            "user": serial(user, "id username role active"), "mode": settings.app_mode}

@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return serial(user, "id username role active")

@app.get("/api/reports")
def reports(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
            user=Depends(current_user), db=Depends(get_db)):
    q = select(Report)
    if user.role != "admin":
        q = q.where(Report.owner_id == user.id)
    return [report_json(r) for r in db.scalars(q.order_by(Report.created_at.desc()).offset(offset).limit(limit))]

@app.post("/api/reports", status_code=201)
def upload(file: UploadFile = File(...), user=Depends(current_user), db=Depends(get_db)):
    filename = (file.filename or "").replace("\\", "/").split("/")[-1]
    if not filename.lower().endswith(".xlsx") or len(filename) > 255:
        raise HTTPException(400, "请上传 .xlsx 文件，文件名不超过 255 字符")
    content = file.file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, "文件超过上传大小限制")
    try:
        rows = parse_excel(content)
    except Exception as exc:
        detail = str(exc) if isinstance(exc, ValueError) else "Excel 文件损坏或包含不支持的内容"
        raise HTTPException(400, detail)
    report = Report(id=uid(), owner_id=user.id, filename=filename,
                    sha256=sha256(content).hexdigest(), item_count=len(rows))
    path = settings.data_dir / (report.id + ".xlsx")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(content)
        db.add(report)
        db.flush()
        for row in rows:
            db.add(Item(report_id=report.id, domain=row["domain"], row_number=row["row_number"],
                        original=row["fields"], current=row["fields"]))
        audit(db, user, "report.upload", report.id,
              after={"filename": filename, "sha256": report.sha256, "items": len(rows)})
        db.commit()
    except Exception:
        db.rollback()
        path.unlink(missing_ok=True)
        raise
    return report_json(report)

@app.get("/api/reports/{report_id}/items")
def items(report_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
          user=Depends(current_user), db=Depends(get_db)):
    visible_report(db, report_id, user)
    return [serial(i, "id report_id domain row_number original current version") for i in db.scalars(
        select(Item).where(Item.report_id == report_id).order_by(Item.domain, Item.row_number).offset(offset).limit(limit))]

@app.patch("/api/items/{item_id}")
def edit_item(item_id: str, data: ItemEdit, user=Depends(current_user), db=Depends(get_db)):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(404, "测试项不存在")
    report = visible_report(db, item.report_id, user, write=True)
    if not attachment_urls(data.fields["附件链接"]):
        raise HTTPException(422, "附件链接必须包含 HTTP(S) 地址")
    before = item.current
    updated = db.execute(update(Item).where(Item.id == item.id, Item.version == data.version)
                         .values(current=data.fields, version=data.version + 1))
    if updated.rowcount != 1:
        raise HTTPException(409, "测试项版本已变化，请刷新")
    db.execute(update(Report).where(Report.id == report.id).values(revision=Report.revision + 1))
    audit(db, user, "item.edit", item.id, before, data.fields, data.reason)
    db.commit()
    return {"version": data.version + 1}

@app.get("/api/reports/{report_id}/original")
def original(report_id: str, user=Depends(current_user), db=Depends(get_db)):
    report = visible_report(db, report_id, user)
    audit(db, user, "report.download", report.id)
    db.commit()
    return FileResponse(settings.data_dir / (report.id + ".xlsx"), filename=report.filename)

@app.post("/api/reports/{report_id}/runs", status_code=201)
def start_run(report_id: str, user=Depends(current_user), db=Depends(get_db)):
    report = visible_report(db, report_id, user, write=True)
    # 同 edit_item 的 report UPDATE 锁串行化；编辑版本与本批次快照一致。
    db.execute(update(Report).where(Report.id == report_id).values(revision=Report.revision))
    db.refresh(report)
    if db.scalar(select(Run.id).where(Run.report_id == report.id, Run.state.in_(["queued", "running"]))):
        raise HTTPException(409, "该报告已有正在执行的审查")
    keywords = [keyword_dict(k) for k in db.scalars(select(Keyword).where(Keyword.deleted.is_(False)))]
    run = Run(report_id=report.id, actor_id=user.id, revision=report.revision,
              total=report.item_count, keyword_snapshot=keywords,
              model="demo" if settings.app_mode == "demo" else settings.ai_model,
              rule_version=rule_version(), mode=settings.app_mode)
    db.add(run)
    db.flush()
    for item in db.scalars(select(Item).where(Item.report_id == report.id)):
        db.add(Result(run_id=run.id, item_id=item.id, domain=item.domain,
                      row_number=item.row_number, fields=item.current))
    audit(db, user, "run.start", run.id, after={"report_id": report.id, "revision": report.revision,
          "model": run.model, "rule_version": run.rule_version, "keywords": keywords})
    db.commit()
    return run_json(run)

@app.get("/api/reports/{report_id}/runs")
def runs(report_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
         user=Depends(current_user), db=Depends(get_db)):
    visible_report(db, report_id, user)
    return [run_json(r) for r in db.scalars(select(Run).where(Run.report_id == report_id)
            .order_by(Run.created_at.desc()).offset(offset).limit(limit))]

@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, user=Depends(current_user), db=Depends(get_db)):
    run = visible_run(db, run_id, user)
    return {**run_json(run), "keyword_snapshot": run.keyword_snapshot}

@app.get("/api/runs/{run_id}/results")
def results(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
            status: str | None = None, user=Depends(current_user), db=Depends(get_db)):
    visible_run(db, run_id, user)
    q = select(Result).where(Result.run_id == run_id)
    if status:
        q = q.where(Result.status == status)
    return [serial(r, "id item_id domain row_number fields status explanation evidence suggestion raw")
            for r in db.scalars(q.order_by(Result.domain, Result.row_number).offset(offset).limit(limit))]

@app.get("/api/runs/{run_id}/export")
def export(run_id: str, user=Depends(current_user), db=Depends(get_db)):
    run = visible_run(db, run_id, user)
    if run.state in {"queued", "running"}:
        raise HTTPException(409, "请等待本批次审查结束")
    content = export_excel((settings.data_dir / (run.report_id + ".xlsx")).read_bytes(), run,
        db.scalars(select(Result).where(Result.run_id == run.id).order_by(Result.domain, Result.row_number)).all())
    audit(db, user, "run.export", run.id)
    db.commit()
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="review-{run.id}.xlsx"'})

@app.get("/api/keywords")
def keywords(user=Depends(current_user), db=Depends(get_db)):
    return [keyword_dict(k) for k in db.scalars(select(Keyword).where(Keyword.deleted.is_(False))
                                               .order_by(Keyword.domain, Keyword.key))]

@app.post("/api/keyword-requests", status_code=201)
def request_change(data: KeywordChange, user=Depends(current_user), db=Depends(get_db)):
    if len(json.dumps(data.payload, ensure_ascii=False)) > 32000:
        raise HTTPException(422, "关键词内容过大")
    if data.result_id:
        result = db.get(Result, data.result_id)
        if not result:
            raise HTTPException(404, "关联结果不存在")
        run = visible_run(db, result.run_id, user)
        visible_report(db, run.report_id, user, write=True)
        if result.domain != data.domain or result.status not in {"failed", "needs_review", "error"}:
            raise HTTPException(422, "关联项必须属于相同领域且需要处理")
    existing = db.scalar(select(Keyword).where(Keyword.domain == data.domain, Keyword.key == data.key))
    if data.action == "create":
        if existing or data.base_version != 0:
            raise HTTPException(409, "关键词已存在")
    elif not existing or existing.deleted or existing.version != data.base_version:
        raise HTTPException(409, "关键词版本已变化")
    change = ChangeRequest(owner_id=user.id, **data.model_dump())
    db.add(change)
    db.flush()
    if user.role == "admin":
        apply_change(db, change, user)
        change.state, change.reviewer_id = "approved", user.id
        change.review_note = "管理员直接生效"
    audit(db, user, "keyword.request", change.id, after=data.model_dump(), reason=data.reason)
    db.commit()
    return {"id": change.id, "state": change.state}

@app.get("/api/keyword-requests")
def changes(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
            user=Depends(current_user), db=Depends(get_db)):
    q = select(ChangeRequest)
    if user.role != "admin":
        q = q.where(ChangeRequest.owner_id == user.id)
    return [serial(c, "id owner_id action domain key payload base_version reason result_id state reviewer_id review_note created_at")
            for c in db.scalars(q.order_by(ChangeRequest.created_at.desc()).offset(offset).limit(limit))]

@app.post("/api/keyword-requests/{change_id}/decision")
def decide(change_id: str, data: Decision, user=Depends(admin_user), db=Depends(get_db)):
    change = db.scalar(select(ChangeRequest).where(ChangeRequest.id == change_id).with_for_update())
    if not change:
        raise HTTPException(404, "申请不存在")
    if change.state != "pending":
        raise HTTPException(409, "该申请已处理")
    if change.owner_id == user.id:
        raise HTTPException(403, "不能审批自己的历史申请")
    if data.approve:
        apply_change(db, change, user)
    change.state = "approved" if data.approve else "rejected"
    change.reviewer_id, change.review_note = user.id, data.note
    audit(db, user, "keyword.decision", change.id, after={"state": change.state}, reason=data.note)
    db.commit()
    return {"state": change.state}

@app.get("/api/users")
def users(user=Depends(admin_user), db=Depends(get_db)):
    return [serial(u, "id username role active created_at") for u in db.scalars(select(User).order_by(User.username))]

@app.patch("/api/users/{user_id}")
def change_user(user_id: str, data: UserChange, user=Depends(admin_user), db=Depends(get_db)):
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target.id == user.id:
        raise HTTPException(409, "不能变更自己的角色或启用状态")
    before = serial(target, "role active")
    target.role, target.active = data.role, data.active
    audit(db, user, "user.permissions", target.id, before, serial(target, "role active"), data.reason)
    db.commit()
    return serial(target, "id username role active")

@app.get("/api/audit")
def audits(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
           user=Depends(current_user), db=Depends(get_db)):
    q = select(Audit)
    if user.role != "admin":
        q = q.where(Audit.actor_id == user.id)
    return [serial(a, "id actor_id action object_id before after reason created_at")
            for a in db.scalars(q.order_by(Audit.created_at.desc()).offset(offset).limit(limit))]

dist = ROOT / "frontend" / "dist"
if dist.is_dir():
    app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
