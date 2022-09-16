#define CATCH_CONFIG_RUNNER
#include <QCoreApplication>

#include "catch2/catch.hpp"
#include "selfdrive/ui/qt/widgets/offroad_alerts.h"

int main(int argc, char **argv) {
  // unit tests for Qt
  QCoreApplication app(argc, argv);
  const int res = Catch::Session().run(argc, argv);
  return (res < 0xff ? res : 0xff);
}

TEST_CASE("offroadAlerts") {
  std::vector<std::string> keys;
  for (auto k : Params().allKeys()) {
    if (k.find("Offroad_") == 0) {
      keys.push_back(k);
    }
  }

  // make sure if the keys are same.
  REQUIRE(keys.size() == OFFROAD_ALERTS.size());
  for (const auto &k : keys) {
    auto result = std::find_if(OFFROAD_ALERTS.begin(), OFFROAD_ALERTS.end(), [=](auto &v) {
        return std::get<0>(v) == k;
    });
    REQUIRE(result != OFFROAD_ALERTS.end());
  }
}
