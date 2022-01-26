#include "selfdrive/ui/replay/consoleui.h"

#include <QApplication>
#include <initializer_list>

#include "selfdrive/common/version.h"

namespace {

const int BORDER_SIZE = 3;

enum Color {
  None,
  Info,
  Debug,
  Warning,
  Critical,
  bgWhite,
  BrightWhite,
  Engaged,
  Disengaged,
  AlertInfo,
};

template <typename T>
void add_str(WINDOW *w, T str, Color color = Color::None, bool bold = false) {
  if (color != Color::None) wattron(w, COLOR_PAIR(color));
  if (bold) wattron(w, A_BOLD);

  if constexpr (std::is_same<T, const char *>::value)
    waddstr(w, str);
  else if constexpr (std::is_same<T, std::string>::value)
    waddstr(w, str.c_str());
  else
    waddch(w, str);

  if (bold) wattroff(w, A_BOLD);
  if (color != Color::None) wattroff(w, COLOR_PAIR(color));
}

template <typename T>
void mv_add_str(WINDOW *w, int y, int x, T str, Color color = Color::None, bool bold = false) {
  wmove(w, y, x);
  add_str(w, str, color, bold);
}

}  // namespace

ConsoleUI::ConsoleUI(Replay *replay, QObject *parent) : replay(replay), sm({"carState", "liveParameters"}), QObject(parent) {
  // Initialize curses
  initscr();
  clear();
  curs_set(false);
  cbreak();  // Line buffering disabled. pass on everything
  noecho();
  keypad(stdscr, true);
  nodelay(stdscr, true);  // non-blocking getchar()

  // Initialize all the colors
  start_color();
  init_pair(Color::Info, COLOR_WHITE, COLOR_BLACK);
  init_pair(Color::Debug, 8, COLOR_BLACK);
  init_pair(Color::Warning, COLOR_YELLOW, COLOR_BLACK);
  init_pair(Color::Critical, COLOR_RED, COLOR_BLACK);
  init_pair(Color::bgWhite, COLOR_BLACK, 15);
  init_pair(Color::BrightWhite, 15, COLOR_BLACK);
  init_pair(Color::Disengaged, COLOR_BLUE, COLOR_BLUE);
  init_pair(Color::Engaged, 28, 28);
  init_pair(Color::AlertInfo, 28, COLOR_BLACK);

  initWindows();

  qRegisterMetaType<uint64_t>("uint64_t");
  installMessageHandler([this](ReplyMsgType type, const std::string msg) {
    emit logMessageSignal((int)type, QString::fromStdString(msg));
  });
  installDownloadProgressHandler([this](uint64_t cur, uint64_t total, bool success) {
    emit updateProgressBarSignal(cur, total, success);
  });

  QObject::connect(replay, &Replay::streamStarted, this, &ConsoleUI::updateSummary);
  QObject::connect(&notifier, SIGNAL(activated(int)), SLOT(readyRead()));
  QObject::connect(this, &ConsoleUI::updateProgressBarSignal, this, &ConsoleUI::updateProgressBar);
  QObject::connect(this, &ConsoleUI::logMessageSignal, this, &ConsoleUI::logMessage);

  sm_timer.callOnTimeout(this, &ConsoleUI::updateStatus);
  sm_timer.start(50);
  getch_timer.start(1000, this);
  readyRead();
}

ConsoleUI::~ConsoleUI() {
  endwin();
}

void ConsoleUI::initWindows() {
  getmaxyx(stdscr, max_height, max_width);
  w.fill(nullptr);
  w[Win::Title] = newwin(1, max_width, 0, 0);
  w[Win::Stats] = newwin(2, max_width - 2 * BORDER_SIZE, 2, BORDER_SIZE);
  w[Win::Timeline] = newwin(4, max_width - 2 * BORDER_SIZE, 5, BORDER_SIZE);
  w[Win::TimelineDesc] = newwin(1, 100, 10, BORDER_SIZE);
  w[Win::CarState] = newwin(3, 100, 12, BORDER_SIZE);
  w[Win::DownloadBar] = newwin(1, 100, 16, BORDER_SIZE);
  if (int log_height = max_height - 27; log_height > 4) {
    w[Win::LogBorder] = newwin(log_height, max_width - 2 * (BORDER_SIZE - 1), 17, BORDER_SIZE - 1);
    box(w[Win::LogBorder], 0, 0);
    w[Win::Log] = newwin(log_height - 2, max_width - 2 * BORDER_SIZE, 18, BORDER_SIZE);
    scrollok(w[Win::Log], true);
  }
  w[Win::Help] = newwin(5, max_width - (2 * BORDER_SIZE), max_height - 6, BORDER_SIZE);

  // set the title bar
  wbkgd(w[Win::Title], COLOR_PAIR(Color::bgWhite));
  mvwprintw(w[Win::Title], 0, 3, "openpilot replay %s", COMMA_VERSION);

  // show windows on the real screen
  refresh();
  displayTimelineDesc();
  displayHelp();
  updateSummary();
  updateTimeline();
  for (auto win : w) {
    if (win) wrefresh(win);
  }
}

