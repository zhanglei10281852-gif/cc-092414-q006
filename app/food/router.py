from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import current_principal
from app.core.security import Principal
from app.food.certificates import CertificateService
from app.food.schemas import (
    CertificateApprove,
    CertificateCreate,
    CertificateReturn,
    CertificateRevoke,
    CertificateSubmit,
    CertificateUpdate,
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


def certificates() -> CertificateService:
    return CertificateService()


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


# ---------- 第三方实验室证书复核流转 ----------


@router.post("/lots/{lot_id}/certificates", status_code=201)
def create_certificate(lot_id: int, payload: CertificateCreate, principal: Principal = Depends(current_principal)):
    try:
        return certificates().create(lot_id, payload.model_dump(), principal)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="证书编号已存在") from exc
        raise


@router.get("/lots/{lot_id}/certificates")
def list_certificates(lot_id: int, principal: Principal = Depends(current_principal)):
    principal.require("food.certificates.read")
    return {"items": certificates().list_for_lot(lot_id)}


@router.get("/lots/{lot_id}/reevaluations")
def list_reevaluations(lot_id: int, principal: Principal = Depends(current_principal)):
    principal.require("food.certificates.read")
    return {"items": certificates().reevaluations(lot_id)}


@router.get("/delivery/lots/{lot_id}/certificates")
def delivery_certificates(lot_id: int, principal: Principal = Depends(current_principal)):
    """配送企业视图：仅复核通过的证书可见。"""
    principal.require("food.delivery.read")
    return {"items": certificates().delivery_list(lot_id)}


@router.get("/certificates/{certificate_id}")
def get_certificate(certificate_id: int, principal: Principal = Depends(current_principal)):
    principal.require("food.certificates.read")
    return certificates().get(certificate_id)


@router.get("/certificates/{certificate_id}/versions")
def get_certificate_versions(certificate_id: int, principal: Principal = Depends(current_principal)):
    principal.require("food.certificates.read")
    return {"items": certificates().versions(certificate_id)}


@router.put("/certificates/{certificate_id}")
def update_certificate(certificate_id: int, payload: CertificateUpdate, principal: Principal = Depends(current_principal)):
    return certificates().update(certificate_id, payload.model_dump(exclude_unset=True), principal)


@router.post("/certificates/{certificate_id}/submit")
def submit_certificate(certificate_id: int, payload: CertificateSubmit, principal: Principal = Depends(current_principal)):
    return certificates().submit(certificate_id, payload.expected_version, principal)


@router.post("/certificates/{certificate_id}/return")
def return_certificate(certificate_id: int, payload: CertificateReturn, principal: Principal = Depends(current_principal)):
    return certificates().send_back(certificate_id, payload.expected_version, payload.opinion, principal)


@router.post("/certificates/{certificate_id}/approve")
def approve_certificate(certificate_id: int, payload: CertificateApprove, principal: Principal = Depends(current_principal)):
    return certificates().approve(certificate_id, payload.expected_version, payload.opinion, principal)


@router.post("/certificates/{certificate_id}/revoke")
def revoke_certificate(certificate_id: int, payload: CertificateRevoke, principal: Principal = Depends(current_principal)):
    return certificates().revoke(certificate_id, payload.expected_version, payload.reason, principal)
