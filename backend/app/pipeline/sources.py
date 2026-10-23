"""Источники кадров: видеофайл/поток через OpenCV и «пустой» источник для replay без видео."""

import logging
import time
from collections.abc import Iterator

import cv2

from app.pipeline.interfaces import Frame, FrameSource

logger = logging.getLogger(__name__)


class FrameSourceError(RuntimeError):
    """Источник недоступен: файл не открылся или поток оборвался."""


class VideoFileSource(FrameSource):
    """Кадры видеофайла или RTSP-потока, прореженные до analysis_fps.

    video_ts = frame_idx / fps видео (детерминированно); если fps неизвестен — CAP_PROP_POS_MSEC.
    realtime=True выдаёт кадры не быстрее реального времени (для демо на записанном видео).
    """

    def __init__(
        self,
        source_id: str,
        uri: str,
        analysis_fps: float,
        *,
        is_stream: bool = False,
        realtime: bool = False,
    ) -> None:
        self.source_id = source_id
        self.uri = uri
        self.analysis_fps = analysis_fps
        self.is_stream = is_stream
        self.realtime = realtime
        self._cap: cv2.VideoCapture | None = None

    def _open(self) -> cv2.VideoCapture:
        cap = cv2.VideoCapture(self.uri)
        if not cap.isOpened():
            cap.release()
            raise FrameSourceError(f"Не удалось открыть видео: {self.uri}")
        return cap

    def __iter__(self) -> Iterator[Frame]:
        cap = self._cap = self._open()
        native_fps = cap.get(cv2.CAP_PROP_FPS)
        if not native_fps or native_fps <= 0 or native_fps > 1000:
            native_fps = 0.0
        step = 1.0 / self.analysis_fps
        next_ts = 0.0
        frame_idx = -1
        wall_start = time.monotonic()
        try:
            while True:
                if not cap.grab():
                    if self.is_stream:
                        raise FrameSourceError(f"Поток прервался: {self.uri}")
                    return  # конец файла
                frame_idx += 1
                if native_fps:
                    video_ts = frame_idx / native_fps
                else:
                    video_ts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                # Допуск 1 мс: чтобы при fps видео, кратном analysis_fps, не терять кадры
                # из-за ошибок округления.
                if video_ts + 1e-3 < next_ts:
                    continue
                ok, image = cap.retrieve()
                if not ok or image is None:
                    if self.is_stream:
                        raise FrameSourceError(f"Не удалось декодировать кадр: {self.uri}")
                    return
                next_ts += step
                while next_ts <= video_ts:
                    next_ts += step  # в видео дырка — не пытаемся «догнать» пропущенные кадры
                if self.realtime:
                    delay = video_ts - (time.monotonic() - wall_start)
                    if delay > 0:
                        time.sleep(delay)
                yield Frame(
                    source_id=self.source_id,
                    frame_idx=frame_idx,
                    video_ts=video_ts,
                    width=image.shape[1],
                    height=image.shape[0],
                    image=image,
                )
        finally:
            self.close()

    def close(self) -> None:
        if self._cap is not None:
            self._cap.release()
            self._cap = None


class NullFrameSource(FrameSource):
    """Кадры без картинки с заданным шагом — для replay-детекций без видеофайла и тестов."""

    def __init__(
        self,
        source_id: str,
        duration_s: float,
        analysis_fps: float,
        width: int = 1280,
        height: int = 720,
    ) -> None:
        self.source_id = source_id
        self.duration_s = duration_s
        self.analysis_fps = analysis_fps
        self.width = width
        self.height = height

    def __iter__(self) -> Iterator[Frame]:
        count = int(self.duration_s * self.analysis_fps) + 1
        for idx in range(count):
            yield Frame(
                source_id=self.source_id,
                frame_idx=idx,
                video_ts=round(idx / self.analysis_fps, 6),
                width=self.width,
                height=self.height,
                image=None,
            )
