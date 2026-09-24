from __future__ import annotations

import pytest

PASSWORD = "Passw0rd!234"


def make_user(client, admin, username: str, permissions: list[str]) -> dict:
    role = client.post(
        "/api/roles",
        headers=admin["headers"],
        json={"code": f"role.{username}", "name": f"岗位-{username}", "permission_codes": permissions},
    )
    assert role.status_code == 201, role.text
    user = client.post(
        "/api/users",
        headers=admin["headers"],
        json={"username": username, "password": PASSWORD, "display_name": f"用户{username}", "role_codes": [f"role.{username}"]},
    )
    assert user.status_code == 201, user.text
    login = client.post("/api/auth/login", json={"username": username, "password": PASSWORD, "client_label": "tests"})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['token']}"}


@pytest.fixture()
def recorder(client, admin) -> dict:
    return make_user(client, admin, "lab.recorder", ["food.certificates.read", "food.certificates.write"])


@pytest.fixture()
def reviewer(client, admin) -> dict:
    return make_user(client, admin, "lab.reviewer", ["food.certificates.read", "food.certificates.review"])


@pytest.fixture()
def delivery(client, admin) -> dict:
    return make_user(client, admin, "delivery.clerk", ["food.delivery.read"])


def lot(client, code="LOT-C01") -> dict:
    response = client.post(
        "/api/food/lots",
        json={"lot_code": code, "product_name": "菠菜", "category": "叶菜", "supplier": "安心农场", "origin": "山东寿光",
              "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": code + "-TRACE"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def certificate_payload(number="CERT-0001", value=0.02, limit=0.05) -> dict:
    return {
        "certificate_no": number,
        "lab_name": "华测第三方实验室",
        "items": [{"analyte": "毒死蜱", "method": "GB 23200.113", "value_mg_kg": value, "limit_mg_kg": limit, "unit": "mg/kg"}],
        "remark": "冷链采样",
    }


def create_certificate(client, headers, lot_id: int, number="CERT-0001", value=0.02, limit=0.05) -> dict:
    response = client.post(f"/api/food/lots/{lot_id}/certificates", headers=headers, json=certificate_payload(number, value, limit))
    assert response.status_code == 201, response.text
    return response.json()


def submit(client, headers, certificate: dict) -> dict:
    response = client.post(
        f"/api/food/certificates/{certificate['id']}/submit", headers=headers, json={"expected_version": certificate["version"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, headers, certificate: dict, opinion="复核无误") -> dict:
    response = client.post(
        f"/api/food/certificates/{certificate['id']}/approve",
        headers=headers,
        json={"expected_version": certificate["version"], "opinion": opinion},
    )
    assert response.status_code == 200, response.text
    return response.json()


def approved_certificate(client, recorder, reviewer, lot_id: int, number="CERT-0001", value=0.02, limit=0.05) -> dict:
    certificate = create_certificate(client, recorder, lot_id, number, value, limit)
    certificate = submit(client, recorder, certificate)
    return approve(client, reviewer, certificate)


def test_certificate_full_review_flow(client, recorder, reviewer, delivery):
    lot_id = lot(client)["id"]
    certificate = create_certificate(client, recorder, lot_id)
    assert certificate["status"] == "draft"
    assert certificate["conclusion"] == "pass"
    assert certificate["near_limit"] is False
    assert certificate["version"] == 1

    # 复核通过前配送企业不可见
    assert client.get(f"/api/food/delivery/lots/{lot_id}/certificates", headers=delivery).json()["items"] == []

    certificate = submit(client, recorder, certificate)
    assert certificate["status"] == "pending_review" and certificate["version"] == 2
    assert client.get(f"/api/food/delivery/lots/{lot_id}/certificates", headers=delivery).json()["items"] == []

    certificate = approve(client, reviewer, certificate)
    assert certificate["status"] == "approved" and certificate["version"] == 3
    assert certificate["reviewed_by"] == "lab.reviewer"
    assert certificate["review_opinion"] == "复核无误"

    visible = client.get(f"/api/food/delivery/lots/{lot_id}/certificates", headers=delivery).json()["items"]
    assert [item["certificate_no"] for item in visible] == ["CERT-0001"]
    assert "review_opinion" not in visible[0]

    versions = client.get(f"/api/food/certificates/{certificate['id']}/versions", headers=recorder).json()["items"]
    assert [item["action"] for item in versions] == ["create", "submit", "approve"]
    assert [item["version"] for item in versions] == [1, 2, 3]
    assert versions[-1]["opinion"] == "复核无误"
    lot_snapshot = versions[-1]["lot_snapshot"]
    assert lot_snapshot["lot_id"] == lot_id
    assert lot_snapshot["approved_certificate_count"] == 1
    assert lot_snapshot["status"] == "pending"


def test_return_edit_and_resubmit(client, recorder, reviewer):
    lot_id = lot(client, "LOT-C02")["id"]
    certificate = submit(client, recorder, create_certificate(client, recorder, lot_id))

    returned = client.post(
        f"/api/food/certificates/{certificate['id']}/return",
        headers=reviewer,
        json={"expected_version": certificate["version"], "opinion": "检测方法填写不规范"},
    )
    assert returned.status_code == 200, returned.text
    returned = returned.json()
    assert returned["status"] == "returned"
    assert returned["review_opinion"] == "检测方法填写不规范"

    edited = client.put(
        f"/api/food/certificates/{returned['id']}",
        headers=recorder,
        json={"expected_version": returned["version"], "items": certificate_payload()["items"], "remark": "已更正方法"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "returned"
    assert edited.json()["remark"] == "已更正方法"

    certificate = submit(client, recorder, edited.json())
    certificate = approve(client, reviewer, certificate)
    assert certificate["status"] == "approved"

    versions = client.get(f"/api/food/certificates/{certificate['id']}/versions", headers=recorder).json()["items"]
    assert [item["action"] for item in versions] == ["create", "submit", "return", "edit", "submit", "approve"]
    assert versions[2]["opinion"] == "检测方法填写不规范"


def test_recorder_cannot_review_own_certificate(client, admin):
    both = make_user(client, admin, "lab.both", ["food.certificates.read", "food.certificates.write", "food.certificates.review"])
    lot_id = lot(client, "LOT-C03")["id"]
    certificate = submit(client, both, create_certificate(client, both, lot_id))

    denied = client.post(
        f"/api/food/certificates/{certificate['id']}/approve",
        headers=both,
        json={"expected_version": certificate["version"], "opinion": "自审"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "self_review_forbidden"

    denied_return = client.post(
        f"/api/food/certificates/{certificate['id']}/return",
        headers=both,
        json={"expected_version": certificate["version"], "opinion": "自退"},
    )
    assert denied_return.status_code == 403
    assert denied_return.json()["error"]["code"] == "self_review_forbidden"

    audits = client.get("/api/audit?resource_type=food_certificate&outcome=denied", headers=admin["headers"]).json()
    assert audits["total"] == 2
    assert {row["action"] for row in audits["data"]} == {"certificate.approve", "certificate.return"}

    # 证书仍处于送审状态，可由另一名授权人员复核
    reviewer = make_user(client, admin, "lab.other", ["food.certificates.read", "food.certificates.review"])
    certificate = approve(client, reviewer, certificate)
    assert certificate["status"] == "approved"


def test_permission_denied_is_observable(client, admin, recorder, reviewer, delivery):
    lot_id = lot(client, "LOT-C04")["id"]
    outsider = make_user(client, admin, "outsider", [])

    assert client.post(f"/api/food/lots/{lot_id}/certificates", headers=outsider, json=certificate_payload()).status_code == 403
    certificate = create_certificate(client, recorder, lot_id)
    certificate = submit(client, recorder, certificate)

    # 录入岗没有复核权限
    denied = client.post(
        f"/api/food/certificates/{certificate['id']}/approve",
        headers=recorder,
        json={"expected_version": certificate["version"], "opinion": "越权"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "permission_denied"

    # 复核岗没有录入权限
    assert client.post(f"/api/food/lots/{lot_id}/certificates", headers=reviewer, json=certificate_payload("CERT-X1")).status_code == 403
    # 配送视图需要专门权限
    assert client.get(f"/api/food/delivery/lots/{lot_id}/certificates", headers=recorder).status_code == 403
    # 未认证与无权限读取
    assert client.get(f"/api/food/certificates/{certificate['id']}").status_code == 401
    assert client.get(f"/api/food/certificates/{certificate['id']}", headers=delivery).status_code == 403


def test_near_limit_requires_second_person_and_opinion(client, recorder, reviewer):
    lot_id = lot(client, "LOT-C05")["id"]
    # 0.048 / 0.05 = 96%，属于接近限值
    certificate = create_certificate(client, recorder, lot_id, "CERT-N1", value=0.048, limit=0.05)
    assert certificate["near_limit"] is True
    assert certificate["conclusion"] == "pass"
    certificate = submit(client, recorder, certificate)

    missing_opinion = client.post(
        f"/api/food/certificates/{certificate['id']}/approve",
        headers=reviewer,
        json={"expected_version": certificate["version"], "opinion": ""},
    )
    assert missing_opinion.status_code == 422
    assert missing_opinion.json()["error"]["code"] == "validation_error"

    certificate = approve(client, reviewer, certificate, opinion="接近限值，已核对原始记录")
    assert certificate["status"] == "approved"
    versions = client.get(f"/api/food/certificates/{certificate['id']}/versions", headers=recorder).json()["items"]
    assert versions[-1]["opinion"] == "接近限值，已核对原始记录"
    assert versions[-1]["snapshot"]["near_limit"] == 1


def test_over_limit_certificate_concludes_fail(client, recorder):
    lot_id = lot(client, "LOT-C06")["id"]
    certificate = create_certificate(client, recorder, lot_id, "CERT-F1", value=0.3, limit=0.05)
    assert certificate["conclusion"] == "fail"


def test_repeated_review_and_revoke_have_no_duplicate_side_effects(client, admin, recorder, reviewer):
    lot_id = lot(client, "LOT-C07")["id"]
    certificate = submit(client, recorder, create_certificate(client, recorder, lot_id))

    stale = client.post(
        f"/api/food/certificates/{certificate['id']}/approve",
        headers=reviewer,
        json={"expected_version": certificate["version"] - 1, "opinion": "旧版本"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_version"
    assert stale.json()["error"]["context"]["current_version"] == certificate["version"]

    certificate = approve(client, reviewer, certificate)

    # 重复点击复核：旧版本号与新版本号都不能再次生效
    for version in (certificate["version"] - 1, certificate["version"]):
        repeated = client.post(
            f"/api/food/certificates/{certificate['id']}/approve",
            headers=reviewer,
            json={"expected_version": version, "opinion": "重复点击"},
        )
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] in {"stale_version", "invalid_state"}

    revoked = client.post(
        f"/api/food/certificates/{certificate['id']}/revoke",
        headers=admin["headers"],
        json={"expected_version": certificate["version"], "reason": "实验室撤回报告"},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["status"] == "revoked"

    # 重复点击撤销：同样不产生二次副作用
    for version in (certificate["version"], revoked.json()["version"]):
        repeated = client.post(
            f"/api/food/certificates/{certificate['id']}/revoke",
            headers=admin["headers"],
            json={"expected_version": version, "reason": "重复点击"},
        )
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] in {"stale_version", "invalid_state"}

    versions = client.get(f"/api/food/certificates/{certificate['id']}/versions", headers=recorder).json()["items"]
    assert [item["action"] for item in versions] == ["create", "submit", "approve", "revoke"]
    reevaluations = client.get(f"/api/food/lots/{lot_id}/reevaluations", headers=recorder).json()["items"]
    assert len(reevaluations) == 1


def test_revoke_triggers_lot_reevaluation_instead_of_silent_delete(client, admin, recorder, reviewer, delivery):
    lot_id = lot(client, "LOT-C08")["id"]
    certificate = approved_certificate(client, recorder, reviewer, lot_id)
    released = client.post(f"/api/food/lots/{lot_id}/risk", json={"decision": "release", "reason": "证书齐全", "operator": "监管员"})
    assert released.json()["status"] == "released"

    revoked = client.post(
        f"/api/food/certificates/{certificate['id']}/revoke",
        headers=admin["headers"],
        json={"expected_version": certificate["version"], "reason": "实验室资质被暂停"},
    )
    assert revoked.status_code == 200, revoked.text

    # 证书仍在，状态为已撤销，不是静默删除
    current = client.get(f"/api/food/certificates/{certificate['id']}", headers=recorder).json()
    assert current["status"] == "revoked"
    assert current["revoke_reason"] == "实验室资质被暂停"

    # 批次被重新评估：回到检测中且风险未知
    detail = client.get(f"/api/food/lots/{lot_id}").json()
    assert detail["status"] == "testing"
    assert detail["risk_level"] == "unknown"

    reevaluations = client.get(f"/api/food/lots/{lot_id}/reevaluations", headers=recorder).json()["items"]
    assert len(reevaluations) == 1
    record = reevaluations[0]
    assert record["trigger"] == "certificate.revoke"
    assert record["certificate_id"] == certificate["id"]
    assert (record["previous_status"], record["new_status"]) == ("released", "testing")
    assert (record["previous_risk"], record["new_risk"]) == ("unknown", "unknown")

    # 撤销后配送企业不再可见
    assert client.get(f"/api/food/delivery/lots/{lot_id}/certificates", headers=delivery).json()["items"] == []

    audits = client.get("/api/audit?resource_type=food_lot&action=lot.reevaluate", headers=admin["headers"]).json()
    assert audits["total"] == 1
    assert audits["data"][0]["after_json"].find("testing") != -1


def test_revoke_with_remaining_approved_certificate_keeps_lot_released(client, admin, recorder, reviewer):
    lot_id = lot(client, "LOT-C09")["id"]
    first = approved_certificate(client, recorder, reviewer, lot_id, "CERT-A1")
    approved_certificate(client, recorder, reviewer, lot_id, "CERT-A2")
    client.post(f"/api/food/lots/{lot_id}/risk", json={"decision": "release", "reason": "证书齐全", "operator": "监管员"})

    revoked = client.post(
        f"/api/food/certificates/{first['id']}/revoke",
        headers=admin["headers"],
        json={"expected_version": first["version"], "reason": "单张证书作废"},
    )
    assert revoked.status_code == 200
    detail = client.get(f"/api/food/lots/{lot_id}").json()
    assert detail["status"] == "released"
    assert detail["risk_level"] == "low"


def test_audit_trail_is_observable(client, admin, recorder, reviewer):
    lot_id = lot(client, "LOT-C10")["id"]
    certificate = approved_certificate(client, recorder, reviewer, lot_id, "CERT-AUD")

    audits = client.get("/api/audit?resource_type=food_certificate", headers=admin["headers"]).json()
    actions = [row["action"] for row in audits["data"]]
    assert actions == ["certificate.approve", "certificate.submit", "certificate.create"]
    create_event = next(row for row in audits["data"] if row["action"] == "certificate.create")
    assert create_event["outcome"] == "success"
    assert create_event["actor_name"] == "用户lab.recorder"
    assert str(certificate["id"]) == create_event["resource_id"]


def test_certificate_number_must_be_unique(client, recorder):
    lot_id = lot(client, "LOT-C11")["id"]
    create_certificate(client, recorder, lot_id, "CERT-DUP")
    duplicate = client.post(f"/api/food/lots/{lot_id}/certificates", headers=recorder, json=certificate_payload("CERT-DUP"))
    assert duplicate.status_code == 409


def test_missing_lot_and_certificate_return_404(client, recorder):
    assert client.post("/api/food/lots/9999/certificates", headers=recorder, json=certificate_payload()).status_code == 404
    assert client.get("/api/food/certificates/9999", headers=recorder).status_code == 404
