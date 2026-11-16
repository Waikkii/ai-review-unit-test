"""Portable, complete history snapshots tied to an unchanged original Excel."""
import json
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .config import settings
from .db import uid
from .models import (Item, ItemSource, ItemNote, ItemReviewState, Result, ResultMeasurements,
                     ResultWork, Run, RunControl, RunItemReviewState, RunRetainedItem)
from .review_display import display_query
from .history_state import is_history_pending
from .review_workflow import ACTIVE_STATES
from .services import audit


class ArchiveModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class ArchivedResult(ArchiveModel):
    fields: dict[str, str]
    status: Literal['pending', 'passed', 'failed', 'needs_review', 'missing_keyword', 'error']
    explanation: str
    evidence: list
    suggestion: str
    raw: dict
    ai_threshold: str
    ai_value: str


class ArchivedItem(ArchiveModel):
    domain: str = Field(min_length=1, max_length=128)
    row_number: int = Field(ge=1)
    source_group: int = Field(ge=0)
    source_label: str
    original_fields: dict[str, str]
    pending: bool
    stale: bool
    note: str = Field(max_length=16000)
    result: ArchivedResult | None


class ArchivedRun(ArchiveModel):
    source_id: str
    created_at: str
    state: str
    selection_mode: Literal['full', 'missing']
    model: str = Field(max_length=128)
    rule_version: str = Field(max_length=128)
    mode: str
    keyword_snapshot: list[dict]


class ResultArchive(ArchiveModel):
    format: Literal['ai-review-results']
    version: Literal[1]
    excel_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    filename: str
    run: ArchivedRun
    items: list[ArchivedItem]


def export_archive(db, report, run):
    if run.state in ACTIVE_STATES:
        raise HTTPException(409, '请等待审查结束或中止后再导出审查结果')
    control = db.get(RunControl, run.id)
    entries = []
    query, _, _ = display_query(run)
    for item, result, measured, note, state, work, source, _, snapshot in db.execute(query.order_by(
            Item.domain, Item.row_number, ItemSource.group_index, Item.id)):
        entry = {
            'domain': item.domain, 'row_number': item.row_number,
            'source_group': source.group_index if source else 0,
            'source_label': source.group_label if source else '',
            'original_fields': item.original, 'pending': bool(is_history_pending(snapshot, result)),
            'stale': bool(snapshot and snapshot.stale), 'note': note.note if note else '',
            'result': None if result is None else {
                **{key: getattr(result, key) for key in 'fields status explanation evidence suggestion raw'.split()},
                'ai_threshold': measured.ai_threshold if measured else '',
                'ai_value': measured.ai_value if measured else ''},
        }
        entries.append(entry)
    archive = ResultArchive.model_validate({
        'format': 'ai-review-results', 'version': 1, 'excel_sha256': report.sha256,
        'filename': report.filename,
        'run': {'source_id': run.id, 'created_at': run.created_at.isoformat(), 'state': run.state,
                'selection_mode': control.selection_mode if control else 'full',
                'model': run.model, 'rule_version': run.rule_version, 'mode': run.mode,
                'keyword_snapshot': run.keyword_snapshot},
        'items': entries,
    })
    content = archive.model_dump_json(indent=2).encode('utf-8')
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, '审查结果超过文件大小限制，请管理员调整 MAX_UPLOAD_MB 后再导出')
    return content


def read_archive(content):
    def reject_constant(value):
        raise ValueError('non-finite JSON number')
    def unique_keys(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError('duplicate JSON key')
            obj[key] = value
        return obj
    try:
        return ResultArchive.model_validate(json.loads(content.decode('utf-8-sig'),
            parse_constant=reject_constant, object_pairs_hook=unique_keys))
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise HTTPException(422, '审查结果文件格式无效，请使用本系统导出的 JSON 文件（版本 1）') from None


def import_archive(db, report, user, archive):
    if db.scalar(select(Run.id).where(Run.report_id == report.id, Run.state.in_(ACTIVE_STATES))):
        raise HTTPException(409, '该报告仍有活动审查，请先结束或中止后再导入')
    if archive.excel_sha256 != report.sha256:
        raise HTTPException(422, '原始 Excel 不一致：请先上传导出结果所对应的同一份原始 Excel')
    rows = db.execute(select(Item, ItemSource).outerjoin(ItemSource, ItemSource.item_id == Item.id)
                      .where(Item.report_id == report.id)).all()
    targets = {(item.domain, item.row_number, source.group_index if source else 0): (item, source)
               for item, source in rows}
    if len(rows) != len(targets) or len(archive.items) != len(rows):
        raise HTTPException(422, '测试项数量或原始位置不一致，无法全量导入')
    seen = set()
    for entry in archive.items:
        key = (entry.domain, entry.row_number, entry.source_group)
        if key in seen or key not in targets:
            raise HTTPException(422, '审查结果包含重复或未知的领域、行号、分组')
        seen.add(key)
        item, source = targets[key]
        if entry.original_fields != item.original or entry.source_label != (source.group_label if source else ''):
            raise HTTPException(422, '原始测试字段或分组不一致，请检查 Excel 和审查结果文件')
        if entry.result and set(entry.result.fields) != set(item.original):
            raise HTTPException(422, '审查快照的原始字段不完整，无法导入')
        if entry.stale and not entry.pending:
            raise HTTPException(422, '审查状态不一致：失效项必须为待审查')
    run = Run(id=uid(), report_id=report.id, actor_id=user.id, revision=report.revision,
              state='completed', total=len(rows), completed=len(rows),
              failed=sum(bool(entry.result and entry.result.status == 'error') for entry in archive.items),
              keyword_snapshot=archive.run.keyword_snapshot, model=archive.run.model,
              rule_version=archive.run.rule_version, mode='imported')
    db.add(run)
    db.flush()
    # 导入视图始终覆盖整份报告，包括没有 AI 结果但被手动标为已审查的项。
    db.add(RunControl(run_id=run.id, selection_mode='missing'))
    for entry in archive.items:
        item, _ = targets[(entry.domain, entry.row_number, entry.source_group)]
        state = db.get(ItemReviewState, item.id)
        if state is None:
            state = ItemReviewState(item_id=item.id, revision=1, reviewed_revision=0)
            db.add(state)
        else:
            state.revision += 1
        result_id = None
        if entry.result:
            result_id = uid()
            value = entry.result
            db.add(Result(id=result_id, run_id=run.id, item_id=item.id, domain=item.domain,
                          row_number=item.row_number, fields=value.fields, status=value.status,
                          explanation=value.explanation, evidence=value.evidence,
                          suggestion=value.suggestion, raw=value.raw))
            db.flush()
            db.add(ResultMeasurements(result_id=result_id, ai_threshold=value.ai_threshold, ai_value=value.ai_value))
            db.add(ResultWork(result_id=result_id, item_revision=state.revision))
        else:
            db.add(RunRetainedItem(run_id=run.id, item_id=item.id, result_id=None))
        state.source_result_id = result_id
        state.reviewed_revision = 0 if entry.pending else state.revision
        db.add(RunItemReviewState(run_id=run.id, item_id=item.id, pending=entry.pending, stale=entry.stale))
        note = db.get(ItemNote, item.id)
        if note is None and entry.note:
            db.add(ItemNote(item_id=item.id, note=entry.note, version=1))
        elif note is not None and note.note != entry.note:
            note.note, note.version = entry.note, note.version + 1
    audit(db, user, 'run.import', run.id, after={'filename': report.filename,
          'source_run_id': archive.run.source_id, 'items': len(rows),
          'pending': sum(entry.pending for entry in archive.items)})
    return run
