"""Responsive main window and navigation shell."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core import AppSettings
from ..runtime_paths import RuntimePaths
from ..services import (
    DashboardService,
    DuplicateQuarantineService,
    OperationRecordService,
    ReportService,
    SettingsService,
)
from .icons import icon, icon_size
from .pages import (
    ActivityPage,
    DuplicatesPage,
    FirstRunOnboardingDialog,
    HelpPage,
    ImportPage,
    OverviewPage,
    QuarantinePage,
    SettingsPage,
)
from .theme import Spacing, ThemeManager
from .widgets import add_shortcut, set_accessible


@dataclass(frozen=True)
class Route:
    key: str
    label: str
    icon_name: str
    shortcut: str


ROUTES = (
    Route("overview", "Overview", "overview", "Alt+1"),
    Route("duplicates", "Find Duplicates", "duplicates", "Alt+2"),
    Route("import", "Import Files", "import", "Alt+3"),
    Route("activity", "Activity", "activity", "Alt+4"),
    Route("quarantine", "Quarantine", "quarantine", "Alt+5"),
    Route("settings", "Settings", "settings", "Alt+6"),
    Route("help", "Help", "help", "Alt+7"),
)


class Sidebar(QFrame):
    route_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setMinimumWidth(76)
        self.setMaximumWidth(248)
        self._collapsed = False
        self._icon_color = "#667085"
        layout = QVBoxLayout(self)
        layout.setContentsMargins(Spacing.MD, Spacing.LG, Spacing.MD, Spacing.LG)
        layout.setSpacing(Spacing.XS)

        brand = QHBoxLayout()
        brand.setSpacing(Spacing.SM)
        self.brand_mark = QLabel("D")
        self.brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_mark.setFixedSize(36, 36)
        self.brand_mark.setStyleSheet(
            "background:#2563EB;color:white;border-radius:10px;font-size:16px;font-weight:700;"
        )
        self.brand_text = QLabel("Duplicate &\nTransfer Manager")
        self.brand_text.setProperty("role", "section")
        self.brand_text.setAccessibleName("Duplicate & Transfer Manager")
        brand.addWidget(self.brand_mark)
        brand.addWidget(self.brand_text, 1)
        layout.addLayout(brand)
        layout.addSpacing(Spacing.XL)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[str, QPushButton] = {}
        self.shortcuts = []
        for index, route in enumerate(ROUTES):
            if index == 5:
                layout.addStretch(1)
            button = QPushButton(route.label)
            button.setCheckable(True)
            button.setProperty("nav", True)
            button.setIcon(icon(route.icon_name, self._icon_color))
            button.setIconSize(icon_size())
            button.setToolTip(f"{route.label}  ({route.shortcut})")
            set_accessible(
                button,
                route.label,
                f"Open {route.label}. Keyboard shortcut {route.shortcut}.",
            )
            button.clicked.connect(
                lambda checked=False, key=route.key: checked
                and self.route_requested.emit(key)
            )
            self.group.addButton(button, index)
            self.buttons[route.key] = button
            self.shortcuts.append(add_shortcut(button, route.shortcut))
            layout.addWidget(button)
        self.buttons["overview"].setChecked(True)

    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.setFixedWidth(76 if collapsed else 248)
        self.brand_text.setVisible(not collapsed)
        for route in ROUTES:
            button = self.buttons[route.key]
            button.setText("" if collapsed else route.label)
            button.setToolTip(f"{route.label}  ({route.shortcut})")

    def select(self, key: str) -> None:
        if key in self.buttons:
            self.buttons[key].setChecked(True)

    def update_icon_color(self, color: str) -> None:
        self._icon_color = color
        for route in ROUTES:
            self.buttons[route.key].setIcon(icon(route.icon_name, color))


class DeviceIndicator(QPushButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("No Android device", parent)
        self.setProperty("variant", "quiet")
        self.setIcon(icon("device"))
        self.setIconSize(icon_size())
        set_accessible(
            self,
            "Android device status",
            "No Android device is currently selected.",
        )

    def set_status(self, text: str, connected: bool = False) -> None:
        self.setText(text)
        self.setProperty("connected", connected)
        self.setAccessibleDescription(text)


class TopBar(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TopBar")
        self.setFixedHeight(68)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(Spacing.XL, 0, Spacing.XL, 0)
        self.title = QLabel("Overview")
        self.title.setProperty("role", "section")
        self.context = QLabel("Everything stays local")
        self.context.setProperty("muted", True)
        self.device = DeviceIndicator()
        self.device.hide()
        layout.addWidget(self.title)
        layout.addSpacing(Spacing.SM)
        layout.addWidget(self.context)
        layout.addStretch()
        layout.addWidget(self.device)

    def set_route(self, route: Route) -> None:
        self.title.setText(route.label)
        is_device_context = route.key in {"duplicates", "import"}
        self.context.setText(
            "Android and local sources"
            if is_device_context
            else "Everything stays local"
        )
        self.device.setVisible(is_device_context)


class MainWindow(QMainWindow):
    route_changed = Signal(str)

    def __init__(
        self,
        theme_manager: ThemeManager,
        settings: AppSettings,
        settings_service: SettingsService,
        hash_cache=None,
        runtime_paths: RuntimePaths | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.theme_manager = theme_manager
        self.settings = settings
        self.settings_service = settings_service
        self.hash_cache = hash_cache
        self.runtime_paths = runtime_paths
        self.dashboard_service = DashboardService(runtime_paths)
        self.operation_records = OperationRecordService(runtime_paths)
        self.report_service = ReportService(runtime_paths)
        self.current_route = "overview"
        self.setWindowTitle("Duplicate & Transfer Manager")
        self.setWindowIcon(icon("duplicates", "#2563EB", 32))
        self.setMinimumSize(900, 620)
        self.resize(1360, 860)
        self.setAccessibleName("Duplicate & Transfer Manager main window")

        root = QWidget()
        root.setObjectName("AppRoot")
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.sidebar = Sidebar()
        root_layout.addWidget(self.sidebar)

        workspace = QWidget()
        workspace.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        self.top_bar = TopBar()
        self.stack = QStackedWidget()
        workspace_layout.addWidget(self.top_bar)
        workspace_layout.addWidget(self.stack, 1)
        root_layout.addWidget(workspace, 1)
        self.setCentralWidget(root)

        self.pages = {
            "overview": OverviewPage(self.dashboard_service),
            "duplicates": DuplicatesPage(
                hash_cache=hash_cache,
                quarantine_service=DuplicateQuarantineService(runtime_paths),
                operation_service=self.operation_records,
                settings=settings,
            ),
            "import": ImportPage(hash_cache=hash_cache, operation_service=self.operation_records),
            "activity": ActivityPage(self.operation_records, self.report_service),
            "quarantine": QuarantinePage(DuplicateQuarantineService(runtime_paths)),
            "settings": SettingsPage(settings, settings_service, self.dashboard_service),
            "help": HelpPage(),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.sidebar.route_requested.connect(self.navigate)
        self.pages["overview"].navigate_requested.connect(self.navigate)
        self.pages["activity"].navigate_requested.connect(self.navigate)
        self.pages["quarantine"].navigate_requested.connect(self.navigate)
        self.pages["settings"].theme_requested.connect(self.set_theme)
        self.theme_manager.theme_changed.connect(self._theme_changed)
        self.navigate("overview")
        self._apply_responsive_state()
        if not settings.onboarding_completed:
            QTimer.singleShot(250, self._show_onboarding)

    def navigate(self, key: str) -> None:
        if key not in self.pages:
            return
        self.current_route = key
        self.stack.setCurrentWidget(self.pages[key])
        self.sidebar.select(key)
        route = next(route for route in ROUTES if route.key == key)
        self.top_bar.set_route(route)
        self.route_changed.emit(key)
        page = self.pages[key]
        page.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_theme(self, preference: str) -> None:
        self.settings.appearance = preference
        settings_page = self.pages["settings"]
        expected_index = settings_page.theme.findData(preference)
        if expected_index >= 0 and settings_page.theme.currentIndex() != expected_index:
            settings_page.theme.blockSignals(True)
            settings_page.theme.setCurrentIndex(expected_index)
            settings_page.theme.blockSignals(False)
        self.theme_manager.apply(preference)

    def _theme_changed(self, _active_theme: str) -> None:
        self.sidebar.update_icon_color(self.theme_manager.colors.text_muted)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_responsive_state()

    def _apply_responsive_state(self) -> None:
        self.sidebar.set_collapsed(self.width() < 1080)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.settings_service.save(self.settings)
        super().closeEvent(event)

    def _show_onboarding(self) -> None:
        dialog = FirstRunOnboardingDialog(self.settings, self.settings_service, self)
        dialog.exec()
