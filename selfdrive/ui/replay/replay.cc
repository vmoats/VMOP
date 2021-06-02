#include "selfdrive/ui/replay/replay.h"

#include <capnp/dynamic.h>

#include "cereal/services.h"
#include "selfdrive/camerad/cameras/camera_common.h"
#include "selfdrive/common/timing.h"
#include "selfdrive/hardware/hw.h"

const int SEGMENT_LENGTH = 60;  // 60s
const int FORWARD_SEGS = 2;
const int BACKWARD_SEGS = 2;

// class Replay

Replay::Replay(SubMaster *sm) : sm_(sm) {
  QStringList block = QString(getenv("BLOCK")).split(",");
  qDebug() << "blocklist" << block;
  QStringList allow = QString(getenv("ALLOW")).split(",");
  qDebug() << "allowlist" << allow;

  std::vector<const char *> s;
  for (const auto &it : services) {
    if ((allow[0].size() == 0 || allow.contains(it.name)) &&
        !block.contains(it.name)) {
      s.push_back(it.name);
      socks_.insert(it.name);
    }
  }
  qDebug() << "services " << s;

  if (sm_ == nullptr) {
    pm_ = new PubMaster(s);
  }
}

Replay::~Replay() {
  stop();
  delete pm_;
}

bool Replay::start(const QString &routeName) {
  Route route(routeName);
  if (!route.load()) {
    qInfo() << "failed to retrieve files for route " << routeName;
    return false;
  }
  return start(route);
}

bool Replay::start(const Route &route) {
  assert(!running());
  if (!route.segments().size()) return false;

  route_ = route;
  current_segment_ = route_.segments().firstKey();

  qDebug() << "replay route " << route_.name() << " from " << current_segment_ << ", total segments:" << route.segments().size();
  queue_thread_ = std::thread(&Replay::queueSegmentThread, this);
  stream_thread_ = std::thread(&Replay::streamThread, this);
  return true;
}

void Replay::stop() {
  if (!running()) return;

  // wait until threads finished
  camera_server_.stop();
  exit_ = true;
  stream_thread_.join();
  exit_ = false;

  segments_.clear();
  current_ts_ = seek_ts_ = 0;
  current_segment_ = 0;
  route_start_ts_ = 0;
}

void Replay::relativeSeek(int ts) {
  seekTo(current_ts_ + ts);
}

void Replay::seekTo(int to_ts) {
  const auto &rs = route_.segments();
  if (!rs.size()) return;

  seek_ts_ = to_ts;
  current_segment_ = std::clamp(to_ts / SEGMENT_LENGTH, 0, rs.lastKey());
  qDebug() << "seeking to " << seek_ts_;
}

// return nullptr if segment is not loaded
std::shared_ptr<Segment> Replay::getSegment(int segment) {
  auto it = segments_.find(segment);
  return (it != segments_.end() && it->second->loaded()) ? it->second : nullptr;
}

void Replay::pushFrame(CameraType cam_type, uint32_t frame_id) {
  if (!camera_server_.hasCamera(cam_type)) return;

  auto eidx = encoderIdx_[cam_type].find(frame_id);
  if (eidx != encoderIdx_[cam_type].end()) {
    auto seg = getSegment(eidx->second.segmentNum);
    if (seg) {
      camera_server_.pushFrame(cam_type, seg, eidx->second.segmentId);
    }
  }
}

const std::string &Replay::eventSocketName(const cereal::Event::Reader &e) {
  auto it = eventNameMap.find(e.which());
  if (it == eventNameMap.end()) {
    std::string type;
    KJ_IF_MAYBE(e_, static_cast<capnp::DynamicStruct::Reader>(e).which()) {
      type = e_->getProto().getName();
    }
    if (socks_.find(type) == socks_.end()) {
      type = "";
    }
    it = eventNameMap.insert(it, {e.which(), type});
  }
  return it->second;
}

void Replay::removeSegment(int n) {
  QVector<Event *> remove_items;
  {
    std::unique_lock lk(mutex_);
    segments_.erase(n);
    updating_events = true;
    auto end_it = events_.lowerBound(route_start_ts_ + (n + 1) * SEGMENT_LENGTH * 1e9);
    if (end_it != events_.end()) {
      auto it = events_.begin();
      remove_items.resize(std::distance(it, end_it) + 1);
      while (it != end_it) {
        remove_items << it.value();
        it = events_.erase(it);
      }
    }
  }

  // remove items out of lock
  if (remove_items.size()) {
    for (Event *evt : remove_items) {
      delete evt;
    }
  }
}

