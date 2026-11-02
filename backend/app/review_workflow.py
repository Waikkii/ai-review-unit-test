from fastapi import HTTPException
from sqlalchemy import select, update
from .config import settings
from .db import uid
from .models import Item, ItemReviewState, Result, ResultWork, Run, RunControl, Keyword, Report
from .reviewer import rule_version
from .keywords import ENTRY_DOMAIN, matches_entry

ACTIVE_STATES = ("queued", "running", "pausing", "paused")


def is_pending(state):
    return state is None or state.reviewed_revision < state.revision


def lock_report(db, report):
    db.execute(update(Report).where(Report.id == report.id).values(revision=Report.revision))
    db.refresh(report)


def run_json(db, run):
    control = db.get(RunControl, run.id)
    fields = "id report_id actor_id revision state total completed failed model rule_version mode created_at".split()
    return {**{key: getattr(run, key) for key in fields},
            "selection_mode": control.selection_mode if control else "full",
            "error_code": control.error_code if control else "",
            "error_message": control.error_message if control else ""}


def create_run(db, report, actor, selection_mode):
    from .services import audit, keyword_dict
    if db.scalar(select(Run.id).where(Run.report_id == report.id, Run.state.in_(ACTIVE_STATES))):
        raise HTTPException(409, "该报告已有活动审查，请先继续或重新开始")
    candidates = db.execute(select(Item, ItemReviewState).outerjoin(ItemReviewState,
        ItemReviewState.item_id == Item.id).where(Item.report_id == report.id)
        .order_by(Item.domain, Item.row_number)).all()
    chosen = [(item, state) for item, state in candidates if selection_mode == "full" or is_pending(state)]
    if not chosen:
        raise HTTPException(409, "没有待审查的测试项")
    snapshots = [keyword_dict(k) for k in db.scalars(select(Keyword).where(Keyword.deleted.is_(False)))]
    run = Run(id=uid(), report_id=report.id, actor_id=actor.id, revision=report.revision,
              total=len(chosen), keyword_snapshot=snapshots, completed=0, failed=0,
              model="demo" if settings.app_mode == "demo" else settings.review_model,
              rule_version=rule_version(), mode=settings.app_mode)
    db.add(run)
    db.flush()
    db.add(RunControl(run_id=run.id, selection_mode=selection_mode))
    for item, state in chosen:
        if state is None:
            state = ItemReviewState(item_id=item.id, revision=1, reviewed_revision=0)
            db.add(state)
        if selection_mode == "full":
            state.revision += 1
        result = Result(id=uid(), run_id=run.id, item_id=item.id, domain=item.domain,
                        row_number=item.row_number, fields=item.current)
        db.add(result)
        db.flush()
        db.add(ResultWork(result_id=result.id, item_revision=state.revision))
    audit(db, actor, "run.start", run.id, after={"report_id": report.id, "selection_mode": selection_mode,
          "revision": run.revision, "model": run.model, "rule_version": run.rule_version,
          "filename": report.filename})
    return run


def invalidate_keyword_items(db, keyword):
    """Only effective keyword changes invalidate work; pending approval does not."""
    if keyword.domain == ENTRY_DOMAIN:
        # Match decoded JSON: SQLite JSON paths differ for escaped Chinese keys.
        ids = [item_id for item_id, fields in db.execute(select(Item.id, Item.current)).yield_per(500)
               if matches_entry(keyword.payload, fields)]
        for offset in range(0, len(ids), 500):
            db.execute(update(ItemReviewState).where(ItemReviewState.item_id.in_(ids[offset:offset + 500]))
                       .values(revision=ItemReviewState.revision + 1))
    else:
        db.execute(update(ItemReviewState).where(ItemReviewState.item_id.in_(
            select(Item.id).where(Item.domain == keyword.domain))).values(revision=ItemReviewState.revision + 1))