void ConsoleUI::timerEvent(QTimerEvent *ev) {
  if (ev->timerId() != getch_timer.timerId()) return;

  if (is_term_resized(max_height, max_width)) {
    for (auto win : w) {
      if (win) delwin(win);
    }
    endwin();
    clear();
    refresh();
    initWindows();
    rWarning("resize term %dx%d", max_height, max_width);
  }
  updateTimeline();
}

void ConsoleUI::updateStatus() {
  auto write_item = [this](int y, int x, const char *key, const std::string &value, const char *unit) {
    auto win = w[Win::CarState];
    mv_add_str(win, y, x, key);
    add_str(win, value, Color::BrightWhite, true);
    add_str(win, unit);
  };

  write_item(0, 0, "SECONDS:   ", std::to_string(replay->currentSeconds()), " s     ");
  sm.update(0);
  if (sm.updated("carState")) {
    write_item(0, 25, "SPEED:   ", util::string_format("%.2f", sm["carState"].getCarState().getVEgo()), " m/s");
  }
  if (sm.updated("liveParameters")) {
    auto p = sm["liveParameters"].getLiveParameters();
    write_item(1, 0, "STIFFNESS: ", util::string_format("%.2f", p.getStiffnessFactor() * 100), " deg");
    write_item(1, 25, "STEER RATIO: ", util::string_format("%.2f", p.getSteerRatio()), "");
    auto angle_offsets = util::string_format("%.2f|%.2f", p.getAngleOffsetAverageDeg(), p.getAngleOffsetDeg());
    write_item(2, 0, "ANGLE OFFSET(AVG|INSTANCE): ", angle_offsets, " deg");
  }
  wrefresh(w[Win::CarState]);
}

void ConsoleUI::displayHelp() {
  std::initializer_list<std::pair<const char *, const char *>> single_line_keys{
      {"s", "+10s"},
      {"shift+s", "-10s"},
      {"m", "+60s"},
      {"shift+m", "+60s"},
      {"p", "Pause/Resume"},
      {"e", "Next Engmt"},
      {"d", "Next DisEngmt"},
  };
  std::initializer_list<std::pair<const char *, const char *>> multi_line_keys = {
      {"enter", "Enter seek request"},
      {"x", "Replay at full speed"},
      {"q", "Exit"},
  };

  auto write_shortcut = [=](std::string key, std::string desc) {
    add_str(w[Win::Help], ' ' + key + ' ', Color::bgWhite);
    add_str(w[Win::Help], " " + desc + " ");
  };

  for (auto [key, desc] : single_line_keys) {
    write_shortcut(key, desc);
  }
  int y = 2;
  for (auto [key, desc] : multi_line_keys) {
    wmove(w[Win::Help], y++, 0);
    write_shortcut(key, desc);
  }
  wrefresh(w[Win::Help]);
}

void ConsoleUI::displayTimelineDesc() {
  std::tuple<Color, const char *, bool> indicators[]{
      {Color::Engaged, " Engaged ", false},
      {Color::Disengaged, " Disengaged ", false},
      {Color::AlertInfo, " Info ", true},
      {Color::Warning, " Warning ", true},
      {Color::Critical, " Critical ", true},
  };
  for (auto [color, name, bold] : indicators) {
    add_str(w[Win::TimelineDesc], "__", color, bold);
    add_str(w[Win::TimelineDesc], name);
  }
}

void ConsoleUI::logMessage(int type, const QString &msg) {
  if (auto win = w[Win::Log]) {
    add_str(win, qPrintable(msg + "\n"), (Color)(type + 1));
    wrefresh(win);
  }
}