// maintain the segment window
void Replay::queueSegmentThread() {
  static int prev_segment = -1;
  while (!exit_) {
    int segment = current_segment_;
    if (prev_segment == segment) {
      QThread::msleep(100);
      continue;
    }

    const auto &rs = route_.segments();
    const int cur_idx = std::distance(rs.begin(), rs.find(segment));
    int i = 0;
    for (auto it = rs.begin(); it != rs.end(); ++it, ++i) {
      int n = it.key();
      if (i < cur_idx - BACKWARD_SEGS || i > cur_idx + FORWARD_SEGS) {
        if (segments_.find(n) != segments_.end()) {
          removeSegment(n);
        }
      } else if (segments_.find(n) == segments_.end()) {
        std::unique_lock lk(mutex_);
        segments_[n] = std::make_shared<Segment>(n, rs[n], *this);
      }
    }
    prev_segment = segment;
  }
}

void Replay::streamThread() {
  int last_print = 0;
  while (!exit_) {
    mutex_.lock();
    std::shared_ptr<Segment> seg = getSegment(current_segment_);
    auto eit = events_.lowerBound(route_start_ts_ + seek_ts_ * 1e9);
    if (!seg || eit == events_.end()) {
      mutex_.unlock();
      qDebug() << "waiting for events";
      QThread::msleep(100);
      continue;
    }
    mutex_.unlock();

    camera_server_.ensureServerForSegment(seg.get());

    // TODO: use initData's logMonoTime
    if (route_start_ts_ == 0) {
      route_start_ts_ = events_.firstKey();
    }
    // set evt_start_tm to current event's mono_time
    uint64_t evt_start_tm = eit.key();
    current_ts_ = (evt_start_tm - route_start_ts_) / 1e9;
    qDebug() << "unlogging at" << seek_ts_;
    uint64_t loop_start_tm = nanos_since_boot();
    int current_seek_ts_ = seek_ts_;
    while (!exit_ && current_seek_ts_ == seek_ts_ && !updating_events && eit != events_.end()) {
      Event *evt = (*eit);
      uint64_t mono_time = evt->event.getLogMonoTime();
      current_ts_ = std::max(mono_time - route_start_ts_, (uint64_t)0) / 1e9;
      const std::string &sock_name = eventSocketName(evt->event);
      if (!sock_name.empty()) {
        if (std::abs(current_ts_ - last_print) > 5.0) {
          last_print = current_ts_;
          qInfo() << "at " << last_print;
        }

        // keep time
        long etime = mono_time - evt_start_tm;
        long rtime = nanos_since_boot() - loop_start_tm;
        long us_behind = ((etime - rtime) * 1e-3) + 0.5;
        if (us_behind > 0 && us_behind < 1e6) {
          QThread::usleep(us_behind);
          //qDebug() << "sleeping" << us_behind << etime << timer.nsecsElapsed();
        }

        // publish frames
        switch (evt->event.which()) {
          case cereal::Event::ROAD_CAMERA_STATE:
            pushFrame(RoadCam, evt->event.getRoadCameraState().getFrameId());
            break;
          case cereal::Event::DRIVER_CAMERA_STATE:
            pushFrame(DriverCam, evt->event.getDriverCameraState().getFrameId());
            break;
          case cereal::Event::WIDE_ROAD_CAMERA_STATE:
            pushFrame(WideRoadCam, evt->event.getWideRoadCameraState().getFrameId());
            break;
          default:
            break;
        }

        // publish msg
        if (sm_ == nullptr) {
          auto bytes = evt->bytes();
          pm_->send(sock_name.c_str(), (capnp::byte *)bytes.begin(), bytes.size());
        } else {
          // TODO: subMaster is not thread safe.
          sm_->update_msgs(nanos_since_boot(), {{sock_name, evt->event}});
        }
      }
      current_segment_ = current_ts_ / SEGMENT_LENGTH;
      ++eit;
    }
    if (updating_events) {
      updating_events = false;
      seek_ts_ = current_ts_.load();
    }
  }
}

void Replay::mergeEvents(const Events &events, EncodeIdxMap encoderIdx[]) {
  std::unique_lock lk(mutex_);
  updating_events = true;
  events_.unite(events);
  for (auto cam_type : ALL_CAMERAS) {
    encoderIdx_[cam_type].merge(encoderIdx[cam_type]);
  }
}

// class Segment

