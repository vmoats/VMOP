#include "tools/cabana/streams/routes.h"

#include <QDateTime>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QListWidget>
#include <QMessageBox>
#include <QPainter>

class OneShotHttpRequest : public HttpRequest {
public:
  OneShotHttpRequest(QObject *parent) : HttpRequest(parent, false) {}
  void send(const QString &url) {
    if (reply) {
      reply->disconnect();
      reply->abort();
      reply->deleteLater();
      reply = nullptr;
    }
    sendRequest(url);
  }
};

// The RouteListWidget class extends QListWidget to display a custom message when empty
class RouteListWidget : public QListWidget {
public:
  RouteListWidget(QWidget *parent = nullptr) : QListWidget(parent) {}
  void setEmptyText(const QString &text) {
    empty_text_ = text;
    viewport()->update();
  }
  void paintEvent(QPaintEvent *event) override {
    QListWidget::paintEvent(event);
    if (count() == 0) {
      QPainter painter(viewport());
      painter.drawText(viewport()->rect(), Qt::AlignCenter, empty_text_);
    }
  }
  QString empty_text_ = tr("No items");
};

RoutesDialog::RoutesDialog(QWidget *parent) : QDialog(parent), route_requester_(new OneShotHttpRequest(this)) {
  setWindowTitle(tr("Remote routes"));

  auto all_routes_widget = new QWidget;
  auto all_routes_layout = new QVBoxLayout(all_routes_widget);
  all_routes_layout->addWidget(period_selector_ = new QComboBox(this));
  all_routes_layout->addWidget(route_list_ = new RouteListWidget());

  routes_type_selector_ = new QTabWidget(this);
  routes_type_selector_->addTab(all_routes_widget, tr("&All"));
  routes_type_selector_->addTab(preserved_route_list_ = new RouteListWidget, tr("&Preserved"));

  QFormLayout *layout = new QFormLayout(this);
  layout->addRow(tr("Device"), device_list_ = new QComboBox(this));
  layout->addRow(routes_type_selector_);

  auto button_box = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
  layout->addRow(button_box);

  device_list_->addItem(tr("Loading..."));
  // Populate period selector with predefined durations
  period_selector_->addItem(tr("Last week"), 7);
  period_selector_->addItem(tr("Last 2 weeks"), 14);
  period_selector_->addItem(tr("Last month"), 30);
  period_selector_->addItem(tr("Last 6 months"), 180);

  // Connect signals and slots
  connect(device_list_, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &RoutesDialog::fetchRoutes);
  connect(period_selector_, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &RoutesDialog::fetchRoutes);
  connect(routes_type_selector_, &QTabWidget::currentChanged, this, &RoutesDialog::fetchRoutes);
  connect(route_list_, &QListWidget::itemDoubleClicked, this, &QDialog::accept);
  connect(preserved_route_list_, &QListWidget::itemDoubleClicked, this, &QDialog::accept);
  QObject::connect(button_box, &QDialogButtonBox::accepted, this, &QDialog::accept);
  QObject::connect(button_box, &QDialogButtonBox::rejected, this, &QDialog::reject);

  // Send request to fetch devices
  HttpRequest *http = new HttpRequest(this, false);
  QObject::connect(http, &HttpRequest::requestDone, this, &RoutesDialog::parseDeviceList);
  http->sendRequest(CommaApi::BASE_URL + "/v1/me/devices/");
}

void RoutesDialog::parseDeviceList(const QString &json, bool success, QNetworkReply::NetworkError err) {
  if (success) {
    device_list_->clear();
    auto devices = QJsonDocument::fromJson(json.toUtf8()).array();
    for (const QJsonValue &device : devices) {
      QString dongle_id = device["dongle_id"].toString();
      device_list_->addItem(dongle_id, dongle_id);
    }
  } else {
    bool unauthorized = (err == QNetworkReply::ContentAccessDenied || err == QNetworkReply::AuthenticationRequiredError);
    QMessageBox::warning(this, tr("Error"), unauthorized ? tr("Unauthorized, Authenticate with tools/lib/auth.py") : tr("Network error"));
    reject();
  }
  sender()->deleteLater();
}

void RoutesDialog::fetchRoutes() {
  if (device_list_->currentIndex() == -1 || device_list_->currentData().isNull())
    return;

  currentRoutesList()->clear();
  currentRoutesList()->setEmptyText(tr("Loading..."));

  // Construct URL with selected device and date range
  auto dongle_id = device_list_->currentData().toString();
  QString url = QString("%1/v1/devices/%2")
                      .arg(CommaApi::BASE_URL)
                      .arg(dongle_id);

  QObject::connect(
    route_requester_,
    &HttpRequest::requestDone,
    this,
    isPreservedTabSelected() ?  &RoutesDialog::parsePreservedRouteList : &RoutesDialog::parseRouteList,
    Qt::UniqueConnection
  );

  if(isPreservedTabSelected()) {
    url += "/routes/preserved";
  } else {
    QDateTime current = QDateTime::currentDateTime();
    url += QString("/routes_segments?start=%2&end=%3")
      .arg(current.addDays(-(period_selector_->currentData().toInt())).toMSecsSinceEpoch())
      .arg(current.toMSecsSinceEpoch());
  }

  route_requester_->sendRequest(url);
}

void RoutesDialog::onFinish(RouteListWidget *list, bool success, const QDateTime &from, const QDateTime &to, QJsonValue fullName) {
  if (success) {
    QListWidgetItem *item = new QListWidgetItem(QString("%1    %2min").arg(from.toString()).arg(from.secsTo(to) / 60));
    item->setData(Qt::UserRole, fullName.toString());
    list->addItem(item);

    // Select first route if available
    if (list->count() > 0) list->setCurrentRow(0);
  } else {
    QMessageBox::warning(this, tr("Error"), tr("Failed to fetch routes. Check your network connection."));
    reject();
  }
  list->setEmptyText(tr("No items"));
  sender()->deleteLater();
}

void RoutesDialog::parsePreservedRouteList(const QString &json, bool success, QNetworkReply::NetworkError err) {
  if (success) {
    for (const QJsonValue &route : QJsonDocument::fromJson(json.toUtf8()).array()) {
      QDateTime from = QDateTime::fromString(route["start_time"].toString(), Qt::ISODateWithMs);
      QDateTime to = QDateTime::fromString(route["end_time"].toString(), Qt::ISODateWithMs);

      onFinish(preserved_route_list_, success, from, to, route["fullname"]);
    }
  }
}

void RoutesDialog::parseRouteList(const QString &json, bool success, QNetworkReply::NetworkError err) {
  if (success) {
    for (const QJsonValue &route : QJsonDocument::fromJson(json.toUtf8()).array()) {
      QDateTime from = QDateTime::fromMSecsSinceEpoch(route["start_time_utc_millis"].toDouble());
      QDateTime to = QDateTime::fromMSecsSinceEpoch(route["end_time_utc_millis"].toDouble());

      onFinish(route_list_, success, from, to, route["fullname"]);
    }
  }
}

QString RoutesDialog::route() {
  auto current_item = route_list_->currentItem();
  return current_item ? current_item->data(Qt::UserRole).toString() : "";
}

bool RoutesDialog::isPreservedTabSelected() {
  return routes_type_selector_->currentIndex() == 1;
}

RouteListWidget* RoutesDialog::currentRoutesList() {
  return isPreservedTabSelected() ? preserved_route_list_ : route_list_;
}
