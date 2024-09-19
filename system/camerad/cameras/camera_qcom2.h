#pragma once

#include <memory>
#include <utility>

#include "system/camerad/cameras/camera_common.h"
#include "system/camerad/cameras/camera_util.h"
#include "system/camerad/cameras/tici.h"
#include "system/camerad/cameras/spectra.h"
#include "system/camerad/sensors/sensor.h"
#include "common/util.h"


class CameraState : public SpectraCamera {
public:
  std::mutex exp_lock;

  int exposure_time = 5;
  bool dc_gain_enabled = false;
  int dc_gain_weight = 0;
  int gain_idx = 0;
  float analog_gain_frac = 0;

  float cur_ev[3] = {};
  float best_ev_score = 0;
  int new_exp_g = 0;
  int new_exp_t = 0;

  Rect ae_xywh = {};
  float measured_grey_fraction = 0;
  float target_grey_fraction = 0.3;

  float fl_pix = 0;

  CameraState(MultiCameraState *multi_camera_state, const CameraConfig &config);
  void handle_camera_event(void *evdat);
  void update_exposure_score(float desired_ev, int exp_t, int exp_g_idx, float exp_gain);
  void set_camera_exposure(float grey_frac);

  void set_exposure_rect();
  void sensor_set_parameters();
  void run();
};

class MultiCameraState : public SpectraMaster {
public:
  MultiCameraState()
    : driver_cam(this, DRIVER_CAMERA_CONFIG),
      road_cam(this, ROAD_CAMERA_CONFIG),
      wide_road_cam(this, WIDE_ROAD_CAMERA_CONFIG) {
  };

  CameraState road_cam;
  CameraState wide_road_cam;
  CameraState driver_cam;
};
