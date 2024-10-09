#!/usr/bin/env python3
import os
import sys
from collections import defaultdict
from typing import Any
import tempfile
from itertools import zip_longest

import matplotlib.pyplot as plt

from openpilot.common.git import get_commit
from openpilot.system.hardware import PC
from openpilot.tools.lib.openpilotci import get_url
from openpilot.selfdrive.test.process_replay.compare_logs import compare_logs, format_diff
from openpilot.selfdrive.test.process_replay.process_replay import get_process_config, replay_process
from openpilot.tools.lib.framereader import FrameReader
from openpilot.tools.lib.logreader import LogReader, save_log
from openpilot.tools.lib.github_utils import GithubUtils

TEST_ROUTE = "2f4452b03ccb98f0|2022-12-03--13-45-30"
SEGMENT = 6
MAX_FRAMES = 100 if PC else 600

NO_MODEL = "NO_MODEL" in os.environ
SEND_EXTRA_INPUTS = bool(int(os.getenv("SEND_EXTRA_INPUTS", "0")))

DATA_TOKEN = os.getenv("CI_ARTIFACTS_TOKEN","")
API_TOKEN = os.getenv("GITHUB_COMMENTS_TOKEN","")
MODEL_REPLAY_BUCKET="model_replay_master"
GITHUB = GithubUtils(API_TOKEN, DATA_TOKEN)


def get_log_fn(test_route, ref="master"):
  return f"{test_route}_model_tici_{ref}.bz2"

def plot(proposed, master, title, tmp):
  proposed = list(proposed)
  master = list(master)
  fig, ax = plt.subplots()
  ax.plot(proposed, label='PROPOSED')
  ax.plot(master, label='MASTER')
  plt.legend(loc='best')
  plt.title(title)
  plt.savefig(f'{tmp}/{title}.png')
  return (title + '.png', proposed == master)

def get_event(logs, event):
  return (getattr(m, m.which()) for m in filter(lambda m: m.which() == event, logs))

def zl(array, fill):
  return zip_longest(array, [], fillvalue=fill)

def generate_report(proposed, master, tmp):
  ModelV2_Plots = zl([
                     (lambda x: x.velocity.x[0], "velocity.x"),
                     (lambda x: x.action.desiredCurvature, "desiredCurvature"),
                     (lambda x: x.leadsV3[0].x[0], "leadsV3.x"),
                     (lambda x: x.laneLines[1].y[0], "laneLines.y"),
                     (lambda x: x.meta.disengagePredictions.gasPressProbs[1], "gasPressProbs")
                    ], "modelV2")

  return [plot(map(v[0], get_event(proposed, event)), \
               map(v[0], get_event(master, event)), v[1], tmp) \
               for v,event in [*ModelV2_Plots]]

def create_table(title, files, link, open_table=False):
  if not files:
    return ""
  table = [f'<details {"open" if open_table else ""}><summary>{title}</summary><table>']
  for i,f in enumerate(files):
    if not (i % 2):
      table.append("<tr>")
    table.append(f'<td><img src=\\"{link}/{f[0]}\\"></td>')
    if (i % 2):
      table.append("</tr>")
  table.append("</table></details>")
  table = "".join(table)
  return table

def comment_replay_report(proposed, master, full_logs):
  with tempfile.TemporaryDirectory() as tmp:
    PR_BRANCH = os.getenv("GIT_BRANCH","")
    DATA_BUCKET = f"model_replay_{PR_BRANCH}"

    files = generate_report(proposed, master, tmp)

    GITHUB.upload_files(DATA_BUCKET, [(x[0], tmp + '/' + x[0]) for x in files])

    log_name = get_log_fn(TEST_ROUTE, get_commit())
    save_log(log_name, full_logs)
    GITHUB.upload_file(DATA_BUCKET, os.path.basename(log_name), log_name)

    diff_files = [x for x in files if not x[1]]
    link = GITHUB.get_bucket_link(DATA_BUCKET)
    diff_plots = create_table("Model Replay Differences", diff_files, link, open_table=True)
    all_plots = create_table("All Model Replay Plots", files, link)
    comment = f"new ref: {link}/{log_name}" + diff_plots + all_plots
    GITHUB.comment_on_pr(comment, "commaci-public", PR_BRANCH)

def trim_logs_to_max_frames(logs, max_frames, frs_types, include_all_types):
  all_msgs = []
  cam_state_counts = defaultdict(int)
  # keep adding messages until cam states are equal to MAX_FRAMES
  for msg in sorted(logs, key=lambda m: m.logMonoTime):
    all_msgs.append(msg)
    if msg.which() in frs_types:
      cam_state_counts[msg.which()] += 1

    if all(cam_state_counts[state] == max_frames for state in frs_types):
      break

  if len(include_all_types) != 0:
    other_msgs = [m for m in logs if m.which() in include_all_types]
    all_msgs.extend(other_msgs)

  return all_msgs


