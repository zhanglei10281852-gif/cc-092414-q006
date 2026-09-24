from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class LotCreate(BaseModel):
    lot_code: str = Field(..., min_length=3, max_length=64)
    product_name: str = Field(..., min_length=1, max_length=120)
    category: str = Field(..., min_length=1, max_length=40)
    supplier: str = Field(..., min_length=1, max_length=120)
    origin: str = Field(..., min_length=1, max_length=160)
    harvest_date: str = Field(..., min_length=10, max_length=40)
    quantity_kg: float = Field(..., gt=0, le=1000000)
    trace_code: str = Field(..., min_length=4, max_length=120)

    @field_validator("lot_code", "trace_code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        return value.strip().upper()


class SampleCreate(BaseModel):
    sample_code: str = Field(..., min_length=3, max_length=64)
    collected_at: str = Field(..., min_length=20, max_length=40)
    collector: str = Field(..., min_length=1, max_length=80)
    location: str = Field(..., min_length=1, max_length=160)
    sample_weight_g: float = Field(..., gt=0, le=10000)


class TestResultCreate(BaseModel):
    analyte: str = Field(..., min_length=1, max_length=80)
    method: str = Field(..., min_length=1, max_length=80)
    value_mg_kg: float = Field(..., ge=0, le=100000)
    limit_mg_kg: float = Field(..., ge=0, le=100000)
    unit: str = Field(default="mg/kg", min_length=1, max_length=20)
    lab_operator: str = Field(..., min_length=1, max_length=80)
    tested_at: str = Field(..., min_length=20, max_length=40)
    certificate_no: str = Field(default="", max_length=80)


class ShipmentCreate(BaseModel):
    shipment_code: str = Field(..., min_length=3, max_length=64)
    carrier: str = Field(..., min_length=1, max_length=120)
    vehicle_no: str = Field(..., min_length=1, max_length=40)
    departure_at: str = Field(..., min_length=20, max_length=40)
    arrival_due_at: str = Field(..., min_length=20, max_length=40)
    destination: str = Field(..., min_length=1, max_length=160)
    target_temp_min: float = Field(default=0, ge=-40, le=30)
    target_temp_max: float = Field(default=8, ge=-20, le=50)


class TemperatureRecord(BaseModel):
    recorded_at: str = Field(..., min_length=20, max_length=40)
    temperature_c: float = Field(..., ge=-80, le=100)
    source: str = Field(default="sensor", min_length=1, max_length=40)


class RiskDecision(BaseModel):
    decision: str = Field(..., pattern="^(release|hold|recall|destroy)$")
    reason: str = Field(..., min_length=1, max_length=300)
    operator: str = Field(..., min_length=1, max_length=80)


class CertificateItem(BaseModel):
    analyte: str = Field(..., min_length=1, max_length=80)
    method: str = Field(..., min_length=1, max_length=80)
    value_mg_kg: float = Field(..., ge=0, le=100000)
    limit_mg_kg: float = Field(..., ge=0, le=100000)
    unit: str = Field(default="mg/kg", min_length=1, max_length=20)


class CertificateCreate(BaseModel):
    certificate_no: str = Field(..., min_length=3, max_length=80)
    lab_name: str = Field(..., min_length=1, max_length=120)
    sample_id: int | None = None
    items: list[CertificateItem] = Field(..., min_length=1, max_length=50)
    remark: str = Field(default="", max_length=300)

    @field_validator("certificate_no")
    @classmethod
    def normalize_certificate_no(cls, value: str) -> str:
        return value.strip().upper()


class CertificateUpdate(BaseModel):
    expected_version: int = Field(..., ge=1)
    lab_name: str | None = Field(default=None, min_length=1, max_length=120)
    sample_id: int | None = None
    items: list[CertificateItem] | None = Field(default=None, min_length=1, max_length=50)
    remark: str | None = Field(default=None, max_length=300)


class CertificateSubmit(BaseModel):
    expected_version: int = Field(..., ge=1)


class CertificateReturn(BaseModel):
    expected_version: int = Field(..., ge=1)
    opinion: str = Field(..., min_length=1, max_length=300)


class CertificateApprove(BaseModel):
    expected_version: int = Field(..., ge=1)
    opinion: str = Field(default="", max_length=300)


class CertificateRevoke(BaseModel):
    expected_version: int = Field(..., ge=1)
    reason: str = Field(..., min_length=1, max_length=300)

