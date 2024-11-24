#include "tools/cabana/streams/routes.h"

#include <QDateTime>
#include <QDialogButtonBox>
#include <QTabWidget>
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
  auto all_routes_layout = new QVBoxLayout;
  all_routes_layout->addWidget(period_selector_ = new QComboBox(this));
  all_routes_layout->addWidget(route_list_ = new RouteListWidget(all_routes_widget));
  all_routes_widget->setLayout(all_routes_layout);

  auto preserved_routes_widget = new QWidget;
  auto preserved_layout = new QVBoxLayout;
  preserved_layout->addWidget(preserved_route_list_ = new RouteListWidget(all_routes_widget));
  preserved_routes_widget->setLayout(preserved_layout);

  routes_type_selector_ = new QTabWidget(this);
  routes_type_selector_->addTab(all_routes_widget, tr("&All"));
  routes_type_selector_->addTab(preserved_routes_widget, tr("&Preserved"));

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
  QObject::connect(route_requester_, &HttpRequest::requestDone, this, &RoutesDialog::parseRouteList);
  connect(device_list_, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &RoutesDialog::fetchRoutes);
  connect(period_selector_, QOverload<int>::of(&QComboBox::currentIndexChanged), this, &RoutesDialog::fetchRoutes);
  connect(routes_type_selector_, QOverload<int>::of(&QTabWidget::currentChanged), this, &RoutesDialog::fetchRoutes);
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

  QObject::connect(http, &HttpRequest::requestDone, this, &RoutesDialog::parseRouteList);

  // Construct URL with selected device and date range
  auto dongle_id = device_list_->currentData().toString();
  QDateTime current = QDateTime::currentDateTime();
  QString url = QString("%1/v1/devices/%2/routes_segments?start=%3&end=%4")
                    .arg(CommaApi::BASE_URL).arg(dongle_id)
                    .arg(current.addDays(-(period_selector_->currentData().toInt())).toMSecsSinceEpoch())
                    .arg(current.toMSecsSinceEpoch());
  route_requester_->sendRequest(url);
  if(isPreservedTabSelected()) {
    QString url = QString("%1/v1/devices/%2/routes/preserved")
                      .arg(CommaApi::BASE_URL)
                      .arg(dongle_id);
    http->sendRequest(url);
  } else {
    QDateTime current = QDateTime::currentDateTime();
    QString url = QString("%1/v1/devices/%2/routes_segments?start=%3&end=%4")
                      .arg(CommaApi::BASE_URL)
                      .arg(dongle_id)
                      .arg(current.addDays(-(period_selector_->currentData().toInt())).toMSecsSinceEpoch())
                      .arg(current.toMSecsSinceEpoch());
    route_requester_->sendRequest(url);
  }
}

void RoutesDialog::parseRouteList(const QString &json, bool success, QNetworkReply::NetworkError err) {
  if (success) {
    for (const QJsonValue &route : QJsonDocument::fromJson(json.toUtf8()).array()) {
      uint64_t to;
      QDateTime from;

      if(isPreservedTabSelected()) {
        QString start_time = route["start_time"].toString();
        QString end_time = route["end_time"].toString();

        from = QDateTime::fromString(start_time, Qt::ISODateWithMs);
        to = QDateTime::fromString(end_time, Qt::ISODateWithMs).msecsTo(to);
      } else {
        uint64_t start_time = route["start_time_utc_millis"].toDouble();
        uint64_t end_time = route["end_time_utc_millis"].toDouble();
        from = QDateTime::fromMSecsSinceEpoch(start_time);
        to = (end_time - start_time);
      }

      QListWidgetItem *item = new QListWidgetItem(QString("%1    %2min").arg(from.toString()).arg(to / (1000 * 60)));
      item->setData(Qt::UserRole, route["fullname"].toString());
      currentRoutesList()->addItem(item);
    }
    // Select first route if available
    if (currentRoutesList()->count() > 0) currentRoutesList()->setCurrentRow(0);
  } else {
    QMessageBox::warning(this, tr("Error"), tr("Failed to fetch routes. Check your network connection."));
    reject();
  }
  currentRoutesList()->setEmptyText(tr("No items"));
  sender()->deleteLater();
}

void RoutesDialog::accept() {
  if (auto current_item = currentRoutesList()->currentItem()) {
    route_ = current_item->data(Qt::UserRole).toString();
  }
  QDialog::accept();
}

RouteListWidget* RoutesDialog::currentRoutesList() {
  if(routes_type_selector_->currentIndex() == 1) {
    return preserved_route_list_;
  }

  return route_list_;
}

bool RoutesDialog::isPreservedTabSelected() {
  return routes_type_selector_->currentIndex() == 1;
}
