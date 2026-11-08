"""五字段关键词键与版本/审批存储之间的适配。"""
import hashlib
import json

LEGACY_KEY_FIELDS = {"a": "测试对象", "b": "用例编号", "c": "用例名称", "d": "测试小项"}
KEY_FIELDS = {**LEGACY_KEY_FIELDS, "e": "指标名称"}
LEGACY_ENTRY_DOMAIN = "__five_column_keywords__"
ENTRY_DOMAIN = "__six_column_keywords__"


def entry_key(payload):
    # JSON 数组避免简单拼接造成复合键碰撞；不 trim，保持 TEXT 精确匹配。
    identity = json.dumps([payload[k] for k in KEY_FIELDS], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def matches_entry(payload, fields, key_fields=KEY_FIELDS):
    return all(name in fields and fields[name] == payload.get(key)
               for key, name in key_fields.items())


def keyword_text(entry):
    payload = entry.get("payload", {})
    return payload.get("i" if entry.get("domain") == ENTRY_DOMAIN else "e")


def matching_keywords(snapshot, fields, domain):
    return [k for k in snapshot if not k.get("deleted") and (
        matches_entry(k["payload"], fields, KEY_FIELDS if k["domain"] == ENTRY_DOMAIN else LEGACY_KEY_FIELDS)
        if k["domain"] in (ENTRY_DOMAIN, LEGACY_ENTRY_DOMAIN) else k["domain"] == domain)]
