"""Анализ сцены по зонам: модель фона, маски, неподвижные посторонние предметы.
Общий сервис конвейера.

Один SceneAnalyzer на видеопоток создаёт Pipeline рядом с трекером — только если есть правило с
requires_scene_analysis, и только для зон таких правил. Обновляется в process_frame ДО правил с
частотой SCENE_ANALYSIS_HZ по video_ts; между обновлениями отдаётся последний результат.
Правила (blocked_exit) только читают результат из FrameContext.scene.

По каждой зоне (в прямоугольнике вокруг полигона, уменьшенном до ~320 px по ширине, в сером,
с размытием и нормализацией яркости по медиане):
- эталон пустого прохода — сохранённый кадр зоны, иначе медиана кадров первых SCENE_BOOTSTRAP_S
  секунд (пиксели под людьми исключаются). Обновляется МЕДЛЕННО и только когда зона свободна
  (нет людей и почти нет отличий). До готовности — «оценить нельзя»;
- быстрый фон — экспоненциальное среднее: неподвижный предмет «оседает» в нём за 1–2 с;
- неподвижный посторонний предмет = отличается от эталона, но НЕ от быстрого фона. Движущееся
  (тележка, погрузчик) отличается от обоих — это не препятствие. Лежащий предмет не «впитывается»
  в эталон, поэтому тревога сама не закрывается;
- люди (рамки всех треков и детекций, с запасом и тенью под ногами) вырезаются из маски изменений;
- скачок средней яркости или изменение больше max_change_pct площади зоны — «оценить нельзя»
  (свет, сдвиг камеры), а не «заблокировано»; если это длится долго — флаг unstable для UI;
- «призрак»: предмет, стоявший в проходе при сборе эталона, уехал — на его месте пол отличается
  от эталона и неподвижен. Отличаем по ГРАНИЦЕ пятна: у нового предмета на границе есть резкий край,
  которого не было в эталоне; у «призрака» край был в эталоне, а теперь на его месте сплошной пол.
  «Призрак» не считается препятствием и сразу вписывается в эталон;
- сдвиг камеры / смена ракурса: весь кадр (уменьшенный) сравнивается со своим эталоном ВНЕ зон и
  людей — предмет в проходе снаружи ничего не меняет, а сдвиг или зум меняет почти всё. Изменилось
  больше CAMERA_SHIFT_PCT — все зоны «оценить нельзя».

Ограничение: если проход был заблокирован уже в начале видео и эталона нет, предмет войдёт в
автоматический эталон и не будет обнаружен — сохраните эталон пустого прохода
(кнопка в редакторе зон).
"""

import time
import warnings
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from app.pipeline.interfaces import Frame, FrameDetections, Track, ZoneGeometry

ROI_WIDTH = 320  # ширина, до которой уменьшается прямоугольник зоны
NORM_LEVEL = 128.0  # медиана яркости после нормализации
UNSTABLE_AFTER_S = 10.0  # «оценить нельзя» из-за изменения всей зоны дольше — scene_unstable
FREE_CHANGE_PCT = 0.005  # зона «подтверждённо свободна»: изменения меньше этой доли площади
SETTLED_FRAC = 0.8  # компонент — предмет, когда неподвижна эта доля его площади
GHOST_EDGE_RATIO = 0.8  # край на границе пятна слабее этой доли от эталона — «призрак»
GLOBAL_WIDTH = 160  # ширина уменьшенного кадра для проверки сдвига камеры
CAMERA_SHIFT_PCT = (
    0.1  # вне зон изменилось больше этой доли — камера сдвинулась (неподвижные камеры: ≤ 0.1%)
)


@dataclass(frozen=True, slots=True)
class SceneParams:
    """Параметры анализа зоны — из правила, которому он нужен (blocked_exit)."""

    diff_threshold: float = 28.0  # отличие в уровнях серого (после нормализации)
    person_mask_margin: float = 0.15  # запас вокруг рамки человека, доли её высоты
    max_change_pct: float = 0.6  # изменилось больше этой доли зоны — «оценить нельзя»
    brightness_jump: float = 0.25  # скачок медианы яркости больше этой доли — «оценить нельзя»
    fast_alpha: float = 0.3  # скорость быстрого фона на одно обновление
    reference_alpha: float = 0.02  # скорость эталона (только когда зона свободна)


@dataclass(slots=True)
class ZoneScene:
    """Результат анализа зоны. Контуры — доли кадра 0..1, площади — доли площади зоны."""

    ready: bool
    unknown_reason: str | None = None
    static_blobs: list[dict[str, Any]] = field(default_factory=list)  # {"polygon", "area_pct"}
    changed_pct: float = 0.0
    people_in_zone: int = 0
    unstable: bool = False
    reference: str | None = None  # saved — сохранённый эталон; auto — собран по первым секундам


