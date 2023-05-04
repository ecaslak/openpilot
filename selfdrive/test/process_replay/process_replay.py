#!/usr/bin/env python3
import importlib
import os
import sys
import threading
import time
import signal
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable

import capnp
from tqdm import tqdm

import cereal.messaging as messaging
from cereal import car, log
from cereal.services import service_list
from common.params import Params
from common.timeout import Timeout
from common.realtime import DT_CTRL
from panda.python import ALTERNATIVE_EXPERIENCE
from selfdrive.car.car_helpers import get_car, interfaces
from selfdrive.test.process_replay.helpers import OpenpilotPrefix
from selfdrive.manager.process import PythonProcess
from selfdrive.manager.process_config import managed_processes

# Numpy gives different results based on CPU features after version 19
NUMPY_TOLERANCE = 1e-7
CI = "CI" in os.environ
TIMEOUT = 15
PROC_REPLAY_DIR = os.path.dirname(os.path.abspath(__file__))
FAKEDATA = os.path.join(PROC_REPLAY_DIR, "fakedata/")

class ReplayContext:
  def __init__(self, cfg):
    self.non_polled_pubs = list(set(cfg.pub_sub.keys()) - set(cfg.polled_pubs))
    self.polled_pubs = cfg.polled_pubs
    self.drained_pubs = cfg.drained_pubs
    assert(len(self.non_polled_pubs) != 0 or len(self.polled_pubs) != 0)
    assert(set(self.drained_pubs) & set(self.polled_pubs) == set())
    self.subs = [s for sub in cfg.pub_sub.values() for s in sub]
    self.recv_called_events = {
      s: messaging.fake_event(s, messaging.FAKE_EVENT_RECV_CALLED)
      for s in cfg.pub_sub.keys() if s not in cfg.polled_pubs
    }
    self.recv_ready_events = {
      s: messaging.fake_event(s, messaging.FAKE_EVENT_RECV_READY)
      for s in cfg.pub_sub.keys() if s not in cfg.polled_pubs
    }
    if len(cfg.polled_pubs) > 0 and len(cfg.drained_pubs) == 0:
      self.poll_called_event = messaging.fake_event(cfg.proc_name, messaging.FAKE_EVENT_POLL_CALLED)
      self.poll_ready_event = messaging.fake_event(cfg.proc_name, messaging.FAKE_EVENT_POLL_READY)
  
  def __enter__(self):
    messaging.toggle_fake_events(True)
    return self

  def __exit__(self, exc_type, exc_obj, exc_tb):
    messaging.toggle_fake_events(False)

  def unlock_sockets(self, msg_type):
    if len(self.non_polled_pubs) <= 1 and len(self.polled_pubs) == 0:
      return
    
    if len(self.polled_pubs) > 0 and len(self.drained_pubs) == 0:
      self.poll_called_event.wait()
      self.poll_called_event.clear()
      if msg_type not in self.polled_pubs:
        self.poll_ready_event.set()

    for pub in self.non_polled_pubs:
      if pub == msg_type:
        continue

      self.recv_ready_events[pub].set()

  def wait_for_next_recv(self, msg_type, next_msg_type):
    if len(self.drained_pubs) > 0:
      return self._wait_for_next_recv_drained(msg_type, next_msg_type)
    elif len(self.polled_pubs) > 0:
      return self._wait_for_next_recv_using_polls(msg_type)
    else:
      return self._wait_for_next_recv_standard(msg_type)

  def _wait_for_next_recv_drained(self, msg_type, next_msg_type):
    # if the next message is also drained message, then we need to fake the recv_ready event
    # in order to start the next cycle
    if msg_type in self.drained_pubs and next_msg_type == msg_type:
      self.recv_called_events[self.drained_pubs[0]].wait()
      self.recv_called_events[self.drained_pubs[0]].clear()
      self.recv_ready_events[self.drained_pubs[0]].set()
    self.recv_called_events[self.drained_pubs[0]].wait()

  def _wait_for_next_recv_standard(self, msg_type):
    if len(self.non_polled_pubs) <= 1 and len(self.polled_pubs) == 0:
      return self.recv_called_events[self.non_polled_pubs[0]].wait()

    values = defaultdict(int)
    # expected sets is len(self.pubs) - 1 (current) + 1 (next)
    if len(self.non_polled_pubs) == 0:
      return

    expected_total_sets = len(self.non_polled_pubs)
    while expected_total_sets > 0:
      for pub in self.non_polled_pubs:
        if not self.recv_called_events[pub].peek():
          continue

        value = self.recv_called_events[pub].clear()
        values[pub] += value
        expected_total_sets -= value
      time.sleep(0)

    max_key = max(values.keys(), key=lambda k: values[k])
    self.recv_called_events[max_key].set()

  def _wait_for_next_recv_using_polls(self, msg_type):
    if msg_type in self.polled_pubs:
      self.poll_ready_event.set()
    self.poll_called_event.wait()


