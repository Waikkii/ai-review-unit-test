from sqlalchemy import select, func
from .models import RunItemReviewState, Result, Run, RunRetainedItem


def pending_expression():
    return func.coalesce(RunItemReviewState.pending,
                         Result.status.in_(['pending', 'error', 'missing_keyword']), True)


def is_history_pending(snapshot, result):
    return snapshot.pending if snapshot else result is None or result.status in ('pending', 'error', 'missing_keyword')


def seed_history_states(db):
    """Recover history display from its own saved results, not deleted global sources."""
    from .models import ItemReviewState
    for run in db.scalars(select(Run)):
        present = set(db.scalars(select(RunItemReviewState.item_id).where(RunItemReviewState.run_id == run.id)))
        results = {result.item_id: result for result in db.scalars(select(Result).where(Result.run_id == run.id))}
        inherited = set(db.scalars(select(RunRetainedItem.item_id).where(RunRetainedItem.run_id == run.id)))
        for item_id in set(results) | inherited:
            if item_id in present:
                continue
            result = results.get(item_id)
            pending = result.status in ('pending', 'error', 'missing_keyword') if result else item_id not in inherited
            global_state = db.get(ItemReviewState, item_id)
            stale = False
            if result and global_state and global_state.source_result_id == result.id:
                pending = global_state.reviewed_revision < global_state.revision
                stale = pending and result.status not in ('pending', 'error', 'missing_keyword')
            db.add(RunItemReviewState(run_id=run.id, item_id=item_id, pending=pending, stale=stale))
    db.flush()
