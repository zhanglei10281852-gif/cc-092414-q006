from __future__ import annotations


PASSWORD = "Role!12345"


def _login(client, username: str) -> dict:
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD, "client_label": "tests"})
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


def _setup_users(client, admin):
    """创建录入员（create）、复核员（review）和无权限用户。"""
    for code, name, perms in (
        ("cert_entry", "证书录入员", ["food.cert.read", "food.cert.create"]),
        ("cert_reviewer", "证书复核员", ["food.cert.read", "food.cert.review"]),
        ("cert_nobody", "无关人员", []),
    ):
        role = client.post("/api/roles", json={"code": code, "name": name, "permission_codes": perms}, headers=admin["headers"])
        assert role.status_code == 201, role.text
        user = client.post(
            "/api/users",
            json={"username": code, "password": PASSWORD, "display_name": name, "role_codes": [code]},
            headers=admin["headers"],
        )
        assert user.status_code == 201, user.text
    return _login(client, "cert_entry"), _login(client, "cert_reviewer"), _login(client, "cert_nobody")


def _lot_with_result(client, code: str, value: float = 0.046, limit: float = 0.05):
    lot = client.post("/api/food/lots", json={"lot_code": code, "product_name": "菠菜", "category": "叶菜", "supplier": "安心农场", "origin": "山东寿光", "harvest_date": "2026-09-20", "quantity_kg": 500, "trace_code": code + "-TRACE"}).json()
    sample = client.post(f"/api/food/lots/{lot['id']}/samples", json={"sample_code": code + "-S", "collected_at": "2026-09-21T08:00:00+00:00", "collector": "监管员", "location": "批发市场", "sample_weight_g": 250}).json()
    result = client.post(f"/api/food/samples/{sample['id']}/results", json={"analyte": "毒死蜱", "method": "GB/T 5009", "value_mg_kg": value, "limit_mg_kg": limit, "lab_operator": "实验员", "tested_at": "2026-09-21T18:00:00+00:00"}).json()
    return lot, result


def _cert_payload(result, no: str):
    return {"certificate_no": no, "lab_name": "齐鲁检测中心", "issued_at": "2026-09-22T09:00:00+00:00", "summary": "合格", "result_ids": [result["id"]]}


def _audit_count(client, admin, action: str | None = None, outcome: str | None = None) -> int:
    params = {"resource_type": "food_certificate", "size": 100}
    if action:
        params["action"] = action
    if outcome:
        params["outcome"] = outcome
    body = client.get("/api/audit", params=params, headers=admin["headers"]).json()
    return body["total"]


