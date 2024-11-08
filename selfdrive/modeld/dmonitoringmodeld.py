#!/usr/bin/env python3
import os
from openpilot.system.hardware import TICI
## TODO this is hack
if TICI:
  GPU_BACKEND = 'QCOM'
else:
  GPU_BACKEND = 'GPU'
os.environ[GPU_BACKEND] = '1'
import gc
import math
import time
import pickle
import ctypes
import numpy as np
from pathlib import Path
from setproctitle import setproctitle

from cereal import messaging
from cereal.messaging import PubMaster, SubMaster
from msgq.visionipc import VisionIpcClient, VisionStreamType, VisionBuf
from openpilot.common.swaglog import cloudlog
from openpilot.common.params import Params
from openpilot.common.realtime import set_realtime_priority
from openpilot.selfdrive.modeld.models.commonmodel_pyx import CLContext, cl_from_visionbuf
from openpilot.selfdrive.modeld.parse_model_outputs import sigmoid
from openpilot.selfdrive.modeld.runners.tinygrad_helpers import tinygrad_tensor_from_opencl_address
from tinygrad.tensor import Tensor
from tinygrad.dtype import dtypes

CALIB_LEN = 3
MODEL_WIDTH = 1440
MODEL_HEIGHT = 960
FEATURE_LEN = 512
OUTPUT_SIZE = 84 + FEATURE_LEN

PROCESS_NAME = "selfdrive.modeld.dmonitoringmodeld"
SEND_RAW_PRED = os.getenv('SEND_RAW_PRED')
MODEL_PKL_PATH = Path(__file__).parent / 'models/dmonitoring_model_tinygrad.pkl'

class DriverStateResult(ctypes.Structure):
  _fields_ = [
    ("face_orientation", ctypes.c_float*3),
    ("face_position", ctypes.c_float*3),
    ("face_orientation_std", ctypes.c_float*3),
    ("face_position_std", ctypes.c_float*3),
    ("face_prob", ctypes.c_float),
    ("_unused_a", ctypes.c_float*8),
    ("left_eye_prob", ctypes.c_float),
    ("_unused_b", ctypes.c_float*8),
    ("right_eye_prob", ctypes.c_float),
    ("left_blink_prob", ctypes.c_float),
    ("right_blink_prob", ctypes.c_float),
    ("sunglasses_prob", ctypes.c_float),
    ("occluded_prob", ctypes.c_float),
    ("ready_prob", ctypes.c_float*4),
    ("not_ready_prob", ctypes.c_float*2)]

class DMonitoringModelResult(ctypes.Structure):
  _fields_ = [
    ("driver_state_lhd", DriverStateResult),
    ("driver_state_rhd", DriverStateResult),
    ("poor_vision_prob", ctypes.c_float),
    ("wheel_on_right_prob", ctypes.c_float),
    ("features", ctypes.c_float*FEATURE_LEN)]

class ModelState:
  inputs: dict[str, np.ndarray]
  output: np.ndarray

  def __init__(self, cl_ctx):
    assert ctypes.sizeof(DMonitoringModelResult) == OUTPUT_SIZE * ctypes.sizeof(ctypes.c_float)
    self.numpy_inputs = {'calib': np.zeros((1, CALIB_LEN), dtype=np.float32)}
    self.img = None


    with open(MODEL_PKL_PATH, "rb") as f:
      self.model_run = pickle.load(f)

  def run(self, buf:VisionBuf, calib:np.ndarray) -> tuple[np.ndarray, float]:
    self.numpy_inputs['calib'][0,:] = calib

    t1 = time.perf_counter()
    if TICI:
      if self.img is None:
        input_img_cl = cl_from_visionbuf(buf)
        self.img =  qcom_tensor_from_opencl_address(, input_img_cl.mem_address, (1, buf.height * 3 // 2, buf.width), dtypes.uint8)
    else:
      self.img = Tensor(buf.data).reshape((1,buf.height * 3 // 2,buf.width))

    tensor_inputs = {k: Tensor(v) for k,v in self.numpy_inputs.items()}
    tensor_inputs['input_img'] = self.img
    output = self.model_run(**tensor_inputs)['outputs'].numpy().flatten()

    t2 = time.perf_counter()
    return output, t2 - t1


def fill_driver_state(msg, ds_result: DriverStateResult):
  msg.faceOrientation = list(ds_result.face_orientation)
  msg.faceOrientationStd = [math.exp(x) for x in ds_result.face_orientation_std]
  msg.facePosition = list(ds_result.face_position[:2])
  msg.facePositionStd = [math.exp(x) for x in ds_result.face_position_std[:2]]
  msg.faceProb = float(sigmoid(ds_result.face_prob))
  msg.leftEyeProb = float(sigmoid(ds_result.left_eye_prob))
  msg.rightEyeProb = float(sigmoid(ds_result.right_eye_prob))
  msg.leftBlinkProb = float(sigmoid(ds_result.left_blink_prob))
  msg.rightBlinkProb = float(sigmoid(ds_result.right_blink_prob))
  msg.sunglassesProb = float(sigmoid(ds_result.sunglasses_prob))
  msg.occludedProb = float(sigmoid(ds_result.occluded_prob))
  msg.readyProb = [float(sigmoid(x)) for x in ds_result.ready_prob]
  msg.notReadyProb = [float(sigmoid(x)) for x in ds_result.not_ready_prob]

def get_driverstate_packet(model_output: np.ndarray, frame_id: int, location_ts: int, execution_time: float, gpu_execution_time: float):
  model_result = ctypes.cast(model_output.ctypes.data, ctypes.POINTER(DMonitoringModelResult)).contents
  msg = messaging.new_message('driverStateV2', valid=True)
  ds = msg.driverStateV2
  ds.frameId = frame_id
  ds.modelExecutionTime = execution_time
  ds.gpuExecutionTime = gpu_execution_time
  ds.poorVisionProb = float(sigmoid(model_result.poor_vision_prob))
  ds.wheelOnRightProb = float(sigmoid(model_result.wheel_on_right_prob))
  ds.rawPredictions = model_output.tobytes() if SEND_RAW_PRED else b''
  fill_driver_state(ds.leftDriverData, model_result.driver_state_lhd)
  fill_driver_state(ds.rightDriverData, model_result.driver_state_rhd)
  return msg


def main():
  gc.disable()
  setproctitle(PROCESS_NAME)
  set_realtime_priority(1)

  cl_context = CLContext()
  model = ModelState(cl_context)
  cloudlog.warning("models loaded, dmonitoringmodeld starting")
  Params().put_bool("DmModelInitialized", True)

  cloudlog.warning("connecting to driver stream")
  vipc_client = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_DRIVER, True, cl_context)
  while not vipc_client.connect(False):
    time.sleep(0.1)
  assert vipc_client.is_connected()
  cloudlog.warning(f"connected with buffer size: {vipc_client.buffer_len}")

  sm = SubMaster(["liveCalibration"])
  pm = PubMaster(["driverStateV2"])

  calib = np.zeros(CALIB_LEN, dtype=np.float32)

  while True:
    buf = vipc_client.recv()
    if buf is None:
      continue

    sm.update(0)
    if sm.updated["liveCalibration"]:
      calib[:] = np.array(sm["liveCalibration"].rpyCalib)

    t1 = time.perf_counter()
    model_output, gpu_execution_time = model.run(buf, calib)
    t2 = time.perf_counter()

    pm.send("driverStateV2", get_driverstate_packet(model_output, vipc_client.frame_id, vipc_client.timestamp_sof, t2 - t1, gpu_execution_time))


if __name__ == "__main__":
  main()
