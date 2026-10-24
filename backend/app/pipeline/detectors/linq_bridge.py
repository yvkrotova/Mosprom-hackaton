"""JPEG transport to the authenticated Jupyter proxy; TPU inference remains remote."""
import time
import cv2
import httpx
from app.pipeline.interfaces import Detector, Detection, FrameDetections

class LinqBridgeDetector(Detector):
    kind = 'linq_tpu'
    def __init__(self, settings):
        self.settings = settings
        if not settings.linq_bridge_url or not settings.linq_jupyter_token:
            raise ValueError('LINQ_BRIDGE_URL and LINQ_JUPYTER_TOKEN are required')
        if settings.model_input_size != 640:
            raise ValueError('Selected TPU program requires MODEL_INPUT_SIZE=640')
        self.model_version = 'linq:jpeg-bridge-v2'
        self.device_ms = None
        self.client = httpx.Client(
            timeout=httpx.Timeout(90, connect=15), follow_redirects=False, trust_env=False,
            headers={'Authorization':'token '+settings.linq_jupyter_token})
    def infer(self, frame):
        if frame.image is None:
            raise ValueError('Image is required')
        t0=time.perf_counter()
        ok,jpeg=cv2.imencode('.jpg',frame.image,[cv2.IMWRITE_JPEG_QUALITY,90])
        if not ok:raise RuntimeError('JPEG encoding failed')
        t1=time.perf_counter()
        response=self.client.post(self.settings.linq_bridge_url,content=jpeg.tobytes(),headers={
            'Content-Type':'image/jpeg',
            'X-Confidence':str(self.settings.detector_conf_threshold),
            'X-Iou':str(self.settings.iou_threshold),
            'X-Classes':self.settings.detect_classes})
        t2=time.perf_counter()
        if response.status_code!=200:
            raise RuntimeError(f'LinQ bridge HTTP {response.status_code}: {response.text[:300]}')
        data=response.json()
        if data.get('protocol_version')!=2:
            raise RuntimeError('Restart Jupyter with linq_bridge_server.py v2')
        if data.get('frame_w')!=frame.width or data.get('frame_h')!=frame.height:
            raise ValueError('LinQ response frame dimensions mismatch')
        if data.get('conf_threshold')!=self.settings.detector_conf_threshold or data.get('iou_threshold')!=self.settings.iou_threshold:
            raise ValueError('LinQ response thresholds mismatch')
        if not isinstance(data.get('detections'),list) or not data.get('program'):
            raise ValueError('Invalid LinQ response')
        detections=[Detection(**d) for d in data['detections']]
        self.model_version='linq:'+data['program']
        self.device_ms=data.get('device_ms')
        t3=time.perf_counter()
        return FrameDetections(source_id=frame.source_id,frame_idx=frame.frame_idx,
            video_ts=frame.video_ts,frame_w=frame.width,frame_h=frame.height,
            inference_ms=(t2-t1)*1000,preprocess_ms=(t1-t0)*1000,
            postprocess_ms=(t3-t2)*1000,model_version=self.model_version,
            detector_kind=self.kind,detections=detections)
    def close(self):
        self.client.close()
