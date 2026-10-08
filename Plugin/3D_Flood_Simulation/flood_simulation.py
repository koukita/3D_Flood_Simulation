import math
import os
import shutil
import tempfile
from urllib.parse import unquote
import uuid

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QColor, QIcon, QPainter
from qgis.PyQt.QtWidgets import (
    QDoubleSpinBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSlider,
    QToolButton,
    QWidget,
    QVBoxLayout,
)
try:
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from qgis.analysis import QgsRasterCalculator, QgsRasterCalculatorEntry
from qgis.core import (
    QgsApplication,
    Qgis,
    QgsColorRampShader,
    QgsCoordinateTransform,
    QgsCsException,
    QgsFeedback,
    QgsFeature,
    QgsFillSymbol,
    QgsGeometry,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsRasterShader,
    QgsSingleBandPseudoColorRenderer,
    QgsSingleSymbolRenderer,
    QgsTask,
    QgsVectorLayer,
)
from qgis.gui import QgsMapLayerComboBox, QgsMapToolEmitPoint

_QT_ORIENTATION = getattr(Qt, "Orientation", Qt)
_QT_DOCK_AREA = getattr(Qt, "DockWidgetArea", Qt)
_CAMERA_SETUP_RETRIES = 10
_MAX_FLOOD_RASTER_SIDE = 512
_EXTENT_UPDATE_DELAY_MS = 700
_GSI_PNG_NODATA = 1 << 23
_GSI_PNG_RGB_MAX = 1 << 24
_MAPTILER_GSI_NODATA_VALUE = -10000 + _GSI_PNG_NODATA * 0.1
_MAPTILER_GSI_RGB_RANGE = _GSI_PNG_RGB_MAX * 0.1


class _FloodRasterTask(QgsTask):
    def __init__(
        self,
        plugin,
        revision,
        source,
        provider_type,
        formula,
        output_path,
        output_extent,
        output_crs,
        output_width,
        output_height,
        transform_context,
        water_level,
    ):
        super().__init__("3D Flood Simulation: 浸水深ラスタ計算", QgsTask.CanCancel)
        self.plugin = plugin
        self.revision = revision
        self.source = source
        self.provider_type = provider_type
        self.formula = formula
        self.output_path = output_path
        self.output_extent = output_extent
        self.output_crs = output_crs
        self.output_width = output_width
        self.output_height = output_height
        self.transform_context = transform_context
        self.water_level = water_level
        self.error = None
        self.feedback = QgsFeedback()

    def run(self):
        try:
            input_layer = QgsRasterLayer(
                self.source,
                "Flood calculation DEM",
                self.provider_type,
            )
            if not input_layer.isValid() or input_layer.bandCount() < 1:
                self.error = "計算用のDEMを開けませんでした。"
                return False

            entry = QgsRasterCalculatorEntry()
            entry.ref = "dem@1"
            entry.raster = input_layer
            entry.bandNumber = 1
            calculator = QgsRasterCalculator(
                self.formula,
                self.output_path,
                "GTiff",
                self.output_extent,
                self.output_crs,
                self.output_width,
                self.output_height,
                [entry],
                self.transform_context,
            )
            calculator.setNoDataValue(-9999)
            self.feedback.progressChanged.connect(self.setProgress)
            result = calculator.processCalculation(self.feedback)
            if result != QgsRasterCalculator.Success:
                self.error = calculator.lastError() or "浸水深ラスタの計算に失敗しました。"
                return False
            return not self.isCanceled()
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            return False

    def cancel(self):
        self.feedback.cancel()
        super().cancel()

    def finished(self, result):
        self.plugin._on_flood_task_finished(self, result)