def polygon_mask(points_px: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """Растеризация полигона (пиксели) в булеву маску shape=(h, w). Общая для всех зон."""
    mask = np.zeros(shape, np.uint8)
    cv2.fillPoly(mask, [np.round(points_px).astype(np.int32)], 1)
    return mask.astype(bool)


def edge_strength(gray: np.ndarray) -> np.ndarray:
    """Модуль градиента (Собель) — насколько «текстурный» участок."""
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    return cv2.magnitude(gx, gy)


def prepare_gray(image_bgr: np.ndarray, roi: tuple[int, int, int, int], size: tuple[int, int]):
    """Вырезка ROI → серый → уменьшение → размытие. Возвращает (кадр float32, медиана яркости)."""
    x0, y0, x1, y1 = roi
    gray = cv2.cvtColor(image_bgr[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, size, interpolation=cv2.INTER_AREA)
    gray = cv2.GaussianBlur(gray, (5, 5), 0).astype(np.float32)
    median = float(np.median(gray))
    return gray * (NORM_LEVEL / max(median, 1.0)), median


class _ZoneModel:
    def __init__(
        self, zone: ZoneGeometry, params: SceneParams, frame_w: int, frame_h: int,
        reference_bgr: np.ndarray | None, bootstrap_s: float,
    ) -> None:  # fmt: skip
        self.zone, self.params, self.bootstrap_s = zone, params, bootstrap_s
        xs = [p[0] * frame_w for p in zone.polygon]
        ys = [p[1] * frame_h for p in zone.polygon]
        x0, y0 = max(0, int(min(xs)) - 2), max(0, int(min(ys)) - 2)
        x1, y1 = min(frame_w, int(np.ceil(max(xs))) + 2), min(frame_h, int(np.ceil(max(ys))) + 2)
        self.roi = (x0, y0, max(x1, x0 + 4), max(y1, y0 + 4))
        self.scale = min(1.0, ROI_WIDTH / (self.roi[2] - self.roi[0]))
        self.size = (
            max(4, round((self.roi[2] - self.roi[0]) * self.scale)),
            max(4, round((self.roi[3] - self.roi[1]) * self.scale)),
        )
        local = np.array(
            [((x - x0) * self.scale, (y - y0) * self.scale) for x, y in zip(xs, ys, strict=True)]
        )
        self.poly = polygon_mask(local, (self.size[1], self.size[0]))
        self.poly_area = max(int(self.poly.sum()), 1)
        self.frame_w, self.frame_h = frame_w, frame_h
        self.reference: np.ndarray | None = None
        self.reference_kind: str | None = None
        if reference_bgr is not None and reference_bgr.shape[:2] == (frame_h, frame_w):
            self.reference, _ = prepare_gray(reference_bgr, self.roi, self.size)
            self.reference_kind = "saved"
        self.fast: np.ndarray | None = None
        self.boot: list[np.ndarray] = []
        self.boot_start: float | None = None
        self.prev_median: float | None = None
        self.unstable_since: float | None = None

    # --- маски людей -------------------------------------------------------------------------

    def person_mask(self, boxes: list[tuple[float, float, float, float]]) -> tuple[np.ndarray, int]:
        """Маска людей в координатах ROI и сколько людей задевают зону."""
        mask = np.zeros(self.poly.shape, bool)
        people = 0
        m = self.params.person_mask_margin
        for x1, y1, x2, y2 in boxes:
            h = (y2 - y1) * self.frame_h
            px1, px2 = x1 * self.frame_w - m * h, x2 * self.frame_w + m * h
            py1, py2 = (
                y1 * self.frame_h - m * h,
                y2 * self.frame_h + (m + 0.25) * h,
            )  # тень под ногами
            ax1 = int(max(0, (px1 - self.roi[0]) * self.scale))
            ax2 = int(min(self.size[0], np.ceil((px2 - self.roi[0]) * self.scale)))
            ay1 = int(max(0, (py1 - self.roi[1]) * self.scale))
            ay2 = int(min(self.size[1], np.ceil((py2 - self.roi[1]) * self.scale)))
            if ax2 <= ax1 or ay2 <= ay1:
                continue
            mask[ay1:ay2, ax1:ax2] = True
            if self.poly[ay1:ay2, ax1:ax2].any():
                people += 1
        return mask, people

    # --- обновление --------------------------------------------------------------------------

    def _unknown(
        self, ts: float, reason: str, people: int, changed: float = 0.0, whole=False
    ) -> ZoneScene:
        if whole:
            self.unstable_since = self.unstable_since if self.unstable_since is not None else ts
        unstable = self.unstable_since is not None and ts - self.unstable_since >= UNSTABLE_AFTER_S
        return ZoneScene(False, reason, [], changed, people, unstable, self.reference_kind)

    def update(self, image: np.ndarray, ts: float, boxes) -> ZoneScene:
        p = self.params
        gray, median = prepare_gray(image, self.roi, self.size)
        persons, people = self.person_mask(boxes)
        jump = self.prev_median is not None and abs(
            median - self.prev_median
        ) > p.brightness_jump * max(self.prev_median, 1.0)
        self.prev_median = median

        if self.reference is None:  # собираем эталон пустого прохода по первым секундам
            if jump:
                self.boot, self.boot_start = [], None
            masked = np.where(persons, np.nan, gray)
            self.boot.append(masked)
            self.boot = self.boot[-60:]
            self.boot_start = ts if self.boot_start is None else self.boot_start
            enough = np.sum(~np.isnan(np.stack(self.boot)), axis=0)
            if ts - self.boot_start >= self.bootstrap_s and (enough[self.poly] >= 2).all():
                ref = np.nanmedian(np.stack(self.boot), axis=0)
                self.reference = np.where(np.isnan(ref), gray, ref).astype(np.float32)
                self.reference_kind = "auto"
                self.fast = gray.copy()
                self.boot = []
            return self._unknown(ts, "сцена не готова: запоминаю пустой проход", people)

        if self.fast is None or jump:
            self.fast = gray.copy()
        if jump:
            return self._unknown(ts, "резкая смена освещения", people, whole=True)

        thr = p.diff_threshold
        valid = self.poly & ~persons
        valid_area = max(int(valid.sum()), 1)
        d_ref = np.abs(gray - self.reference) > thr
        d_fast = np.abs(gray - self.fast) > thr
        changed = d_ref & valid
        changed_pct = float(changed.sum()) / valid_area
        self.fast += p.fast_alpha * (gray - self.fast)
        if changed_pct > p.max_change_pct:
            return self._unknown(ts, "изменилась почти вся зона: свет или сдвиг камеры", people,
                                 changed_pct, whole=True)  # fmt: skip
        self.unstable_since = None

        # Компоненты — по всей маске отличий от эталона; предмет — компонент, который «осел»
        # (неподвижна большая часть его площади). Пока оседает — не предмет и не «призрак».
        kernel = np.ones((5, 5), np.uint8)
        changed_u8 = cv2.morphologyEx(
            changed.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)
        )
        changed_u8 = cv2.morphologyEx(changed_u8, cv2.MORPH_CLOSE, kernel)
        static = changed & ~d_fast
        contours, _ = cv2.findContours(changed_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        blobs = []
        edges_now = edges_ref = None
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < 4:
                continue
            comp = np.zeros(static.shape, np.uint8)
            cv2.drawContours(comp, [c], -1, 1, thickness=-1)
            inside = comp.astype(bool) & changed
            if static[inside].mean() < SETTLED_FRAC:
                continue  # движется или ещё оседает
            ring = (cv2.dilate(comp, kernel) & (1 - cv2.erode(comp, kernel))).astype(bool)
            if edges_now is None:
                edges_now, edges_ref = edge_strength(gray), edge_strength(self.reference)
            if ring.any() and edges_now[ring].mean() < GHOST_EDGE_RATIO * edges_ref[ring].mean():
                grown = cv2.dilate(comp, kernel).astype(bool)
                self.reference[grown] = gray[grown]  # «призрак» ушедшего предмета — в эталон
                continue
            pts = c.reshape(-1, 2).astype(np.float32)
            poly = [
                (round((self.roi[0] + x / self.scale) / self.frame_w, 4),
                 round((self.roi[1] + y / self.scale) / self.frame_h, 4))
                for x, y in pts
            ]  # fmt: skip
            blobs.append({"polygon": poly, "area_pct": round(area / self.poly_area, 4)})

        # Эталон обновляется медленно и только когда зона подтверждённо свободна.
        if people == 0 and changed_pct < FREE_CHANGE_PCT:
            self.reference += p.reference_alpha * (gray - self.reference)
        return ZoneScene(
            True, None, blobs, round(changed_pct, 4), people, False, self.reference_kind
        )


class _CameraModel:
    """Проверка сдвига камеры по всему кадру вне зон и людей (эталон собирается вместе с зонами)."""

    def __init__(
        self, zones: list[ZoneGeometry], frame_w: int, frame_h: int, threshold: float
    ) -> None:
        self.scale = GLOBAL_WIDTH / frame_w
        self.size = (GLOBAL_WIDTH, max(4, round(frame_h * self.scale)))
        self.frame_w, self.frame_h, self.threshold = frame_w, frame_h, threshold
        outside = np.ones((self.size[1], self.size[0]), bool)
        for z in zones:
            pts = np.array([(x * self.size[0], y * self.size[1]) for x, y in z.polygon])
            outside &= ~polygon_mask(pts, outside.shape)
        self.outside = outside
        self.boot: list[np.ndarray] = []
        self.reference: np.ndarray | None = None

    def _people(self, boxes) -> np.ndarray:
        mask = np.zeros(self.outside.shape, bool)
        sw, sh = self.size
        for x1, y1, x2, y2 in boxes:
            h = (y2 - y1) * sh
            ya, yb = max(0, int(y1 * sh - 0.2 * h)), int(np.ceil(y2 * sh + 0.4 * h))
            xa, xb = max(0, int(x1 * sw - 0.2 * h)), int(np.ceil(x2 * sw + 0.2 * h))
            mask[ya:yb, xa:xb] = True
        return mask

    def shifted(self, image: np.ndarray, boxes, bootstrap_done: bool) -> bool:
        gray, _ = prepare_gray(image, (0, 0, self.frame_w, self.frame_h), self.size)
        valid = self.outside & ~self._people(boxes)
        if self.reference is None:
            self.boot.append(np.where(valid, gray, np.nan))
            if bootstrap_done and len(self.boot) >= 2:
                with warnings.catch_warnings():  # пиксели, всё время закрытые людьми, — NaN
                    warnings.simplefilter("ignore", RuntimeWarning)
                    ref = np.nanmedian(np.stack(self.boot[-40:]), axis=0)
                self.reference = np.where(np.isnan(ref), gray, ref).astype(np.float32)
                self.boot = []
            return False
        changed = (np.abs(gray - self.reference) > self.threshold) & valid
        frac = float(changed.sum()) / max(int(valid.sum()), 1)
        if frac > CAMERA_SHIFT_PCT:
            return True
        self.reference += 0.05 * (gray - self.reference)  # медленно следует за освещением
        return False


class SceneAnalyzer:
    """Анализ сцены по зонам для одного потока. Вызывается Pipeline на каждом кадре."""

    def __init__(
        self,
        zones: dict[str, tuple[ZoneGeometry, SceneParams]],
        *,
        hz: float = 4.0,
        bootstrap_s: float = 3.0,
        references: dict[str, np.ndarray] | None = None,
    ) -> None:
        self.zones = zones
        self.period = 1.0 / hz if hz > 0 else 0.0
        self.bootstrap_s = bootstrap_s
        self.references = references or {}
        self._models: dict[str, _ZoneModel] = {}
        self._camera: _CameraModel | None = None
        self._last: dict[str, ZoneScene] = {}
        self._last_ts: float | None = None
        self.last_ms: float | None = None  # время последнего обновления (в замеры)

    def update(
        self, frame: Frame, tracks: list[Track], detections: FrameDetections
    ) -> dict[str, ZoneScene]:
        ts = frame.video_ts
        if frame.image is None:
            return {zid: ZoneScene(False, "нет изображения кадра") for zid in self.zones}
        if self._last_ts is not None and ts - self._last_ts < self.period - 1e-6:
            self.last_ms = None
            return self._last
        t0 = time.perf_counter()
        boxes = [t.bbox for t in tracks if t.label == "person"]
        boxes += [d.bbox for d in detections.detections if d.label == "person"]
        if self._camera is None:
            # Чувствительнее зон: малоконтрастная сцена после поворота камеры меняется слабо.
            threshold = min(p.diff_threshold for _, p in self.zones.values()) / 2
            self._camera = _CameraModel(
                [z for z, _ in self.zones.values()], frame.width, frame.height, threshold
            )
        ready = all(m.reference is not None for m in self._models.values()) and bool(self._models)
        shifted = self._camera.shifted(frame.image, boxes, ready)
        for zid, (zone, params) in self.zones.items():
            model = self._models.get(zid)
            if model is None:
                model = self._models[zid] = _ZoneModel(
                    zone,
                    params,
                    frame.width,
                    frame.height,
                    self.references.get(zid),
                    self.bootstrap_s,
                )
            if shifted:
                model.fast = None  # после сдвига быстрый фон собирается заново
                self._last[zid] = model._unknown(
                    ts, "сдвиг камеры или смена ракурса", 0, whole=True
                )
            else:
                self._last[zid] = model.update(frame.image, ts, boxes)
        self._last_ts = ts
        self.last_ms = (time.perf_counter() - t0) * 1000
        return self._last
