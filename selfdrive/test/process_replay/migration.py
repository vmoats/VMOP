from collections import defaultdict
import functools

from cereal import messaging, car
from opendbc.car.fingerprints import MIGRATION
from opendbc.car.toyota.values import EPS_SCALE
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.modeld.fill_model_msg import fill_xyz_poly, fill_lane_line_meta
from openpilot.selfdrive.test.process_replay.vision_meta import meta_from_encode_index
from openpilot.system.manager.process_config import managed_processes
from panda import Panda


def migrate_all(lr, manager_states=False, panda_states=False, camera_states=False):
  migrations = [
    migrate_sensorEvents,
    migrate_carParams,
    migrate_gpsLocation,
    migrate_deviceState,
    migrate_carOutput,
    migrate_controlsState,
    migrate_liveLocationKalman,
    migrate_liveTracks,
    migrate_driverAssistance,
    migrate_drivingModelData,
  ]
  if manager_states:
    migrations.append(migrate_managerState)
  if panda_states:
    migrations.extend([migrate_pandaStates, migrate_peripheralState])
  if camera_states:
    migrations.append(migrate_cameraStates)

  lr = list(lr)
  grouped = defaultdict(list)
  for i, msg in enumerate(lr):
    grouped[msg.which()].append(i)

  all_ops = []
  for migration in migrations:
    if migration.product in grouped: # skip if product already exists
      continue

    sorted_indices = sorted(ii for i in migration.inputs for ii in grouped[i])
    msg_gen = ((i, lr[i]) for i in sorted_indices)
    ops = migration(msg_gen)
    all_ops.extend(ops)

  for op in all_ops:
    if op[0] == "delete":
      del lr[op[1]]
    elif op[0] == "replace":
      lr[op[1]] = op[2]
    elif op[0] == "add":
      lr.append(op[2])

  lr = sorted(lr, key=lambda x: x.logMonoTime)
  return lr



