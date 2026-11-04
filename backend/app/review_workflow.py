from fastapi import HTTPException
from sqlalchemy import select, update
from .config import settings
from .db import uid
from .models import Item, ItemReviewState, Result, ResultWork, ResultMeasurements, Run, RunControl, Keyword, Report, RunRetainedItem
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


def work_results():
    return ~select(RunRetainedItem.item_id).where(RunRetainedItem.run_id == Result.run_id,
        RunRetainedItem.result_id == Result.id).exists()


def retain_result(db, run_id, item_id, source, activate=False):
    """Own the inherited result so deleting the source history cannot erase it."""
    if source is None:
        return None
    from copy import deepcopy
    clone = Result(id=uid(), run_id=run_id, **{key: deepcopy(getattr(source, key)) for key in
        'item_id domain row_number fields status explanation evidence suggestion raw'.split()})
    db.add(clone)
    db.flush()
    measured, work = db.get(ResultMeasurements, source.id), db.get(ResultWork, source.id)
    if measured:
        db.add(ResultMeasurements(result_id=clone.id, ai_threshold=measured.ai_threshold, ai_value=measured.ai_value))
    state = db.get(ItemReviewState, item_id)
    db.add(ResultWork(result_id=clone.id, item_revision=state.revision if activate and state else work.item_revision if work else 1))
    if state and not is_pending(state) and (activate or state.source_result_id == source.id):
        state.source_result_id = clone.id
    return clone.id


def detach_retained_results(db):
    """Upgrade legacy references while their source results still exist."""
    for retained, source in db.execute(select(RunRetainedItem, Result).join(Result,
            Result.id == RunRetainedItem.result_id).where(Result.run_id != RunRetainedItem.run_id)).all():
        retained.result_id = retain_result(db, retained.run_id, retained.item_id, source)
    db.flush()


def create_run(db, report, actor, selection_mode, base_run=None):
    from .services import audit, keyword_dict
    if db.scalar(select(Run.id).where(Run.report_id == report.id, Run.state.in_(ACTIVE_STATES))):
        raise HTTPException(409, "该报告已有活动审查，请先继续或重新开始")
    candidates = db.execute(select(Item, ItemReviewState).outerjoin(ItemReviewState,
        ItemReviewState.item_id == Item.id).where(Item.report_id == report.id)
        .order_by(Item.domain, Item.row_number)).all()
    baseline = None
    if selection_mode == "missing" and base_run:
        from .review_display import display_query
        query, _, _ = display_query(base_run)
        baseline = {row[0].id: row[1] for row in db.execute(query)}
    def needs_review(item, state):
        if is_pending(state):
            return True
        if baseline is None:
            return False
        source = baseline.get(item.id)
        return source.status in {'pending', 'error', 'missing_keyword'} if source else bool(state.source_result_id)
    chosen = [(item, state) for item, state in candidates if selection_mode == "full" or needs_review(item, state)]
    chosen_ids = {item.id for item, _ in chosen}
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
    if selection_mode == "missing":
        for item, state in candidates:
            if item.id in chosen_ids:
                continue
            source = baseline.get(item.id) if baseline is not None else db.get(Result, state.source_result_id) if state and state.source_result_id else None
            result_id = retain_result(db, run.id, item.id, source, activate=True)
            db.add(RunRetainedItem(run_id=run.id, item_id=item.id, result_id=result_id))
    for item, state in chosen:
        if state is None:
            state = ItemReviewState(item_id=item.id, revision=1, reviewed_revision=0)
            db.add(state)
        if selection_mode == "full" or not is_pending(state):
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
