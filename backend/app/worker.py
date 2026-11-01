"""独立进程，数据库持久化队列。租约过期后可恢复；外部 AI 调用为至少一次。"""
import logging
import time
from datetime import timedelta
from sqlalchemy import select, update, or_, and_, func
from .config import settings
from .db import Session, now, uid
from .models import Run, Result, User
from .reviewer import review, rule_version
from .services import audit
from .keywords import matching_keywords

log = logging.getLogger(__name__)

def process_once():
    owner = uid()
    with Session() as db:
        candidates = db.scalars(select(Run.id).where(or_(Run.state == "queued",
            and_(Run.state == "running", Run.lease_until < now()))).order_by(Run.created_at).limit(10)).all()
        claimed = None
        for run_id in candidates:
            updated = db.execute(update(Run).where(Run.id == run_id, or_(Run.state == "queued",
                and_(Run.state == "running", Run.lease_until < now()))).values(
                    state="running", lease_owner=owner, lease_until=now() + timedelta(seconds=settings.lease_seconds)))
            db.commit()
            if updated.rowcount == 1:
                claimed = run_id
                break
        if not claimed:
            return False
    while True:
        with Session() as db:
            run = db.get(Run, claimed)
            if run.lease_owner != owner:
                return True
            renewed = db.execute(update(Run).where(Run.id == claimed, Run.lease_owner == owner).values(
                lease_until=now() + timedelta(seconds=settings.lease_seconds)))
            db.commit()
            if renewed.rowcount != 1:
                return True
            result = db.scalar(select(Result).where(Result.run_id == claimed, Result.status == "pending")
                               .order_by(Result.domain, Result.row_number).limit(1))
            if not result:
                run.completed = db.scalar(select(func.count()).select_from(Result).where(
                    Result.run_id == claimed, Result.status != "pending"))
                run.failed = db.scalar(select(func.count()).select_from(Result).where(
                    Result.run_id == claimed, Result.status == "error"))
                run.state = "partial_failed" if run.failed else "completed"
                run.lease_until = None
                audit(db, run.actor_id, "run.finished", claimed, after={"state": run.state, "failed": run.failed})
                db.commit()
                return True
            result_id, fields, domain = result.id, result.fields, result.domain
            keywords = matching_keywords(run.keyword_snapshot, fields, domain)
            username = db.get(User, run.actor_id).username
        try:
            current_model = "demo" if settings.app_mode == "demo" else settings.ai_model
            if (run.mode, run.model, run.rule_version) != (settings.app_mode, current_model, rule_version()):
                raise RuntimeError("任务模式、模型或规则版本与 Worker 不一致")
            verdict, raw = review(fields, keywords, username)
            values = {**verdict.model_dump(), "raw": raw}
        except Exception as exc:
            # 不把 HTTP 请求、响应或凭据拼入用户错误及日志。
            values = {"status": "error", "explanation": "审查执行失败，请管理员检查集成配置后重新发起批次。",
                      "evidence": [], "suggestion": "", "raw": {"error_type": type(exc).__name__, "response": getattr(exc, "raw", {})}}
            log.warning("Review failed: result=%s type=%s", result_id, type(exc).__name__)
        with Session() as db:
            # 抢占后旧进程不可写入，即使外部调用延迟返回。
            locked = db.scalar(select(Run).where(Run.id == claimed).with_for_update())
            if locked.lease_owner != owner:
                return True
            result = db.get(Result, result_id)
            if result.status == "pending":
                for key, value in values.items():
                    setattr(result, key, value)
                locked.completed += 1
                if result.status == "error":
                    locked.failed += 1
                audit(db, locked.actor_id, "item.reviewed", result.id, after={"run_id": claimed, "status": result.status})
            db.commit()

def main():
    settings.validate_runtime()
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            if not process_once():
                time.sleep(1)
        except KeyboardInterrupt:
            return
        except Exception:
            log.error("Worker database cycle failed; retrying in 5 seconds")
            time.sleep(5)

if __name__ == "__main__":
    main()
