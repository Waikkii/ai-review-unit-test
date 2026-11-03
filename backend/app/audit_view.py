"""Concise, human-readable activity feed over the retained audit trail."""
SUMMARY_ACTIONS = {
    "report.upload": ("上传报告", "report"),
    "run.start": ("发起 AI 审查", "review"),
    "report.download": ("下载原始报告", "download"),
    "run.export": ("导出审查结果", "download"),
    "keyword.request": ("提交关键词变更", "keyword"),
    "report.clear": ("清除报告历史", "report"),
    "run.clear": ("删除审查批次", "review"),
    "run.abort": ("中止审查", "review"),
    "item.review_state": ("修改审查状态", "review"),
}


def describe_activity(record, report_names, run_names):
    label, category = SUMMARY_ACTIONS.get(record.action, ("其他操作", "other"))
    after = record.after or {}
    if record.action == "keyword.request":
        verb = {"create": "新增", "update": "修改", "delete": "删除"}.get(after.get("action"), "变更")
        label = "提交关键词" + verb
        payload = after.get("payload", {})
        target = " / ".join(str(payload[k]) for k in ("a", "c", "d") if payload.get(k))
        target = target or " / ".join(str(after[k]) for k in ("domain", "key") if after.get(k))
        return label, category, target or "关键词申请"
    target = report_names.get(record.object_id) or run_names.get(record.object_id)
    target = target or after.get("filename") or "历史操作"
    return label, category, target
