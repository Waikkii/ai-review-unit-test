"""独立进程，数据库持久化队列。租约过期后可恢复；外部 AI 调用为至少一次。"""
import logging
import time
from datetime import timedelta
from sqlalchemy import select, update, or_, and_, func
from .config import settings
from .db import Session, now, uid
from .models import Run, Result, User, ResultMeasurements, RunControl, ItemReviewState, ResultWork, ItemSource
from .auth_sessions import SessionAuthFailure, AUTH_FAILURE_MESSAGE
from .reviewer import review, rule_version
from .services import audit
from .keywords import matching_keywords

log = logging.getLogger(__name__)

def _complete(db, run):
    run.completed = db.scalar(select(func.count()).select_from(Result).where(
        Result.run_id == run.id, Result.status != "pending"))
    run.failed = db.scalar(select(func.count()).select_from(Result).where(
        Result.run_id == run.id, Result.status == "error"))
    run.state = "partial_failed" if run.failed else "completed"
    run.lease_owner = run.lease_until = None
    audit(db, run.actor_id, "run.finished", run.id, after={"state": run.state, "failed": run.failed})


def process_once():
    owner = uid()
    with Session() as db:
        candidates = db.scalars(select(Run.id).where(or_(Run.state == "queued",
            and_(Run.state.in_(["running", "pausing"]), Run.lease_until < now())))
            .order_by(Run.created_at).limit(10)).all()
        claimed = None
        for run_id in candidates:
            updated = db.execute(update(Run).where(Run.id == run_id, or_(Run.state == "queued",
                and_(Run.state.in_(["running", "pausing"]), Run.lease_until < now()))).values(
                    lease_owner=owner, lease_until=now() + timedelta(seconds=settings.lease_seconds)))
            # Preserve a pending pause when recovering an abandoned worker.
            if updated.rowcount == 1:
                row = db.get(Run, run_id)
                if row.state == "queued":
                    row.state = "running"
            db.commit()
            if updated.rowcount == 1:
                claimed = run_id
                break
        if not claimed:
            return False
    while True:
        with Session() as db:
            owned = db.execute(update(Run).where(Run.id == claimed, Run.lease_owner == owner,
                Run.state.in_({"running", "pausing"})).values(
                    lease_until=now() + timedelta(seconds=settings.lease_seconds)))
            if owned.rowcount != 1:
                db.rollback()
                return True
            run = db.scalar(select(Run).where(Run.id == claimed).with_for_update())
            if run is None or run.lease_owner != owner:
                return True
            if run.state == "pausing":
                pending = db.scalar(select(Result.id).where(Result.run_id == claimed, Result.status == "pending").limit(1))
                if pending:
                    run.state, run.lease_owner, run.lease_until = "paused", None, None
                else:
                    _complete(db, run)
                db.commit()
                return True
            if run.state != "running":
                return True
            result = db.scalar(select(Result).outerjoin(ItemSource, ItemSource.item_id == Result.item_id)
                               .where(Result.run_id == claimed, Result.status == "pending")
                               .order_by(Result.domain, Result.row_number, func.coalesce(ItemSource.group_index, 0), Result.id).limit(1))
            if not result:
                _complete(db, run)
                db.commit()
                return True
            result_id, fields, domain = result.id, result.fields, result.domain
            keywords = matching_keywords(run.keyword_snapshot, fields, domain)
            username = db.get(User, run.actor_id).username
            work = db.get(ResultWork, result.id)
            item_revision = work.item_revision if work else 1
            db.commit()
        auth_failed = False
        try:
            current_model = "demo" if settings.app_mode == "demo" else settings.review_model
            if (run.mode, run.model, run.rule_version) != (settings.app_mode, current_model, rule_version()):
                raise RuntimeError("任务模式、模型或规则版本与 Worker 不一致")
            verdict, raw = review(fields, keywords, username)
            values = {**verdict.model_dump(), "raw": raw}
        except SessionAuthFailure:
            auth_failed = True
            values = {"status": "error", "explanation": AUTH_FAILURE_MESSAGE,
                      "evidence": [], "suggestion": "请联系管理员处理后重新登录。",
                      "raw": {"error_type": "SessionAuthFailure"}}
        except Exception as exc:
            # 不把 HTTP 请求、响应或凭据拼入用户错误及日志。
            values = {"status": "error", "explanation": "审查执行失败，请管理员检查集成配置后重新发起批次。",
                      "evidence": [], "suggestion": "", "raw": {"error_type": type(exc).__name__, "response": getattr(exc, "raw", {})}}
            log.warning("Review failed: result=%s type=%s", result_id, type(exc).__name__)
        with Session() as db:
            # 抢占后旧进程不可写入，即使外部调用延迟返回。
            # SQLite 的 FOR UPDATE 不提供行锁：条件更新同时校验租约并取得写锁，
            # 与中止/删除互斥，防止外部调用返回后复活已删除的批次或写入已中止的结果。
            writable = db.execute(update(Run).where(Run.id == claimed, Run.lease_owner == owner,
                Run.state.in_({"running", "pausing"})).values(lease_until=Run.lease_until))
            if writable.rowcount != 1:
                db.rollback()
                return True
            locked = db.scalar(select(Run).where(Run.id == claimed).with_for_update())
            if locked is None or locked.lease_owner != owner or locked.state not in {"running", "pausing"}:
                return True
            result = db.get(Result, result_id)
            if result is None:
                return True
            if result.status == "pending":
                threshold = values.pop("ai_threshold", "")
                measured = values.pop("ai_value", "")
                db.add(ResultMeasurements(result_id=result.id, ai_threshold=threshold, ai_value=measured))
                for key, value in values.items():
                    setattr(result, key, value)
                locked.completed += 1
                if result.status == "error":
                    locked.failed += 1
                elif result.status == "missing_keyword":
                    db.execute(update(ItemReviewState).where(ItemReviewState.item_id == result.item_id,
                        ItemReviewState.revision == item_revision).values(source_result_id=result.id))
                else:
                    db.execute(update(ItemReviewState).where(ItemReviewState.item_id == result.item_id,
                        ItemReviewState.revision == item_revision).values(
                            reviewed_revision=item_revision, source_result_id=result.id))
                audit(db, locked.actor_id, "item.reviewed", result.id, after={"run_id": claimed, "status": result.status})
            if auth_failed:
                control = db.get(RunControl, claimed)
                if control is None:
                    control = RunControl(run_id=claimed)
                    db.add(control)
                control.error_code, control.error_message = "auth_session_failed", AUTH_FAILURE_MESSAGE
                locked.state, locked.lease_owner, locked.lease_until = "blocked", None, None
            db.commit()
            if auth_failed:
                return True

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