@dataclass
class ProcessConfig:
  proc_name: str
  pub_sub: Dict[str, List[str]]
  ignore: List[str]
  init_callback: Optional[Callable]
  should_recv_callback: Optional[Callable]
  tolerance: Optional[float]
  fake_pubsubmaster: bool
  submaster_config: Dict[str, List[str]] = field(default_factory=dict)
  environ: Dict[str, str] = field(default_factory=dict)
  subtest_name: str = ""
  field_tolerances: Dict[str, float] = field(default_factory=dict)
  timeout: int = 30
  polled_pubs: List[str] = field(default_factory=list)
  drained_pubs: List[str] = field(default_factory=list)


def wait_for_event(evt):
  if not evt.wait(TIMEOUT):
    if threading.currentThread().getName() == "MainThread":
      # tested process likely died. don't let test just hang
      raise Exception(f"Timeout reached. Tested process {os.environ['PROC_NAME']} likely crashed.")
    else:
      # done testing this process, let it die
      sys.exit(0)


class FakeSocket:
  def __init__(self, wait=True):
    self.data = []
    self.wait = wait
    self.recv_called = threading.Event()
    self.recv_ready = threading.Event()

  def receive(self, non_blocking=False):
    if non_blocking:
      return None

    if self.wait:
      self.recv_called.set()
      wait_for_event(self.recv_ready)
      self.recv_ready.clear()
    return self.data.pop()

  def send(self, data):
    if self.wait:
      wait_for_event(self.recv_called)
      self.recv_called.clear()

    self.data.append(data)

    if self.wait:
      self.recv_ready.set()

  def wait_for_recv(self):
    wait_for_event(self.recv_called)


class DumbSocket:
  def __init__(self, s=None):
    if s is not None:
      try:
        dat = messaging.new_message(s)
      except capnp.lib.capnp.KjException:  # pylint: disable=c-extension-no-member
        # lists
        dat = messaging.new_message(s, 0)

      self.data = dat.to_bytes()

  def receive(self, non_blocking=False):
    return self.data

  def send(self, dat):
    pass


class FakeSubMaster(messaging.SubMaster):
  def __init__(self, services, ignore_alive=None, ignore_avg_freq=None):
    super().__init__(services, ignore_alive=ignore_alive, ignore_avg_freq=ignore_avg_freq, addr=None)
    self.sock = {s: DumbSocket(s) for s in services}
    self.update_called = threading.Event()
    self.update_ready = threading.Event()
    self.wait_on_getitem = False

  def __getitem__(self, s):
    # hack to know when fingerprinting is done
    if self.wait_on_getitem:
      self.update_called.set()
      wait_for_event(self.update_ready)
      self.update_ready.clear()
    return self.data[s]

  def update(self, timeout=-1):
    self.update_called.set()
    wait_for_event(self.update_ready)
    self.update_ready.clear()

  def update_msgs(self, cur_time, msgs):
    wait_for_event(self.update_called)
    self.update_called.clear()
    super().update_msgs(cur_time, msgs)
    self.update_ready.set()

  def wait_for_update(self):
    wait_for_event(self.update_called)