void ConsoleUI::updateProgressBar(uint64_t cur, uint64_t total, bool success) {
  werase(w[Win::DownloadBar]);
  if (success && cur < total) {
    const int width = 30;
    const float progress = cur / (double)total;
    const int pos = width * progress;
    wprintw(w[Win::DownloadBar], "Downloading [%s>%s]  %d%% %s", std::string(pos, '=').c_str(),
            std::string(width - pos, ' ').c_str(), int(progress * 100.0), formattedDataSize(total).c_str());
  }
  wrefresh(w[Win::DownloadBar]);
}

void ConsoleUI::updateSummary() {
  const auto &route = replay->route();
  mvwprintw(w[Win::Stats], 0, 0, "Route: %s, %d segments", qPrintable(route->name()), route->segments().size());
  mvwprintw(w[Win::Stats], 1, 0, "Car Fingerprint: %s", replay->carFingerprint().c_str());
  wrefresh(w[Win::Stats]);
}

void ConsoleUI::updateTimeline() {
  auto win = w[Win::Timeline];
  int width = getmaxx(win);
  werase(win);

  for (int i = 0; i < width; ++i) {
    mv_add_str(win, 1, i, ' ', Color::Disengaged);
    mv_add_str(win, 2, i, ' ', Color::Disengaged);
  }

  const int total_sec = replay->totalSeconds();
  for (auto [begin, end, type] : replay->getTimeline()) {
    int start_pos = ((double)begin / total_sec) * width;
    int end_pos = ((double)end / total_sec) * width;

    if (type == TimelineType::Engaged) {
      for (int i = start_pos; i <= end_pos; ++i) {
        mv_add_str(win, 1, i, ' ', Color::Engaged);
        mv_add_str(win, 2, i, ' ', Color::Engaged);
      }
    } else {
      auto color_id = Color::AlertInfo;
      if (type != TimelineType::AlertInfo) {
        color_id = type == TimelineType::AlertWarning ? Color::Warning : Color::Critical;
      }
      for (int i = start_pos; i <= end_pos; ++i) {
        mv_add_str(win, 3, i, ACS_S3, color_id, true);
      }
    }
  }

  int cur_pos = ((double)replay->currentSeconds() / total_sec) * width;
  mv_add_str(win, 0, cur_pos, ACS_VLINE, Color::BrightWhite);
  mv_add_str(win, 3, cur_pos, ACS_VLINE, Color::BrightWhite);

  wrefresh(win);
}

void ConsoleUI::readyRead() {
  int c;
  while ((c = getch()) != ERR) {
    handleKey(c);
  }
}

void ConsoleUI::handleKey(char c) {
  if (c == '\n') {
    // Pausing replay and blocking getchar()
    replay->pause(true);
    getch_timer.stop();
    curs_set(true);
    nodelay(stdscr, false);

    // Wait for user input
    rWarning("Waiting for input...");
    int y = getmaxy(stdscr) - 9;
    mv_add_str(stdscr, y, 3, "Enter seek request: ", Color::BrightWhite, true);
    refresh();

    // Seek to choice
    echo();
    int choice = 0;
    scanw((char *)"%d", &choice);
    noecho();
    replay->pause(false);
    replay->seekTo(choice, false);

    // Clean up and turn off the blocking mode
    move(y, 0);
    clrtoeol();
    nodelay(stdscr, true);
    curs_set(false);
    refresh();
    getch_timer.start(1000, this);

  } else if (c == 'x') {
    if (replay->hasFlag(REPLAY_FLAG_FULL_SPEED)) {
      replay->removeFlag(REPLAY_FLAG_FULL_SPEED);
      rWarning("replay at normal speed");
    } else {
      replay->addFlag(REPLAY_FLAG_FULL_SPEED);
      rWarning("replay at full speed");
    }
  } else if (c == 'e') {
    replay->seekToFlag(FindFlag::nextEngagement);
  } else if (c == 'd') {
    replay->seekToFlag(FindFlag::nextDisEngagement);
  } else if (c == 'm') {
    replay->seekTo(+60, true);
  } else if (c == 'M') {
    replay->seekTo(-60, true);
  } else if (c == 's') {
    replay->seekTo(+10, true);
  } else if (c == 'S') {
    replay->seekTo(-10, true);
  } else if (c == ' ') {
    replay->pause(!replay->isPaused());
  } else if (c == 'q' || c == 'Q') {
    replay->stop();
    qApp->exit();
  }
}
