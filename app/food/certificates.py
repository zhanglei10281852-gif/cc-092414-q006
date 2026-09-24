from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    StaleVersionError,
    ValidationError,
)
from app.core.security import Principal
from app.database import transaction
from app.services.audit import AuditContext, AuditService

# 检测值达到限值的 90% 即视为“接近限值”，需要在证书上显式标注并强制复核
NEAR_LIMIT_RATIO = 0.9

CERT_STATUSES = {"draft", "pending_review", "returned", "approved", "revoked"}


class CertificateWorkflowService:
    """第三方实验室合格证书的草稿、送审、退回、复核通过与撤销流转。

    所有状态变更都在 IMMEDIATE 事务中以“条件更新 + 版本号”完成：
    重复点击要么命中幂等分支直接返回当前状态，要么被条件更新拦截，
    不会产生重复意见、重复审计或重复的批次风险动作。
    """

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        from app.database import get_connection

        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.audit = AuditService(self.connection, self.clock)

    # ------------------------------------------------------------------ 工具

    def _now(self) -> str:
        return to_storage(self.clock.now())

    def _require_permission(self, principal: Principal, permission: str, action: str, lot_id: int | None, cert_id: int | None) -> None:
        if principal.can(permission):
            return
        # 拒绝发生在事务之外，INSERT 自动提交，随后抛出的异常不会回滚这条留痕
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=f"certificate.{action}",
            resource_type="food_certificate",
            resource_id=cert_id or lot_id,
            outcome="denied",
            metadata={"required_permission": permission},
        )
        raise PermissionDeniedError(f"缺少权限：{permission}")

    def _record_denied(self, principal: Principal, action: str, cert: dict[str, Any], reason: str) -> None:
        self.audit.record(
            AuditContext(principal.user_id, principal.display_name),
            action=f"certificate.{action}",
            resource_type="food_certificate",
            resource_id=cert["id"],
            outcome="denied",
            metadata={"reason": reason, "certificate_status": cert["status"], "doc_version": cert["doc_version"]},
        )

    def _get_cert(self, cert_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM food_certificates WHERE id=?", (cert_id,)).fetchone()
        if row is None:
            raise NotFoundError("证书不存在")
        return dict(row)

    def _check_version(self, cert: dict[str, Any], expected_version: int | None) -> None:
        if expected_version is not None and cert["doc_version"] != expected_version:
            raise StaleVersionError(
                "证书版本已过期，请刷新后重试",
                context={"expected_version": expected_version, "current_version": cert["doc_version"]},
            )

    def _load_results(self, conn: sqlite3.Connection, lot_id: int, result_ids: list[int]) -> list[dict[str, Any]]:
        if not result_ids:
            raise ValidationError("证书至少要关联一条检测结果")
        placeholders = ",".join("?" for _ in result_ids)
        rows = conn.execute(
            f"SELECT r.*, s.lot_id AS lot_id, s.sample_code AS sample_code "
            f"FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id "
            f"WHERE r.id IN ({placeholders})",
            tuple(dict.fromkeys(result_ids)),
        ).fetchall()
        if len(rows) != len(set(result_ids)):
            raise ValidationError("部分检测结果不存在")
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item["lot_id"] != lot_id:
                raise ValidationError("证书只能引用本批次的检测结果")
            limit = float(item["limit_mg_kg"])
            value = float(item["value_mg_kg"])
            ratio = round(value / limit, 4) if limit > 0 else None
            item["ratio_to_limit"] = ratio
            item["near_limit"] = int(ratio is not None and NEAR_LIMIT_RATIO <= ratio <= 1.0)
            results.append(item)
        results.sort(key=lambda item: item["id"])
        return results

    @staticmethod
    def _build_payload(data: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "certificate_no": data["certificate_no"],
            "lab_name": data["lab_name"],
            "issued_at": data["issued_at"],
            "summary": data.get("summary", ""),
            "results": [
                {
                    "result_id": item["id"],
                    "sample_code": item["sample_code"],
                    "analyte": item["analyte"],
                    "method": item["method"],
                    "value_mg_kg": item["value_mg_kg"],
                    "limit_mg_kg": item["limit_mg_kg"],
                    "unit": item["unit"],
                    "verdict": item["verdict"],
                    "ratio_to_limit": item["ratio_to_limit"],
                    "near_limit": item["near_limit"],
                }
                for item in results
            ],
        }

    def _content_hash(self, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _risk_snapshot(self, conn: sqlite3.Connection, lot: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        lot_id = lot["id"]
        stats = conn.execute(
            "SELECT r.verdict, COUNT(*) AS c FROM food_test_results r "
            "JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? GROUP BY r.verdict",
            (lot_id,),
        ).fetchall()
        counts = {row["verdict"]: row["c"] for row in stats}
        results = conn.execute(
            "SELECT r.id, r.analyte, r.value_mg_kg, r.limit_mg_kg, r.verdict "
            "FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id "
            "WHERE s.lot_id=? ORDER BY r.id",
            (lot_id,),
        ).fetchall()
        near_count = 0
        for row in results:
            limit = float(row["limit_mg_kg"])
            if limit > 0 and NEAR_LIMIT_RATIO <= float(row["value_mg_kg"]) / limit <= 1.0:
                near_count += 1
        approved_certs = conn.execute(
            "SELECT id, certificate_no, overall_verdict, near_limit, doc_version "
            "FROM food_certificates WHERE lot_id=? AND status='approved' ORDER BY id",
            (lot_id,),
        ).fetchall()
        return {
            "captured_at": self._now(),
            "lot": {
                "id": lot["id"],
                "lot_code": lot["lot_code"],
                "status": lot["status"],
                "risk_level": lot["risk_level"],
                "version": lot["version"],
            },
            "result_count": sum(counts.values()),
            "failed_count": counts.get("fail", 0),
            "near_limit_count": near_count,
            "approved_certificates": [dict(row) for row in approved_certs],
        }

    def _module_audit(self, conn: sqlite3.Connection, cert: dict[str, Any], action: str, actor: str, extra: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)",
            (cert["lot_id"], action, actor, json.dumps(extra, ensure_ascii=False), self._now()),
        )

    def _build_detail(self, conn: sqlite3.Connection, cert: dict[str, Any]) -> dict[str, Any]:
        data = dict(cert)
        data.pop("payload_json", None)
        version_rows = conn.execute(
            "SELECT * FROM food_certificate_versions WHERE certificate_id=? ORDER BY doc_version",
            (cert["id"],),
        ).fetchall()
        versions: list[dict[str, Any]] = []
        for row in version_rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            item["risk_snapshot"] = json.loads(item.pop("risk_snapshot_json"))
            versions.append(item)
        data["versions"] = versions
        if versions:
            data["payload"] = versions[-1]["payload"]
            data["content_hash"] = versions[-1]["content_hash"]
        else:  # 理论上不会发生，防御性兜底
            data["payload"] = {}
            data["content_hash"] = None
        opinion_rows = conn.execute(
            "SELECT * FROM food_certificate_opinions WHERE certificate_id=? ORDER BY id",
            (cert["id"],),
        ).fetchall()
        data["opinions"] = [dict(row) for row in opinion_rows]
        return data

    # ------------------------------------------------------------------ 草稿

    def create_certificate(self, principal: Principal, lot_id: int, data: dict[str, Any]) -> dict[str, Any]:
        self._require_permission(principal, "food.cert.create", "create", lot_id, None)
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFoundError("批次不存在")
        now = self._now()
        with transaction(immediate=True) as conn:
            lot = conn.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            if lot is None:
                raise NotFoundError("批次不存在")
            results = self._load_results(conn, lot_id, data["result_ids"])
            payload = self._build_payload(data, results)
            verdict = "fail" if any(item["verdict"] == "fail" for item in results) else "pass"
            near_limit = int(any(item["near_limit"] for item in results))
            snapshot = self._risk_snapshot(conn, lot)
            content_hash = self._content_hash(payload)
            try:
                cursor = conn.execute(
                    "INSERT INTO food_certificates(lot_id,certificate_no,lab_name,issued_at,overall_verdict,near_limit,"
                    "payload_json,status,doc_version,created_by_user_id,created_by_name,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'draft',1,?,?,?,?)",
                    (
                        lot_id, data["certificate_no"], data["lab_name"], data["issued_at"], verdict, near_limit,
                        json.dumps(payload, ensure_ascii=False), principal.user_id, principal.display_name, now, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("证书编号已存在") from exc
            cert_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO food_certificate_versions(certificate_id,doc_version,change_type,payload_json,content_hash,"
                "overall_verdict,near_limit,risk_snapshot_json,snapshot_lot_version,snapshot_lot_status,"
                "snapshot_lot_risk_level,created_by_user_id,created_by_name,created_at) "
                "VALUES(?,1,'create',?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cert_id, json.dumps(payload, ensure_ascii=False), content_hash, verdict, near_limit,
                    json.dumps(snapshot, ensure_ascii=False), lot["version"], lot["status"], lot["risk_level"],
                    principal.user_id, principal.display_name, now,
                ),
            )
            cert = self._get_cert_locked(conn, cert_id)
            self._module_audit(conn, cert, "certificate.create", principal.display_name, {"certificate_no": data["certificate_no"]})
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="certificate.create",
                resource_type="food_certificate",
                resource_id=cert_id,
                after={"status": "draft", "doc_version": 1, "overall_verdict": verdict, "near_limit": near_limit},
                metadata={"lot_id": lot_id},
            )
            return self._build_detail(conn, cert)

    def revise_certificate(self, principal: Principal, cert_id: int, data: dict[str, Any], expected_version: int | None) -> dict[str, Any]:
        self._require_permission(principal, "food.cert.create", "revise", None, cert_id)
        cert = self._get_cert(cert_id)
        self._check_version(cert, expected_version)
        if cert["status"] not in {"draft", "returned"}:
            raise ConflictError(f"当前状态 {cert['status']} 不允许修改内容，草稿或退回状态才能修订")
        now = self._now()
        with transaction(immediate=True) as conn:
            locked = self._get_cert_locked(conn, cert_id)
            self._check_version(locked, expected_version)
            if locked["status"] not in {"draft", "returned"}:
                raise ConflictError(f"当前状态 {locked['status']} 不允许修改内容")
            lot = conn.execute("SELECT * FROM food_lots WHERE id=?", (locked["lot_id"],)).fetchone()
            results = self._load_results(conn, locked["lot_id"], data["result_ids"])
            payload = self._build_payload(data, results)
            verdict = "fail" if any(item["verdict"] == "fail" for item in results) else "pass"
            near_limit = int(any(item["near_limit"] for item in results))
            snapshot = self._risk_snapshot(conn, lot)
            content_hash = self._content_hash(payload)
            new_version = locked["doc_version"] + 1
            try:
                cursor = conn.execute(
                    "UPDATE food_certificates SET certificate_no=?,lab_name=?,issued_at=?,overall_verdict=?,"
                    "near_limit=?,payload_json=?,status='draft',doc_version=?,submitted_by_user_id=NULL,"
                    "submitted_by_name='',submitted_at=NULL,reviewed_by_user_id=NULL,reviewed_by_name='',"
                    "reviewed_at=NULL,latest_review_opinion='',updated_at=? WHERE id=?",
                    (
                        data["certificate_no"], data["lab_name"], data["issued_at"], verdict, near_limit,
                        json.dumps(payload, ensure_ascii=False), new_version, now, cert_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("证书编号已存在") from exc
            if cursor.rowcount == 0:
                raise ConflictError("证书状态已变化，请刷新后重试")
            conn.execute(
                "INSERT INTO food_certificate_versions(certificate_id,doc_version,change_type,payload_json,content_hash,"
                "overall_verdict,near_limit,risk_snapshot_json,snapshot_lot_version,snapshot_lot_status,"
                "snapshot_lot_risk_level,created_by_user_id,created_by_name,created_at) "
                "VALUES(?,?,'revise',?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cert_id, new_version, json.dumps(payload, ensure_ascii=False), content_hash, verdict, near_limit,
                    json.dumps(snapshot, ensure_ascii=False), lot["version"], lot["status"], lot["risk_level"],
                    principal.user_id, principal.display_name, now,
                ),
            )
            cert = self._get_cert_locked(conn, cert_id)
            self._module_audit(conn, cert, "certificate.revise", principal.display_name, {"doc_version": new_version})
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="certificate.revise",
                resource_type="food_certificate",
                resource_id=cert_id,
                before={"status": locked["status"], "doc_version": locked["doc_version"]},
                after={"status": "draft", "doc_version": new_version, "overall_verdict": verdict, "near_limit": near_limit},
            )
            return self._build_detail(conn, cert)

    # ------------------------------------------------------------------ 流转

    def submit_certificate(self, principal: Principal, cert_id: int, opinion: str, expected_version: int | None) -> dict[str, Any]:
        self._require_permission(principal, "food.cert.create", "submit", None, cert_id)
        cert = self._get_cert(cert_id)
        self._check_version(cert, expected_version)
        if cert["status"] == "pending_review":
            # 重复送审：幂等返回，不再写意见或审计
            return self.detail(cert_id)
        if cert["status"] not in {"draft", "returned"}:
            raise ConflictError(f"当前状态 {cert['status']} 不允许送审")
        return self._transition(
            principal, cert,
            action="submit", target="pending_review",
            permission=None,
            opinion=opinion, expected_version=expected_version,
            set_columns={
                "submitted_by_user_id": principal.user_id,
                "submitted_by_name": principal.display_name,
                "submitted_at": self._now(),
            },
        )

    def return_certificate(self, principal: Principal, cert_id: int, opinion: str, expected_version: int | None) -> dict[str, Any]:
        if not opinion.strip():
            raise ValidationError("退回证书必须填写退回意见")
        self._require_permission(principal, "food.cert.review", "return", None, cert_id)
        cert = self._get_cert(cert_id)
        self._assert_reviewer_is_another_person(principal, cert, "return")
        self._check_version(cert, expected_version)
        if cert["status"] == "returned":
            return self.detail(cert_id)
        if cert["status"] != "pending_review":
            raise ConflictError(f"当前状态 {cert['status']} 不能退回，只有送审中的证书可以退回")
        return self._transition(
            principal, cert,
            action="return", target="returned",
            permission="food.cert.review",
            opinion=opinion, expected_version=expected_version,
            set_columns={"latest_review_opinion": opinion.strip()},
        )

    def approve_certificate(self, principal: Principal, cert_id: int, opinion: str, expected_version: int | None) -> dict[str, Any]:
        self._require_permission(principal, "food.cert.review", "approve", None, cert_id)
        cert = self._get_cert(cert_id)
        self._assert_reviewer_is_another_person(principal, cert, "approve")
        self._check_version(cert, expected_version)
        if cert["status"] == "approved":
            # 重复点击复核通过：幂等返回，不重复记录意见、审计
            return self.detail(cert_id)
        if cert["status"] != "pending_review":
            raise ConflictError(f"当前状态 {cert['status']} 不能复核通过，只有送审中的证书可以通过")
        return self._transition(
            principal, cert,
            action="approve", target="approved",
            permission="food.cert.review",
            opinion=opinion, expected_version=expected_version,
            set_columns={
                "reviewed_by_user_id": principal.user_id,
                "reviewed_by_name": principal.display_name,
                "reviewed_at": self._now(),
                "latest_review_opinion": opinion.strip(),
            },
        )

    def revoke_certificate(self, principal: Principal, cert_id: int, reason: str, expected_version: int | None) -> dict[str, Any]:
        if not reason.strip():
            raise ValidationError("撤销证书必须填写撤销原因")
        self._require_permission(principal, "food.cert.review", "revoke", None, cert_id)
        cert = self._get_cert(cert_id)
        self._assert_reviewer_is_another_person(principal, cert, "revoke")
        self._check_version(cert, expected_version)
        if cert["status"] == "revoked":
            # 重复点击撤销：幂等返回，不再次触发批次重新评估
            return self.detail(cert_id)
        if cert["status"] != "approved":
            raise ConflictError(f"当前状态 {cert['status']} 不能撤销，只有已复核通过的证书可以撤销")
        now = self._now()
        with transaction(immediate=True) as conn:
            locked = self._get_cert_locked(conn, cert_id)
            self._check_version(locked, expected_version)
            if locked["status"] == "revoked":
                return self._build_detail(conn, locked)
            if locked["status"] != "approved":
                raise ConflictError(f"当前状态 {locked['status']} 不能撤销")
            cursor = conn.execute(
                "UPDATE food_certificates SET status='revoked',revoked_by_user_id=?,revoked_by_name=?,"
                "revoked_at=?,revoke_reason=?,updated_at=? WHERE id=? AND status='approved' AND doc_version=?",
                (
                    principal.user_id, principal.display_name, now, reason.strip(), now,
                    cert_id, locked["doc_version"],
                ),
            )
            if cursor.rowcount == 0:  # 极端并发下被其他请求抢先处理
                raise ConflictError("证书状态已变化，请刷新后重试")
            reevaluation = self._trigger_lot_reevaluation(conn, locked, principal, reason.strip(), now)
            conn.execute(
                "UPDATE food_certificates SET reevaluation_status=? WHERE id=?",
                (reevaluation, cert_id),
            )
            conn.execute(
                "INSERT INTO food_certificate_opinions(certificate_id,doc_version,action,from_status,to_status,"
                "opinion,actor_user_id,actor_name,created_at) VALUES(?,?,'revoke','approved','revoked',?,?,?,?)",
                (cert_id, locked["doc_version"], reason.strip(), principal.user_id, principal.display_name, now),
            )
            self._module_audit(
                conn, locked, "certificate.revoke", principal.display_name,
                {"reason": reason.strip(), "reevaluation": reevaluation},
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action="certificate.revoke",
                resource_type="food_certificate",
                resource_id=cert_id,
                before={"status": "approved", "doc_version": locked["doc_version"]},
                after={"status": "revoked", "reevaluation": reevaluation},
                metadata={"lot_id": locked["lot_id"], "reason": reason.strip()},
            )
            return self._build_detail(conn, self._get_cert_locked(conn, cert_id))

    def _assert_reviewer_is_another_person(self, principal: Principal, cert: dict[str, Any], action: str) -> None:
        if cert["created_by_user_id"] == principal.user_id:
            self._record_denied(principal, action, cert, "复核人不能是证书录入人")
            raise PermissionDeniedError("复核人不能是证书录入人，必须由另一名授权人员复核")

    def _get_cert_locked(self, conn: sqlite3.Connection, cert_id: int) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM food_certificates WHERE id=?", (cert_id,)).fetchone()
        if row is None:
            raise NotFoundError("证书不存在")
        return dict(row)

    def _transition(
        self,
        principal: Principal,
        cert: dict[str, Any],
        *,
        action: str,
        target: str,
        permission: str | None,
        opinion: str,
        expected_version: int | None,
        set_columns: dict[str, Any],
    ) -> dict[str, Any]:
        now = self._now()
        with transaction(immediate=True) as conn:
            locked = self._get_cert_locked(conn, cert["id"])
            self._check_version(locked, expected_version)
            if locked["status"] == target:  # 并发下已被其他请求处理，幂等不重复副作用
                return self._build_detail(conn, locked)
            if locked["status"] != cert["status"]:
                raise ConflictError("证书状态已变化，请刷新后重试")
            if permission is not None:
                # 事务内再次鉴权（防御深度）；拒绝记录在事务外无法写，这里直接抛出回滚
                principal.require(permission)
            assignments = ", ".join(f"{column}=?" for column in set_columns)
            cursor = conn.execute(
                f"UPDATE food_certificates SET status=?,{assignments},updated_at=? "
                f"WHERE id=? AND status=? AND doc_version=?",
                [target, *set_columns.values(), now, locked["id"], locked["status"], locked["doc_version"]],
            )
            if cursor.rowcount == 0:
                raise ConflictError("证书状态已变化，请刷新后重试")
            conn.execute(
                "INSERT INTO food_certificate_opinions(certificate_id,doc_version,action,from_status,to_status,"
                "opinion,actor_user_id,actor_name,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    locked["id"], locked["doc_version"], action, locked["status"], target,
                    opinion.strip(), principal.user_id, principal.display_name, now,
                ),
            )
            self._module_audit(
                conn, locked, f"certificate.{action}", principal.display_name,
                {"from_status": locked["status"], "to_status": target, "opinion": opinion.strip()},
            )
            self.audit.record(
                AuditContext(principal.user_id, principal.display_name),
                action=f"certificate.{action}",
                resource_type="food_certificate",
                resource_id=locked["id"],
                before={"status": locked["status"], "doc_version": locked["doc_version"]},
                after={"status": target, "doc_version": locked["doc_version"]},
                metadata={"lot_id": locked["lot_id"], "opinion": opinion.strip()},
            )
            return self._build_detail(conn, self._get_cert_locked(conn, locked["id"]))

    def _trigger_lot_reevaluation(
        self,
        conn: sqlite3.Connection,
        cert: dict[str, Any],
        principal: Principal,
        reason: str,
        now: str,
    ) -> str:
        """证书撤销后批次不能静默放行：冻结批次并写风险动作，等待人工重新评估。"""
        lot = conn.execute("SELECT * FROM food_lots WHERE id=?", (cert["lot_id"],)).fetchone()
        if lot is None:
            return "lot_missing"
        failed_count = conn.execute(
            "SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id "
            "WHERE s.lot_id=? AND r.verdict='fail'",
            (lot["id"],),
        ).fetchone()[0]
        new_risk = "high" if failed_count or cert["overall_verdict"] == "fail" or cert["near_limit"] else "medium"
        previous_status = lot["status"]
        conn.execute(
            "UPDATE food_lots SET status='held',risk_level=?,version=version+1,updated_at=? WHERE id=?",
            (new_risk, now, lot["id"]),
        )
        conn.execute(
            "INSERT INTO food_risk_actions(lot_id,decision,reason,operator,previous_status,new_status,created_at) "
            "VALUES(?,?,?,?,?, 'held',?)",
            (
                lot["id"], "hold",
                f"证书 {cert['certificate_no']} 撤销，触发重新评估：{reason}",
                principal.display_name, previous_status, now,
            ),
        )
        return f"held:{new_risk}"

    # ------------------------------------------------------------------ 查询

    def detail(self, cert_id: int) -> dict[str, Any]:
        cert = self._get_cert(cert_id)
        return self._build_detail(self.connection, cert)

    def list_certificates(self, lot_id: int | None = None, status: str | None = None) -> list[dict[str, Any]]:
        if status is not None and status not in CERT_STATUSES:
            raise ValidationError(f"未知证书状态：{status}")
        conditions: list[str] = []
        params: list[Any] = []
        if lot_id is not None:
            conditions.append("c.lot_id=?")
            params.append(lot_id)
        if status is not None:
            conditions.append("c.status=?")
            params.append(status)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.connection.execute(
            f"SELECT c.* FROM food_certificates c{where} ORDER BY c.id DESC", tuple(params)
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def visible_certificates(self, lot_id: int) -> list[dict[str, Any]]:
        """配送企业视图：只暴露已复核通过（且未撤销）的证书。"""
        lot = self.connection.execute("SELECT id FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise NotFoundError("批次不存在")
        rows = self.connection.execute(
            "SELECT id,lot_id,certificate_no,lab_name,issued_at,overall_verdict,near_limit,"
            "doc_version,reviewed_by_name,reviewed_at,payload_json FROM food_certificates "
            "WHERE lot_id=? AND status='approved' ORDER BY reviewed_at,id",
            (lot_id,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result
