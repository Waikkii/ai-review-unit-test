from fastapi import HTTPException
from sqlalchemy import select, update
from .models import Report, Run, Keyword, Audit

def audit(db, actor, action, object_id, before=None, after=None, reason=""):
    db.add(Audit(actor_id=actor.id if hasattr(actor, "id") else actor,
                 action=action, object_id=object_id, before=before or {}, after=after or {}, reason=reason))

def visible_report(db, report_id, user, write=False):
    report = db.get(Report, report_id)
    if not report or (report.owner_id != user.id and (write or user.role != "admin")):
        raise HTTPException(404, "报告不存在")
    return report

def visible_run(db, run_id, user):
    run = db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "审查批次不存在")
    visible_report(db, run.report_id, user)
    return run

def keyword_dict(k):
    return {"id": k.id, "domain": k.domain, "key": k.key, "payload": k.payload,
            "version": k.version, "deleted": k.deleted}

def apply_change(db, change, actor):
    existing = db.scalar(select(Keyword).where(Keyword.domain == change.domain, Keyword.key == change.key))
    before = keyword_dict(existing) if existing else {}
    if change.action == "create":
        if existing or change.base_version != 0:
            raise HTTPException(409, "关键词已存在（含删除历史），请刷新版本")
        keyword = Keyword(domain=change.domain, key=change.key, payload=change.payload)
        db.add(keyword)
        db.flush()
    else:
        if not existing or existing.deleted or existing.version != change.base_version:
            raise HTTPException(409, "关键词版本已变化，请重新申请")
        values = {"version": existing.version + 1}
        if change.action == "delete":
            values["deleted"] = True
        else:
            values["payload"] = change.payload
        result = db.execute(update(Keyword).where(Keyword.id == existing.id,
            Keyword.version == change.base_version, Keyword.deleted.is_(False)).values(**values))
        if result.rowcount != 1:
            raise HTTPException(409, "关键词被其他请求修改")
        db.refresh(existing)
        keyword = existing
    audit(db, actor, "keyword." + change.action, keyword.id, before, keyword_dict(keyword), change.reason)
