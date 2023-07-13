#!/usr/bin/env python3
import os
import json
import time
import datetime
import unittest
import subprocess
import numpy as np

import cereal.messaging as messaging
from system.hardware import TICI
from system.sensord.rawgps.rawgpsd import at_cmd
from selfdrive.manager.process_config import managed_processes
from common.transformations.coordinates import ecef_from_geodetic

GOOD_SIGNAL = bool(int(os.getenv("GOOD_SIGNAL", '0')))


class TestRawgpsd(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    if not TICI:
      raise unittest.SkipTest

    cls.sm = messaging.SubMaster(['qcomGnss', 'gpsLocation', 'gnssMeasurements'])

  def tearDown(self):
    managed_processes['rawgpsd'].stop()
    os.system("sudo systemctl restart systemd-resolved")

  def _wait_for_output(self, t=10):
    time.sleep(t)
    self.sm.update()

  def test_wait_for_modem(self):
    os.system("sudo systemctl stop ModemManager lte")
    managed_processes['rawgpsd'].start()
    self._wait_for_output(10)
    assert not self.sm.updated['qcomGnss']

    os.system("sudo systemctl restart ModemManager lte")
    self._wait_for_output(30)
    assert self.sm.updated['qcomGnss']


  def test_startup_time_no_internet(self):
    os.system("sudo systemctl stop systemd-resolved")
    for _ in range(1):
      managed_processes['rawgpsd'].start()

      self._wait_for_output(7)
      assert self.sm.updated['qcomGnss']
      managed_processes['rawgpsd'].stop()

  def test_startup_time_internet(self):
    for _ in range(1):
      managed_processes['rawgpsd'].start()

      self._wait_for_output(7)
      assert self.sm.updated['qcomGnss']
      managed_processes['rawgpsd'].stop()

  def test_turns_off_gnss(self):
    for s in (0.1, 0.5, 1, 5):
      managed_processes['rawgpsd'].start()
      time.sleep(s)
      managed_processes['rawgpsd'].stop()

      ls = subprocess.check_output("mmcli -m any --location-status --output-json", shell=True, encoding='utf-8')
      loc_status = json.loads(ls)
      assert set(loc_status['modem']['location']['enabled']) <= {'3gpp-lac-ci'}

  def test_assistance_loading(self):
    # clear assistance data
    at_cmd("AT+QGPSDEL=0")

    managed_processes['rawgpsd'].start()
    self._wait_for_output(10)
    assert self.sm.updated['qcomGnss']
    managed_processes['rawgpsd'].stop()

    # after QGPSDEL: '+QGPSXTRADATA: 0,"1980/01/05,19:00:00"'
    # after loading: '+QGPSXTRADATA: 10080,"2023/06/24,19:00:00"'
    out = at_cmd("AT+QGPSXTRADATA?")
    out = out.split("+QGPSXTRADATA:")[1].split("'")[0].strip()
    valid_duration, injected_time_str = out.split(",", 1)
    assert valid_duration == "10080"  # should be max time
    injected_time = datetime.datetime.strptime(injected_time_str.replace("\"", ""), "%Y/%m/%d,%H:%M:%S")
    self.assertLess(abs((datetime.datetime.utcnow() - injected_time).total_seconds()), 60*60*12)

  def test_no_assistance_loading(self):
    os.system("sudo systemctl stop systemd-resolved")
    # clear assistance data
    at_cmd("AT+QGPSDEL=0")

    managed_processes['rawgpsd'].start()
    self._wait_for_output(10)
    assert self.sm.updated['qcomGnss']
    managed_processes['rawgpsd'].stop()

    # after QGPSDEL: '+QGPSXTRADATA: 0,"1980/01/05,19:00:00"'
    # after loading: '+QGPSXTRADATA: 10080,"2023/06/24,19:00:00"'
    out = at_cmd("AT+QGPSXTRADATA?")
    out = out.split("+QGPSXTRADATA:")[1].split("'")[0].strip()
    valid_duration, injected_time_str = out.split(",", 1)
    injected_time_str = injected_time_str.replace('\"', '').replace('\'', '')
    assert injected_time_str[:] == '1980/01/05,19:00:00'[:]
    assert valid_duration == '0'


  @unittest.skipIf(not GOOD_SIGNAL, "No good GPS signal")
  def test_fix(self):
    # clear assistance data
    at_cmd("AT+QGPSDEL=0")

    managed_processes['rawgpsd'].start()
    managed_processes['laikad'].start()
    assert self._wait_for_output(60)
    assert self.sm.updated['qcomGnss']
    assert self.sm.updated['gpsLocation']
    assert self.sm['gpsLocation'].flags == 1
    module_fix = ecef_from_geodetic([self.sm['gpsLocation'].latitude,
                                     self.sm['gpsLocation'].longitude,
                                     self.sm['gpsLocation'].altitude])
    assert self.sm['gnssMeasurements'].positionECEF.valid
    total_diff = np.array(self.sm['gnssMeasurements'].positionECEF.value) - module_fix
    self.assertLess(np.linalg.norm(total_diff), 100)
    managed_processes['laikad'].stop()
    managed_processes['rawgpsd'].stop()

if __name__ == "__main__":
  unittest.main(failfast=True)