def migration(inputs: list[str], product: str|None=None):
  def decorator(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
      return func(*args, **kwargs)
    wrapper.inputs = inputs
    wrapper.product = product
    return wrapper
  return decorator


## Migration operations
def _m_delete(index):
  return ("delete", index, None)
def _m_replace(index, msg):
  return ("replace", index, msg)
def _m_add(msg):
  return ("add", None, msg)


@migration(inputs=["longitudinalPlan"], product="driverAssistance")
def migrate_driverAssistance(msgs):
  ops = []
  for _, msg in msgs:
    new_msg = messaging.new_message('driverAssistance', valid=True, logMonoTime=msg.logMonoTime)
    ops.append(_m_add(new_msg.as_reader()))
  return ops


@migration(inputs=["modelV2"], product="drivingModelData")
def migrate_drivingModelData(msgs):
  ops = []
  for _, msg in msgs:
    dmd = messaging.new_message('drivingModelData', valid=msg.valid, logMonoTime=msg.logMonoTime)
    for field in ["frameId", "frameIdExtra", "frameDropPerc", "modelExecutionTime", "action"]:
      setattr(dmd.drivingModelData, field, getattr(msg.modelV2, field))
    for meta_field in ["laneChangeState", "laneChangeState"]:
      setattr(dmd.drivingModelData.meta, meta_field, getattr(msg.modelV2.meta, meta_field))
    if len(msg.modelV2.laneLines) and len(msg.modelV2.laneLineProbs):
      fill_lane_line_meta(dmd.drivingModelData.laneLineMeta, msg.modelV2.laneLines, msg.modelV2.laneLineProbs)
    if all(len(a) for a in [msg.modelV2.position.x, msg.modelV2.position.y, msg.modelV2.position.z]):
      fill_xyz_poly(dmd.drivingModelData.path, ModelConstants.POLY_PATH_DEGREE, msg.modelV2.position.x, msg.modelV2.position.y, msg.modelV2.position.z)
    ops.append(_m_add(dmd.as_reader()))
  return ops


@migration(inputs=["liveTracksDEPRECATED"], product="liveTracks")
def migrate_liveTracks(msgs):
  ops = []
  for index, msg in msgs:
    new_msg = messaging.new_message('liveTracks')
    new_msg.valid = msg.valid
    new_msg.logMonoTime = msg.logMonoTime

    pts = []
    for track in msg.liveTracksDEPRECATED:
      pt = car.RadarData.RadarPoint()
      pt.trackId = track.trackId

      pt.dRel = track.dRel
      pt.yRel = track.yRel
      pt.vRel = track.vRel
      pt.aRel = track.aRel
      pt.measured = True
      pts.append(pt)

    new_msg.liveTracks.points = pts
    ops.append(_m_replace(index, new_msg.as_reader()))
  return ops


@migration(inputs=["liveLocationKalmanDEPRECATED"], product="livePose")
def migrate_liveLocationKalman(msgs):
  ops = []
  for index, msg in msgs:
    m = messaging.new_message('livePose')
    m.valid = msg.valid
    m.logMonoTime = msg.logMonoTime
    for field in ["orientationNED", "velocityDevice", "accelerationDevice", "angularVelocityDevice"]:
      lp_field, llk_field = getattr(m.livePose, field), getattr(msg.liveLocationKalmanDEPRECATED, field)
      lp_field.x, lp_field.y, lp_field.z = llk_field.value
      lp_field.xStd, lp_field.yStd, lp_field.zStd = llk_field.std
      lp_field.valid = llk_field.valid
    for flag in ["inputsOK", "posenetOK", "sensorsOK"]:
      setattr(m.livePose, flag, getattr(msg.liveLocationKalmanDEPRECATED, flag))
    ops.append(_m_replace(index, m.as_reader()))
  return ops


@migration(inputs=["controlsState"], product="selfdriveState")
def migrate_controlsState(msgs):
  ops = []
  for index, msg in msgs:
    m = messaging.new_message('selfdriveState')
    m.valid = msg.valid
    m.logMonoTime = msg.logMonoTime
    ss = m.selfdriveState
    for field in ("enabled", "active", "state", "engageable", "alertText1", "alertText2",
                  "alertStatus", "alertSize", "alertType", "experimentalMode",
                  "personality"):
      setattr(ss, field, getattr(msg.controlsState, field+"DEPRECATED"))
    ops.append(_m_add(m.as_reader()))
  return ops


@migration(inputs=["carState", "controlsState"])
def migrate_carState(msgs):
  ops = []
  last_cs = None
  for index, msg in msgs:
    if msg.which() == 'controlsState':
      last_cs = msg
    elif msg.which() == 'carState' and last_cs is not None:
      if last_cs.controlsState.vCruiseDEPRECATED - msg.carState.vCruise > 0.1:
        msg = msg.as_builder()
        msg.carState.vCruise = last_cs.controlsState.vCruiseDEPRECATED
        msg.carState.vCruiseCluster = last_cs.controlsState.vCruiseClusterDEPRECATED
        msg = msg.as_reader()
        ops.append(_m_replace(index, msg))
  return ops


@migration(inputs=["managerState"])
def migrate_managerState(msgs):
  ops = []
  for index, msg in msgs:
    new_msg = msg.as_builder()
    new_msg.managerState.processes = [{'name': name, 'running': True} for name in managed_processes]
    ops.append(_m_replace(index, new_msg.as_reader()))
  return ops


@migration(inputs=["gpsLocation", "gpsLocationExternal"])
def migrate_gpsLocation(msgs):
  ops = []
  for index, msg in msgs:
    new_msg = msg.as_builder()
    g = getattr(new_msg, new_msg.which())
    # hasFix is a newer field
    if not g.hasFix and g.flags == 1:
      g.hasFix = True
    ops.append(_m_replace(index, new_msg.as_reader()))
  return ops


@migration(inputs=["deviceState", "initData"])
def migrate_deviceState(msgs):
  ops = []
  dt = None
  for i, msg in msgs:
    if msg.which() == 'initData':
      dt = msg.initData.deviceType
    if msg.which() == 'deviceState':
      n = msg.as_builder()
      n.deviceState.deviceType = dt
      ops.append(_m_replace(i, n.as_reader()))
  return ops


@migration(inputs=["carControl"], product="carOutput")
def migrate_carOutput(msgs):
  ops = []
  for _, msg in msgs:
    co = messaging.new_message('carOutput')
    co.valid = msg.valid
    co.logMonoTime = msg.logMonoTime
    co.carOutput.actuatorsOutput = msg.carControl.actuatorsOutputDEPRECATED
    ops.append(_m_add(co.as_reader()))
  return ops


# TODO
@migration(inputs=["pandaStates", "pandaStateDEPRECATED", "carParams"])
def migrate_pandaStates(msgs):
  # TODO: safety param migration should be handled automatically
  safety_param_migration = {
    "TOYOTA_PRIUS": EPS_SCALE["TOYOTA_PRIUS"] | Panda.FLAG_TOYOTA_STOCK_LONGITUDINAL,
    "TOYOTA_RAV4": EPS_SCALE["TOYOTA_RAV4"] | Panda.FLAG_TOYOTA_ALT_BRAKE,
    "KIA_EV6": Panda.FLAG_HYUNDAI_EV_GAS | Panda.FLAG_HYUNDAI_CANFD_HDA2,
  }

  # Migrate safety param base on carState
  CP = next((m.carParams for m in msgs if m.which() == 'carParams'), None)
  assert CP is not None, "carParams message not found"
  if CP.carFingerprint in safety_param_migration:
    safety_param = safety_param_migration[CP.carFingerprint]
  elif len(CP.safetyConfigs):
    safety_param = CP.safetyConfigs[0].safetyParam
    if CP.safetyConfigs[0].safetyParamDEPRECATED != 0:
      safety_param = CP.safetyConfigs[0].safetyParamDEPRECATED
  else:
    safety_param = CP.safetyParamDEPRECATED

  ops = []
  for index, msg in msgs:
    if msg.which() == 'pandaStateDEPRECATED':
      new_msg = messaging.new_message('pandaStates', 1)
      new_msg.valid = msg.valid
      new_msg.logMonoTime = msg.logMonoTime
      new_msg.pandaStates[0] = msg.pandaStateDEPRECATED
      new_msg.pandaStates[0].safetyParam = safety_param
      ops.append(_m_replace(index, new_msg.as_reader()))
    elif msg.which() == 'pandaStates':
      new_msg = msg.as_builder()
      new_msg.pandaStates[-1].safetyParam = safety_param
      ops.append(_m_replace(index, new_msg.as_reader()))
  return ops


@migration(inputs=["pandaStates", "pandaStateDEPRECATED"], product="peripheralState")
def migrate_peripheralState(msgs):
  ops = []

  which = "pandaStates" if any(msg.which() == "pandaStates" for msg in msgs) else "pandaStateDEPRECATED"
  for index, msg in msgs:
    if msg.which() != which:
      continue
    new_msg = messaging.new_message("peripheralState")
    new_msg.valid = msg.valid
    new_msg.logMonoTime = msg.logMonoTime
    ops.append(_m_add(new_msg.as_reader()))
  return ops


@migration(inputs=["roadEncodeIdx", "wideRoadEncodeIdx", "driverEncodeIdx", "roadCameraState", "wideRoadCameraState", "driverCameraState"])
def migrate_cameraStates(msgs):
  ops = []
  frame_to_encode_id = defaultdict(dict)
  # just for encodeId fallback mechanism
  min_frame_id = defaultdict(lambda: float('inf'))

  for _, msg in msgs:
    if msg.which() not in ["roadEncodeIdx", "wideRoadEncodeIdx", "driverEncodeIdx"]:
      continue

    encode_index = getattr(msg, msg.which())
    meta = meta_from_encode_index(msg.which())

    assert encode_index.segmentId < 1200, f"Encoder index segmentId greater that 1200: {msg.which()} {encode_index.segmentId}"
    frame_to_encode_id[meta.camera_state][encode_index.frameId] = encode_index.segmentId

  for index, msg in msgs:
    if msg.which() not in ["roadCameraState", "wideRoadCameraState", "driverCameraState"]:
      continue

    camera_state = getattr(msg, msg.which())
    min_frame_id[msg.which()] = min(min_frame_id[msg.which()], camera_state.frameId)

    encode_id = frame_to_encode_id[msg.which()].get(camera_state.frameId)
    if encode_id is None:
      print(f"Missing encoded frame for camera feed {msg.which()} with frameId: {camera_state.frameId}")
      if len(frame_to_encode_id[msg.which()]) != 0:
        continue

      # fallback mechanism for logs without encodeIdx (e.g. logs from before 2022 with dcamera recording disabled)
      # try to fake encode_id by subtracting lowest frameId
      encode_id = camera_state.frameId - min_frame_id[msg.which()]
      print(f"Faking encodeId to {encode_id} for camera feed {msg.which()} with frameId: {camera_state.frameId}")

    new_msg = messaging.new_message(msg.which())
    new_camera_state = getattr(new_msg, new_msg.which())
    new_camera_state.frameId = encode_id
    new_camera_state.encodeId = encode_id
    # timestampSof was added later so it might be missing on some old segments
    if camera_state.timestampSof == 0 and camera_state.timestampEof > 25000000:
      new_camera_state.timestampSof = camera_state.timestampEof - 18000000
    else:
      new_camera_state.timestampSof = camera_state.timestampSof
    new_camera_state.timestampEof = camera_state.timestampEof
    new_msg.logMonoTime = msg.logMonoTime
    new_msg.valid = msg.valid

    ops.append(_m_add(new_msg.as_reader()))
    ops.append(_m_delete(index))
  return ops


@migration(inputs=["carParams"])
def migrate_carParams(msgs):
  ops = []
  for index, msg in msgs:
    CP = msg.as_builder()
    CP.carParams.carFingerprint = MIGRATION.get(CP.carParams.carFingerprint, CP.carParams.carFingerprint)
    for car_fw in CP.carParams.carFw:
      car_fw.brand = CP.carParams.carName
    CP.logMonoTime = msg.logMonoTime
    ops.append(_m_replace(index, CP.as_reader()))
  return ops

@migration(inputs=["sensorEventsDEPRECATED"], product="sensorEvents")
def migrate_sensorEvents(msgs):
  ops = []
  for index, msg in msgs:
    # migrate to split sensor events
    for evt in msg.sensorEventsDEPRECATED:
      # build new message for each sensor type
      sensor_service = ''
      if evt.which() == 'acceleration':
        sensor_service = 'accelerometer'
      elif evt.which() == 'gyro' or evt.which() == 'gyroUncalibrated':
        sensor_service = 'gyroscope'
      elif evt.which() == 'light' or evt.which() == 'proximity':
        sensor_service = 'lightSensor'
      elif evt.which() == 'magnetic' or evt.which() == 'magneticUncalibrated':
        sensor_service = 'magnetometer'
      elif evt.which() == 'temperature':
        sensor_service = 'temperatureSensor'

      m = messaging.new_message(sensor_service)
      m.valid = True
      m.logMonoTime = msg.logMonoTime

      m_dat = getattr(m, sensor_service)
      m_dat.version = evt.version
      m_dat.sensor = evt.sensor
      m_dat.type = evt.type
      m_dat.source = evt.source
      m_dat.timestamp = evt.timestamp
      setattr(m_dat, evt.which(), getattr(evt, evt.which()))

      ops.append(_m_add(m.as_reader()))
    ops.append(_m_delete(index))
  return ops