def test_full_certificate_review_flow(client, admin):
    entry, reviewer, _ = _setup_users(client, admin)
    lot, result = _lot_with_result(client, "LOT-C1")

    # 0.046/0.05 = 0.92，判定为接近限值
    payload = _cert_payload(result, "CERT-001")
    created = client.post(f"/api/food/lots/{lot['id']}/certificates", json=payload, headers=entry["headers"])
    assert created.status_code == 201, created.text
    cert = created.json()
    assert cert["status"] == "draft"
    assert cert["doc_version"] == 1
    assert cert["near_limit"] == 1 and cert["overall_verdict"] == "pass"
    assert cert["versions"][0]["risk_snapshot"]["lot"]["id"] == lot["id"]
    assert cert["versions"][0]["risk_snapshot"]["near_limit_count"] == 1
    assert cert["content_hash"] == cert["versions"][0]["content_hash"]

    # 草稿与送审中状态对配送企业不可见
    assert client.get(f"/api/food/lots/{lot['id']}/certificates/visible", headers=reviewer["headers"]).json() == []

    # 录入人送审
    submitted = client.post(f"/api/food/certificates/{cert['id']}/submit", json={"opinion": "请复核"}, headers=entry["headers"])
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "pending_review"

    # 重复送审幂等：不产生第二条意见
    duplicate = client.post(f"/api/food/certificates/{cert['id']}/submit", json={"opinion": "请复核"}, headers=entry["headers"])
    assert duplicate.status_code == 200 and duplicate.json()["status"] == "pending_review"
    opinions = [o for o in duplicate.json()["opinions"] if o["action"] == "submit"]
    assert len(opinions) == 1

    # 复核员退回，必须填写意见
    empty_return = client.post(f"/api/food/certificates/{cert['id']}/return", json={"opinion": ""}, headers=reviewer["headers"])
    assert empty_return.status_code == 422
    returned = client.post(f"/api/food/certificates/{cert['id']}/return", json={"opinion": "原始记录缺页码"}, headers=reviewer["headers"])
    assert returned.status_code == 200 and returned.json()["status"] == "returned"
    assert returned.json()["latest_review_opinion"] == "原始记录缺页码"

    # 录入人按退回意见修订，产生新版本与新的风险快照
    payload["summary"] = "已补全原始记录"
    revised = client.put(f"/api/food/certificates/{cert['id']}", json={**payload, "expected_version": 1}, headers=entry["headers"])
    assert revised.status_code == 200, revised.text
    assert revised.json()["status"] == "draft" and revised.json()["doc_version"] == 2
    assert [v["doc_version"] for v in revised.json()["versions"]] == [1, 2]
    assert revised.json()["versions"][1]["change_type"] == "revise"

    resubmit = client.post(f"/api/food/certificates/{cert['id']}/submit", json={"opinion": "已补正"}, headers=entry["headers"])
    assert resubmit.status_code == 200 and resubmit.json()["status"] == "pending_review"

    # 复核通过
    approved = client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "材料齐全，同意发布"}, headers=reviewer["headers"])
    assert approved.status_code == 200, approved.text
    body = approved.json()
    assert body["status"] == "approved"
    assert body["reviewed_by_name"] == "证书复核员"
    assert body["created_by_name"] == "证书录入员"

    # 已通过证书才对配送企业可见
    visible = client.get(f"/api/food/lots/{lot['id']}/certificates/visible", headers=reviewer["headers"]).json()
    assert [item["certificate_no"] for item in visible] == ["CERT-001"]
    assert visible[0]["reviewed_by_name"] == "证书复核员"

    # 重复点击复核通过幂等：只有一条 approve 意见和一条成功审计
    client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "再点一次"}, headers=reviewer["headers"])
    detail = client.get(f"/api/food/certificates/{cert['id']}", headers=admin["headers"]).json()
    assert len([o for o in detail["opinions"] if o["action"] == "approve"]) == 1
    assert _audit_count(client, admin, action="certificate.approve", outcome="success") == 1


def test_reviewer_must_be_different_person(client, admin):
    entry, reviewer, nobody = _setup_users(client, admin)
    lot, result = _lot_with_result(client, "LOT-C2")
    cert = client.post(f"/api/food/lots/{lot['id']}/certificates", json=_cert_payload(result, "CERT-002"), headers=entry["headers"]).json()
    client.post(f"/api/food/certificates/{cert['id']}/submit", json={"opinion": "送审"}, headers=entry["headers"])

    # 录入人自己复核 -> 403
    self_approve = client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "自批"}, headers=entry["headers"])
    assert self_approve.status_code == 403
    assert self_approve.json()["error"]["code"] == "permission_denied"
    # 拒绝事件可经审计接口观察
    denied = client.get("/api/audit", params={"resource_type": "food_certificate", "outcome": "denied", "size": 100}, headers=admin["headers"]).json()
    assert any(item["action"] == "certificate.approve" for item in denied["data"])

    # 无权限用户 -> 403，且证书仍是送审中
    forbidden = client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "越权"}, headers=nobody["headers"])
    assert forbidden.status_code == 403
    # 未认证 -> 401
    assert client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "x"}).status_code == 401

    # 另一名授权复核员可以通过
    ok = client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "合规"}, headers=reviewer["headers"])
    assert ok.status_code == 200 and ok.json()["status"] == "approved"


def test_stale_version_rejected(client, admin):
    entry, reviewer, _ = _setup_users(client, admin)
    lot, result = _lot_with_result(client, "LOT-C3")
    cert = client.post(f"/api/food/lots/{lot['id']}/certificates", json=_cert_payload(result, "CERT-003"), headers=entry["headers"]).json()

    stale = client.post(f"/api/food/certificates/{cert['id']}/submit", json={"opinion": "x", "expected_version": 99}, headers=entry["headers"])
    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "stale_version"
    assert error["context"]["current_version"] == 1
    # 过期版本没有产生任何流转
    detail = client.get(f"/api/food/certificates/{cert['id']}", headers=admin["headers"]).json()
    assert detail["status"] == "draft" and detail["opinions"] == []

    # 正确版本可以送审与通过
    client.post(f"/api/food/certificates/{cert['id']}/submit", json={"opinion": "x", "expected_version": 1}, headers=entry["headers"])
    client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "ok", "expected_version": 1}, headers=reviewer["headers"])

    # 已通过的证书不能再按草稿修订
    resp = client.put(f"/api/food/certificates/{cert['id']}", json={**_cert_payload(result, "CERT-003"), "expected_version": 1}, headers=entry["headers"])
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "conflict"


