"""RabbitMQ protocol from organizers' example.ipynb; one connection per detector."""

import importlib
import json
import time
import uuid
from contextlib import suppress
from urllib.parse import quote, urlsplit

import numpy as np


class LinqTransport:
    def __init__(self, settings):
        self.settings = settings
        self.connection = None
        self.channel = None
        self.reply_queue = None
        self.device_ms = None
        self.pika = None
        self.wire = None

    def _connect(self):
        s = self.settings
        if not s.linq_api_url or not s.linq_program:
            raise ValueError("Задайте LINQ_API_URL и LINQ_PROGRAM в backend/.env")
        if not s.linq_program.startswith("/auto/cache/") or ".." in s.linq_program.split("/"):
            raise ValueError("LINQ_PROGRAM должен быть абсолютным путём /auto/cache/.../*.tpu")
        if not s.linq_program.endswith(".tpu"):
            raise ValueError("LINQ_PROGRAM должен указывать на .tpu")
        url = urlsplit(s.linq_api_url)
        if url.scheme != "amqp" or not url.hostname or url.username or url.password:
            raise ValueError("LINQ_API_URL: amqp://HOST:5672/ без логина и пароля")
        if url.query or url.fragment or url.path not in ("", "/"):
            raise ValueError("В этой конфигурации поддерживается только корневой vhost /")
        if not s.linq_login or not s.linq_password:
            raise ValueError("Задайте LINQ_LOGIN и LINQ_PASSWORD для RabbitMQ, не Jupyter")
        try:
            self.pika = importlib.import_module("pika")
            self.wire = importlib.import_module("tpu_pycloud_format")
        except ImportError as exc:
            raise RuntimeError(
                "Нужны pika и пакет организаторов tpu_pycloud_format. "
                "См. docs/LINQ_CONNECT.md; не устанавливайте tpu_framework для инференса."
            ) from exc
        p = self.pika
        self.connection = p.BlockingConnection(
            p.ConnectionParameters(
                host=url.hostname,
                port=url.port or 5672,
                virtual_host="/",
                credentials=p.PlainCredentials(s.linq_login, s.linq_password),
                connection_attempts=1,
                socket_timeout=10,
                stack_timeout=20,
                blocked_connection_timeout=20,
                heartbeat=60,
            )
        )
        self.channel = self.connection.channel()
        # Exclusive queue is removed when the connection closes, including a failed worker.
        self.reply_queue = "notebook_" + uuid.uuid4().hex
        self.channel.queue_declare(queue=self.reply_queue, exclusive=True, auto_delete=True)
        self.channel.queue_bind(
            exchange="tpu.direct",
            queue=self.reply_queue,
            routing_key=self.reply_queue,
        )
        self.channel.confirm_delivery()

    def run(self, tensor):
        try:
            if self.connection is None or not self.connection.is_open:
                self._connect()
            w = self.wire
            task_id = uuid.uuid4().hex
            self.device_ms = None
            self.channel.basic_publish(
                exchange="tpu.direct",
                routing_key="input",
                mandatory=True,
                body=w.serialize(w.PyTask({"images": tensor})),
                properties=self.pika.BasicProperties(
                    headers={
                        w.TARGET_URI_ATTR_NAME: "file://"
                        + quote(self.settings.linq_program, safe="/")
                        + "?raw=False",
                        w.RESPONSE_ROUTING_KEYS_ATTR_NAME: json.dumps([self.reply_queue]),
                        w.MESSAGE_ID_ATTR_NAME: task_id,
                    }
                ),
            )
            deadline = time.monotonic() + self.settings.linq_timeout_s
            while time.monotonic() < deadline:
                method, props, body = self.channel.basic_get(
                    queue=self.reply_queue,
                    auto_ack=True,
                )
                if method is None:
                    self.connection.process_data_events(time_limit=0.02)
                    continue
                if (props.headers or {}).get(w.MESSAGE_ID_ATTR_NAME) != task_id:
                    raise RuntimeError("LinQ: ID ответа не совпадает с запросом")
                data, _metadata, worker = w.deserialize(body)
                if str(worker.get("status_code")) != "0":
                    raise RuntimeError(f"LinQ worker error: {worker.get('error_message', worker)}")
                if not data or "output0" not in data:
                    raise RuntimeError("LinQ: в успешном ответе отсутствует output0")
                result = np.asarray(data["output0"])
                if result.shape != (1, 84, 8400) or result.dtype != np.float32:
                    raise ValueError(
                        f"LinQ: ожидается float32 [1,84,8400], получено "
                        f"{result.dtype} {result.shape}"
                    )
                if not np.isfinite(result).all():
                    raise ValueError("LinQ: выход содержит NaN/Inf")
                device_s = worker.get("inference_time")
                if isinstance(device_s, (int, float)) and np.isfinite(device_s) and device_s >= 0:
                    self.device_ms = device_s * 1000
                return result
            raise TimeoutError(f"LinQ: нет ответа за {self.settings.linq_timeout_s} секунд")
        except Exception:
            self.close()
            # No CPU fallback or silent empty detections.
            raise

    def close(self):
        connection = self.connection
        self.connection = self.channel = self.reply_queue = None
        if connection is not None and connection.is_open:
            # Cleanup must not mask the original transport error.
            with suppress(Exception):
                connection.close()
