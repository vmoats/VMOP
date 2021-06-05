#include "selfdrive/ui/replay/replay.h"

#include <capnp/dynamic.h>

#include "cereal/services.h"
#include "selfdrive/common/timing.h"
#include "selfdrive/hardware/hw.h"

const int SEGMENT_LENGTH = 60;  // 60s
const int FORWARD_SEGS = 2;
const int BACKWARD_SEGS = 2;

// class Replay

Replay::Replay(SubMaster *sm, QObject *parent) : sm_(sm), QObject(parent) {
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
  events_ = new std::vector<Event *>();
}

Replay::~Replay() {
  stop();
  delete pm_;
  delete events_;
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
  queue_thread_.join();
  exit_ = false;

  // clear all
  events_->clear();
  segments_.clear();
  current_ts_ = route_start_ts_ = 0;
  current_segment_ = 0;
}

QString Replay::elapsedTime(uint64_t ns) {
  QTime time(0, 0, 0);
  auto a = time.addSecs((ns - route_start_ts_) / 1e9);
  return a.toString("hh:mm:ss");
}

void Replay::seek(int seconds) {
  if (route_start_ts_ > 0) {
    seekTo(route_start_ts_ + seconds * 1e9);
  }
}

void Replay::relativeSeek(int seconds) {
  if (current_ts_ > 0) {
    seekTo(current_ts_ + seconds * 1e9);
  }
}

// return nullptr if segment is not loaded
std::shared_ptr<Segment> Replay::getSegment(int segment) {
  auto it = segments_.find(segment);
  return (it != segments_.end() && it->second->loaded) ? it->second : nullptr;
}

void Replay::pushFrame(int cur_seg_num, CameraType cam_type, uint32_t frame_id) {
  // search encodeIdx in adjacent segments.
  for (auto n : {cur_seg_num, cur_seg_num - 1, cur_seg_num + 1}) {
    if (auto seg = getSegment(n)) {
      auto eidxMap = seg->log->encoderIdx[cam_type];
      if (auto eidx = eidxMap.find(frame_id); eidx != eidxMap.end()) {
        camera_server_.pushFrame(cam_type, seg->frames[cam_type], eidx->second.segmentId);
        break;
      }
    }
  }
}

const std::string &Replay::eventSocketName(const Event *e) {
  auto it = eventNameMap.find(e->which);
  if (it == eventNameMap.end()) {
    std::string type;
    KJ_IF_MAYBE(e_, static_cast<capnp::DynamicStruct::Reader>(e->event).which()) {
      type = e_->getProto().getName();
    }
    if (socks_.find(type) == socks_.end()) {
      type = "";
    }
    it = eventNameMap.insert(it, {e->which, type});
  }
  return it->second;
}

void Replay::doMergeEvent() {
  Segment *sender = qobject_cast<Segment *>(QObject::sender());
  mergeEvents(sender);
}

void Replay::mergeEvents(Segment *seg) {
  // double t1 = millis_since_boot();
  LogReader *log = seg->log;
  if (log->events.empty()) return;

  auto log_event_begin_it = log->events.begin();
  if (auto e = (*log_event_begin_it); e->which == cereal::Event::INIT_DATA) {
    route_start_ts_ = e->mono_time;
    // don't merge INIT_DATA
    log_event_begin_it += 1;
  }
  assert(route_start_ts_ != 0);

  uint64_t min_tm = route_start_ts_ + std::max(current_segment_ - BACKWARD_SEGS, 0) * SEGMENT_LENGTH * 1e9;
  uint64_t max_tm = route_start_ts_ + (current_segment_ + FORWARD_SEGS + 1) * SEGMENT_LENGTH * 1e9;
  auto begin_merge_it = std::lower_bound(events_->begin(), events_->end(), min_tm, [](const Event *e, uint64_t v) {
    return e->mono_time < v;
  });
  if (begin_merge_it == events_->end()) {
    begin_merge_it = events_->begin();
  }
  auto end_merge_it = std::upper_bound(begin_merge_it, events_->end(), max_tm, [](uint64_t v, const Event *e) {
    return v < e->mono_time;
  });

  // merge segment
  std::vector<Event *> *dst = new std::vector<Event *>;
  dst->reserve((end_merge_it - begin_merge_it) + log->events.size());
  std::merge(begin_merge_it, end_merge_it, log_event_begin_it, log->events.end(),
             std::back_inserter(*dst), [](const Event *l, const Event *r) { return *l < *r; });

  {
    std::unique_lock events_lock(mutex_);
    delete events_;
    events_ = dst;
    seg->loaded = true;
    events_changed_ = true;
  }
  // remove segments
  for (auto it = segments_.begin(); it != segments_.end();) {
    if (auto &e = it->second->log->events; !e.empty()) {
      if (e.back()->mono_time < min_tm || e.front()->mono_time > max_tm) {
        it = segments_.erase(it);
        continue;
      }
    }
    ++it;
  }
  // qInfo() << "merge array " << millis_since_boot() - t1 << "Ksize " << events_->size();
}

