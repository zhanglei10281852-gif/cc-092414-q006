from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.core.errors import InvalidStateError, NotFoundError, SelfReviewError, StaleVersionError, ValidationError
from app.core.security import Principal
from app.database import get_connection, transaction
from app.food.service import _now
from app.services.audit import AuditContext, AuditService

# 结果达到限值 90% 及以上视为接近限值，复核时必须填写意见且严禁录入人自审。
NEAR_LIMIT_RATIO = 0.9

CERTIFICATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS food_certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_no TEXT NOT NULL UNIQUE,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    sample_id INTEGER REFERENCES food_samples(id) ON DELETE RESTRICT,
    lab_name TEXT NOT NULL,
    conclusion TEXT NOT NULL CHECK(conclusion IN ('pass','fail')),
    near_limit INTEGER NOT NULL DEFAULT 0 CHECK(near_limit IN (0,1)),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending_review','returned','approved','revoked')),
    version INTEGER NOT NULL DEFAULT 1,
    remark TEXT NOT NULL DEFAULT '',
    items_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_by_id INTEGER NOT NULL,
    updated_by TEXT NOT NULL,
    updated_by_id INTEGER NOT NULL,
    submitted_by TEXT,
    submitted_at TEXT,
    reviewed_by TEXT,
    reviewed_by_id INTEGER,
    reviewed_at TEXT,
    review_opinion TEXT,
    revoked_by TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_certificate_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_id INTEGER NOT NULL REFERENCES food_certificates(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('create','edit','submit','return','approve','revoke')),
    status TEXT NOT NULL,
    actor TEXT NOT NULL,
    actor_id INTEGER,
    opinion TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL,
    lot_snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(certificate_id, version)
);
CREATE TABLE IF NOT EXISTS food_risk_reevaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    certificate_id INTEGER NOT NULL REFERENCES food_certificates(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    previous_risk TEXT NOT NULL,
    new_status TEXT NOT NULL,
    new_risk TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_certificates_lot ON food_certificates(lot_id, status);
CREATE INDEX IF NOT EXISTS idx_food_certificate_versions ON food_certificate_versions(certificate_id, version);
CREATE INDEX IF NOT EXISTS idx_food_reevaluations_lot ON food_risk_reevaluations(lot_id, id);
"""

# 允许的状态流转：草稿/退回可编辑与送审，送审后可退回或通过，仅已通过可撤销。
EDITABLE_STATES = {"draft", "returned"}
SUBMITTABLE_STATES = {"draft", "returned"}
REVIEWABLE_STATES = {"pending_review"}
REVOKABLE_STATES = {"approved"}


def ensure_certificate_schema() -> None:
    get_connection().executescript(CERTIFICATE_SCHEMA)


def _analyze_items(items: list[dict[str, Any]]) -> tuple[str, bool]:
    """根据检测项推导证书结论与是否接近限值。"""
    conclusion = "pass"
    near_limit = False
    for item in items:
        value = float(item["value_mg_kg"])
        limit = float(item["limit_mg_kg"])
        if value > limit:
            conclusion = "fail"
        elif limit <= 0 or value >= limit * NEAR_LIMIT_RATIO:
            near_limit = True
    return conclusion, near_limit


class CertificateService:
    """第三方实验室证书的复核流转、版本留痕与批次风险再评估。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_certificate_schema()

    # ---------- 查询 ----------

    def get(self, certificate_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM food_certificates WHERE id=?", (certificate_id,)).fetchone()
        if row is None:
            raise NotFoundError("证书不存在", context={"certificate_id": certificate_id})
        return self._present(dict(row))

    def list_for_lot(self, lot_id: int) -> list[dict[str, Any]]:
        self._require_lot(lot_id)
        rows = self.connection.execute("SELECT * FROM food_certificates WHERE lot_id=? ORDER BY id", (lot_id,)).fetchall()
        return [self._present(dict(row)) for row in rows]

    def versions(self, certificate_id: int) -> list[dict[str, Any]]:
        self.get(certificate_id)
        rows = self.connection.execute(
            "SELECT * FROM food_certificate_versions WHERE certificate_id=? ORDER BY version", (certificate_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json"))
            item["lot_snapshot"] = json.loads(item.pop("lot_snapshot_json"))
            result.append(item)
        return result

    def reevaluations(self, lot_id: int) -> list[dict[str, Any]]:
        self._require_lot(lot_id)
        rows = self.connection.execute("SELECT * FROM food_risk_reevaluations WHERE lot_id=? ORDER BY id", (lot_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result

    def delivery_list(self, lot_id: int) -> list[dict[str, Any]]:
        """配送企业视图：仅复核通过的证书可见，不含内部复核意见。"""
        self._require_lot(lot_id)
        rows = self.connection.execute(
            "SELECT * FROM food_certificates WHERE lot_id=? AND status='approved' ORDER BY id", (lot_id,)
        ).fetchall()
        return [self._present_delivery(dict(row)) for row in rows]

    # ---------- 流转 ----------

    def create(self, lot_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("food.certificates.write")
        self._require_lot(lot_id)
        if payload.get("sample_id") is not None:
            sample = self.connection.execute("SELECT id FROM food_samples WHERE id=? AND lot_id=?", (payload["sample_id"], lot_id)).fetchone()
            if sample is None:
                raise NotFoundError("样品不存在或不属于该批次", context={"sample_id": payload["sample_id"]})
        conclusion, near_limit = _analyze_items(payload["items"])
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO food_certificates(certificate_no,lot_id,sample_id,lab_name,conclusion,near_limit,status,version,"
                "remark,items_json,created_by,created_by_id,updated_by,updated_by_id,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'draft',1,?,?,?,?,?,?,?,?)",
                (
                    payload["certificate_no"], lot_id, payload.get("sample_id"), payload["lab_name"], conclusion,
                    int(near_limit), payload.get("remark", ""), json.dumps(payload["items"], ensure_ascii=False),
                    principal.username, principal.user_id, principal.username, principal.user_id, now, now,
                ),
            )
            certificate_id = int(cursor.lastrowid)
            certificate = self._row(connection, certificate_id)
            self._append_version(connection, certificate, "create", principal, "", lot_id, now)
            self._audit(connection, principal, "certificate.create", certificate_id, None, certificate)
            return self._present(certificate)

    def update(self, certificate_id: int, payload: dict[str, Any], principal: Principal) -> dict[str, Any]:
        principal.require("food.certificates.write")
        if not any(key in payload for key in ("lab_name", "sample_id", "items", "remark")):
            raise ValidationError("没有可更新的证书字段")
        with transaction(immediate=True) as connection:
            before = self._require_transition(connection, certificate_id, payload["expected_version"], EDITABLE_STATES, "编辑")
            items = payload["items"] if payload.get("items") else json.loads(before["items_json"])
            conclusion, near_limit = _analyze_items(items)
            lab_name = payload["lab_name"] if payload.get("lab_name") else before["lab_name"]
            remark = (payload["remark"] or "") if "remark" in payload else before["remark"]
            sample_id = payload["sample_id"] if "sample_id" in payload else before["sample_id"]
            if sample_id is not None:
                sample = connection.execute("SELECT id FROM food_samples WHERE id=? AND lot_id=?", (sample_id, before["lot_id"])).fetchone()
                if sample is None:
                    raise NotFoundError("样品不存在或不属于该批次", context={"sample_id": sample_id})
            now = _now()
            connection.execute(
                "UPDATE food_certificates SET lab_name=?,sample_id=?,conclusion=?,near_limit=?,remark=?,items_json=?,"
                "updated_by=?,updated_by_id=?,version=version+1,updated_at=? WHERE id=?",
                (lab_name, sample_id, conclusion, int(near_limit), remark, json.dumps(items, ensure_ascii=False),
                 principal.username, principal.user_id, now, certificate_id),
            )
            certificate = self._row(connection, certificate_id)
            self._append_version(connection, certificate, "edit", principal, "", before["lot_id"], now)
            self._audit(connection, principal, "certificate.update", certificate_id, before, certificate)
            return self._present(certificate)

    def submit(self, certificate_id: int, expected_version: int, principal: Principal) -> dict[str, Any]:
        principal.require("food.certificates.write")
        with transaction(immediate=True) as connection:
            before = self._require_transition(connection, certificate_id, expected_version, SUBMITTABLE_STATES, "送审")
            now = _now()
            connection.execute(
                "UPDATE food_certificates SET status='pending_review',submitted_by=?,submitted_at=?,"
                "reviewed_by=NULL,reviewed_by_id=NULL,reviewed_at=NULL,review_opinion=NULL,"
                "version=version+1,updated_at=? WHERE id=?",
                (principal.username, now, now, certificate_id),
            )
            certificate = self._row(connection, certificate_id)
            self._append_version(connection, certificate, "submit", principal, "", before["lot_id"], now)
            self._audit(connection, principal, "certificate.submit", certificate_id, before, certificate)
            return self._present(certificate)

    def send_back(self, certificate_id: int, expected_version: int, opinion: str, principal: Principal) -> dict[str, Any]:
        principal.require("food.certificates.review")
        try:
            with transaction(immediate=True) as connection:
                before = self._require_transition(connection, certificate_id, expected_version, REVIEWABLE_STATES, "退回")
                self._require_other_reviewer(before, principal)
                now = _now()
                connection.execute(
                    "UPDATE food_certificates SET status='returned',reviewed_by=?,reviewed_by_id=?,reviewed_at=?,review_opinion=?,"
                    "version=version+1,updated_at=? WHERE id=?",
                    (principal.username, principal.user_id, now, opinion, now, certificate_id),
                )
                certificate = self._row(connection, certificate_id)
                self._append_version(connection, certificate, "return", principal, opinion, before["lot_id"], now)
                self._audit(connection, principal, "certificate.return", certificate_id, before, certificate, {"opinion": opinion})
                return self._present(certificate)
        except SelfReviewError:
            self._record_denied_review(principal, "certificate.return", certificate_id)
            raise

    def approve(self, certificate_id: int, expected_version: int, opinion: str, principal: Principal) -> dict[str, Any]:
        principal.require("food.certificates.review")
        try:
            with transaction(immediate=True) as connection:
                before = self._require_transition(connection, certificate_id, expected_version, REVIEWABLE_STATES, "复核通过")
                self._require_other_reviewer(before, principal)
                if before["near_limit"] and not opinion.strip():
                    raise ValidationError("接近限值的证书复核通过时必须填写复核意见")
                now = _now()
                connection.execute(
                    "UPDATE food_certificates SET status='approved',reviewed_by=?,reviewed_by_id=?,reviewed_at=?,review_opinion=?,"
                    "version=version+1,updated_at=? WHERE id=?",
                    (principal.username, principal.user_id, now, opinion, now, certificate_id),
                )
                certificate = self._row(connection, certificate_id)
                self._append_version(connection, certificate, "approve", principal, opinion, before["lot_id"], now)
                self._audit(connection, principal, "certificate.approve", certificate_id, before, certificate, {"opinion": opinion})
                return self._present(certificate)
        except SelfReviewError:
            self._record_denied_review(principal, "certificate.approve", certificate_id)
            raise

    def revoke(self, certificate_id: int, expected_version: int, reason: str, principal: Principal) -> dict[str, Any]:
        principal.require("food.certificates.revoke")
        with transaction(immediate=True) as connection:
            before = self._require_transition(connection, certificate_id, expected_version, REVOKABLE_STATES, "撤销")
            now = _now()
            connection.execute(
                "UPDATE food_certificates SET status='revoked',revoked_by=?,revoked_at=?,revoke_reason=?,"
                "version=version+1,updated_at=? WHERE id=?",
                (principal.username, now, reason, now, certificate_id),
            )
            certificate = self._row(connection, certificate_id)
            self._append_version(connection, certificate, "revoke", principal, reason, before["lot_id"], now)
            reevaluation = self._reevaluate_lot(connection, before["lot_id"], certificate_id, principal, now)
            self._audit(connection, principal, "certificate.revoke", certificate_id, before, certificate,
                        {"reason": reason, "reevaluation": reevaluation})
            return self._present(certificate)

    # ---------- 内部 ----------

    def _require_lot(self, lot_id: int) -> None:
        if self.connection.execute("SELECT id FROM food_lots WHERE id=?", (lot_id,)).fetchone() is None:
            raise NotFoundError("批次不存在", context={"lot_id": lot_id})

    def _row(self, connection: sqlite3.Connection, certificate_id: int) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM food_certificates WHERE id=?", (certificate_id,)).fetchone()
        if row is None:
            raise NotFoundError("证书不存在", context={"certificate_id": certificate_id})
        return dict(row)

    def _require_transition(
        self, connection: sqlite3.Connection, certificate_id: int, expected_version: int, allowed: set[str], action: str
    ) -> dict[str, Any]:
        certificate = self._row(connection, certificate_id)
        if certificate["version"] != expected_version:
            raise StaleVersionError(
                "证书版本已过期，请刷新后重试",
                context={"certificate_id": certificate_id, "expected_version": expected_version, "current_version": certificate["version"]},
            )
        if certificate["status"] not in allowed:
            raise InvalidStateError(
                f"当前状态不允许{action}",
                context={"certificate_id": certificate_id, "status": certificate["status"], "allowed": sorted(allowed)},
            )
        return certificate

    def _require_other_reviewer(self, certificate: dict[str, Any], principal: Principal) -> None:
        """录入人（含最近修改人）不能复核同一张证书，接近限值的结果尤其禁止自录自审。"""
        if principal.user_id in {certificate["created_by_id"], certificate["updated_by_id"]}:
            raise SelfReviewError(
                "录入人与复核人必须为不同的授权人员",
                context={"certificate_id": certificate["id"], "near_limit": bool(certificate["near_limit"])},
            )

    def _record_denied_review(self, principal: Principal, action: str, certificate_id: int) -> None:
        """自审拒绝独立成事务留痕，避免随业务回滚一起消失。"""
        with transaction(immediate=True) as connection:
            certificate = self._row(connection, certificate_id)
            self._audit(
                connection, principal, action, certificate_id, certificate, None,
                {"denied": "self_review", "near_limit": bool(certificate["near_limit"])}, outcome="denied",
            )

    def _lot_snapshot(self, connection: sqlite3.Connection, lot_id: int) -> dict[str, Any]:
        lot = connection.execute("SELECT id,status,risk_level,version FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        failed = connection.execute(
            "SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? AND r.verdict='fail'",
            (lot_id,),
        ).fetchone()[0]
        approved = connection.execute(
            "SELECT COUNT(*) FROM food_certificates WHERE lot_id=? AND status='approved'", (lot_id,)
        ).fetchone()[0]
        near = connection.execute(
            "SELECT COUNT(*) FROM food_certificates WHERE lot_id=? AND status='approved' AND near_limit=1", (lot_id,)
        ).fetchone()[0]
        return {
            "lot_id": lot_id,
            "status": lot["status"] if lot else None,
            "risk_level": lot["risk_level"] if lot else None,
            "lot_version": lot["version"] if lot else None,
            "failed_result_count": failed,
            "approved_certificate_count": approved,
            "near_limit_certificate_count": near,
        }

    def _append_version(
        self,
        connection: sqlite3.Connection,
        certificate: dict[str, Any],
        action: str,
        principal: Principal,
        opinion: str,
        lot_id: int,
        now: str,
    ) -> None:
        snapshot = {key: certificate[key] for key in ("certificate_no", "lot_id", "sample_id", "lab_name", "conclusion", "near_limit", "status", "version", "remark")}
        snapshot["items"] = json.loads(certificate["items_json"])
        connection.execute(
            "INSERT INTO food_certificate_versions(certificate_id,version,action,status,actor,actor_id,opinion,snapshot_json,lot_snapshot_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                certificate["id"], certificate["version"], action, certificate["status"], principal.username, principal.user_id,
                opinion, json.dumps(snapshot, ensure_ascii=False), json.dumps(self._lot_snapshot(connection, lot_id), ensure_ascii=False), now,
            ),
        )

    def _reevaluate_lot(
        self, connection: sqlite3.Connection, lot_id: int, certificate_id: int, principal: Principal, now: str
    ) -> dict[str, Any]:
        """证书撤销后依据剩余有效证据重新评估批次，而不是静默删除。"""
        lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            return {"lot_id": lot_id, "changed": False, "reason": "lot_missing"}
        previous_status, previous_risk = lot["status"], lot["risk_level"]
        snapshot = self._lot_snapshot(connection, lot_id)
        failed_approved = connection.execute(
            "SELECT COUNT(*) FROM food_certificates WHERE lot_id=? AND status='approved' AND conclusion='fail'", (lot_id,)
        ).fetchone()[0]
        if previous_status in {"recalled", "destroyed"}:
            new_status, new_risk = previous_status, previous_risk
        else:
            if snapshot["failed_result_count"] or failed_approved:
                new_risk = "high"
            elif snapshot["approved_certificate_count"] == 0:
                new_risk = "unknown"
            elif snapshot["near_limit_certificate_count"]:
                new_risk = "medium"
            else:
                new_risk = "low"
            new_status = previous_status
            if new_risk == "high" and previous_status in {"pending", "testing", "released"}:
                new_status = "held"
            elif previous_status == "released" and snapshot["approved_certificate_count"] == 0:
                new_status = "testing"
        changed = (new_status, new_risk) != (previous_status, previous_risk)
        if changed:
            connection.execute(
                "UPDATE food_lots SET status=?,risk_level=?,version=version+1,updated_at=? WHERE id=?",
                (new_status, new_risk, now, lot_id),
            )
        detail = {"changed": changed, "lot_snapshot": snapshot, "certificate_id": certificate_id}
        connection.execute(
            "INSERT INTO food_risk_reevaluations(lot_id,certificate_id,trigger,previous_status,previous_risk,new_status,new_risk,detail_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (lot_id, certificate_id, "certificate.revoke", previous_status, previous_risk, new_status, new_risk,
             json.dumps(detail, ensure_ascii=False), now),
        )
        self._audit(
            connection, principal, "lot.reevaluate", lot_id,
            {"status": previous_status, "risk_level": previous_risk},
            {"status": new_status, "risk_level": new_risk},
            {"trigger": "certificate.revoke", "certificate_id": certificate_id},
            resource_type="food_lot",
        )
        return {"lot_id": lot_id, "changed": changed, "previous_status": previous_status, "previous_risk": previous_risk,
                "new_status": new_status, "new_risk": new_risk}

    def _audit(
        self,
        connection: sqlite3.Connection,
        principal: Principal,
        action: str,
        resource_id: int,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        metadata: dict[str, Any] | None = None,
        outcome: str = "success",
        resource_type: str = "food_certificate",
    ) -> None:
        AuditService(connection).record(
            AuditContext(principal.user_id, principal.display_name),
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            before=before,
            after=after,
            metadata=metadata,
        )

    def _present(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["items"] = json.loads(result.pop("items_json"))
        result["near_limit"] = bool(result["near_limit"])
        return result

    def _present_delivery(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "certificate_no": row["certificate_no"],
            "lot_id": row["lot_id"],
            "lab_name": row["lab_name"],
            "conclusion": row["conclusion"],
            "near_limit": bool(row["near_limit"]),
            "items": json.loads(row["items_json"]),
            "approved_at": row["reviewed_at"],
            "approved_by": row["reviewed_by"],
        }