def model_replay(lr, frs):
  # modeld is using frame pairs
  modeld_logs = trim_logs_to_max_frames(lr, MAX_FRAMES, {"roadCameraState", "wideRoadCameraState"}, {"roadEncodeIdx", "wideRoadEncodeIdx", "carParams"})
  dmodeld_logs = trim_logs_to_max_frames(lr, MAX_FRAMES, {"driverCameraState"}, {"driverEncodeIdx", "carParams"})

  if not SEND_EXTRA_INPUTS:
    modeld_logs = [msg for msg in modeld_logs if msg.which() != 'liveCalibration']
    dmodeld_logs = [msg for msg in dmodeld_logs if msg.which() != 'liveCalibration']

  # initial setup
  for s in ('liveCalibration', 'deviceState'):
    msg = next(msg for msg in lr if msg.which() == s).as_builder()
    msg.logMonoTime = lr[0].logMonoTime
    modeld_logs.insert(1, msg.as_reader())
    dmodeld_logs.insert(1, msg.as_reader())

  modeld = get_process_config("modeld")
  dmonitoringmodeld = get_process_config("dmonitoringmodeld")

  modeld_msgs = replay_process(modeld, modeld_logs, frs)
  dmonitoringmodeld_msgs = replay_process(dmonitoringmodeld, dmodeld_logs, frs)
  return modeld_msgs + dmonitoringmodeld_msgs


if __name__ == "__main__":
  update = "--update" in sys.argv or (os.getenv("GIT_BRANCH", "") == 'master')
  replay_dir = os.path.dirname(os.path.abspath(__file__))

  # load logs
  lr = list(LogReader(get_url(TEST_ROUTE, SEGMENT, "rlog.bz2")))
  frs = {
    'roadCameraState': FrameReader(get_url(TEST_ROUTE, SEGMENT, "fcamera.hevc"), readahead=True),
    'driverCameraState': FrameReader(get_url(TEST_ROUTE, SEGMENT, "dcamera.hevc"), readahead=True),
    'wideRoadCameraState': FrameReader(get_url(TEST_ROUTE, SEGMENT, "ecamera.hevc"), readahead=True)
  }

  log_msgs = []
  # run replays
  if not NO_MODEL:
    log_msgs += model_replay(lr, frs)

  # get diff
  failed = False
  if not update:
    log_fn = get_log_fn(TEST_ROUTE)
    try:
      all_logs = list(LogReader(GITHUB.get_file_url(MODEL_REPLAY_BUCKET, log_fn)))
      cmp_log = []

      # logs are ordered based on type: modelV2, drivingModelData, driverStateV2
      if not NO_MODEL:
        model_start_index = next(i for i, m in enumerate(all_logs) if m.which() in ("modelV2", "drivingModelData", "cameraOdometry"))
        cmp_log += all_logs[model_start_index:model_start_index + MAX_FRAMES*3]
        dmon_start_index = next(i for i, m in enumerate(all_logs) if m.which() == "driverStateV2")
        cmp_log += all_logs[dmon_start_index:dmon_start_index + MAX_FRAMES]

      ignore = [
        'logMonoTime',
        'drivingModelData.frameDropPerc',
        'drivingModelData.modelExecutionTime',
        'modelV2.frameDropPerc',
        'modelV2.modelExecutionTime',
        'driverStateV2.modelExecutionTime',
        'driverStateV2.gpuExecutionTime'
      ]
      if PC:
        # TODO We ignore whole bunch so we can compare important stuff
        # like posenet with reasonable tolerance
        ignore += ['modelV2.acceleration.x',
                   'modelV2.position.x',
                   'modelV2.position.xStd',
                   'modelV2.position.y',
                   'modelV2.position.yStd',
                   'modelV2.position.z',
                   'modelV2.position.zStd',
                   'drivingModelData.path.xCoefficients',]
        for i in range(3):
          for field in ('x', 'y', 'v', 'a'):
            ignore.append(f'modelV2.leadsV3.{i}.{field}')
            ignore.append(f'modelV2.leadsV3.{i}.{field}Std')
        for i in range(4):
          for field in ('x', 'y', 'z', 't'):
            ignore.append(f'modelV2.laneLines.{i}.{field}')
        for i in range(2):
          for field in ('x', 'y', 'z', 't'):
            ignore.append(f'modelV2.roadEdges.{i}.{field}')
      tolerance = .3 if PC else None
      results: Any = {TEST_ROUTE: {}}
      log_paths: Any = {TEST_ROUTE: {"models": {'ref': log_fn, 'new': log_fn}}}
      results[TEST_ROUTE]["models"] = compare_logs(cmp_log, log_msgs, tolerance=tolerance, ignore_fields=ignore)
      diff_short, diff_long, failed = format_diff(results, log_paths, 'master')

      if "CI" in os.environ:
        comment_replay_report(log_msgs, cmp_log, log_msgs)
        failed = False
        print(diff_long)
      print('-------------\n'*5)
      print(diff_short)
      with open("model_diff.txt", "w") as f:
        f.write(diff_long)
    except Exception as e:
      print(str(e))
      failed = True

  # upload new refs
  if update and not PC:
    print("Uploading new refs")
    log_fn = get_log_fn(TEST_ROUTE)
    save_log(log_fn, log_msgs)
    try:
      GITHUB.upload_file(MODEL_REPLAY_BUCKET, os.path.basename(log_fn), log_fn)
    except Exception as e:
      print("failed to upload", e)

  sys.exit(int(failed))