def test_revoke_triggers_lot_reevaluation_and_is_idempotent(client, admin):
    entry, reviewer, _ = _setup_users(client, admin)
    lot, result = _lot_with_result(client, "LOT-C4")
    cert = client.post(f"/api/food/lots/{lot['id']}/certificates", json=_cert_payload(result, "CERT-004"), headers=entry["headers"]).json()
    client.post(f"/api/food/certificates/{cert['id']}/submit", json={"opinion": "送审"}, headers=entry["headers"])
    client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "通过"}, headers=reviewer["headers"])

    lot_before = client.get(f"/api/food/lots/{lot['id']}").json()

    # 撤销必须填原因；录入员无权撤销
    assert client.post(f"/api/food/certificates/{cert['id']}/revoke", json={"reason": ""}, headers=reviewer["headers"]).status_code == 422
    assert client.post(f"/api/food/certificates/{cert['id']}/revoke", json={"reason": "实验室报告抽查异常"}, headers=entry["headers"]).status_code == 403

    revoked = client.post(f"/api/food/certificates/{cert['id']}/revoke", json={"reason": "实验室报告抽查异常"}, headers=reviewer["headers"])
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["status"] == "revoked"
    # 接近限值证书撤销 -> 批次冻结为高风险，等待重新评估
    assert body["reevaluation_status"] == "held:high"

    lot_after = client.get(f"/api/food/lots/{lot['id']}").json()
    assert lot_after["status"] == "held" and lot_after["risk_level"] == "high"
    assert lot_after["version"] == lot_before["version"] + 1

    # 撤销后配送企业立即看不到该证书
    assert client.get(f"/api/food/lots/{lot['id']}/certificates/visible", headers=reviewer["headers"]).json() == []

    # 重复撤销幂等：批次版本不再变化，不重复触发重新评估
    client.post(f"/api/food/certificates/{cert['id']}/revoke", json={"reason": "再点一次"}, headers=reviewer["headers"])
    lot_again = client.get(f"/api/food/lots/{lot['id']}").json()
    assert lot_again["version"] == lot_after["version"]
    detail = client.get(f"/api/food/certificates/{cert['id']}", headers=admin["headers"]).json()
    assert len([o for o in detail["opinions"] if o["action"] == "revoke"]) == 1
    assert _audit_count(client, admin, action="certificate.revoke", outcome="success") == 1

    # 已撤销证书不能再次通过
    again = client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "试图恢复"}, headers=reviewer["headers"])
    assert again.status_code == 409


def test_illegal_transitions_and_cross_lot_results(client, admin):
    entry, reviewer, _ = _setup_users(client, admin)
    lot, result = _lot_with_result(client, "LOT-C5")
    other_lot, other_result = _lot_with_result(client, "LOT-C6")
    cert = client.post(f"/api/food/lots/{lot['id']}/certificates", json=_cert_payload(result, "CERT-005"), headers=entry["headers"]).json()

    # 草稿状态不能直接退回或通过
    assert client.post(f"/api/food/certificates/{cert['id']}/approve", json={"opinion": "x"}, headers=reviewer["headers"]).status_code == 409
    assert client.post(f"/api/food/certificates/{cert['id']}/return", json={"opinion": "x"}, headers=reviewer["headers"]).status_code == 409
    # 未通过的证书不能撤销
    assert client.post(f"/api/food/certificates/{cert['id']}/revoke", json={"reason": "x"}, headers=reviewer["headers"]).status_code == 409
    # 证书不能引用其他批次的检测结果
    cross = client.post(f"/api/food/lots/{lot['id']}/certificates", json=_cert_payload(other_result, "CERT-006"), headers=entry["headers"])
    assert cross.status_code == 422
    # 证书编号重复
    dup = client.post(f"/api/food/lots/{other_lot['id']}/certificates", json=_cert_payload(other_result, "CERT-005"), headers=entry["headers"])
    assert dup.status_code == 409
    # 不存在的证书
    assert client.get("/api/food/certificates/9999", headers=admin["headers"]).status_code == 404