class FloodSimulationPlugin:
    """QGIS plugin for DEM-based flood depth visualization."""

    def __init__(self, iface):
        self.iface = iface
        self.project = QgsProject.instance()
        self.control_dock = None
        self.control_panel = None
        self.control_layout = None
        self.menu_action = None
        self.dem_combo = None
        self.minimum_spin = None
        self.maximum_spin = None
        self.depth_slider = None
        self.drawing_toggle = None
        self.base_value = None
        self.level_label = None
        self.capture_button = None
        self.view_3d_button = None
        self.water_layer = None
        self._water_layer_id = None
        self._water_layer_source = None
        self.water_surface_layer = None
        self.temp_dir = None
        self.map_tool = None
        self.previous_map_tool = None
        self.canvas_3d = None
        self.canvas_3d_name = None
        self._updating_range = False
        self._request_revision = 0
        self._flood_task = None
        self._rerun_after_task = False
        self._is_unloading = False
        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(150)
        self.update_timer.timeout.connect(self._update_flood_layer)
        self.extent_update_timer = QTimer()
        self.extent_update_timer.setSingleShot(True)
        self.extent_update_timer.setInterval(_EXTENT_UPDATE_DELAY_MS)
        self.extent_update_timer.timeout.connect(self._update_flood_layer)

    def initGui(self):
        self.temp_dir = tempfile.mkdtemp(prefix="qgis_3d_flood_")
        self.control_dock = QDockWidget("3D Flood Simulation", self.iface.mainWindow())
        self.control_dock.setObjectName("3D_Flood_Simulation_ControlPanel")
        self.control_panel = QWidget(self.control_dock)
        self.control_layout = QVBoxLayout(self.control_panel)
        self.control_layout.setContentsMargins(6, 4, 6, 4)
        self.control_layout.setSpacing(6)
        first_row = QHBoxLayout()
        first_row.setSpacing(6)
        slider_row = QHBoxLayout()
        slider_row.setSpacing(6)
        self.control_layout.addLayout(first_row)
        self.control_layout.addLayout(slider_row)
        self.control_dock.setWidget(self.control_panel)
        self.iface.addDockWidget(_QT_DOCK_AREA.TopDockWidgetArea, self.control_dock)

        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        self.menu_action = QAction(QIcon(icon_path), "3D Flood Simulation", self.iface.mainWindow())
        self.menu_action.triggered.connect(self._toggle_toolbar)
        self.iface.addPluginToMenu("3D Flood Simulation", self.menu_action)

        first_row.addWidget(QLabel("DEM", self.control_panel))
        self.dem_combo = QgsMapLayerComboBox(self.control_panel)
        self.dem_combo.setProject(self.project)
        self.dem_combo.setFilters(Qgis.LayerFilter.RasterLayer)
        self.dem_combo.setAllowEmptyLayer(True, "DEMを選択")
        self.dem_combo.setFixedWidth(260)
        self.dem_combo.setMinimumContentsLength(22)
        self.dem_combo.setMaxVisibleItems(20)
        first_row.addWidget(self.dem_combo)

        self.minimum_spin = self._make_depth_spin(1.0)
        self.minimum_spin.setPrefix("最小 ")
        self.minimum_spin.valueChanged.connect(self._minimum_changed)
        slider_row.addWidget(self.minimum_spin)

        self.depth_slider = QSlider(_QT_ORIENTATION.Horizontal)
        self.depth_slider.setMinimumWidth(180)
        self.depth_slider.valueChanged.connect(self._depth_changed)
        slider_row.addWidget(self.depth_slider, 1)

        self.maximum_spin = self._make_depth_spin(30.0)
        self.maximum_spin.setPrefix("最大 ")
        self.maximum_spin.valueChanged.connect(self._maximum_changed)
        slider_row.addWidget(self.maximum_spin)

        self.level_label = QLabel("水深 1.0 m / 水面標高 --")
        self.level_label.setMinimumWidth(165)
        slider_row.addWidget(self.level_label)

        self.drawing_toggle = QToolButton(self.control_panel)
        self.drawing_toggle.setCheckable(True)
        self.drawing_toggle.setChecked(False)
        self.drawing_toggle.setText("描画中")
        self.drawing_toggle.setToolTip("オンにすると浸水深ラスタの再計算を停止します。")
        self.drawing_toggle.toggled.connect(self._drawing_toggled)
        first_row.addWidget(self.drawing_toggle)

        self.base_value = QLineEdit(self.control_panel)
        self.base_value.setPlaceholderText("未取得")
        self.base_value.setFixedWidth(95)
        self.base_value.editingFinished.connect(self._base_value_edited)
        first_row.addWidget(QLabel("基準値", self.control_panel))
        first_row.addWidget(self.base_value)

        self.capture_button = QToolButton(self.control_panel)
        self.capture_button.setText("地図から標高値取得")
        self.capture_button.clicked.connect(self._start_elevation_pick)
        first_row.addWidget(self.capture_button)

        self.view_3d_button = QToolButton(self.control_panel)
        self.view_3d_button.setText("3Dビュー")
        self.view_3d_button.clicked.connect(self._toggle_3d_view)
        first_row.addWidget(self.view_3d_button)
        first_row.addStretch(1)

        self._set_slider_range()
        self.dem_combo.layerChanged.connect(self._on_dem_changed)
        self._refresh_rasters()
        self.project.layersAdded.connect(self._refresh_rasters)
        self.project.layersRemoved.connect(self._refresh_rasters)
        self.iface.currentLayerChanged.connect(self._select_active_raster)
        self.iface.mapCanvas().extentsChanged.connect(self._schedule_extent_update)
        self.control_dock.visibilityChanged.connect(self._on_panel_visibility_changed)
        self.control_dock.show()
        self.control_dock.raise_()

    @staticmethod
    def _make_depth_spin(value):
        spin = QDoubleSpinBox()
        spin.setDecimals(1)
        spin.setSingleStep(10.0)
        spin.setRange(0.1, 1000.0)
        spin.setSuffix(" m")
        spin.setValue(value)
        spin.setMaximumWidth(120)
        return spin

    def _toggle_toolbar(self):
        if self.control_dock:
            self.control_dock.setVisible(not self.control_dock.isVisible())

    def _raster_layers(self):
        return [
            layer
            for layer in self.project.mapLayers().values()
            if isinstance(layer, QgsRasterLayer)
            and layer.isValid()
            and layer.bandCount() > 0
            and not layer.customProperty("3d_flood_simulation_output", False)
        ]

    def _refresh_rasters(self, *_args):
        if not self.dem_combo:
            return
        selected = self.dem_combo.currentLayer()
        active = self.iface.activeLayer()
        excluded_layers = [
            layer
            for layer in self.project.mapLayers().values()
            if layer.customProperty("3d_flood_simulation_output", False)
        ]
        self.dem_combo.setExceptedLayerList(excluded_layers)

        available_layers = self._raster_layers()
        if selected in available_layers:
            self.dem_combo.setLayer(selected)
        elif isinstance(active, QgsRasterLayer) and active in available_layers:
            self.dem_combo.setLayer(active)
        elif available_layers:
            self.dem_combo.setLayer(available_layers[0])
        else:
            self.dem_combo.setLayer(None)
            self._message("有効な標高ラスタ（DEM）をプロジェクトに追加してください。", Qgis.Warning)

    def _select_active_raster(self, layer):
        if (
            self._panel_is_visible()
            and isinstance(layer, QgsRasterLayer)
            and self.dem_combo
        ):
            if layer in self._raster_layers():
                self.dem_combo.setLayer(layer)

    def _panel_is_visible(self):
        return bool(self.control_dock and self.control_dock.isVisible())

    def _on_panel_visibility_changed(self, visible):
        if not visible:
            self.update_timer.stop()
            self.extent_update_timer.stop()
            self._request_revision += 1
            self._rerun_after_task = False
            if self._flood_task:
                self._flood_task.cancel()
            self._finish_elevation_pick()
            return
        self._refresh_rasters()
        self._schedule_extent_update()

    def _dem_layer(self):
        layer = self.dem_combo.currentLayer() if self.dem_combo else None
        if isinstance(layer, QgsRasterLayer) and layer.isValid():
            return layer
        return None

    @staticmethod
    def _is_gsi_dem_maptile_layer(layer):
        source = unquote(layer.source()).lower()
        return (
            "cyberjapandata.gsi.go.jp" in source
            and "/xyz/dem" in source
            and "interpretation=maptilerterrain" in source
        )

    @staticmethod
    def _decode_gsi_maptile_elevation(value):
        packed_rgb = round((value + 10000.0) * 10.0)
        if packed_rgb < 0 or packed_rgb >= _GSI_PNG_RGB_MAX:
            return None
        if packed_rgb == _GSI_PNG_NODATA:
            return None
        if packed_rgb < _GSI_PNG_NODATA:
            return packed_rgb * 0.01
        return (packed_rgb - _GSI_PNG_RGB_MAX) * 0.01

    def _read_elevation_from_block(self, layer, point):
        canvas = self.iface.mapCanvas()
        canvas_settings = canvas.mapSettings()
        transform = QgsCoordinateTransform(
            canvas_settings.destinationCrs(),
            layer.crs(),
            self.project.transformContext(),
        )
        extent = transform.transformBoundingBox(canvas.extent())
        output_size = canvas_settings.outputSize()
        pixel_width = extent.width() / max(output_size.width(), 1)
        pixel_height = extent.height() / max(output_size.height(), 1)
        if (
            extent.isEmpty()
            or not math.isfinite(pixel_width)
            or not math.isfinite(pixel_height)
            or pixel_width <= 0
            or pixel_height <= 0
        ):
            return None

        sample_extent = QgsRectangle(
            point.x() - pixel_width / 2,
            point.y() - pixel_height / 2,
            point.x() + pixel_width / 2,
            point.y() + pixel_height / 2,
        )
        block = layer.dataProvider().block(1, sample_extent, 1, 1)
        if not block or not block.isValid() or block.isNoData(0, 0):
            return None
        value = block.value(0, 0)
        if not math.isfinite(value):
            return None
        if self._is_gsi_dem_maptile_layer(layer):
            return self._decode_gsi_maptile_elevation(value)
        return value

    def _minimum_changed(self, value):
        if self._updating_range:
            return
        self._updating_range = True
        if value >= self.maximum_spin.value():
            if value <= 999.8:
                self.maximum_spin.setValue(value + 0.1)
            else:
                self.minimum_spin.setValue(999.9)
        self._set_slider_range()
        self._updating_range = False

    def _maximum_changed(self, value):
        if self._updating_range:
            return
        self._updating_range = True
        if value <= self.minimum_spin.value():
            if value >= 0.2:
                self.minimum_spin.setValue(value - 0.1)
            else:
                self.maximum_spin.setValue(0.2)
        self._set_slider_range()
        self._updating_range = False

    def _set_slider_range(self):
        if not self.depth_slider:
            return
        self.depth_slider.blockSignals(True)
        self.depth_slider.setRange(
            round(self.minimum_spin.value() * 10),
            round(self.maximum_spin.value() * 10),
        )
        if self.depth_slider.value() < self.depth_slider.minimum():
            self.depth_slider.setValue(self.depth_slider.minimum())
        elif self.depth_slider.value() > self.depth_slider.maximum():
            self.depth_slider.setValue(self.depth_slider.maximum())
        self.depth_slider.blockSignals(False)
        self._depth_changed(self.depth_slider.value())

    def _depth_changed(self, slider_value):
        depth = slider_value / 10.0
        if self.level_label:
            base_text = self.base_value.text().strip() if self.base_value else ""
            try:
                level = float(base_text) + depth if base_text else None
            except ValueError:
                level = None
            if level is not None and not math.isfinite(level):
                level = None
            level_text = f"{level:.2f} m" if level is not None else "--"
            self.level_label.setText(f"水深 {depth:.1f} m / 水面標高 {level_text}")
        self._schedule_update()

    def _base_value_edited(self):
        if not self.base_value:
            return
        text = self.base_value.text().strip()
        if not text:
            self._depth_changed(self.depth_slider.value())
            return
        try:
            value = float(text)
        except ValueError:
            self._message("基準値には数値を入力してください。", Qgis.Warning)
            return
        if not math.isfinite(value):
            self._message("基準値には有限の数値を入力してください。", Qgis.Warning)
            return
        self.base_value.setText(f"{value:.3f}")
        self._depth_changed(self.depth_slider.value())

    def _on_dem_changed(self, _index):
        if not self._panel_is_visible():
            return
        if self.base_value:
            self.base_value.clear()
        if self.depth_slider:
            self._depth_changed(self.depth_slider.value())
        self._remove_water_layer()
        self._update_3d_scene()
        self._schedule_update()

    def _schedule_update(self):
        if not self._panel_is_visible() or self._drawing_stopped():
            return
        if self.base_value and self.base_value.text().strip():
            self._request_revision += 1
            self.extent_update_timer.stop()
            self.update_timer.start()

    def _schedule_extent_update(self, *_args):
        if not self._panel_is_visible() or self._drawing_stopped():
            return
        if self.base_value and self.base_value.text().strip():
            self._request_revision += 1
            self.extent_update_timer.start()

    def _drawing_stopped(self):
        return bool(self.drawing_toggle and self.drawing_toggle.isChecked())

    def _drawing_toggled(self, stopped):
        self.drawing_toggle.setText("描画停止" if stopped else "描画中")
        self.update_timer.stop()
        self.extent_update_timer.stop()
        if stopped:
            self._request_revision += 1
            if self._flood_task:
                self._flood_task.cancel()
        if not stopped:
            self._schedule_extent_update()

    def _start_elevation_pick(self):
        dem = self._dem_layer()
        if not dem:
            self._message("先にDEMラスタを選択してください。", Qgis.Warning)
            return
        if dem.bandCount() < 1:
            self._message("有効な標高ラスタを選択してください。", Qgis.Warning)
            return
        self.previous_map_tool = self.iface.mapCanvas().mapTool()
        self.map_tool = QgsMapToolEmitPoint(self.iface.mapCanvas())
        self.map_tool.canvasClicked.connect(self._read_elevation_at_point)
        self.iface.mapCanvas().setMapTool(self.map_tool)
        self.capture_button.setText("地図をクリックしてください…")
        self.capture_button.setEnabled(False)

    def _read_elevation_at_point(self, point, _button=None):
        layer = self._dem_layer()
        if not layer:
            self._finish_elevation_pick()
            self._message("選択中のDEMが無効になりました。", Qgis.Warning)
            return
        try:
            transform = QgsCoordinateTransform(
                self.iface.mapCanvas().mapSettings().destinationCrs(),
                layer.crs(),
                self.project.transformContext(),
            )
            raster_point = transform.transform(point)
            elevation = self._read_elevation_from_block(layer, raster_point)
        except (QgsCsException, RuntimeError, ValueError) as error:
            self._finish_elevation_pick()
            self._message(f"標高値を取得できませんでした: {error}", Qgis.Critical)
            return
        self._finish_elevation_pick()
        if elevation is None:
            self._message("クリック位置の標高値を読み取れないか、NoDataです。", Qgis.Warning)
            return
        self.base_value.setText(f"{elevation:.3f}")
        self._depth_changed(self.depth_slider.value())
        self._schedule_update()

    def _finish_elevation_pick(self):
        if self.map_tool and self.iface.mapCanvas().mapTool() is self.map_tool:
            if self.previous_map_tool:
                self.iface.mapCanvas().setMapTool(self.previous_map_tool)
            else:
                self.iface.mapCanvas().unsetMapTool(self.map_tool)
        self.map_tool = None
        if self.capture_button:
            self.capture_button.setText("地図から標高値取得")
            self.capture_button.setEnabled(True)

    def _update_flood_layer(self):
        if not self._panel_is_visible() or self._drawing_stopped():
            return
        if self._flood_task:
            self._rerun_after_task = True
            self._flood_task.cancel()
            return
        dem = self._dem_layer()
        base_text = self.base_value.text().strip() if self.base_value else ""
        if not dem or not base_text:
            return
        try:
            water_level = float(base_text) + self.depth_slider.value() / 10.0
        except ValueError:
            self._message("基準値が数値ではありません。", Qgis.Warning)
            return

        if dem.bandCount() < 1:
            self._message("DEMに読み取り可能なバンドがありません。", Qgis.Warning)
            return

        try:
            canvas = self.iface.mapCanvas()
            canvas_settings = canvas.mapSettings()
            canvas_extent = canvas.extent()
            if (
                canvas_extent.isEmpty()
                or canvas_extent.width() <= 0
                or canvas_extent.height() <= 0
                or not all(
                    math.isfinite(value)
                    for value in (
                        canvas_extent.xMinimum(),
                        canvas_extent.yMinimum(),
                        canvas_extent.xMaximum(),
                        canvas_extent.yMaximum(),
                    )
                )
            ):
                raise ValueError("マップキャンバスの表示範囲が無効です。")
            if canvas_settings.destinationCrs() == dem.crs():
                dem_extent = dem.extent()
                visible_extent = QgsRectangle(
                    max(canvas_extent.xMinimum(), dem_extent.xMinimum()),
                    max(canvas_extent.yMinimum(), dem_extent.yMinimum()),
                    min(canvas_extent.xMaximum(), dem_extent.xMaximum()),
                    min(canvas_extent.yMaximum(), dem_extent.yMaximum()),
                )
            else:
                to_canvas_crs = QgsCoordinateTransform(
                    dem.crs(),
                    canvas_settings.destinationCrs(),
                    self.project.transformContext(),
                )
                dem_extent_in_canvas = to_canvas_crs.transformBoundingBox(dem.extent())
                if dem_extent_in_canvas.isEmpty() or not all(
                    math.isfinite(value)
                    for value in (
                        dem_extent_in_canvas.xMinimum(),
                        dem_extent_in_canvas.yMinimum(),
                        dem_extent_in_canvas.xMaximum(),
                        dem_extent_in_canvas.yMaximum(),
                    )
                ):
                    raise ValueError("DEM範囲をマップ座標系に変換できません。")
                visible_extent = QgsRectangle(
                    max(canvas_extent.xMinimum(), dem_extent_in_canvas.xMinimum()),
                    max(canvas_extent.yMinimum(), dem_extent_in_canvas.yMinimum()),
                    min(canvas_extent.xMaximum(), dem_extent_in_canvas.xMaximum()),
                    min(canvas_extent.yMaximum(), dem_extent_in_canvas.yMaximum()),
                )
            if visible_extent.isEmpty():
                self._message("現在の表示範囲にDEMがありません。", Qgis.Warning)
                return

            if canvas_settings.destinationCrs() == dem.crs():
                output_extent = visible_extent
            else:
                to_dem_crs = QgsCoordinateTransform(
                    canvas_settings.destinationCrs(),
                    dem.crs(),
                    self.project.transformContext(),
                )
                output_extent = to_dem_crs.transformBoundingBox(visible_extent)
                dem_extent = dem.extent()
                if output_extent.isEmpty() or not all(
                    math.isfinite(value)
                    for value in (
                        output_extent.xMinimum(),
                        output_extent.yMinimum(),
                        output_extent.xMaximum(),
                        output_extent.yMaximum(),
                    )
                ):
                    raise ValueError("表示範囲をDEM座標系に変換できません。")
                output_extent = QgsRectangle(
                    max(output_extent.xMinimum(), dem_extent.xMinimum()),
                    max(output_extent.yMinimum(), dem_extent.yMinimum()),
                    min(output_extent.xMaximum(), dem_extent.xMaximum()),
                    min(output_extent.yMaximum(), dem_extent.yMaximum()),
                )

            output_size = canvas_settings.outputSize()
            canvas_width = max(output_size.width(), 1)
            canvas_height = max(output_size.height(), 1)
            visible_width_fraction = visible_extent.width() / canvas_extent.width()
            visible_height_fraction = visible_extent.height() / canvas_extent.height()
            output_width = max(1, round(canvas_width * visible_width_fraction))
            output_height = max(1, round(canvas_height * visible_height_fraction))
            longest_side = max(output_width, output_height)
            if longest_side > _MAX_FLOOD_RASTER_SIDE:
                scale = _MAX_FLOOD_RASTER_SIDE / longest_side
                output_width = max(1, round(output_width * scale))
                output_height = max(1, round(output_height * scale))
            extent_coordinates = (
                output_extent.xMinimum(),
                output_extent.yMinimum(),
                output_extent.xMaximum(),
                output_extent.yMaximum(),
            )
            if (
                output_extent.isEmpty()
                or not all(math.isfinite(value) for value in extent_coordinates)
                or output_extent.width() <= 0
                or output_extent.height() <= 0
                or output_width <= 0
                or output_height <= 0
            ):
                raise ValueError("浸水計算の範囲または画像サイズが無効です。")
        except (QgsCsException, RuntimeError, ValueError) as error:
            self._message(f"浸水計算範囲を取得できませんでした: {error}", Qgis.Critical)
            return

        if self._is_gsi_dem_maptile_layer(dem):
            encoded_elevation = '"dem@1"'
            elevation = (
                f'if({encoded_elevation} < {_MAPTILER_GSI_NODATA_VALUE:.1f}, '
                f'({encoded_elevation} + 10000) * 0.1, '
                f'({encoded_elevation} + 10000 - {_MAPTILER_GSI_RGB_RANGE:.1f}) * 0.1)'
            )
            is_valid = (
                f'abs({encoded_elevation} - {_MAPTILER_GSI_NODATA_VALUE:.1f}) > 0.1'
            )
            formula = (
                f'if({is_valid} AND ({elevation} < {water_level:.12g}), '
                f'{water_level:.12g} - {elevation}, -9999)'
            )
        else:
            formula = f'if("dem@1" < {water_level:.12g}, {water_level:.12g} - "dem@1", -9999)'
        output_path = os.path.join(self.temp_dir, f"flood_{uuid.uuid4().hex}.tif")
        task = _FloodRasterTask(
            self,
            self._request_revision,
            dem.source(),
            dem.providerType(),
            formula,
            output_path,
            output_extent,
            dem.crs(),
            output_width,
            output_height,
            self.project.transformContext(),
            water_level,
        )
        self._flood_task = task
        if not QgsApplication.taskManager().addTask(task):
            self._flood_task = None
            self._remove_temp_file(output_path)
            self._message("浸水深ラスタのバックグラウンド計算を開始できませんでした。", Qgis.Critical)

    def _on_flood_task_finished(self, task, succeeded):
        if self._flood_task is task:
            self._flood_task = None
        current_request = (
            not self._is_unloading
            and self._panel_is_visible()
            and not self._drawing_stopped()
            and task.revision == self._request_revision
        )
        if succeeded and current_request:
            self._install_flood_layer(task.output_path, task.water_level)
        else:
            self._remove_temp_file(task.output_path)
            if current_request and not task.isCanceled():
                self._message(
                    f"浸水深ラスタを計算できませんでした: {task.error or '不明な計算エラー'}",
                    Qgis.Critical,
                )

        should_rerun = self._rerun_after_task or task.revision != self._request_revision
        self._rerun_after_task = False
        if self._is_unloading:
            self._cleanup_temp_dir()
        elif (
            should_rerun
            and self._panel_is_visible()
            and not self._drawing_stopped()
            and not self.update_timer.isActive()
            and not self.extent_update_timer.isActive()
        ):
            self._schedule_extent_update()

    def _install_flood_layer(self, output_path, water_level):
        new_layer = QgsRasterLayer(output_path, f"浸水深 ({water_level:.2f} m)", "gdal")
        if not new_layer.isValid():
            self._message("計算結果のラスタを開けませんでした。", Qgis.Critical)
            self._remove_temp_file(output_path)
            return
        new_layer.setCustomProperty("3d_flood_simulation_output", True)
        self._style_flood_layer(new_layer)
        self._set_multiply_blend_mode(new_layer)

        new_surface_layer = None
        if self.canvas_3d:
            try:
                new_surface_layer = self._create_water_surface_layer(water_level)
            except (ImportError, RuntimeError, ValueError) as error:
                self._remove_water_surface_layer()
                self._message(f"3D水面を作成できませんでした: {error}", Qgis.Critical)

        old_layer_id = self._water_layer_id
        old_layer_source = self._water_layer_source
        self.water_layer = new_layer
        self._water_layer_id = new_layer.id()
        self._water_layer_source = output_path
        self.project.addMapLayer(new_layer)
        if old_layer_id and self.project.mapLayer(old_layer_id):
            self.project.removeMapLayer(old_layer_id)
            self._remove_temp_file(old_layer_source)

        if new_surface_layer:
            self._replace_water_surface_layer(new_surface_layer)
        self._update_3d_layers()

    @staticmethod
    def _set_multiply_blend_mode(layer):
        composition_modes = getattr(QPainter, "CompositionMode", QPainter)
        multiply_mode = getattr(
            composition_modes,
            "CompositionMode_Multiply",
            getattr(QPainter, "CompositionMode_Multiply", None),
        )
        if multiply_mode is None:
            raise RuntimeError("このQt環境では乗算の混合モードを利用できません。")
        layer.setBlendMode(multiply_mode)

    @staticmethod
    def _style_flood_layer(layer):
        shader_function = QgsColorRampShader()
        shader_function.setColorRampType(Qgis.ShaderInterpolationMethod.Discrete)
        shader_function.setMinimumValue(0.0)
        shader_function.setMaximumValue(999.0)
        shader_function.setClip(False)
        ramp_items = [
            QgsColorRampShader.ColorRampItem(0.0, QColor("#ffffb2"), "0 m"),
            QgsColorRampShader.ColorRampItem(0.5, QColor("#ffff00"), "0–0.5 m"),
            QgsColorRampShader.ColorRampItem(3.0, QColor("#ff9900"), "0.5–3 m"),
            QgsColorRampShader.ColorRampItem(5.0, QColor("#ff0000"), "3–5 m"),
            QgsColorRampShader.ColorRampItem(10.0, QColor("#cc00cc"), "5–10 m"),
            QgsColorRampShader.ColorRampItem(20.0, QColor("#990099"), "10–20 m"),
            QgsColorRampShader.ColorRampItem(999.0, QColor("#990099"), "20 m以上"),
        ]
        shader_function.setColorRampItemList(ramp_items)
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(shader_function)
        renderer = QgsSingleBandPseudoColorRenderer(layer.dataProvider(), 1, shader)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

    def _create_water_surface_layer(self, water_level):
        from qgis._3d import QgsPhongMaterialSettings, QgsPolygon3DSymbol, QgsVectorLayer3DRenderer

        canvas = self.iface.mapCanvas()
        canvas_settings = canvas.mapSettings()
        surface = QgsVectorLayer(
            "Polygon",
            f"3D水面 ({water_level:.2f} m)",
            "memory",
        )
        if not surface.isValid():
            raise RuntimeError("3D水面レイヤーを作成できませんでした。")
        surface.setCrs(canvas_settings.destinationCrs())
        feature = QgsFeature(surface.fields())
        feature.setGeometry(QgsGeometry.fromRect(canvas.extent()))
        if not surface.dataProvider().addFeature(feature):
            raise RuntimeError("DEM範囲の水面ポリゴンを作成できませんでした。")
        surface.updateExtents()

        surface.setCustomProperty("3d_flood_simulation_output", True)
        self._set_multiply_blend_mode(surface)
        water_color = QColor("#a6e3f5")
        fill_symbol = QgsFillSymbol.createSimple(
            {
                "color": f"{water_color.red()},{water_color.green()},{water_color.blue()},0",
                "outline_style": "no",
            }
        )
        surface.setRenderer(QgsSingleSymbolRenderer(fill_symbol))
        symbol = QgsPolygon3DSymbol()
        symbol.setAltitudeClamping(Qgis.AltitudeClamping.Absolute)
        symbol.setAltitudeBinding(Qgis.AltitudeBinding.Vertex)
        symbol.setOffset(water_level)
        material = QgsPhongMaterialSettings()
        material.setDiffuse(water_color)
        material.setAmbient(water_color.darker(120))
        material.setOpacity(0.15)
        symbol.setMaterialSettings(material)
        surface.setRenderer3D(QgsVectorLayer3DRenderer(symbol))
        surface.trigger3DUpdate()
        return surface

    def _replace_water_surface_layer(self, layer):
        old_layer = self.water_surface_layer
        self.water_surface_layer = layer
        self.project.addMapLayer(layer)
        node = self.project.layerTreeRoot().findLayer(layer.id())
        if node:
            node.setItemVisibilityChecked(True)
        if old_layer and self.project.mapLayer(old_layer.id()):
            old_source = old_layer.source()
            self.project.removeMapLayer(old_layer.id())
            self._remove_temp_file(old_source)

    def _layers_for_3d_scene(self):
        layers = [
            layer
            for layer in self.iface.mapCanvas().layers()
        ]
        if self.water_layer and self.water_layer not in layers:
            layers.append(self.water_layer)
        if self.water_surface_layer and self.water_surface_layer not in layers:
            layers.append(self.water_surface_layer)
        return layers

    def _remove_water_surface_layer(self):
        if self.water_surface_layer and self.project.mapLayer(self.water_surface_layer.id()):
            source = self.water_surface_layer.source()
            self.project.removeMapLayer(self.water_surface_layer.id())
            self.water_surface_layer = None
            self._remove_temp_file(source)
            self._update_3d_layers()

    def _remove_temp_file(self, path):
        if path and os.path.isfile(path):
            try:
                os.remove(path)
            except OSError as error:
                self._message(f"一時ラスタを削除できませんでした: {error}", Qgis.Warning)

    def _remove_water_layer(self):
        self._remove_water_surface_layer()
        layer_id = self._water_layer_id
        source = self._water_layer_source
        if layer_id and self.project.mapLayer(layer_id):
            self.project.removeMapLayer(layer_id)
        self.water_layer = None
        self._water_layer_id = None
        self._water_layer_source = None
        if source:
            self._remove_temp_file(source)
        if layer_id:
            self._update_3d_layers()

    def _toggle_3d_view(self):
        if self.canvas_3d_name:
            self._close_3d_view()
            self.view_3d_button.setText("3Dビュー")
            return
        dem = self._dem_layer()
        if not dem:
            self._message("3D表示には有効なDEMラスタが必要です。", Qgis.Warning)
            return
        try:
            from qgis._3d import QgsDemTerrainSettings
        except ImportError as error:
            self._message(f"このQGIS環境では3D表示を利用できません: {error}", Qgis.Critical)
            return

        self.canvas_3d_name = f"3D Flood Simulation {uuid.uuid4().hex[:8]}"
        try:
            self.canvas_3d = self.iface.createNewMapCanvas3D(self.canvas_3d_name)
            if not self.canvas_3d:
                raise RuntimeError("QGISが3Dキャンバスを作成できませんでした。")
            self.canvas_3d.destroyed.connect(self._on_3d_canvas_destroyed)
            settings = self.canvas_3d.mapSettings()
            if not settings:
                raise RuntimeError("3Dキャンバスの地図設定を取得できませんでした。")

            map_settings_2d = self.iface.mapCanvas().mapSettings()
            scene_crs = map_settings_2d.destinationCrs()
            terrain = QgsDemTerrainSettings()
            terrain.setLayer(dem)
            terrain.setResolution(512)
            settings.setTransformContext(self.project.transformContext())
            settings.setCrs(scene_crs)
            settings.setExtent(self.iface.mapCanvas().extent())
            settings.setTerrainSettings(terrain)
            settings.setLayers(self._layers_for_3d_scene())

            self.view_3d_button.setText("3Dビューを閉じる")
            base_text = self.base_value.text().strip() if self.base_value else ""
            if base_text:
                water_level = float(base_text) + self.depth_slider.value() / 10.0
                try:
                    surface = self._create_water_surface_layer(water_level)
                except (ImportError, RuntimeError, ValueError) as error:
                    self._message(f"3D水面を作成できませんでした: {error}", Qgis.Critical)
                else:
                    self._replace_water_surface_layer(surface)
                    self._update_3d_layers()
            QTimer.singleShot(100, lambda: self._set_3d_camera_view(_CAMERA_SETUP_RETRIES))
        except (AttributeError, RuntimeError, TypeError) as error:
            self._close_3d_view()
            self._message(f"3Dビューを初期化できませんでした: {error}", Qgis.Critical)

    def _set_3d_camera_view(self, retries_left):
        if not self.canvas_3d:
            return
        camera = self.canvas_3d.cameraController()
        if not camera:
            if retries_left > 0:
                QTimer.singleShot(
                    100,
                    lambda: self._set_3d_camera_view(retries_left - 1),
                )
            else:
                self._message("3Dカメラの初期化を確認できませんでした。", Qgis.Warning)
            return

        try:
            from qgis.core import QgsVector3D

            extent = self.iface.mapCanvas().extent()
            center = extent.center()
            focus_z = 0.0
            base_text = self.base_value.text().strip() if self.base_value else ""
            if base_text:
                focus_z = float(base_text) + self.depth_slider.value() / 10.0
            camera.setLookingAtMapPoint(
                QgsVector3D(center.x(), center.y(), focus_z),
                max(extent.width(), extent.height(), 10.0) * 1.5,
                55.0,
                0.0,
            )
        except (AttributeError, ImportError, TypeError, ValueError) as error:
            self._message(f"3Dカメラの初期位置を設定できませんでした: {error}", Qgis.Warning)

    def _close_3d_view(self):
        canvas_name = self.canvas_3d_name
        self.canvas_3d_name = None
        self.canvas_3d = None
        self._remove_water_surface_layer()
        if canvas_name:
            self.iface.closeMapCanvas3D(canvas_name)

    def _on_3d_canvas_destroyed(self, *_args):
        self.canvas_3d_name = None
        self.canvas_3d = None
        self._remove_water_surface_layer()
        if self.view_3d_button:
            self.view_3d_button.setText("3Dビュー")

    def _update_3d_scene(self):
        if not self.canvas_3d:
            return
        dem = self._dem_layer()
        if not dem:
            self._message("選択中のDEMが無効なため、3D表示を更新できません。", Qgis.Warning)
            return
        settings = self.canvas_3d.mapSettings()
        from qgis._3d import QgsDemTerrainSettings

        terrain = QgsDemTerrainSettings()
        terrain.setLayer(dem)
        terrain.setResolution(512)
        map_settings_2d = self.iface.mapCanvas().mapSettings()
        settings.setCrs(map_settings_2d.destinationCrs())
        settings.setExtent(self.iface.mapCanvas().extent())
        settings.setTerrainSettings(terrain)
        settings.setLayers(self._layers_for_3d_scene())

    def _update_3d_layers(self):
        if self.canvas_3d:
            self.canvas_3d.mapSettings().setLayers(self._layers_for_3d_scene())

    def _message(self, text, level):
        bar = self.iface.messageBar()
        if bar:
            bar.pushMessage("3D Flood Simulation", text, level=level, duration=8)

    def unload(self):
        self._is_unloading = True
        self.update_timer.stop()
        self.extent_update_timer.stop()
        if self._flood_task:
            self._flood_task.cancel()
        self._finish_elevation_pick()
        if self.menu_action:
            self.iface.removePluginMenu("3D Flood Simulation", self.menu_action)
        if self.canvas_3d_name:
            self._close_3d_view()
        self._remove_water_surface_layer()
        if self.control_dock:
            self.iface.removeDockWidget(self.control_dock)
            self.control_dock.deleteLater()
            self.control_dock = None
            self.control_panel = None
            self.control_layout = None
        if not self._flood_task:
            self._cleanup_temp_dir()
        self.project.layersAdded.disconnect(self._refresh_rasters)
        self.project.layersRemoved.disconnect(self._refresh_rasters)
        self.iface.currentLayerChanged.disconnect(self._select_active_raster)
        self.iface.mapCanvas().extentsChanged.disconnect(self._schedule_extent_update)

    def _cleanup_temp_dir(self):
        if self.temp_dir and os.path.isdir(self.temp_dir):
            if (
                self._water_layer_id
                and self.project.mapLayer(self._water_layer_id) is not None
                and self._water_layer_source
                and os.path.dirname(os.path.abspath(self._water_layer_source))
                == os.path.abspath(self.temp_dir)
            ):
                return
            try:
                shutil.rmtree(self.temp_dir)
            except OSError as error:
                self._message(f"一時フォルダーを削除できませんでした: {error}", Qgis.Warning)
