"""Excel-style column facets, evaluated before pagination across the whole report."""
import json
from fastapi import HTTPException
from .keywords import KEY_FIELDS, ENTRY_DOMAIN

ORIGINAL_COLUMNS = ('测试对象', '用例编号', '用例名称', '测试小项', '指标名称', '测试条件',
                    '判据门限', '单位', '门限', '值', '备注', '附件链接')
COLUMNS = ('domain', *('field:' + name for name in ORIGINAL_COLUMNS),
           'ai_threshold', 'ai_value', 'result', 'note', 'state', 'keyword')


def table_page(rows, filters, offset, limit, keywords, is_run):
    try:
        selected = json.loads(filters)
        if not isinstance(selected, dict) or any(key not in COLUMNS or not isinstance(values, list)
                or any(not isinstance(value, str) for value in values) for key, values in selected.items()):
            raise ValueError()
    except (ValueError, TypeError):
        raise HTTPException(422, '表格筛选格式无效') from None
    index = {tuple(k.payload.get(key) for key in KEY_FIELDS): k.payload['i']
             for k in keywords if k.domain == ENTRY_DOMAIN and not k.deleted}
    decorated = []
    for row in rows:
        fields = row['fields'] if is_run else row['current']
        result = row if is_run else row.get('review')
        pending = bool(result and row['pending_review'] and (
            result['status'] not in ('error', 'missing_keyword') or result.get('stale')))
        visible = result and not pending
        values = {'domain': row['domain'], 'state': '待审查' if row['pending_review'] else '已审查',
                  'note': row.get('note', ''),
                  'keyword': index.get(tuple(fields.get(name) for name in KEY_FIELDS.values()), '未配置'),
                  'ai_threshold': result.get('ai_threshold', '') if visible else '',
                  'ai_value': result.get('ai_value', '') if visible else '',
                  'result': result.get('result_label', '') if visible and result['status'] != 'pending' else '待审查'}
        values.update({'field:' + name: str(fields.get(name, '') or '') for name in ORIGINAL_COLUMNS})
        decorated.append((row, values))
    options = {key: set() for key in COLUMNS}
    matching = []
    for row, values in decorated:
        excluded = {key for key, choices in selected.items() if values[key] not in choices}
        if not excluded:
            matching.append(row)
        for key in COLUMNS:
            if not excluded or excluded == {key}:
                options[key].add(values[key])
    return {'items': matching[offset:offset + limit], 'total': len(matching),
            'options': {key: sorted(values) for key, values in options.items()}}