class FakePubMaster(messaging.PubMaster):
  def __init__(self, services):  # pylint: disable=super-init-not-called
    self.data = {}
    self.sock = {}
    self.last_updated = None
    for s in services:
      try:
        data = messaging.new_message(s)
      except capnp.lib.capnp.KjException:
        data = messaging.new_message(s, 0)
      self.data[s] = data.as_reader()
      self.sock[s] = DumbSocket()
    self.send_called = threading.Event()
    self.get_called = threading.Event()

  def send(self, s, dat):
    self.last_updated = s
    if isinstance(dat, bytes):
      self.data[s] = log.Event.from_bytes(dat)
    else:
      self.data[s] = dat.as_reader()
    self.send_called.set()
    wait_for_event(self.get_called)
    self.get_called.clear()

  def wait_for_msg(self):
    wait_for_event(self.send_called)
    self.send_called.clear()
    dat = self.data[self.last_updated]
    self.get_called.set()
    return dat


def fingerprint(msgs, fsm, can_sock, fingerprint):
  print("start fingerprinting")
  fsm.wait_on_getitem = True

  # populate fake socket with data for fingerprinting
  canmsgs = [msg for msg in msgs if msg.which() == "can"]
  wait_for_event(can_sock.recv_called)
  can_sock.recv_called.clear()
  can_sock.data = [msg.as_builder().to_bytes() for msg in canmsgs[:300]]
  can_sock.recv_ready.set()
  can_sock.wait = False

  # we know fingerprinting is done when controlsd sets sm['lateralPlan'].sensorValid
  wait_for_event(fsm.update_called)
  fsm.update_called.clear()

  fsm.wait_on_getitem = False
  can_sock.wait = True
  can_sock.data = []

  fsm.update_ready.set()


def get_car_params(msgs, fsm, can_sock, fingerprint):
  if fingerprint:
    CarInterface, _, _ = interfaces[fingerprint]
    CP = CarInterface.get_non_essential_params(fingerprint)
  else:
    can = FakeSocket(wait=False)
    sendcan = FakeSocket(wait=False)

    canmsgs = [msg for msg in msgs if msg.which() == 'can']
    for m in canmsgs[:300]:
      can.send(m.as_builder().to_bytes())
    _, CP = get_car(can, sendcan, Params().get_bool("ExperimentalLongitudinalEnabled"))
  Params().put("CarParams", CP.to_bytes())


def controlsd_rcv_callback(msg, CP, cfg, frame):
  # no sendcan until controlsd is initialized
  socks = [s for s in cfg.pub_sub[msg.which()] if
           (frame) % int(service_list[msg.which()].frequency / service_list[s].frequency) == 0]
  if "sendcan" in socks and (frame - 1) < 2000:
    socks.remove("sendcan")
  return socks, len(socks) > 0


def radar_rcv_callback(msg, CP, cfg, frame):
  if msg.which() != "can":
    return [], False
  elif CP.radarUnavailable:
    return ["radarState", "liveTracks"], True

  radar_msgs = {"honda": [0x445], "toyota": [0x19f, 0x22f], "gm": [0x474],
                "chrysler": [0x2d4]}.get(CP.carName, None)

  if radar_msgs is None:
    raise NotImplementedError

  for m in msg.can:
    if m.src == 1 and m.address in radar_msgs:
      return ["radarState", "liveTracks"], True
  return [], False


def calibration_rcv_callback(msg, CP, cfg, frame):
  # calibrationd publishes 1 calibrationData every 5 cameraOdometry packets.
  # should_recv always true to increment frame
  recv_socks = []
  if frame == 0 or (msg.which() == 'cameraOdometry' and (frame % 5) == 0):
    recv_socks = ["liveCalibration"]
  return recv_socks, (frame - 1) == 0 or msg.which() == 'cameraOdometry'


def torqued_rcv_callback(msg, CP, cfg, frame):
  # should_recv always true to increment frame
  recv_socks = []
  if msg.which() == 'liveLocationKalman' and (frame % 5) == 0:
    recv_socks = ["liveTorqueParameters"]
  return recv_socks, (frame - 1) == 0 or msg.which() == 'liveLocationKalman'


