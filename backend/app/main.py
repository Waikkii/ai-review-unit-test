from contextlib import asynccontextmanager
from hashlib import sha256
import json
from fastapi import FastAPI, Depends, File, UploadFile, HTTPException, Query
from fastapi.responses import Response, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, update, delete, or_, and_, func
from sqlalchemy.exc import IntegrityError
from .config import settings, ROOT
from .db import get_db, uid, now
from .models import User, Report, Item, Run, Result, Keyword, ChangeRequest, Audit, ItemNote, ResultMeasurements, ItemReviewState, RunControl, ResultWork, SessionCredential
from .schemas import Login, ItemEdit, NoteEdit, KeywordChange, KeywordEntryChange, Decision, UserChange, StartReview
from .keywords import ENTRY_DOMAIN, entry_key, matches_entry
from .auth import authenticate, issue_token, current_user, admin_user
from .excel import parse_excel, export_excel
from .reviewer import rule_version
from .services import audit, visible_report, visible_run, keyword_dict, apply_change
from .audit_view import SUMMARY_ACTIONS, describe_activity
from .review_workflow import ACTIVE_STATES, lock_report, create_run, is_pending, run_json as workflow_run_json
from .auth_sessions import store_credentials, AUTH_FAILURE_MESSAGE

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

def run_json(r, db):
    return workflow_run_json(db, r)

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
    if "_session_cookies" in identity:
        store_credentials(db, name, data.username, data.password, identity["_session_cookies"])
    audit(db, user, "session.login", user.id)
    db.commit()
    return {"access_token": issue_token(user), "token_type": "bearer",
            "user": serial(user, "id username role active"), "mode": settings.app_mode}

@app.get("/api/auth/me")
def me(user=Depends(current_user)):
    return serial(user, "id username role active")


@app.post("/api/auth/logout")
def logout(user=Depends(current_user), db=Depends(get_db)):
    db.execute(delete(SessionCredential).where(SessionCredential.username == user.username))
    db.commit()
    return {"ok": True}

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
        uploaded = [Item(id=uid(), report_id=report.id, domain=row["domain"], row_number=row["row_number"],
                         original=row["fields"], current=row["fields"]) for row in rows]
        db.add_all(uploaded)
        db.flush()
        db.add_all([ItemReviewState(item_id=item.id, revision=1, reviewed_revision=0) for item in uploaded])
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
          domain: str | None = None,
          user=Depends(current_user), db=Depends(get_db)):
    visible_report(db, report_id, user)
    query = select(Item, ItemNote, ItemReviewState, Result, ResultMeasurements, ResultWork).outerjoin(ItemNote, ItemNote.item_id == Item.id)
    query = query.outerjoin(ItemReviewState, ItemReviewState.item_id == Item.id)
    query = query.outerjoin(Result, Result.id == ItemReviewState.source_result_id)
    query = query.outerjoin(ResultMeasurements, ResultMeasurements.result_id == Result.id).where(Item.report_id == report_id)
    query = query.outerjoin(ResultWork, ResultWork.result_id == Result.id)
    if domain:
        query = query.where(Item.domain == domain)
    rows = db.execute(query.order_by(Item.domain, Item.row_number).offset(offset).limit(limit))
    return [{**serial(i, "id report_id domain row_number original current version"),
             "note": note.note if note else "", "note_version": note.version if note else 0,
             "pending_review": is_pending(state),
             "review": {**serial(result, "id item_id domain row_number fields status explanation evidence suggestion"),
                        "ai_threshold": measured.ai_threshold if measured else "",
                        "ai_value": measured.ai_value if measured else ""}
                        if result and (not is_pending(state) or result.status == "missing_keyword"
                            and state.revision == (work.item_revision if work else 1)) else None}
            for i, note, state, result, measured, work in rows]


@app.get("/api/reports/{report_id}/domains")
def report_domains(report_id: str, user=Depends(current_user), db=Depends(get_db)):
    visible_report(db, report_id, user)
    return list(db.scalars(select(Item.domain).where(Item.report_id == report_id).distinct().order_by(Item.domain)))


@app.patch("/api/items/{item_id}/note")
def edit_note(item_id: str, data: NoteEdit, user=Depends(current_user), db=Depends(get_db)):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(404, "测试项不存在")
    visible_report(db, item.report_id, user, write=True)
    note = db.get(ItemNote, item_id)
    before = {"note": note.note, "version": note.version} if note else {"note": "", "version": 0}
    if data.version != before["version"]:
        raise HTTPException(409, "备注已被修改，请刷新后重试")
    if note:
        updated = db.execute(update(ItemNote).where(ItemNote.item_id == item_id,
            ItemNote.version == data.version).values(note=data.note, version=data.version + 1))
        if updated.rowcount != 1:
            raise HTTPException(409, "备注已被修改，请刷新后重试")
    else:
        db.add(ItemNote(item_id=item_id, note=data.note, version=1))
    after = {"note": data.note, "version": data.version + 1}
    audit(db, user, "item.note", item_id, before, after, "用户备注")
    db.commit()
    return after

