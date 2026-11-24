"""客户端退出：先优雅收尾（停驱动、收拾自动化页），再让壳杀进程。"""

from __future__ import annotations


def test_shutdown_endpoint_prepares_then_stops_driver(client, monkeypatch):
    from backend.providers.pugongying import session

    calls: list[str] = []
    monkeypatch.setattr(session, "prepare_for_exit", lambda: calls.append("prepare"))
    monkeypatch.setattr(session, "shutdown_playwright", lambda: calls.append("shutdown"))

    response = client.post("/api/shutdown")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert calls == ["prepare", "shutdown"], "必须先收拾自动化页，再停驱动"
