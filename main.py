import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import openmeteo_requests
import pandas as pd
import pytz
import requests_cache
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.ticker import MaxNLocator
from retry_requests import retry


@dataclass
class PhotoMetadata:
    latitude: float
    longitude: float
    datetime_original: str


class ExifService:
    @staticmethod
    def _ratio_to_float(value):
        try:
            return float(value)
        except TypeError:
            return float(value.numerator) / float(value.denominator)

    @classmethod
    def convert_to_degrees(cls, value):
        degrees = cls._ratio_to_float(value[0])
        minutes = cls._ratio_to_float(value[1])
        seconds = cls._ratio_to_float(value[2])
        return degrees + (minutes / 60.0) + (seconds / 3600.0)

    @classmethod
    def extract_photo_metadata(cls, image_path):
        try:
            with Image.open(image_path) as image:
                exif_data = image._getexif()

            if exif_data is None:
                raise ValueError("No EXIF data found in the selected image.")

            gps_info = None
            date_time = None

            for tag, value in exif_data.items():
                tag_name = TAGS.get(tag, tag)
                if tag_name == "GPSInfo":
                    gps_info = value
                elif tag_name == "DateTimeOriginal":
                    date_time = value

            if gps_info is None:
                raise ValueError("No GPS data found in the image EXIF metadata.")

            if date_time is None:
                raise ValueError("No DateTimeOriginal found in the image EXIF metadata.")

            gps_data = {}
            for key, value in gps_info.items():
                gps_tag_name = GPSTAGS.get(key, key)
                gps_data[gps_tag_name] = value

            lat = gps_data.get("GPSLatitude")
            lat_ref = gps_data.get("GPSLatitudeRef")
            lon = gps_data.get("GPSLongitude")
            lon_ref = gps_data.get("GPSLongitudeRef")

            if lat is None or lon is None or lat_ref is None or lon_ref is None:
                raise ValueError("Incomplete GPS data in EXIF metadata.")

            latitude = cls.convert_to_degrees(lat)
            longitude = cls.convert_to_degrees(lon)

            if lat_ref != "N":
                latitude = -latitude
            if lon_ref != "E":
                longitude = -longitude

            return PhotoMetadata(
                latitude=latitude,
                longitude=longitude,
                datetime_original=date_time,
            )

        except Exception as exc:
            raise ValueError(f"Failed to read EXIF metadata: {exc}") from exc


