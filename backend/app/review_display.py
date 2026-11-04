"""查漏补缺的整份报告视图：包含独立继承的结论，Worker 只处理本轮待审查项。"""
from sqlalchemy import select, func
from .models import Item, Result, RunRetainedItem, ResultWork, ResultMeasurements, ItemReviewState, ItemNote, ItemSource
from .result_filters import effective_result_expressions


def display_query(run):
    current = select(Result.id).where(Result.run_id == run.id, Result.item_id == Item.id).correlate(Item).scalar_subquery()
    effective = func.coalesce(current, RunRetainedItem.result_id)
    status, label = effective_result_expressions()
    label = func.coalesce(label, "待审查")
    query = select(Item, Result, ResultMeasurements, ItemNote, ItemReviewState, ResultWork, ItemSource, label)
    query = query.outerjoin(RunRetainedItem, (RunRetainedItem.run_id == run.id) & (RunRetainedItem.item_id == Item.id))
    query = query.outerjoin(Result, Result.id == effective)
    query = query.outerjoin(ResultMeasurements, ResultMeasurements.result_id == Result.id)
    query = query.outerjoin(ItemNote, ItemNote.item_id == Item.id)
    query = query.outerjoin(ItemReviewState, ItemReviewState.item_id == Item.id)
    query = query.outerjoin(ResultWork, ResultWork.result_id == Result.id)
    query = query.outerjoin(ItemSource, ItemSource.item_id == Item.id).where(Item.report_id == run.report_id)
    return query, func.coalesce(status, "pending"), label


def display_page(db, run, offset, limit, domains, results, statuses, include_total):
    query, status, label = display_query(run)
    if domains:
        query = query.where(Item.domain.in_(domains))
    if results:
        query = query.where(label.in_(results))
    if statuses:
        query = query.where(status.in_(statuses))
    total = db.scalar(select(func.count()).select_from(query.subquery())) if include_total else None
    rows = db.execute(query.order_by(Item.domain, Item.row_number, func.coalesce(ItemSource.group_index, 0), Item.id).offset(offset).limit(limit))
    data = []
    for item, result, measured, note, state, work, source, result_label in rows:
        entry = {key: getattr(result, key) for key in "id item_id domain row_number fields status explanation evidence suggestion raw".split()} if result else {
            "id": "", "item_id": item.id, "domain": item.domain, "row_number": item.row_number, "fields": item.original,
            "status": "pending", "explanation": "", "evidence": [], "suggestion": "", "raw": {}}
        entry.update(ai_threshold=measured.ai_threshold if measured else "", ai_value=measured.ai_value if measured else "",
            note=note.note if note else "", note_version=note.version if note else 0,
            pending_review=not state or state.reviewed_revision < state.revision,
            review_revision=state.revision if state else 1, reviewed_revision=state.reviewed_revision if state else 0,
            source_group=source.group_index if source else 0, source_label=source.group_label if source else "",
            result_label=result_label, stale=bool(state and state.revision > (work.item_revision if work else 1)))
        data.append(entry)
    return {"items": data, "total": total} if include_total else data
