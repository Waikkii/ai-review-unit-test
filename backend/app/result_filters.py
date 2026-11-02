"""Shared effective result labels for display and filtering before pagination."""
from sqlalchemy import and_, case, func, or_
from .models import ItemReviewState, Result, ResultWork

def stored_result_label(status, explanation):
    labels = {"passed": "通过", "failed": "未通过", "needs_review": "待人工确认",
              "missing_keyword": "缺少关键词", "error": "执行失败", "pending": "待审查"}
    if status in {"passed", "missing_keyword", "error", "pending"}:
        return labels[status]
    detail = (explanation or "").strip()
    return labels.get(status, status) if not detail or detail == status else detail


def effective_result_expressions():
    pending = or_(ItemReviewState.item_id.is_(None), ItemReviewState.reviewed_revision < ItemReviewState.revision)
    stale = ItemReviewState.revision > func.coalesce(ResultWork.item_revision, 1)
    effective_pending = or_(Result.status == "pending", and_(pending,
        or_(Result.status.not_in(("error", "missing_keyword")), stale)))
    status = case((effective_pending, "pending"), else_=Result.status)
    detail = func.nullif(func.trim(Result.explanation), "")
    label = case(
        (status == "pending", "待审查"), (status == "passed", "通过"),
        (status == "error", "执行失败"), (status == "missing_keyword", "缺少关键词"),
        (status == "failed", case((detail == "failed", "未通过"), else_=func.coalesce(detail, "未通过"))),
        (status == "needs_review", case((detail == "needs_review", "待人工确认"),
                                       else_=func.coalesce(detail, "待人工确认"))),
        else_=func.coalesce(detail, status),
    )
    return status, label