// maintain the segment window
void Replay::queueSegmentThread() {
  while (!exit_) {
    const auto &rs = route_.segments();
    int cur_idx = std::distance(rs.begin(), rs.lowerBound(current_segment_));
    int i = 0;
    for (auto it = rs.begin(); it != rs.end(); ++it, ++i) {
      if (i >= cur_idx - BACKWARD_SEGS && i <= cur_idx + FORWARD_SEGS) {
        if (int n = it.key(); segments_.find(n) == segments_.end()) {
          std::unique_lock lk(mutex_);
          segments_[n] = std::make_shared<Segment>(n, rs[n]);
          connect(segments_[n].get(), &Segment::finishedRead, this, &Replay::doMergeEvent);
        }
      }
    }
    QThread::msleep(20);
  }
}

void Replay::seekTo(uint64_t to_ts, cereal::Event::Which which) {
  const int segment = (to_ts - route_start_ts_) / 1e9 / SEGMENT_LENGTH;
  if (!route_.segments().contains(segment)) {
    qInfo() << "can't seek to " << elapsedTime(to_ts) << ": segment " << segment << " does not exist.";
    return;
  }

  std::unique_lock lk(mutex_);
  current_ts_ = to_ts;
  current_which_ = cereal::Event::INIT_DATA;
  current_segment_ = segment;
  events_changed_ = true;
  qDebug() << "seeking to " << elapsedTime(to_ts);
}

std::optional<std::pair<std::vector<Event *>::iterator, uint64_t>> Replay::findEvent(uint64_t tm, cereal::Event::Which which) {
  std::unique_lock lk(mutex_);
  events_changed_ = false;
  // make sure current segment is loaded
  if (auto seg = getSegment(current_segment_)) {
    camera_server_.ensure(seg->frames);
    auto eit = std::lower_bound(events_->begin(), events_->end(), tm, [&](const Event *e, uint64_t v) {
      return e->mono_time < v || (e->mono_time == v && e->which < which);
    });
    if (eit != events_->end()) {
      return std::make_pair(eit + 1, (*eit)->mono_time);
    }
  }
  return std::nullopt;
}

void Replay::streamThread() {
  uint64_t last_print_ts = 0;
  while (!exit_) {
    auto e = findEvent(current_ts_, current_which_);
    if (!e) {
      qDebug() << "waiting for events";
      QThread::msleep(100);
      continue;
    }
    auto [eit, evt_start_ts] = *e;
    uint64_t loop_start_ts = nanos_since_boot();
    while (!exit_) {
      if (events_changed_ || eit == events_->end()) break;

      const Event *evt = (*eit);
      const std::string &sock_name = eventSocketName(evt);
      if (!sock_name.empty()) {
        current_which_ = evt->which;
        current_ts_ = evt->mono_time;
        current_segment_ = (current_ts_ - route_start_ts_) / 1e9 / SEGMENT_LENGTH;
        if ((current_ts_ - last_print_ts) > 5 * 1e9) {
          last_print_ts = current_ts_;
          qInfo().noquote() << "at" << elapsedTime(last_print_ts);
        }
        // keep time
        uint64_t etime = current_ts_ - evt_start_ts;
        uint64_t rtime = nanos_since_boot() - loop_start_ts;
        uint64_t us_behind = ((etime - rtime) * 1e-3) + 0.5;
        if (us_behind > 0 && us_behind < 1e6) {
          QThread::usleep(us_behind);
          //qDebug() << "sleeping" << us_behind << etime << timer.nsecsElapsed();
        }

        // publish frames
        switch (current_which_) {
          case cereal::Event::ROAD_CAMERA_STATE:
            pushFrame(current_segment_, RoadCam, evt->event.getRoadCameraState().getFrameId());
            break;
          case cereal::Event::DRIVER_CAMERA_STATE:
            pushFrame(current_segment_, DriverCam, evt->event.getDriverCameraState().getFrameId());
            break;
          case cereal::Event::WIDE_ROAD_CAMERA_STATE:
            pushFrame(current_segment_, WideRoadCam, evt->event.getWideRoadCameraState().getFrameId());
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
      ++eit;
    }
  }
}

// class Segment

Segment::Segment(int seg_num, const SegmentFile &file, QObject *parent) : seg_num(seg_num), QObject(parent) {
  // fallback to qlog if rlog not exists.
  const QString &log_file = file.rlog.isEmpty() ? file.qlog : file.rlog;
  if (log_file.isEmpty()) {
    qDebug() << "no log file in segment " << seg_num;
    return;
  }

  loading_ = 1;
  log = new LogReader(log_file, this);
  QObject::connect(log, &LogReader::finished, [&](bool success) {
    if (--loading_ == 0) emit finishedRead();
  });

  // fallback to qcamera if camera not exists.
  std::pair<CameraType, QString> cam_files[] = {{RoadCam, file.camera.isEmpty() ? file.qcamera : file.camera},
                                                {DriverCam, file.dcamera},
                                                {WideRoadCam, file.wcamera}};
  for (const auto &[cam_type, file] : cam_files) {
    if (!file.isEmpty()) {
      loading_ += 1;
      FrameReader *fr = frames[cam_type] = new FrameReader(file.toStdString(), this);
      QObject::connect(fr, &FrameReader::finished, [=]() { if(--loading_ == 0) emit finishedRead(); });
    }
  }
}

Segment::~Segment() {
  // qDebug() << QString("remove segment %1").arg(seg_num);
}