def ublox_rcv_callback(msg, CP, cfg, frame):
  msg_class, msg_id = msg.ubloxRaw[2:4]
  if (msg_class, msg_id) in {(1, 7 * 16)}:
    return ["gpsLocationExternal"], True
  elif (msg_class, msg_id) in {(2, 1 * 16 + 5), (10, 9)}:
    return ["ubloxGnss"], True
  else:
    return [], False


def rate_based_rcv_callback(msg, CP, cfg, frame):
  resp_sockets = [s for s in cfg.pub_sub[msg.which()] if
          frame % max(1, int(service_list[msg.which()].frequency / service_list[s].frequency)) == 0]
  should_recv = bool(len(resp_sockets))

  return resp_sockets, should_recv


CONFIGS = [
  ProcessConfig(
    proc_name="controlsd",
    pub_sub={
      "can": ["controlsState", "carState", "carControl", "sendcan", "carEvents", "carParams"],
      "deviceState": [], "pandaStates": [], "peripheralState": [], "liveCalibration": [], "driverMonitoringState": [],
      "longitudinalPlan": [], "lateralPlan": [], "liveLocationKalman": [], "liveParameters": [], "radarState": [],
      "modelV2": [], "driverCameraState": [], "roadCameraState": [], "wideRoadCameraState": [], "managerState": [],
      "testJoystick": [], "liveTorqueParameters": [],
    },
    ignore=["logMonoTime", "valid", "controlsState.startMonoTime", "controlsState.cumLagMs"],
    init_callback=fingerprint,
    should_recv_callback=controlsd_rcv_callback,
    tolerance=NUMPY_TOLERANCE,
    fake_pubsubmaster=True,
    submaster_config={
      'ignore_avg_freq': ['radarState', 'longitudinalPlan', 'driverCameraState', 'driverMonitoringState'],  # dcam is expected at 20 Hz
      'ignore_alive': [], 
    },
    polled_pubs=[
      "deviceState", "pandaStates", "peripheralState", "liveCalibration", "driverMonitoringState",
      "longitudinalPlan", "lateralPlan", "liveLocationKalman", "liveParameters", "radarState",
      "modelV2", "driverCameraState", "roadCameraState", "wideRoadCameraState", "managerState",
      "testJoystick", "liveTorqueParameters"
    ],
    drained_pubs=["can"]
  ),
  ProcessConfig(
    proc_name="radard",
    pub_sub={
      "can": ["radarState", "liveTracks"],
      "carState": [], "modelV2": [],
    },
    ignore=["logMonoTime", "valid", "radarState.cumLagMs"],
    init_callback=get_car_params,
    should_recv_callback=radar_rcv_callback,
    tolerance=None,
    fake_pubsubmaster=True,
    polled_pubs=["carState", "modelV2"],
    drained_pubs=["can"]
  ),
  ProcessConfig(
    proc_name="plannerd",
    pub_sub={
      "modelV2": ["lateralPlan", "longitudinalPlan", "uiPlan"],
      "carControl": [], "carState": [], "controlsState": [], "radarState": [],
    },
    ignore=["logMonoTime", "valid", "longitudinalPlan.processingDelay", "longitudinalPlan.solverExecutionTime", "lateralPlan.solverExecutionTime"],
    init_callback=get_car_params,
    should_recv_callback=rate_based_rcv_callback,
    tolerance=NUMPY_TOLERANCE,
    fake_pubsubmaster=True,
    polled_pubs=["radarState", "modelV2"],
  ),
  ProcessConfig(
    proc_name="calibrationd",
    pub_sub={
      "carState": ["liveCalibration"],
      "cameraOdometry": [],
      "carParams": [],
    },
    ignore=["logMonoTime", "valid"],
    init_callback=get_car_params,
    should_recv_callback=calibration_rcv_callback,
    tolerance=None,
    fake_pubsubmaster=True,
    polled_pubs=["cameraOdometry"],
  ),
  ProcessConfig(
    proc_name="dmonitoringd",
    pub_sub={
      "driverStateV2": ["driverMonitoringState"],
      "liveCalibration": [], "carState": [], "modelV2": [], "controlsState": [],
    },
    ignore=["logMonoTime", "valid"],
    init_callback=get_car_params,
    should_recv_callback=rate_based_rcv_callback,
    tolerance=NUMPY_TOLERANCE,
    fake_pubsubmaster=True,
    polled_pubs=["driverStateV2"],
  ),
  ProcessConfig(
    proc_name="locationd",
    pub_sub={
      "cameraOdometry": ["liveLocationKalman"],
      "accelerometer": [], "gyroscope": [],
      "gpsLocationExternal": [], "liveCalibration": [], 
      "carState": [], "carParams": [], "gpsLocation": [],
    },
    ignore=["logMonoTime", "valid"],
    init_callback=get_car_params,
    should_recv_callback=None,
    tolerance=NUMPY_TOLERANCE,
    fake_pubsubmaster=False,
    polled_pubs=[
      "cameraOdometry", "gpsLocationExternal", "gyroscope",
      "accelerometer", "liveCalibration", "carState", "carParams", "gpsLocation"
    ],
  ),
  ProcessConfig(
    proc_name="paramsd",
    pub_sub={
      "liveLocationKalman": ["liveParameters"],
      "carState": []
    },
    ignore=["logMonoTime", "valid"],
    init_callback=get_car_params,
    should_recv_callback=rate_based_rcv_callback,
    tolerance=NUMPY_TOLERANCE,
    fake_pubsubmaster=True,
    polled_pubs=["liveLocationKalman"],
  ),
  ProcessConfig(
    proc_name="ubloxd",
    pub_sub={
      "ubloxRaw": ["ubloxGnss", "gpsLocationExternal"],
    },
    ignore=["logMonoTime"],
    init_callback=None,
    should_recv_callback=None,
    tolerance=None,
    fake_pubsubmaster=False,
  ),
  ProcessConfig(
    proc_name="laikad",
    pub_sub={
      "ubloxGnss": ["gnssMeasurements"],
      "qcomGnss": ["gnssMeasurements"],
    },
    ignore=["logMonoTime"],
    init_callback=get_car_params,
    should_recv_callback=None,
    tolerance=NUMPY_TOLERANCE,
    fake_pubsubmaster=False,
    timeout=60*10,  # first messages are blocked on internet assistance
    drained_pubs=["ubloxGnss", "qcomGnss"],
  ),
  ProcessConfig(
    proc_name="torqued",
    pub_sub={
      "liveLocationKalman": ["liveTorqueParameters"],
      "carState": [], "carControl": [],
    },
    ignore=["logMonoTime"],
    init_callback=get_car_params,
    should_recv_callback=torqued_rcv_callback,
    tolerance=NUMPY_TOLERANCE,
    fake_pubsubmaster=True,
    polled_pubs=["liveLocationKalman"],
  ),
]


