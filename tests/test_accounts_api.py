"""账户接口：**密码绝不能回传**。

这是本项目最不能出错的一条（项目设计约束 第四条）。所以断言写成"递归扫描整个响应体"，
而不是只看某个字段 —— 只要哪天有人顺手 `asdict(credential)` 整个透传，这里就会红。
"""

from __future__ import annotations


def _assert_no_password(payload) -> None:
    """递归断言：响应体里任何键、任何值都不出现密码。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            assert "password" not in str(key).lower(), f"响应里出现了 password 字段：{payload}"
            _assert_no_password(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_no_password(item)
    elif isinstance(payload, str):
        assert "s3cret" not in payload, f"响应里回显了密码：{payload}"


def test_empty_account_is_rejected(client):
    response = client.put("/api/account", json={"username": "  ", "password": "s3cret"})
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_CREDENTIALS"
    # 校验失败也**不许回显输入**（Pydantic 默认行为会把 body 原样带出来）
    _assert_no_password(response.json())


def test_save_then_read_never_exposes_password(client):
    saved = client.put("/api/account", json={"username": "daishu@example.com",
                                             "password": "s3cret"})
    assert saved.status_code == 200
    _assert_no_password(saved.json())
    assert saved.json() == {"has_credentials": True, "username_masked": "d***"}

    fetched = client.get("/api/account")
    assert fetched.status_code == 200
    _assert_no_password(fetched.json())
    assert fetched.json()["has_credentials"] is True


def test_password_is_kept_when_only_username_changes(client):
    """只改账号名时留空密码 —— 不该把原密码抹掉。"""
    client.put("/api/account", json={"username": "old@example.com", "password": "s3cret"})
    response = client.put("/api/account", json={"username": "new@example.com", "password": ""})
    assert response.status_code == 200

    from backend.services import credential_service
    loaded = credential_service.load()
    assert loaded is not None
    assert loaded[0] == "new@example.com"
    assert loaded[1] == "s3cret", "留空密码时原密码被清掉了"


def test_new_account_requires_password(client):
    response = client.put("/api/account", json={"username": "a@example.com", "password": ""})
    assert response.status_code == 400
    assert response.json()["code"] == "BAD_CREDENTIALS"


def test_delete_is_idempotent(client):
    client.put("/api/account", json={"username": "a@example.com", "password": "s3cret"})
    assert client.delete("/api/account").json() == {"ok": True, "deleted": True}
    # 再删一次不该报错：用户重复点删除是很正常的操作
    assert client.delete("/api/account").json() == {"ok": True, "deleted": False}


def test_login_without_credentials_reports_actionable_error(client):
    response = client.post("/api/account/login")
    assert response.status_code == 409
    assert response.json()["code"] == "NOT_READY"
    assert "账户" in response.json()["detail"]


def test_login_conflict_is_reported_not_queued(client):
    """检索占着浏览器时点「去登录」，必须当场报冲突而不是静默排队。"""
    from backend.services import selection_service

    # 先配上凭据，否则会先撞上「没有账号」那条分支，测不到守卫
    client.put("/api/account", json={"username": "a@example.com", "password": "s3cret"})

    # 守卫是进程级的锁，而 TestClient 就在本进程里跑应用，所以这里能真实撞上
    with selection_service.operation_guard("检索达人"):
        response = client.post("/api/account/login")
    assert response.status_code == 409
    assert response.json()["code"] == "OPERATION_CONFLICT"
    assert "检索达人" in response.json()["detail"]
