#pragma once

#include "selfdrive/ui/qt/widgets/cameraview.h"
#include "selfdrive/ui/qt/onroad/driver_monitoring.h"
#include "selfdrive/ui/qt/widgets/input.h"

class DriverViewWindow : public CameraWidget {
  Q_OBJECT
public:
  explicit DriverViewWindow(QWidget *parent);
  void paintGL() override;
  mat4 calcFrameMatrix() override;
  QPixmap face_img;
};

class DriverViewDialog : public DialogBase {
  Q_OBJECT
public:
  DriverViewDialog(QWidget *parent);
  void done(int r) override;
  Params params;
  DriverMonitorRenderer driver_monitor;
};