def replay_process(cfg, lr, fingerprint=None):
  with OpenpilotPrefix():
    if cfg.fake_pubsubmaster:
      return python_replay_process(cfg, lr, fingerprint)
    else:
      return replay_process_with_sockets(cfg, lr, fingerprint)


def setup_env(simulation=False, CP=None, cfg=None, controlsState=None, lr=None):
  params = Params()
  params.clear_all()
  params.put_bool("OpenpilotEnabledToggle", True)
  params.put_bool("Passive", False)
  params.put_bool("DisengageOnAccelerator", True)
  params.put_bool("WideCameraOnly", False)
  params.put_bool("DisableLogging", False)

  os.environ["NO_RADAR_SLEEP"] = "1"
  os.environ["REPLAY"] = "1"
  os.environ["SKIP_FW_QUERY"] = ""
  os.environ["FINGERPRINT"] = ""

  if lr is not None:
    services = {m.which() for m in lr}
    params.put_bool("UbloxAvailable", "ubloxGnss" in services)
  
  if cfg is not None:
    # Clear all custom processConfig environment variables
    for config in CONFIGS:
      for k, _ in config.environ.items():
        if k in os.environ:
          del os.environ[k]

    os.environ.update(cfg.environ)
    os.environ['PROC_NAME'] = cfg.proc_name

  if simulation:
    os.environ["SIMULATION"] = "1"
  elif "SIMULATION" in os.environ:
    del os.environ["SIMULATION"]

  # Initialize controlsd with a controlsState packet
  if controlsState is not None:
    params.put("ReplayControlsState", controlsState.as_builder().to_bytes())
  else:
    params.remove("ReplayControlsState")

  # Regen or python process
  if CP is not None:
    if CP.alternativeExperience == ALTERNATIVE_EXPERIENCE.DISABLE_DISENGAGE_ON_GAS:
      params.put_bool("DisengageOnAccelerator", False)

    if CP.fingerprintSource == "fw":
      params.put("CarParamsCache", CP.as_builder().to_bytes())
    else:
      os.environ['SKIP_FW_QUERY'] = "1"
      os.environ['FINGERPRINT'] = CP.carFingerprint

    if CP.openpilotLongitudinalControl:
      params.put_bool("ExperimentalLongitudinalEnabled", True)


