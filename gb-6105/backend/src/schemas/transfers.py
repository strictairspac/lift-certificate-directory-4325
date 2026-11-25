from datetime import date

from pydantic import BaseModel, Field, field_validator

from src.domain.certificates import CODE


class TransferInput(BaseModel):
    left_dossier_id: int
    right_dossier_id: int
    left_codes: list[str] = Field(min_length=1, max_length=400)
    right_codes: list[str] = Field(min_length=1, max_length=400)

    @field_validator("left_codes", "right_codes")
    @classmethod
    def check_codes(cls, codes):
        for code in codes:
            if not CODE.fullmatch(code):
                raise ValueError("编号须为 1–40 位大写字母、数字或连字符")
        return codes


class TransferOrderItem(BaseModel):
    id: int
    order_id: int
    certificate_code: str
    source_dossier_id: int
    target_dossier_id: int


class TransferOrderEntry(BaseModel):
    id: int
    request_key: str
    left_dossier_id: int
    right_dossier_id: int
    status: str
    quantity: int
    actor: str
    created_at: int
    payload: dict
    items: list[TransferOrderItem]


class DossierCertificate(BaseModel):
    code: str
    lift: str
    expires_on: str
    revoked: str


class DossierEntry(BaseModel):
    id: int
    code: str
    title: str
    capacity: int
    created_by: str
    certificates: list[DossierCertificate]


class PointCheckInput(BaseModel):
    probes: list[str] = Field(min_length=1, max_length=100)
    review_date: date

    @field_validator("probes")
    @classmethod
    def check_probes(cls, probes):
        for code in probes:
            if not CODE.fullmatch(code):
                raise ValueError("编号须为 1–40 位大写字母、数字或连字符")
        return probes


class PointCheckRow(BaseModel):
    类别: str
    项目: str
    归属卷宗: str | None
    电梯或槽位: str | None
    桶线索: int | None = None
    结论: str


class TransferVerification(BaseModel):
    order_id: int
    request_key: str
    status: str
    quantity: int
    left_dossier_id: int
    right_dossier_id: int
    rows: list[PointCheckRow]