class WeatherService:
    def __init__(self):
        cache_session = requests_cache.CachedSession(".cache", expire_after=-1)
        retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
        self.client = openmeteo_requests.Client(session=retry_session)

    def fetch_hourly_weather(self, latitude, longitude, mission_start_utc, mission_end_utc=None):
        mission_end_utc = mission_end_utc or mission_start_utc
        end_date_utc = mission_end_utc.strftime("%Y-%m-%d")
        start_date_utc = (mission_start_utc - timedelta(days=1)).strftime("%Y-%m-%d")

        url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start_date_utc,
            "end_date": end_date_utc,
            "hourly": [
                "temperature_2m",
                "relative_humidity_2m",
                "precipitation",
                "cloud_cover",
                "wind_speed_10m",
            ],
        }

        try:
            responses = self.client.weather_api(url, params=params)
            response = responses[0]
            hourly = response.Hourly()

            df = pd.DataFrame(
                {
                    "date": pd.date_range(
                        start=pd.to_datetime(hourly.Time(), unit="s", utc=True),
                        end=pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
                        freq=pd.Timedelta(seconds=hourly.Interval()),
                        inclusive="left",
                    ),
                    "temperature_2m": hourly.Variables(0).ValuesAsNumpy(),
                    "relative_humidity_2m": hourly.Variables(1).ValuesAsNumpy(),
                    "precipitation": hourly.Variables(2).ValuesAsNumpy(),
                    "cloud_cover": hourly.Variables(3).ValuesAsNumpy(),
                    "wind_speed_10m": hourly.Variables(4).ValuesAsNumpy(),
                }
            )
            return df

        except Exception as exc:
            raise RuntimeError(f"Weather request failed: {exc}") from exc


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Meteo Fetcher")
        self.setGeometry(100, 100, 1000, 700)

        self.image_path = None
        self.end_image_path = None
        self.hourly_dataframe = None
        self.flight_datetime_utc = None
        self.mission_end_datetime_utc = None

        self.exif_service = ExifService()
        self.weather_service = WeatherService()

        self.series_config = [
            ("temperature_2m", "Temperature", "Temperature (°C)"),
            ("relative_humidity_2m", "Humidity", "Humidity (%)"),
            ("cloud_cover", "Cloud Cover", "Cloud Cover (%)"),
            ("wind_speed_10m", "Wind Speed", "Wind Speed (km/h)"),
            ("precipitation", "Precipitation", "Precipitation (mm)"),
        ]

        self.plot_colors = {
            "temperature_2m": "#e76f51",
            "relative_humidity_2m": "#2a9d8f",
            "cloud_cover": "#577590",
            "wind_speed_10m": "#f4a261",
            "precipitation": "#3a86ff",
        }

        self._configure_plot_style()
        self._build_ui()

    def _build_ui(self):
        self.status_label = QLabel("Choose a picture to fetch weather data", self)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)

        self.thumbnail_label = QLabel(self)
        self.thumbnail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail_label.setMinimumHeight(220)

        self.choose_image_button = QPushButton("Choose Image", self)
        self.choose_image_button.clicked.connect(self.choose_image)

        self.choose_end_image_button = QPushButton("Choose End Image (Optional)", self)
        self.choose_end_image_button.clicked.connect(self.choose_end_image)

        self.fetch_button = QPushButton("Fetch Meteo!", self)
        self.fetch_button.clicked.connect(self.fetch_meteo)

        timezone_layout = QHBoxLayout()
        timezone_layout.addWidget(QLabel("Photo timezone:"))

        self.timezone_combo = QComboBox()
        self.timezone_combo.setEditable(True)
        self.timezone_combo.addItems(pytz.common_timezones)
        self.timezone_combo.setCurrentText("Europe/Brussels")
        self.timezone_combo.setToolTip(
            "DateTimeOriginal usually has no timezone metadata. "
            "Pick the timezone the camera/device used."
        )
        timezone_layout.addWidget(self.timezone_combo)

        self.timezone_hint_label = QLabel(
            "EXIF DateTimeOriginal often has no timezone metadata. "
            "Confirm the camera timezone before fetching weather."
        )
        self.timezone_hint_label.setWordWrap(True)
        self.timezone_hint_label.setStyleSheet("color: #475569;")

        self.metadata_label = QLabel("No image selected.")
        self.metadata_label.setWordWrap(True)
        self.metadata_label.setStyleSheet("color: #334155;")

        self.end_image_label = QLabel("No end-of-mission image selected.")
        self.end_image_label.setWordWrap(True)
        self.end_image_label.setStyleSheet("color: #334155;")

        self.tab_widget = QTabWidget()

        all_metrics_tab = QWidget()
        all_metrics_layout = QVBoxLayout(all_metrics_tab)
        self.canvas_all_metrics = FigureCanvas(plt.figure())
        all_metrics_layout.addWidget(self.canvas_all_metrics)
        all_metrics_layout.addWidget(NavigationToolbar(self.canvas_all_metrics, self))
        self.tab_widget.addTab(all_metrics_tab, "All Metrics")

        focus_tab = QWidget()
        focus_layout = QVBoxLayout(focus_tab)

        controls_layout = QHBoxLayout()
        controls_layout.addWidget(QLabel("Metric:"))

        self.focus_metric_combo = QComboBox()
        for column, title, _ in self.series_config:
            self.focus_metric_combo.addItem(title, column)
        self.focus_metric_combo.currentIndexChanged.connect(self._on_focus_metric_changed)

        controls_layout.addWidget(self.focus_metric_combo)
        controls_layout.addStretch()

        self.canvas_focus = FigureCanvas(plt.figure())
        focus_layout.addLayout(controls_layout)
        focus_layout.addWidget(self.canvas_focus)
        focus_layout.addWidget(NavigationToolbar(self.canvas_focus, self))
        self.tab_widget.addTab(focus_tab, "Focus Metric")

        controls_panel = QWidget(self)
        controls_layout = QVBoxLayout(controls_panel)
        controls_layout.addWidget(self.status_label)
        controls_layout.addWidget(self.thumbnail_label)
        controls_layout.addWidget(self.metadata_label)
        controls_layout.addWidget(self.choose_image_button)
        controls_layout.addWidget(self.end_image_label)
        controls_layout.addWidget(self.choose_end_image_button)
        controls_layout.addWidget(self.fetch_button)
        controls_layout.addLayout(timezone_layout)
        controls_layout.addWidget(self.timezone_hint_label)
        controls_layout.addStretch()

        controls_dock = QDockWidget("Photo & Controls", self)
        controls_dock.setObjectName("photoControlsDock")
        controls_dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        controls_dock.setWidget(controls_panel)
        controls_dock.setMinimumWidth(300)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, controls_dock)

        self.setCentralWidget(self.tab_widget)

        self.setStatusBar(QStatusBar(self))
        self.statusBar().showMessage("Ready")

    def _configure_plot_style(self):
        try:
            plt.style.use("seaborn-v0_8-whitegrid")
        except OSError:
            plt.style.use("ggplot")

        plt.rcParams.update(
            {
                "figure.facecolor": "#f8fafc",
                "axes.facecolor": "#fcfdff",
                "axes.edgecolor": "#cbd5e1",
                "axes.labelcolor": "#1f2937",
                "xtick.color": "#334155",
                "ytick.color": "#334155",
                "text.color": "#111827",
                "font.size": 10,
                "axes.titlesize": 11,
                "axes.labelsize": 10,
                "legend.fontsize": 9,
                "lines.linewidth": 2.2,
            }
        )

    def _style_time_axis(self, ax, show_xlabel=False):
        locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
        formatter = mdates.ConciseDateFormatter(locator)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(formatter)
        ax.grid(True, which="major", linestyle="--", alpha=0.35)
        ax.tick_params(axis="x", labelsize=9)
        ax.tick_params(axis="y", labelsize=9)
        if show_xlabel:
            ax.set_xlabel("Time (UTC)")

    def _annotate_time_window(self, ax, start_dt, end_dt, flight_dt):
        ax.axvspan(start_dt, end_dt, color="#cbd5e1", alpha=0.26)
        ax.axvline(flight_dt, color="#0f172a", linestyle="--", linewidth=1.2, alpha=0.8)

    def _annotate_mission_duration(self, ax, mission_start_dt, mission_end_dt):
        if mission_end_dt is None:
            return

        ax.axvspan(
            mission_start_dt,
            mission_end_dt,
            color="#8ecae6",
            alpha=0.28,
            label="Mission Duration",
        )
        ax.axvline(
            mission_end_dt,
            color="#1d3557",
            linestyle="-.",
            linewidth=1.2,
            alpha=0.85,
        )

    def _show_error(self, message):
        self.status_label.setText(message)
        self.statusBar().showMessage("Error")
        QMessageBox.critical(self, "Error", message)

    def _get_selected_timezone(self):
        timezone_name = self.timezone_combo.currentText().strip()
        if not timezone_name:
            raise ValueError("Please select a timezone for the photo timestamp.")

        try:
            return pytz.timezone(timezone_name), timezone_name
        except pytz.UnknownTimeZoneError as exc:
            raise ValueError(
                f"Invalid timezone: {timezone_name}. Example: Europe/Brussels"
            ) from exc

    def _set_busy(self, is_busy, message=""):
        self.fetch_button.setEnabled(not is_busy)
        self.choose_image_button.setEnabled(not is_busy)
        self.choose_end_image_button.setEnabled(not is_busy)
        self.timezone_combo.setEnabled(not is_busy)
        self.statusBar().showMessage(message if is_busy else "Ready")
        QApplication.processEvents()

    def _update_metadata_label(self, metadata=None, timezone_name=None, localized_dt=None):
        if metadata is None:
            self.metadata_label.setText("No image selected.")
            return

        lines = [
            f"Latitude: {metadata.latitude:.6f}",
            f"Longitude: {metadata.longitude:.6f}",
            f"EXIF DateTimeOriginal: {metadata.datetime_original}",
        ]

        if timezone_name and localized_dt:
            lines.append(f"Using timezone: {timezone_name}")
            lines.append(f"Localized timestamp: {localized_dt.isoformat()}")

        self.metadata_label.setText(" | ".join(lines))

    def choose_image(self):
        image_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose Image",
            "",
            "Images (*.png *.xpm *.jpg *.jpeg *.tif *.tiff)",
        )

        if not image_path:
            return

        self.image_path = image_path
        self.status_label.setText(f"Selected Image: {self.image_path}")
        self.statusBar().showMessage("Image selected")

        pixmap = QPixmap(self.image_path)
        if pixmap.isNull():
            self._show_error("The selected file could not be loaded as an image.")
            return

        scaled_pixmap = pixmap.scaled(
            240,
            240,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.thumbnail_label.setPixmap(scaled_pixmap)

        try:
            metadata = self.exif_service.extract_photo_metadata(self.image_path)
            self._update_metadata_label(metadata)
        except ValueError as exc:
            self.metadata_label.setText(str(exc))

    def choose_end_image(self):
        image_path, _ = QFileDialog.getOpenFileName(
            self,
            "Choose End Image",
            "",
            "Images (*.png *.xpm *.jpg *.jpeg *.tif *.tiff)",
        )

        if not image_path:
            return

        self.end_image_path = image_path
        self.end_image_label.setText(f"End image: {self.end_image_path}")
        self.statusBar().showMessage("End image selected")

    def fetch_meteo(self):
        if not self.image_path:
            self._show_error("Please select an image first.")
            return

        self._set_busy(True, "Reading photo metadata...")

        try:
            metadata = self.exif_service.extract_photo_metadata(self.image_path)

            local_tz, timezone_name = self._get_selected_timezone()

            photo_datetime = datetime.strptime(
                metadata.datetime_original, "%Y:%m:%d %H:%M:%S"
            )

            try:
                localized_datetime = local_tz.localize(photo_datetime, is_dst=None)
            except pytz.AmbiguousTimeError as exc:
                raise ValueError(
                    f"Ambiguous local time in {timezone_name} (DST switch). "
                    "Please verify timezone or exact capture time."
                ) from exc
            except pytz.NonExistentTimeError as exc:
                raise ValueError(
                    f"Invalid local time in {timezone_name} (DST jump). "
                    "Please verify timezone or camera clock."
                ) from exc

            self.flight_datetime_utc = localized_datetime.astimezone(pytz.utc)
            self.mission_end_datetime_utc = None

            if self.end_image_path:
                end_metadata = self.exif_service.extract_photo_metadata(self.end_image_path)
                end_photo_datetime = datetime.strptime(
                    end_metadata.datetime_original, "%Y:%m:%d %H:%M:%S"
                )

                try:
                    end_localized_datetime = local_tz.localize(end_photo_datetime, is_dst=None)
                except pytz.AmbiguousTimeError as exc:
                    raise ValueError(
                        f"Ambiguous end-image local time in {timezone_name} (DST switch). "
                        "Please verify timezone or exact capture time."
                    ) from exc
                except pytz.NonExistentTimeError as exc:
                    raise ValueError(
                        f"Invalid end-image local time in {timezone_name} (DST jump). "
                        "Please verify timezone or camera clock."
                    ) from exc

                self.mission_end_datetime_utc = end_localized_datetime.astimezone(pytz.utc)
                if self.mission_end_datetime_utc <= self.flight_datetime_utc:
                    raise ValueError(
                        "End-of-mission timestamp must be later than the mission start image timestamp."
                    )

            self.status_label.setText(
                f"Selected Image: {self.image_path} | Using timezone: {timezone_name}"
            )

            if self.mission_end_datetime_utc is not None:
                duration = self.mission_end_datetime_utc - self.flight_datetime_utc
                self.end_image_label.setText(
                    f"End image: {self.end_image_path} | Mission duration: {duration}"
                )
            else:
                self.end_image_label.setText("No end-of-mission image selected.")

            self._update_metadata_label(metadata, timezone_name, localized_datetime)

            self._set_busy(True, "Fetching weather data...")
            self.hourly_dataframe = self.weather_service.fetch_hourly_weather(
                metadata.latitude,
                metadata.longitude,
                self.flight_datetime_utc,
                self.mission_end_datetime_utc,
            )

            self.plot_weather_data(self.flight_datetime_utc)
            self.statusBar().showMessage("Weather data loaded")
            self.status_label.setText("Weather data fetched successfully.")

        except Exception as exc:
            self._show_error(str(exc))

        finally:
            self._set_busy(False)

    def plot_weather_data(self, flight_datetime):
        if self.hourly_dataframe is None or self.hourly_dataframe.empty:
            self._show_error("No weather data available to plot.")
            return

        min_date = self.hourly_dataframe["date"].min()
        max_date = flight_datetime

        self._plot_all_metrics(min_date, max_date, flight_datetime)
        self._plot_focus_metric(min_date, max_date, flight_datetime)

    def _plot_all_metrics(self, min_date, max_date, flight_datetime):
        fig = self.canvas_all_metrics.figure
        fig.clear()
        fig.set_facecolor("#f8fafc")

        metric_count = len(self.series_config)
        max_columns = 2
        ncols = min(max_columns, metric_count)
        nrows = (metric_count + ncols - 1) // ncols

        axes = fig.subplots(nrows, ncols, sharex=True)
        if hasattr(axes, "flatten"):
            axes = axes.flatten()
        else:
            axes = [axes]

        first_last_row_index = (nrows - 1) * ncols

        for index, (column, title, ylabel) in enumerate(self.series_config):
            ax = axes[index]
            color = self.plot_colors[column]

            ax.plot(self.hourly_dataframe["date"], self.hourly_dataframe[column], color=color)

            if column == "precipitation":
                ax.fill_between(
                    self.hourly_dataframe["date"],
                    self.hourly_dataframe[column],
                    color=color,
                    alpha=0.18,
                )

            self._annotate_time_window(ax, min_date, max_date, flight_datetime)
            self._annotate_mission_duration(
                ax,
                flight_datetime,
                self.mission_end_datetime_utc,
            )
            ax.set_title(title, loc="left", fontweight="bold")
            ax.set_ylabel(ylabel)
            ax.yaxis.set_major_locator(MaxNLocator(nbins=5))
            self._style_time_axis(ax, show_xlabel=index >= first_last_row_index)

        for index in range(metric_count, len(axes)):
            axes[index].axis("off")

        fig.suptitle("Hourly Weather Data Around Flight Time", fontsize=14, fontweight="bold", y=0.995)
        fig.tight_layout(rect=[0, 0, 1, 0.97], pad=1.8)
        self.canvas_all_metrics.draw_idle()

    def _plot_focus_metric(self, min_date, max_date, flight_datetime):
        fig = self.canvas_focus.figure
        fig.clear()
        fig.set_facecolor("#f8fafc")
        ax = fig.add_subplot(111)

        selected_column = self.focus_metric_combo.currentData()
        selected_config = next(
            (item for item in self.series_config if item[0] == selected_column),
            self.series_config[0],
        )

        column, title, ylabel = selected_config
        color = self.plot_colors[column]

        ax.plot(
            self.hourly_dataframe["date"],
            self.hourly_dataframe[column],
            color=color,
            label=ylabel,
        )

        if column == "precipitation":
            ax.fill_between(
                self.hourly_dataframe["date"],
                self.hourly_dataframe[column],
                color=color,
                alpha=0.2,
            )

        self._annotate_time_window(ax, min_date, max_date, flight_datetime)
        self._annotate_mission_duration(
            ax,
            flight_datetime,
            self.mission_end_datetime_utc,
        )
        ax.set_title(f"{title} Detail", fontweight="bold")
        ax.set_ylabel(ylabel)
        ax.yaxis.set_major_locator(MaxNLocator(nbins=6))
        self._style_time_axis(ax, show_xlabel=True)
        ax.legend(loc="upper left", frameon=True)

        fig.tight_layout()
        self.canvas_focus.draw_idle()

    def _on_focus_metric_changed(self):
        if self.hourly_dataframe is None or self.flight_datetime_utc is None:
            return

        min_date = self.hourly_dataframe["date"].min()
        self._plot_focus_metric(min_date, self.flight_datetime_utc, self.flight_datetime_utc)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()