def python_replay_process(cfg, lr, fingerprint=None):
  sub_sockets = [s for _, sub in cfg.pub_sub.items() for s in sub]
  pub_sockets = [s for s in cfg.pub_sub.keys() if s != 'can']

  fsm = FakeSubMaster(pub_sockets, **cfg.submaster_config)
  fpm = FakePubMaster(sub_sockets)
  args = (fsm, fpm)
  if 'can' in list(cfg.pub_sub.keys()):
    can_sock = FakeSocket()
    args = (fsm, fpm, can_sock)

  all_msgs = sorted(lr, key=lambda msg: msg.logMonoTime)
  pub_msgs = [msg for msg in all_msgs if msg.which() in list(cfg.pub_sub.keys())]

  controlsState = None
  initialized = False
  for msg in lr:
    if msg.which() == 'controlsState':
      controlsState = msg.controlsState
      if initialized:
        break
    elif msg.which() == 'carEvents':
      initialized = car.CarEvent.EventName.controlsInitializing not in [e.name for e in msg.carEvents]

  assert controlsState is not None and initialized, "controlsState never initialized"

  if fingerprint is not None:
    os.environ['SKIP_FW_QUERY'] = "1"
    os.environ['FINGERPRINT'] = fingerprint
    setup_env(cfg=cfg, controlsState=controlsState, lr=lr)
  else:
    CP = [m for m in lr if m.which() == 'carParams'][0].carParams
    setup_env(CP=CP, cfg=cfg, controlsState=controlsState, lr=lr)

  assert(type(managed_processes[cfg.proc_name]) is PythonProcess)
  managed_processes[cfg.proc_name].prepare()
  mod = importlib.import_module(managed_processes[cfg.proc_name].module)

  thread = threading.Thread(target=mod.main, args=args)
  thread.daemon = True
  thread.start()

  if cfg.init_callback is not None:
    if 'can' not in list(cfg.pub_sub.keys()):
      can_sock = None
    cfg.init_callback(all_msgs, fsm, can_sock, fingerprint)

  CP = car.CarParams.from_bytes(Params().get("CarParams", block=True))

  # wait for started process to be ready
  if 'can' in list(cfg.pub_sub.keys()):
    can_sock.wait_for_recv()
  else:
    fsm.wait_for_update()

  log_msgs, msg_queue = [], []
  for msg in tqdm(pub_msgs):
    if cfg.should_recv_callback is not None:
      recv_socks, should_recv = cfg.should_recv_callback(msg, CP, cfg, fsm.frame + 1)
    else:
      recv_socks = [s for s in cfg.pub_sub[msg.which()] if
                    (fsm.frame + 1) % max(1, int(service_list[msg.which()].frequency / service_list[s].frequency)) == 0]
      should_recv = bool(len(recv_socks))

    if msg.which() == 'can':
      can_sock.send(msg.as_builder().to_bytes())
    else:
      msg_queue.append(msg.as_builder())

    if should_recv:
      fsm.update_msgs(msg.logMonoTime / 1e9, msg_queue)
      msg_queue = []

      recv_cnt = len(recv_socks)
      while recv_cnt > 0:
        m = fpm.wait_for_msg().as_builder()
        m.logMonoTime = msg.logMonoTime
        m = m.as_reader()

        log_msgs.append(m)
        recv_cnt -= m.which() in recv_socks
  return log_msgs