@app.patch("/api/items/{item_id}")
def edit_item(item_id: str, data: ItemEdit, user=Depends(current_user), db=Depends(get_db)):
    item = db.get(Item, item_id)
    if not item:
        raise HTTPException(404, "测试项不存在")
    visible_report(db, item.report_id, user, write=True)
    raise HTTPException(409, "解析后的原始字段只读；可添加用户备注或调整关键词")

@app.get("/api/reports/{report_id}/original")
def original(report_id: str, user=Depends(current_user), db=Depends(get_db)):
    report = visible_report(db, report_id, user)
    audit(db, user, "report.download", report.id)
    db.commit()
    return FileResponse(settings.data_dir / (report.id + ".xlsx"), filename=report.filename)

@app.post("/api/reports/{report_id}/runs", status_code=201)
def start_run(report_id: str, data: StartReview | None = None, user=Depends(current_user), db=Depends(get_db)):
    report = visible_report(db, report_id, user, write=True)
    lock_report(db, report)
    run = create_run(db, report, user, data.selection_mode if data else "full")
    db.commit()
    return run_json(run, db)


@app.post("/api/runs/{run_id}/{operation}")
def control_run(run_id: str, operation: str, data: StartReview | None = None,
                user=Depends(current_user), db=Depends(get_db)):
    if operation not in {"pause", "resume", "restart"}:
        raise HTTPException(404, "操作不存在")
    run = visible_run(db, run_id, user)
    report = visible_report(db, run.report_id, user, write=True)
    lock_report(db, report)
    run = db.scalar(select(Run).where(Run.id == run_id).with_for_update().execution_options(populate_existing=True))
    if operation == "restart":
        control = db.get(RunControl, run.id)
        selection_mode = data.selection_mode if data else control.selection_mode if control else "full"
        # Fence the old worker before creating a new batch in the chosen mode.
        if run.state in ACTIVE_STATES or run.state == "blocked":
            run.state, run.lease_owner, run.lease_until = "cancelled", None, None
            db.flush()
        run = create_run(db, report, user, selection_mode)
    elif operation == "pause":
        if run.state == "queued" or (run.state == "running" and (run.lease_until is None or run.lease_until < now())):
            run.state, run.lease_owner, run.lease_until = "paused", None, None
        elif run.state == "running":
            run.state = "pausing"
        elif run.state not in {"pausing", "paused"}:
            raise HTTPException(409, "当前批次不能暂停")
    else:
        if run.state not in {"paused", "blocked"}:
            raise HTTPException(409, "当前批次不能继续，请等待暂停完成")
        if db.scalar(select(Run.id).where(Run.report_id == report.id, Run.id != run.id, Run.state.in_(ACTIVE_STATES))):
            raise HTTPException(409, "该报告已有其他活动审查")
        if run.state == "blocked":
            for result in db.scalars(select(Result).where(Result.run_id == run.id, Result.status == "error")):
                if result.raw.get("error_type") == "SessionAuthFailure" or result.explanation == AUTH_FAILURE_MESSAGE:
                    db.execute(delete(ResultMeasurements).where(ResultMeasurements.result_id == result.id))
                    result.status, result.explanation, result.suggestion, result.raw = "pending", "", "", {}
                    result.evidence = []
                    run.completed -= 1
                    run.failed -= 1
        run.state, run.lease_owner, run.lease_until = "queued", None, None
        control = db.get(RunControl, run.id)
        if control:
            control.error_code = control.error_message = ""
    audit(db, user, "run." + operation, run.id)
    db.commit()
    return run_json(run, db)

@app.get("/api/reports/{report_id}/runs")
def runs(report_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
         user=Depends(current_user), db=Depends(get_db)):
    visible_report(db, report_id, user)
    return [run_json(r, db) for r in db.scalars(select(Run).where(Run.report_id == report_id)
            .order_by(Run.created_at.desc()).offset(offset).limit(limit))]

@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, user=Depends(current_user), db=Depends(get_db)):
    run = visible_run(db, run_id, user)
    return {**run_json(run, db), "keyword_snapshot": run.keyword_snapshot}

