#pragma once

#include <memory>
#include <optional>
#include <set>
#include <string>
#include <utility>

#include <QFrame>
#include <QPropertyAnimation>
#include <QSlider>
#include <QToolBar>
#include <QTabBar>

#include "selfdrive/ui/qt/widgets/cameraview.h"
#include "tools/cabana/utils/util.h"
#include "tools/replay/logreader.h"
#include "tools/cabana/streams/replaystream.h"

class Slider : public QSlider {
  Q_OBJECT

public:
  Slider(QWidget *parent);
  double currentSecond() const { return value() / factor; }
  void setCurrentSecond(double sec) { setValue(sec * factor); }
  void setTimeRange(double min, double max) { setRange(min * factor, max * factor); }
  void mousePressEvent(QMouseEvent *e) override;
  void paintEvent(QPaintEvent *ev) override;
  const double factor = 1000.0;
};

class StreamCameraView : public CameraWidget {
  Q_OBJECT

public:
  StreamCameraView(std::string stream_name, VisionStreamType stream_type, QWidget *parent = nullptr);
  void paintGL() override;
  void showPausedOverlay() { fade_animation->start(); }
  void parseQLog(std::shared_ptr<LogReader> qlog);

private:
  QPixmap generateThumbnail(QPixmap thumbnail, double seconds);
  void drawAlert(QPainter &p, const QRect &rect, const Timeline::Entry &alert);
  void drawThumbnail(QPainter &p);
  bool eventFilter(QObject *obj, QEvent *event) override;

  QPropertyAnimation *fade_animation;
  QMap<uint64_t, QPixmap> thumbnails;
  std::optional<QPoint> thumbnail_pt_;
};

class VideoWidget : public QFrame {
  Q_OBJECT

public:
  VideoWidget(QWidget *parnet = nullptr);

protected:
  QString formatTime(double sec, bool include_milliseconds = false);
  void timeRangeChanged();
  void updateState();
  void updatePlayBtnState();
  QWidget *createCameraWidget();
  void createPlaybackController();
  void createSpeedDropdown(QToolBar *toolbar);
  void loopPlaybackClicked();
  void vipcAvailableStreamsUpdated(std::set<VisionStreamType> streams);

  StreamCameraView *cam_widget;
  QAction *time_btn = nullptr;
  QAction *play_action = nullptr;
  QToolButton *speed_btn = nullptr;
  QAction *skip_to_end_action = nullptr;
  Slider *slider = nullptr;
  QTabBar *camera_tab = nullptr;
};