def replay_process_with_fake_sockets(cfg, lr, fingerprint=None):
  with ReplayContext(cfg) as rc:
    pm = messaging.PubMaster(cfg.pub_sub.keys())
    sub_sockets = [s for _, sub in cfg.pub_sub.items() for s in sub]
    sockets = {s: messaging.sub_sock(s, timeout=100) for s in sub_sockets}

    all_msgs = sorted(lr, key=lambda msg: msg.logMonoTime)
    pub_msgs = [msg for msg in all_msgs if msg.which() in list(cfg.pub_sub.keys())]

    # We need to fake SubMaster alive since we can't inject a fake clock
    setup_env(simulation=True, cfg=cfg, lr=lr)

    if cfg.proc_name == "laikad":
      ublox = Params().get_bool("UbloxAvailable")
      sub_keys = ({"qcomGnss", } if ublox else {"ubloxGnss", })
      keys = set(rc.non_polled_pubs) - sub_keys
      d_keys = set(rc.drained_pubs) - sub_keys
      rc.non_polled_pubs = list(keys)
      rc.drained_pubs = list(d_keys)
      pub_msgs = [msg for msg in pub_msgs if msg.which() in keys]

    if cfg.proc_name == "locationd":
      ublox = Params().get_bool("UbloxAvailable")
      sub_keys = ({"gpsLocation", } if ublox else {"gpsLocationExternal", })
      np_keys = set(rc.non_polled_pubs) - sub_keys
      p_keys = set(rc.polled_pubs) - sub_keys
      keys = np_keys | p_keys
      rc.non_polled_pubs = list(np_keys)
      rc.polled_pubs = list(p_keys)
      pub_msgs = [msg for msg in pub_msgs if msg.which() in keys]

    controlsState = None
    initialized = False
    for msg in lr:
      if msg.which() == 'controlsState':
        controlsState = msg.controlsState
        if initialized:
          break
      elif msg.which() == 'carEvents':
        initialized = car.CarEvent.EventName.controlsInitializing not in [e.name for e in msg.carEvents]

    assert controlsState is not None and initialized, "controlsState never initialized"

    if fingerprint is not None:
      os.environ['SKIP_FW_QUERY'] = "1"
      os.environ['FINGERPRINT'] = fingerprint
      setup_env(cfg=cfg, controlsState=controlsState, lr=lr)
    else:
      CP = [m for m in lr if m.which() == 'carParams'][0].carParams
      setup_env(CP=CP, cfg=cfg, controlsState=controlsState, lr=lr)

    managed_processes[cfg.proc_name].prepare()
    managed_processes[cfg.proc_name].start()

    if cfg.init_callback is not None:
      cfg.init_callback(all_msgs, None, None, fingerprint)
      CP = car.CarParams.from_bytes(Params().get("CarParams", block=True))

    log_msgs = []
    try:
      # Wait for process to startup
      with Timeout(10, error_msg=f"timed out waiting for process to start: {repr(cfg.proc_name)}"):
        while not any(pm.all_readers_updated(s) for s in cfg.pub_sub.keys()):
          time.sleep(0)
      
      for s in sockets.values():
        messaging.recv_one_or_none(s)

      # Do the replay
      cnt = 0
      for i, msg in enumerate(tqdm(pub_msgs)):
        with Timeout(cfg.timeout, error_msg=f"timed out testing process {repr(cfg.proc_name)}, {cnt}/{len(pub_msgs)} msgs done"):
          resp_sockets = cfg.pub_sub[msg.which()]
          should_recv = True
          if cfg.should_recv_callback is not None:
            resp_sockets, should_recv = cfg.should_recv_callback(msg, CP, cfg, cnt)

          if len(log_msgs) == 0 and len(resp_sockets) > 0:
            for s in sockets.values():
              messaging.recv_one_or_none(s)
          
          rc.unlock_sockets(msg.which())
          pm.send(msg.which(), msg.as_builder())
          # wait for the next receive on the process side
          rc.wait_for_next_recv(msg.which(), pub_msgs[i+1].which() if i+1 < len(pub_msgs) else None)

          if should_recv:
            for s in resp_sockets:
              ms = messaging.drain_sock(sockets[s])
              for m in ms:
                m = m.as_builder()
                m.logMonoTime = msg.logMonoTime
                log_msgs.append(m.as_reader())
            cnt += 1
    finally:
      managed_processes[cfg.proc_name].signal(signal.SIGKILL)
      managed_processes[cfg.proc_name].stop()

    return log_msgs


