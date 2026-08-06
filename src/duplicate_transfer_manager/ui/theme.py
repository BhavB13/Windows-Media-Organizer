"""Reusable Fluent-inspired color, spacing, typography, and theme system."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class ThemeColors:
    canvas: str
    surface: str
    surface_alt: str
    surface_hover: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    accent: str
    accent_hover: str
    accent_soft: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    focus: str


LIGHT = ThemeColors(
    canvas="#F5F7FA",
    surface="#FFFFFF",
    surface_alt="#F8FAFC",
    surface_hover="#F1F5F9",
    border="#E2E8F0",
    border_strong="#CBD5E1",
    text="#172033",
    text_muted="#667085",
    accent="#2563EB",
    accent_hover="#1D4ED8",
    accent_soft="#EAF1FF",
    success="#17803D",
    success_soft="#E9F8EF",
    warning="#A15C00",
    warning_soft="#FFF4DB",
    danger="#C43232",
    danger_soft="#FDECEC",
    focus="#3B82F6",
)

DARK = ThemeColors(
    canvas="#0F141C",
    surface="#171D27",
    surface_alt="#1C2430",
    surface_hover="#242E3C",
    border="#2A3545",
    border_strong="#3A475A",
    text="#F2F5F9",
    text_muted="#A5AFBE",
    accent="#6EA8FE",
    accent_hover="#8DBBFF",
    accent_soft="#1D3152",
    success="#62D38B",
    success_soft="#193827",
    warning="#F4C15D",
    warning_soft="#3D321C",
    danger="#FF8585",
    danger_soft="#432326",
    focus="#8DBBFF",
)


class Spacing:
    XS = 4
    SM = 8
    MD = 12
    LG = 16
    XL = 24
    XXL = 32
    XXXL = 48


class Radius:
    SM = 6
    MD = 10
    LG = 14


def _system_is_dark(application: QApplication) -> bool:
    if application.platformName().lower() in {"offscreen", "minimal"}:
        return False
    hints = application.styleHints()
    if hasattr(hints, "colorScheme"):
        return hints.colorScheme() == Qt.ColorScheme.Dark
    color = application.palette().color(QPalette.ColorRole.Window)
    return color.lightness() < 128


def build_stylesheet(colors: ThemeColors) -> str:
    return f"""
    * {{
        font-family: "Segoe UI", "Arial";
        font-size: 10pt;
        color: {colors.text};
        outline: none;
    }}
    QMainWindow, QWidget#AppRoot, QWidget#PageCanvas {{
        background: {colors.canvas};
    }}
    QFrame[card="true"] {{
        background: {colors.surface};
        border: 1px solid {colors.border};
        border-radius: {Radius.LG}px;
    }}
    QFrame[subtleCard="true"] {{
        background: {colors.surface_alt};
        border: 1px solid {colors.border};
        border-radius: {Radius.MD}px;
    }}
    QLabel[role="display"] {{
        font-size: 22pt;
        font-weight: 700;
    }}
    QLabel[role="title"] {{
        font-size: 18pt;
        font-weight: 700;
    }}
    QLabel[role="subtitle"] {{
        font-size: 11pt;
        color: {colors.text_muted};
    }}
    QLabel[role="section"] {{
        font-size: 12pt;
        font-weight: 650;
    }}
    QLabel[role="caption"], QLabel[muted="true"] {{
        color: {colors.text_muted};
        font-size: 9pt;
    }}
    QLabel[status="success"] {{ color: {colors.success}; }}
    QLabel[status="warning"] {{ color: {colors.warning}; }}
    QLabel[status="danger"] {{ color: {colors.danger}; }}
    QPushButton {{
        min-height: 34px;
        padding: 2px 14px;
        background: {colors.surface};
        border: 1px solid {colors.border_strong};
        border-radius: {Radius.SM}px;
        font-weight: 600;
    }}
    QPushButton:hover {{ background: {colors.surface_hover}; }}
    QPushButton:pressed {{ background: {colors.border}; }}
    QPushButton:focus {{ border: 2px solid {colors.focus}; }}
    QPushButton:disabled {{
        color: {colors.text_muted};
        background: {colors.surface_alt};
        border-color: {colors.border};
    }}
    QPushButton[variant="primary"] {{
        color: white;
        background: {colors.accent};
        border-color: {colors.accent};
    }}
    QPushButton[variant="primary"]:hover {{
        background: {colors.accent_hover};
        border-color: {colors.accent_hover};
    }}
    QPushButton[variant="quiet"] {{
        background: transparent;
        border-color: transparent;
    }}
    QPushButton[variant="danger"] {{
        color: {colors.danger};
        background: {colors.danger_soft};
        border-color: {colors.danger_soft};
    }}
    QPushButton[nav="true"] {{
        min-height: 40px;
        padding: 2px 12px;
        text-align: left;
        background: transparent;
        border: 1px solid transparent;
        border-radius: {Radius.MD}px;
        font-weight: 500;
    }}
    QPushButton[nav="true"]:hover {{ background: {colors.surface_hover}; }}
    QPushButton[nav="true"]:checked {{
        color: {colors.accent};
        background: {colors.accent_soft};
        font-weight: 650;
    }}
    QToolButton[sourceCard="true"] {{
        min-width: 150px;
        min-height: 92px;
        padding: 14px;
        background: {colors.surface};
        border: 1px solid {colors.border};
        border-radius: {Radius.LG}px;
        font-size: 10pt;
        font-weight: 600;
    }}
    QToolButton[sourceCard="true"]:hover {{
        background: {colors.surface_hover};
        border-color: {colors.border_strong};
    }}
    QToolButton[sourceCard="true"]:checked {{
        color: {colors.accent};
        background: {colors.accent_soft};
        border: 2px solid {colors.accent};
    }}
    QToolButton[sourceCard="true"]:focus {{
        border: 2px solid {colors.focus};
    }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
        min-height: 36px;
        padding: 0 10px;
        background: {colors.surface};
        border: 1px solid {colors.border_strong};
        border-radius: {Radius.SM}px;
        selection-background-color: {colors.accent};
        selection-color: white;
    }}
    QComboBox QAbstractItemView {{
        color: {colors.text};
        background: {colors.surface};
        border: 1px solid {colors.border_strong};
        selection-background-color: {colors.accent_soft};
        selection-color: {colors.text};
        outline: 0;
    }}
    QLineEdit:hover, QComboBox:hover {{ border-color: {colors.text_muted}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border: 2px solid {colors.focus};
    }}
    QLineEdit[invalid="true"] {{
        border: 2px solid {colors.danger};
        background: {colors.danger_soft};
    }}
    QComboBox::drop-down {{ border: none; width: 28px; }}
    QCheckBox, QRadioButton {{ spacing: 8px; min-height: 28px; }}
    QCheckBox:focus, QRadioButton:focus {{
        border: 1px solid {colors.focus};
        border-radius: 4px;
    }}
    QProgressBar {{
        min-height: 7px;
        max-height: 7px;
        border: none;
        border-radius: 3px;
        background: {colors.border};
        text-align: center;
    }}
    QProgressBar::chunk {{
        border-radius: 3px;
        background: {colors.accent};
    }}
    QTableWidget {{
        color: {colors.text};
        background: {colors.surface};
        gridline-color: {colors.border};
        border: 1px solid {colors.border};
        border-radius: {Radius.MD}px;
        selection-background-color: {colors.accent_soft};
        selection-color: {colors.text};
    }}
    QTableWidget::item {{
        padding: 6px;
        border-bottom: 1px solid {colors.border};
    }}
    QTableWidget::item:selected {{
        color: {colors.text};
        background: {colors.accent_soft};
    }}
    QHeaderView::section {{
        color: {colors.text_muted};
        background: {colors.surface_alt};
        border: none;
        border-right: 1px solid {colors.border};
        border-bottom: 1px solid {colors.border};
        padding: 6px;
        font-weight: 650;
    }}
    QTableCornerButton::section {{
        background: {colors.surface_alt};
        border: none;
        border-right: 1px solid {colors.border};
        border-bottom: 1px solid {colors.border};
    }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{
        width: 10px;
        background: transparent;
        margin: 2px;
    }}
    QScrollBar::handle:vertical {{
        min-height: 32px;
        background: {colors.border_strong};
        border-radius: 4px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QFrame#Sidebar {{
        background: {colors.surface};
        border-right: 1px solid {colors.border};
    }}
    QFrame#TopBar {{
        background: {colors.canvas};
        border-bottom: 1px solid {colors.border};
    }}
    QFrame[banner="info"] {{
        background: {colors.accent_soft};
        border: 1px solid {colors.accent};
        border-radius: {Radius.MD}px;
    }}
    QFrame[banner="success"] {{
        background: {colors.success_soft};
        border: 1px solid {colors.success};
        border-radius: {Radius.MD}px;
    }}
    QFrame[banner="warning"] {{
        background: {colors.warning_soft};
        border: 1px solid {colors.warning};
        border-radius: {Radius.MD}px;
    }}
    QFrame[banner="danger"] {{
        background: {colors.danger_soft};
        border: 1px solid {colors.danger};
        border-radius: {Radius.MD}px;
    }}
    QToolTip {{
        color: {colors.text};
        background: {colors.surface};
        border: 1px solid {colors.border_strong};
        padding: 6px;
    }}
    QGroupBox {{
        margin-top: 12px;
        padding-top: 12px;
        font-weight: 650;
        border: none;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 0;
        padding: 0 4px 0 0;
    }}
    QToolButton[step="true"] {{
        min-width: 28px;
        min-height: 28px;
        max-width: 28px;
        max-height: 28px;
        border-radius: 14px;
        border: 1px solid {colors.border_strong};
        background: {colors.surface};
        font-weight: 700;
    }}
    QToolButton[stepState="active"] {{
        color: white;
        background: {colors.accent};
        border-color: {colors.accent};
    }}
    QToolButton[stepState="complete"] {{
        color: {colors.success};
        background: {colors.success_soft};
        border-color: {colors.success};
    }}
    """


class ThemeManager(QObject):
    theme_changed = Signal(str)

    def __init__(self, application: QApplication, preference: str = "system") -> None:
        super().__init__()
        self.application = application
        self.preference = preference if preference in {"system", "light", "dark"} else "system"
        self.active_theme = "light"
        self.colors = LIGHT
        self._style_initialized = False
        hints = self.application.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._system_scheme_changed)

    def _system_scheme_changed(self, _scheme: Qt.ColorScheme) -> None:
        if self.preference == "system":
            self.apply()

    def apply(self, preference: str | None = None) -> str:
        if preference is not None:
            self.preference = preference if preference in {"system", "light", "dark"} else "system"
        self.active_theme = (
            "dark"
            if self.preference == "dark"
            or (self.preference == "system" and _system_is_dark(self.application))
            else "light"
        )
        self.colors = DARK if self.active_theme == "dark" else LIGHT
        if not self._style_initialized:
            self.application.setStyle("Fusion")
            self._style_initialized = True
        self.application.setStyleSheet(build_stylesheet(self.colors))
        palette = self.application.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(self.colors.canvas))
        palette.setColor(QPalette.ColorRole.Base, QColor(self.colors.surface))
        palette.setColor(QPalette.ColorRole.Text, QColor(self.colors.text))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(self.colors.text))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(self.colors.accent))
        self.application.setPalette(palette)
        self.theme_changed.emit(self.active_theme)
        return self.active_theme
