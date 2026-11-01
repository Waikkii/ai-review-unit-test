from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator
from .keywords import ENTRY_DOMAIN, entry_key

COLUMNS = ["用例编号", "用例名称", "测试小项", "指标名称", "测试条件", "判据门限", "单位", "门限", "值", "备注", "附件链接"]

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class Login(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)

class ItemEdit(StrictModel):
    version: int = Field(ge=1)
    fields: dict[str, str]
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("fields")
    @classmethod
    def validate_fields(cls, value):
        if (set(value) not in (set(COLUMNS), set(COLUMNS) | {"测试对象"})
                or any(len(v) > 32767 for v in value.values())):
            raise ValueError("必须提供完整原始字段（测试对象兼容旧报告可省略），每字段不超过 32767 字符")
        if not value["值"].strip():
            raise ValueError("审查项的值不能为空")
        return value

class KeywordChange(StrictModel):
    action: Literal["create", "update", "delete"]
    domain: str = Field(min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=128)
    payload: dict = Field(default_factory=dict)
    base_version: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1, max_length=2000)
    result_id: str | None = None

    @model_validator(mode="after")
    def validate_entry_identity(self):
        if self.domain == ENTRY_DOMAIN:
            payload = KeywordEntry.model_validate(self.payload).model_dump()
            if self.key != entry_key(payload):
                raise ValueError("关键词标识与 a～d 联合主键不一致")
        return self

class KeywordEntry(StrictModel):
    a: str = Field(max_length=32767)
    b: str = Field(max_length=32767)
    c: str = Field(max_length=32767)
    d: str = Field(max_length=32767)
    e: str = Field(min_length=1, max_length=32767)

    @field_validator("e")
    @classmethod
    def nonblank_keyword(cls, value):
        if not value.strip():
            raise ValueError("关键词不能为空")
        return value


class KeywordEntryChange(StrictModel):
    action: Literal["create", "update", "delete"]
    entry: KeywordEntry
    base_version: int = Field(default=0, ge=0)
    reason: str = Field(min_length=1, max_length=2000)
    result_id: str | None = None


class Decision(StrictModel):
    approve: bool
    note: str = Field(min_length=1, max_length=2000)

class UserChange(StrictModel):
    role: Literal["user", "admin"]
    active: bool
    reason: str = Field(min_length=1, max_length=2000)

class ReviewVerdict(StrictModel):
    status: Literal["passed", "failed", "needs_review"]
    explanation: str = Field(min_length=1, max_length=16000)
    evidence: list[str] = Field(default_factory=list, max_length=100)
    suggestion: str = Field(default="", max_length=16000)