@app.get("/api/runs/{run_id}/results")
def results(run_id: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
            status: str | None = None, domain: str | None = None, user=Depends(current_user), db=Depends(get_db)):
    visible_run(db, run_id, user)
    q = select(Result).outerjoin(ItemReviewState, ItemReviewState.item_id == Result.item_id)
    q = q.outerjoin(ResultWork, ResultWork.result_id == Result.id).where(Result.run_id == run_id)
    pending = or_(ItemReviewState.item_id.is_(None), ItemReviewState.reviewed_revision < ItemReviewState.revision)
    stale = ItemReviewState.revision > func.coalesce(ResultWork.item_revision, 1)
    if status:
        if status == "pending":
            q = q.where(or_(Result.status == "pending", and_(pending, or_(Result.status != "error", stale))))
        elif status in {"error", "missing_keyword"}:
            q = q.where(Result.status == status, ~stale)
        else:
            q = q.where(Result.status == status, ~pending)
    if domain:
        q = q.where(Result.domain == domain)
    rows = db.execute(q.with_only_columns(Result, ResultMeasurements, ItemNote, ItemReviewState, ResultWork)
        .outerjoin(ResultMeasurements, ResultMeasurements.result_id == Result.id)
        .outerjoin(ItemNote, ItemNote.item_id == Result.item_id)
        .order_by(Result.domain, Result.row_number).offset(offset).limit(limit))
    return [{**serial(r, "id item_id domain row_number fields status explanation evidence suggestion raw"),
             "ai_threshold": measured.ai_threshold if measured else "",
             "ai_value": measured.ai_value if measured else "",
             "note": note.note if note else "", "note_version": note.version if note else 0,
             "pending_review": is_pending(state),
             "stale": bool(state and state.revision > (work.item_revision if work else 1))}
            for r, measured, note, state, work in rows]

@app.get("/api/runs/{run_id}/export")
def export(run_id: str, user=Depends(current_user), db=Depends(get_db)):
    run = visible_run(db, run_id, user)
    if run.state in ACTIVE_STATES:
        raise HTTPException(409, "请等待本批次审查结束")
    result_rows = db.scalars(select(Result).where(Result.run_id == run.id).order_by(Result.domain, Result.row_number)).all()
    measurements = {m.result_id: m for m in db.scalars(select(ResultMeasurements).join(Result)
        .where(Result.run_id == run.id))}
    notes = {n.item_id: n.note for n in db.scalars(select(ItemNote).join(Item)
        .where(Item.report_id == run.report_id))}
    content = export_excel((settings.data_dir / (run.report_id + ".xlsx")).read_bytes(), run,
                           result_rows, measurements, notes)
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
        same_item = (matches_entry(data.payload, result.fields) if data.domain == ENTRY_DOMAIN
                     else result.domain == data.domain)
        if not same_item:
            raise HTTPException(422, "关联项必须匹配关键词适用对象")
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

@app.post("/api/keyword-entry-requests", status_code=201)
def request_entry_change(data: KeywordEntryChange, user=Depends(current_user), db=Depends(get_db)):
    payload = data.entry.model_dump()
    change = KeywordChange(action=data.action, domain=ENTRY_DOMAIN, key=entry_key(payload),
                           payload=payload, base_version=data.base_version,
                           reason=data.reason, result_id=data.result_id)
    return request_change(change, user, db)


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
    if not data.active:
        db.execute(delete(SessionCredential).where(SessionCredential.username == target.username))
    audit(db, user, "user.permissions", target.id, before, serial(target, "role active"), data.reason)
    db.commit()
    return serial(target, "id username role active")

@app.get("/api/audit")
def audits(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
           summary: bool = True,
           user=Depends(current_user), db=Depends(get_db)):
    q = select(Audit, User.username).join(User, User.id == Audit.actor_id)
    if user.role != "admin":
        q = q.where(Audit.actor_id == user.id)
    if summary:
        q = q.where(Audit.action.in_(SUMMARY_ACTIONS))
    rows = db.execute(q.order_by(Audit.created_at.desc(), Audit.id).offset(offset).limit(limit)).all()
    ids = [a.object_id for a, _ in rows]
    report_names = dict(db.execute(select(Report.id, Report.filename).where(Report.id.in_(ids))).all())
    run_names = dict(db.execute(select(Run.id, Report.filename).join(Report, Report.id == Run.report_id)
                               .where(Run.id.in_(ids))).all())
    activities = []
    for record, username in rows:
        label, category, target = describe_activity(record, report_names, run_names)
        activities.append({**serial(record, "id actor_id action object_id before after reason created_at"),
                           "actor_username": username, "action_label": label, "category": category,
                           "target_label": target})
    return activities

dist = ROOT / "frontend" / "dist"
if dist.is_dir():
    app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
