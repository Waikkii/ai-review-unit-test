"""Additive upgrade for existing installations; never rewrites report or result data."""
from .config import settings
from .db import engine
from sqlalchemy import inspect, select, update
from sqlalchemy.orm import Session
from .models import ItemNote, ItemSource, ResultMeasurements, ItemReviewState, ResultWork, RunControl, SessionCredential, Run, Result, Item, Keyword, RunRetainedItem
from .keywords import matching_keywords
from .services import keyword_dict
from .models import RunItemReviewState


def main():
    settings.validate_runtime()
    for table in (ItemNote.__table__, ItemSource.__table__, ResultMeasurements.__table__, ItemReviewState.__table__,
                  ResultWork.__table__, RunControl.__table__, SessionCredential.__table__, RunRetainedItem.__table__, RunItemReviewState.__table__):
        table.create(engine, checkfirst=True)
    indexes = inspect(engine).get_indexes("runs")
    for entry in indexes:
        if entry["name"] == "one_active_run_per_report":
            options = entry.get("dialect_options", {})
            predicate = str(options.get("sqlite_where", options.get("postgresql_where", "")))
            if "paused" not in predicate:
                index = next(i for i in Run.__table__.indexes if i.name == entry["name"])
                index.drop(engine)
                index.create(engine)
    with Session(engine) as db:
        db.execute(update(RunControl).where(RunControl.run_id.in_(select(Run.id).where(Run.state == "blocked")))
                   .values(error_code="auth_session_failed"))
        missing = db.scalars(select(Item).outerjoin(ItemReviewState, ItemReviewState.item_id == Item.id)
                             .where(ItemReviewState.item_id.is_(None))).all()
        if missing:
            ids = [item.id for item in missing]
            latest = {}
            live = [keyword_dict(k) for k in db.scalars(select(Keyword).where(Keyword.deleted.is_(False)))]
            for offset in range(0, len(ids), 500):
                for result, snapshot in db.execute(select(Result, Run.keyword_snapshot).join(Run)
                    .where(Result.item_id.in_(ids[offset:offset + 500])).order_by(Run.created_at, Result.id)):
                    latest[result.item_id] = (result, snapshot)
            for item in missing:
                previous = latest.get(item.id)
                clean, source = False, None
                if previous:
                    result, snapshot = previous
                    source = result.id
                    key = lambda k: (k["domain"], k["key"], k["id"])
                    clean = (result.status in {"passed", "failed", "needs_review"} and result.fields == item.current
                        and sorted(matching_keywords(snapshot, item.current, item.domain), key=key)
                        == sorted(matching_keywords(live, item.current, item.domain), key=key))
                db.add(ItemReviewState(item_id=item.id, revision=1, reviewed_revision=1 if clean else 0,
                                       source_result_id=source))
        for run in db.scalars(select(Run).join(RunControl).where(RunControl.selection_mode == "missing")):
            present = set(db.scalars(select(Result.item_id).where(Result.run_id == run.id)))
            present.update(db.scalars(select(RunRetainedItem.item_id).where(RunRetainedItem.run_id == run.id)))
            for item in db.scalars(select(Item).where(Item.report_id == run.report_id)):
                if item.id in present:
                    continue
                previous = db.scalar(select(Result.id).join(Run, Run.id == Result.run_id).where(
                    Result.item_id == item.id, Run.created_at < run.created_at, Result.status != "pending")
                    .order_by(Run.created_at.desc(), Result.id).limit(1))
                db.add(RunRetainedItem(run_id=run.id, item_id=item.id, result_id=previous))
        from .review_workflow import detach_retained_results
        detach_retained_results(db)
        from .history_state import seed_history_states
        seed_history_states(db)
        db.commit()
    print("Schema upgrade complete: review controls, pending items and encrypted sessions.")


if __name__ == "__main__":
    main()