def replay_process_with_sockets(cfg, lr, fingerprint=None):
  pm = messaging.PubMaster(cfg.pub_sub.keys())
  sub_sockets = [s for _, sub in cfg.pub_sub.items() for s in sub]
  sockets = {s: messaging.sub_sock(s, timeout=100) for s in sub_sockets}

  all_msgs = sorted(lr, key=lambda msg: msg.logMonoTime)
  pub_msgs = [msg for msg in all_msgs if msg.which() in list(cfg.pub_sub.keys())]

  # We need to fake SubMaster alive since we can't inject a fake clock
  setup_env(simulation=True, cfg=cfg, lr=lr)

  if cfg.proc_name == "laikad":
    ublox = Params().get_bool("UbloxAvailable")
    keys = set(cfg.pub_sub.keys()) - ({"qcomGnss", } if ublox else {"ubloxGnss", })
    pub_msgs = [msg for msg in pub_msgs if msg.which() in keys]

  managed_processes[cfg.proc_name].prepare()
  managed_processes[cfg.proc_name].start()

  log_msgs = []
  try:
    # Wait for process to startup
    with Timeout(10, error_msg=f"timed out waiting for process to start: {repr(cfg.proc_name)}"):
      while not any(pm.all_readers_updated(s) for s in cfg.pub_sub.keys()):
        time.sleep(0)

    for s in sockets.values():
      messaging.recv_one_or_none(s)

    # Do the replay
    cnt = 0
    for msg in tqdm(pub_msgs):
      with Timeout(cfg.timeout, error_msg=f"timed out testing process {repr(cfg.proc_name)}, {cnt}/{len(pub_msgs)} msgs done"):
        resp_sockets = cfg.pub_sub[msg.which()]
        if cfg.should_recv_callback is not None:
          resp_sockets, _ = cfg.should_recv_callback(msg, None, None, None)

        # Make sure all subscribers are connected
        if len(log_msgs) == 0 and len(resp_sockets) > 0:
          for s in sockets.values():
            messaging.recv_one_or_none(s)

        pm.send(msg.which(), msg.as_builder())
        while not pm.all_readers_updated(msg.which()):
          time.sleep(0)

        for s in resp_sockets:
          m = messaging.recv_one_retry(sockets[s])
          m = m.as_builder()
          m.logMonoTime = msg.logMonoTime
          log_msgs.append(m.as_reader())
        cnt += 1
  finally:
    managed_processes[cfg.proc_name].signal(signal.SIGKILL)
    managed_processes[cfg.proc_name].stop()

  return log_msgs


def check_enabled(msgs):
  cur_enabled_count = 0
  max_enabled_count = 0
  for msg in msgs:
    if msg.which() == "carParams":
      if msg.carParams.notCar:
        return True
    elif msg.which() == "controlsState":
      if msg.controlsState.active:
        cur_enabled_count += 1
      else:
        cur_enabled_count = 0
      max_enabled_count = max(max_enabled_count, cur_enabled_count)

  return max_enabled_count > int(10. / DT_CTRL)
