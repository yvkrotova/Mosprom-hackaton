"""Единый формат ошибок для исключений приложения, FastAPI и необработанных ошибок."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.errors import ConflictError, NotFoundError, UnauthorizedError


class _Body(BaseModel):
    name: str
    count: int


@pytest.fixture
def client(app: FastAPI):
    @app.get("/_test/not-found")
    def not_found():
        raise NotFoundError("Событие не найдено", code="EVENT_NOT_FOUND", event_id="42")

    @app.get("/_test/conflict")
    def conflict():
        raise ConflictError("Уже запущен", code="SOURCE_ALREADY_RUNNING")

    @app.get("/_test/unauthorized")
    def unauthorized():
        raise UnauthorizedError("Неверный токен")

    @app.post("/_test/validate")
    def validate(body: _Body):
        return body

    @app.get("/_test/crash")
    def crash():
        raise RuntimeError("секретная внутренняя деталь")

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert body["error"]["message"]
    return body["error"]


def test_app_error_404_with_details(client):
    err = _error(client.get("/_test/not-found"), 404, "EVENT_NOT_FOUND")
    assert err["details"] == {"event_id": "42"}


def test_app_error_409(client):
    _error(client.get("/_test/conflict"), 409, "SOURCE_ALREADY_RUNNING")


def test_app_error_401(client):
    _error(client.get("/_test/unauthorized"), 401, "UNAUTHORIZED")


def test_unknown_route_404(client):
    _error(client.get("/api/v1/nope"), 404, "NOT_FOUND")


def test_method_not_allowed_405(client):
    _error(client.delete("/_test/conflict"), 405, "METHOD_NOT_ALLOWED")


def test_validation_422(client):
    err = _error(client.post("/_test/validate", json={"name": "x"}), 422, "VALIDATION_ERROR")
    assert err["details"]["errors"][0]["loc"] == ["body", "count"]


def test_invalid_json_422(client):
    resp = client.post(
        "/_test/validate", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    _error(resp, 422, "VALIDATION_ERROR")


def test_unhandled_500_hides_traceback(client, caplog):
    resp = client.get("/_test/crash")
    err = _error(resp, 500, "INTERNAL_ERROR")
    assert "секретная" not in resp.text
    assert "Traceback" not in resp.text
    assert err["details"] == {}
    assert "секретная внутренняя деталь" in caplog.text  # стектрейс остаётся в логе
