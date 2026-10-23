"""HTTP-клиент воркера к внутреннему API backend.

Повторная отправка безопасна (upsert событий и загрузка кадров идемпотентны), поэтому сетевые
ошибки и ответы 5xx повторяются с экспоненциальной паузой. Ответы 4xx не повторяются.
"""

import json
import logging
import time
from typing import Any

import httpx

from app.schemas.internal import SourceConfig

logger = logging.getLogger(__name__)


class BackendError(RuntimeError):
    pass


class BackendClient:
    """Реализует EpisodeSink: send_event / send_evidence."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        http: httpx.Client | None = None,
        retries: int = 4,
        backoff_s: float = 0.5,
        timeout_s: float = 10.0,
    ) -> None:
        self._http = http or httpx.Client(base_url=base_url, timeout=timeout_s)
        self._owns_http = http is None
        self._headers = {"X-Internal-Token": token}
        self.retries = retries
        self.backoff_s = backoff_s

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._http.request(method, path, headers=self._headers, **kwargs)
            except httpx.TransportError as exc:
                error = exc
            else:
                if resp.status_code < 500:
                    return resp
                error = BackendError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            if attempt < self.retries:
                delay = self.backoff_s * 2**attempt
                logger.warning("%s %s не удался (%s), повтор через %.1f с", method, path, error,
                               delay)  # fmt: skip
                time.sleep(delay)
        raise BackendError(f"Backend недоступен: {method} {path}: {error}")

    @staticmethod
    def _check(resp: httpx.Response) -> httpx.Response:
        if resp.is_error:
            try:
                err = resp.json()["error"]
                detail = f"{err['code']}: {err['message']}"
            except (ValueError, KeyError, TypeError):
                detail = resp.text[:300]
            raise BackendError(f"HTTP {resp.status_code} {resp.request.url.path}: {detail}")
        return resp

    def get_config(self, source_id: str) -> SourceConfig:
        resp = self._check(self._request("GET", f"/internal/sources/{source_id}/config"))
        return SourceConfig.model_validate(resp.json())

    def send_event(self, event_id: str, payload: dict[str, Any]) -> None:
        resp = self._request("PUT", f"/internal/events/{event_id}", json=payload)
        if resp.status_code == 409:
            # Эпизод уже завершён на backend (например, его закрыл backend после сбоя) —
            # конвейер продолжает работу, повторять бессмысленно.
            logger.warning("Событие %s отклонено: %s", event_id, resp.text[:300])
            return
        self._check(resp)

    def send_evidence(self, event_id: str, kind: str, image: bytes, meta: dict[str, Any]) -> None:
        resp = self._request(
            "POST",
            f"/internal/events/{event_id}/evidence",
            files={"file": (f"{kind}.jpg", image, "image/jpeg")},
            data={"metadata": json.dumps(meta, ensure_ascii=False)},
        )
        self._check(resp)

    def send_live(self, source_id: str, meta: dict[str, Any], image: bytes | None) -> None:
        """Живой кадр: одна попытка без повторов — следующий кадр всё равно свежее."""
        files = {"file": ("frame.jpg", image, "image/jpeg")} if image is not None else None
        resp = self._http.post(
            f"/internal/live/{source_id}",
            headers=self._headers,
            data={"metadata": json.dumps(meta)},
            files=files,
            timeout=3.0,
        )
        self._check(resp)

    def heartbeat(self, payload: dict[str, Any]) -> None:
        self._check(self._request("POST", "/internal/heartbeat", json=payload))

    def close(self) -> None:
        if self._owns_http:
            self._http.close()
