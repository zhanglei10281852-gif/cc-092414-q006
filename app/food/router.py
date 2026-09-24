from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.food.certificates import CertificateWorkflowService
from app.food.schemas import (
    CertificateCreate,
    CertificateOpinion,
    CertificateRevise,
    CertificateRevokeRequest,
    LotCreate,
    RiskDecision,
    SampleCreate,
    ShipmentCreate,
    TemperatureRecord,
    TestResultCreate,
)
from app.food.service import FoodService

router = APIRouter(prefix="/api/food", tags=["食品安全"])


def service() -> FoodService:
    return FoodService()


def certificates() -> CertificateWorkflowService:
    return CertificateWorkflowService()


@router.post("/lots", status_code=201)
def create_lot(payload: LotCreate):
    try:
        return service().create_lot(payload.model_dump(), actor=payload.supplier)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="批次编码或追溯码已存在") from exc
        raise


@router.get("/lots/{lot_id}")
def get_lot(lot_id: int, details: bool = True):
    value = service().get_lot(lot_id, details)
    if value is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return value


@router.get("/lots/{lot_id}/summary")
def summary(lot_id: int):
    try:
        return service().summary(lot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.delete("/lots/{lot_id}")
def delete_lot(lot_id: int):
    try:
        service().delete_lot(lot_id)
        return {"message": "批次已删除"}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.post("/lots/{lot_id}/samples", status_code=201)
def add_sample(lot_id: int, payload: SampleCreate):
    try:
        return service().add_sample(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.post("/samples/{sample_id}/results", status_code=201)
def add_result(sample_id: int, payload: TestResultCreate):
    try:
        return service().add_result(sample_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="样品不存在") from exc


@router.post("/lots/{lot_id}/shipments", status_code=201)
def create_shipment(lot_id: int, payload: ShipmentCreate):
    try:
        return service().create_shipment(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/shipments/{shipment_id}/temperatures", status_code=201)
def add_temperature(shipment_id: int, payload: TemperatureRecord):
    try:
        return service().add_temperature(shipment_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运输单不存在") from exc


@router.post("/lots/{lot_id}/risk", status_code=200)
def decide_risk(lot_id: int, payload: RiskDecision):
    try:
        return service().decide_risk(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


# ---------------------------------------------------------------- 证书复核流转

@router.post("/lots/{lot_id}/certificates", status_code=201)
def create_certificate(lot_id: int, payload: CertificateCreate, principal: Principal = Depends(current_principal)):
    return certificates().create_certificate(principal, lot_id, payload.model_dump())


@router.get("/certificates")
def list_certificates(
    lot_id: int | None = None,
    status: str | None = None,
    principal: Principal = Depends(current_principal),
):
    principal.require("food.cert.read")
    return certificates().list_certificates(lot_id=lot_id, status=status)


@router.get("/certificates/{cert_id}")
def get_certificate(cert_id: int, principal: Principal = Depends(current_principal)):
    principal.require("food.cert.read")
    return certificates().detail(cert_id)


@router.put("/certificates/{cert_id}")
def revise_certificate(cert_id: int, payload: CertificateRevise, principal: Principal = Depends(current_principal)):
    data = payload.model_dump(exclude={"expected_version"})
    return certificates().revise_certificate(principal, cert_id, data, payload.expected_version)


@router.post("/certificates/{cert_id}/submit")
def submit_certificate(cert_id: int, payload: CertificateOpinion, principal: Principal = Depends(current_principal)):
    return certificates().submit_certificate(principal, cert_id, payload.opinion, payload.expected_version)


@router.post("/certificates/{cert_id}/return")
def return_certificate(cert_id: int, payload: CertificateOpinion, principal: Principal = Depends(current_principal)):
    return certificates().return_certificate(principal, cert_id, payload.opinion, payload.expected_version)


@router.post("/certificates/{cert_id}/approve")
def approve_certificate(cert_id: int, payload: CertificateOpinion, principal: Principal = Depends(current_principal)):
    return certificates().approve_certificate(principal, cert_id, payload.opinion, payload.expected_version)


@router.post("/certificates/{cert_id}/revoke")
def revoke_certificate(cert_id: int, payload: CertificateRevokeRequest, principal: Principal = Depends(current_principal)):
    return certificates().revoke_certificate(principal, cert_id, payload.reason, payload.expected_version)


@router.get("/lots/{lot_id}/certificates/visible")
def visible_certificates(lot_id: int, principal: Principal = Depends(current_principal)):
    """配送企业视图：只有已复核通过、未撤销的证书可见。"""
    principal.require("food.cert.read")
    return certificates().visible_certificates(lot_id)
