from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator

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
        if set(value) != set(COLUMNS) or any(len(v) > 32767 for v in value.values()):
            raise ValueError("必须提供完整的 11 个原始字段，每字段不超过 32767 字符")
        if not value["附件链接"].strip():
            raise ValueError("审查项的附件链接不能为空")
        return value

class KeywordChange(StrictModel):
    action: Literal["create", "update", "delete"]
    domain: str = Field(min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=128)
    payload: dict = Field(default_factory=dict)
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
