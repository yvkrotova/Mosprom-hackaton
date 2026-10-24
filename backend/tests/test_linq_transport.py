"""Transport contract and failure handling, without access to organizers' broker."""

from types import SimpleNamespace

import numpy as np
import pytest

from app.config import Settings
from app.pipeline.detectors.linq_transport import LinqTransport


def transport(monkeypatch, *, status=0, output=None, mismatch=False, empty=False):
    t = LinqTransport(Settings(_env_file=None, linq_program="/auto/cache/test/model.tpu"))
    state = {}
    conn = SimpleNamespace(is_open=True)

    def close():
        conn.is_open = False

    conn.close = close
    conn.process_data_events = lambda **kwargs: None
    t.connection = conn
    t.reply_queue = "reply"
    t.pika = SimpleNamespace(BasicProperties=lambda **kwargs: SimpleNamespace(**kwargs))
    arr = np.zeros((1, 84, 8400), np.float32) if output is None else output
    t.wire = SimpleNamespace(
        TARGET_URI_ATTR_NAME="target",
        RESPONSE_ROUTING_KEYS_ATTR_NAME="reply",
        MESSAGE_ID_ATTR_NAME="id",
        PyTask=lambda tensors: tensors,
        serialize=lambda task: task,
        deserialize=lambda body: (
            {"output0": arr},
            (),
            {"status_code": status, "error_message": "device failure", "inference_time": 0.012},
        ),
    )

    def publish(**kwargs):
        state.update(kwargs)

    def get(**kwargs):
        if empty:
            return None, None, None
        task_id = "wrong" if mismatch else state["properties"].headers["id"]
        return object(), SimpleNamespace(headers={"id": task_id}), b"reply"

    t.channel = SimpleNamespace(basic_publish=publish, basic_get=get)
    return t, state, conn


def test_float_protocol_and_timing(monkeypatch):
    t, sent, conn = transport(monkeypatch)
    tensor = np.zeros((1, 3, 640, 640), np.float32)
    assert t.run(tensor).shape == (1, 84, 8400)
    assert sent["body"]["images"] is tensor
    assert sent["properties"].headers["target"].endswith(".tpu?raw=False")
    assert sent["mandatory"] is True
    assert t.device_ms == 12.0
    assert conn.is_open
    t.close()
    t.close()
    assert not conn.is_open


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({"status": 1}, RuntimeError),
        ({"mismatch": True}, RuntimeError),
        ({"output": np.zeros((1, 84, 8400), np.int8)}, ValueError),
        ({"output": np.zeros((1, 84, 20), np.float32)}, ValueError),
        ({"output": np.full((1, 84, 8400), np.nan, np.float32)}, ValueError),
    ],
)
def test_invalid_reply_fails_and_closes(monkeypatch, kwargs, error):
    t, _, conn = transport(monkeypatch, **kwargs)
    with pytest.raises(error):
        t.run(np.zeros((1, 3, 640, 640), np.float32))
    assert not conn.is_open
    assert t.connection is None


def test_timeout_closes(monkeypatch):
    t, _, conn = transport(monkeypatch, empty=True)
    ticks = iter([0.0, 1.0, 100.0])
    monkeypatch.setattr("app.pipeline.detectors.linq_transport.time.monotonic", lambda: next(ticks))
    with pytest.raises(TimeoutError):
        t.run(np.zeros((1, 3, 640, 640), np.float32))
    assert not conn.is_open


def test_no_configuration_is_not_empty_detections():
    t = LinqTransport(Settings(_env_file=None))
    with pytest.raises(ValueError, match="LINQ_API_URL"):
        t.run(np.zeros((1, 3, 640, 640), np.float32))