Segment::Segment(int seg_num, const SegmentFile &file, Replay &replay) : seg_num(seg_num) {
  // fallback to qlog if rlog not exists.
  const QString &log_file = file.rlog.isEmpty() ? file.qlog : file.rlog;
  if (log_file.isEmpty()) {
    qDebug() << "no log file in segment " << seg_num;
    return;
  }

  loading = 1;
  log = new LogReader(log_file);
  QObject::connect(log, &LogReader::finished, [&](bool success, const Events &events, EncodeIdxMap encoderIdx[]) {
    if (success) {
      replay.mergeEvents(events, encoderIdx);
    }
    --loading;
  });

  // start framereader threads
  // fallback to qcamera if camera not exists.
  std::pair<CameraType, QString> cam_files[] = {{RoadCam, file.camera.isEmpty() ? file.qcamera : file.camera},
                                                {DriverCam, file.dcamera},
                                                {WideRoadCam, file.wcamera}};
  for (const auto &[cam_type, file] : cam_files) {
    if (!file.isEmpty()) {
      loading += 1;
      FrameReader *fr = frames[cam_type] = new FrameReader(file.toStdString());
      QObject::connect(fr, &FrameReader::finished, [=]() { --loading; });
    }
  }
}

Segment::~Segment() {
  qDebug() << QString("remove segment %1").arg(seg_num);
  delete log;
  for (auto f : frames) delete f;
}

// class CameraServer

CameraServer::CameraServer() {
  device_id_ = cl_get_device_id(CL_DEVICE_TYPE_DEFAULT);
  context_ = CL_CHECK_ERR(clCreateContext(NULL, 1, &device_id_, NULL, NULL, &err));
}

CameraServer::~CameraServer() {
  delete vipc_server_;
  CL_CHECK(clReleaseContext(context_));
}

void CameraServer::ensureServerForSegment(Segment *seg) {
  static VisionStreamType stream_types[] = {
      [RoadCam] = VISION_STREAM_RGB_BACK,
      [DriverCam] = VISION_STREAM_RGB_FRONT,
      [WideRoadCam] = VISION_STREAM_RGB_WIDE,
  };

  if (vipc_server_) {
    // restart vipc server if frame changed. such as switched between qcameras and cameras.
    for (auto cam_type : ALL_CAMERAS) {
      const FrameReader *fr = seg->frames[cam_type];
      const CameraState *s = camera_states_[cam_type];
      bool frame_changed = false;
      if (fr && fr->valid()) {
        frame_changed = !s || s->width != fr->width || s->height != fr->height;
      } else if (s) {
        frame_changed = true;
      }
      if (frame_changed) {
        qDebug() << "restart vipc server";
        stop();
        break;
      }
    }
  }
  if (!vipc_server_) {
    for (auto cam_type : ALL_CAMERAS) {
      const FrameReader *fr = seg->frames[cam_type];
      if (!fr || !fr->valid()) continue;

      if (!vipc_server_) {
        vipc_server_ = new VisionIpcServer("camerad", device_id_, context_);
      }
      vipc_server_->create_buffers(stream_types[cam_type], UI_BUF_COUNT, true, fr->width, fr->height);

      CameraState *state = new CameraState;
      state->width = fr->width;
      state->height = fr->height;
      state->stream_type = stream_types[cam_type];
      state->thread = std::thread(&CameraServer::cameraThread, this, cam_type, state);
      camera_states_[cam_type] = state;
    }
    if (vipc_server_) {
      vipc_server_->start_listener();
    }
  }
}

void CameraServer::stop() {
  if (!vipc_server_) return;

  // stop camera threads
  exit_ = true;
  for (int i = 0; i < std::size(camera_states_); ++i) {
    if (CameraState *state = camera_states_[i]) {
      camera_states_[i] = nullptr;
      state->thread.join();
      delete state;
    }
  }
  exit_ = false;

  // stop vipc server
  delete vipc_server_;
  vipc_server_ = nullptr;
}

void CameraServer::cameraThread(CameraType cam_type, CameraServer::CameraState *s) {
  while (!exit_) {
    std::pair<std::shared_ptr<Segment>, uint32_t> frame;
    if (!s->queue.try_pop(frame, 20)) continue;

    auto &[seg, segmentId] = frame;
    FrameReader *frm = seg->frames[cam_type];
    if (frm->width != s->width || frm->height != s->height) {
      // eidx is not in the same segment with different frame size
      continue;
    }

    if (uint8_t *data = frm->get(segmentId)) {
      VisionIpcBufExtra extra = {};
      VisionBuf *buf = vipc_server_->get_buffer(s->stream_type);
      memcpy(buf->addr, data, frm->getRGBSize());
      vipc_server_->send(buf, &extra, false);
    }
  }
  qDebug() << "camera thread " << cam_type << " stopped ";
}
