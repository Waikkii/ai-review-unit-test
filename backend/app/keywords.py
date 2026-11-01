"""现有五列关键词库与版本/审批存储之间的适配。"""
import hashlib
import json

KEY_FIELDS = {"a": "测试对象", "b": "用例编号", "c": "用例名称", "d": "测试小项"}
ENTRY_DOMAIN = "__five_column_keywords__"


def entry_key(payload):
    # JSON 数组避免简单拼接造成复合键碰撞；不 trim，保持 TEXT 精确匹配。
    identity = json.dumps([payload[k] for k in KEY_FIELDS], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def matches_entry(payload, fields):
    return all(name in fields and fields[name] == payload.get(key)
               for key, name in KEY_FIELDS.items())


def matching_keywords(snapshot, fields, domain):
    return [k for k in snapshot if not k.get("deleted") and (
        matches_entry(k["payload"], fields) if k["domain"] == ENTRY_DOMAIN else k["domain"] == domain)]
