"""Dark theme: palette constants and the application stylesheet.

Colours are defined once here and reused by the stylesheet, the graphs and the
terminal, so the whole app reads as one system rather than three widgets that
happen to share a window.
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase


class Palette:
    """Semantic colour tokens. Reference these, not raw hex, everywhere else."""

    # Surfaces, from furthest back to closest to the user.
    BG_DEEPEST = "#14171c"
    BG_BASE = "#1a1e24"
    BG_RAISED = "#22272f"
    BG_OVERLAY = "#2b313a"
    BG_HOVER = "#333a45"

    BORDER = "#333a45"
    BORDER_STRONG = "#454e5c"

    TEXT = "#e6e9ee"
    TEXT_MUTED = "#9aa4b2"
    TEXT_FAINT = "#6b7583"
    TEXT_INVERSE = "#14171c"

    ACCENT = "#4a9eff"
    ACCENT_HOVER = "#63adff"
    ACCENT_PRESSED = "#3888e6"
    ACCENT_SUBTLE = "#1e3a5c"

    SUCCESS = "#3fb950"
    WARNING = "#d29922"
    DANGER = "#f85149"
    INFO = "#58a6ff"

    # Graph series. Chosen to stay distinguishable for the common forms of
    # colour-vision deficiency, and to keep contrast on the dark surface.
    SERIES = ("#4a9eff", "#3fb950", "#d29922", "#bc8cff", "#f85149", "#39c5cf")

    GRID = "#2b313a"

    @staticmethod
    def qcolor(value: str) -> QColor:
        return QColor(value)


def monospace_font(size: int = 11) -> QFont:
    """Return the best available fixed-width font for the terminal.

    Falls back through the usual per-platform suspects before letting Qt pick,
    since a proportional fallback would wreck column alignment in CLI tables.
    """
    preferred = [
        "JetBrains Mono", "Cascadia Mono", "Cascadia Code", "SF Mono",
        "Menlo", "Consolas", "DejaVu Sans Mono", "Liberation Mono",
        "Courier New",
    ]
    families = set(QFontDatabase.families())
    for name in preferred:
        if name in families:
            font = QFont(name, size)
            font.setFixedPitch(True)
            font.setStyleHint(QFont.StyleHint.Monospace)
            return font

    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(size)
    font.setFixedPitch(True)
    return font


def ui_font(size: int = 10) -> QFont:
    """The regular interface font."""
    font = QFont()
    font.setPointSize(size)
    return font


#: Qt style sheet applied to the whole application.
STYLESHEET = f"""
/* ─── Base ──────────────────────────────────────────────────────────────── */
QWidget {{
    background-color: {Palette.BG_BASE};
    color: {Palette.TEXT};
    font-size: 13px;
}}

QMainWindow, QDialog {{
    background-color: {Palette.BG_BASE};
}}

QToolTip {{
    background-color: {Palette.BG_OVERLAY};
    color: {Palette.TEXT};
    border: 1px solid {Palette.BORDER_STRONG};
    padding: 5px 8px;
    border-radius: 4px;
}}

/* ─── Buttons ───────────────────────────────────────────────────────────── */
QPushButton {{
    background-color: {Palette.BG_OVERLAY};
    color: {Palette.TEXT};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 5px;
    padding: 6px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {Palette.BG_HOVER};
    border-color: {Palette.ACCENT};
}}
QPushButton:pressed {{
    background-color: {Palette.BG_RAISED};
}}
QPushButton:disabled {{
    background-color: {Palette.BG_RAISED};
    color: {Palette.TEXT_FAINT};
    border-color: {Palette.BORDER};
}}
QPushButton[accent="true"] {{
    background-color: {Palette.ACCENT};
    color: #ffffff;
    border-color: {Palette.ACCENT};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover {{
    background-color: {Palette.ACCENT_HOVER};
}}
QPushButton[accent="true"]:pressed {{
    background-color: {Palette.ACCENT_PRESSED};
}}
QPushButton[danger="true"] {{
    background-color: transparent;
    color: {Palette.DANGER};
    border-color: {Palette.DANGER};
}}
QPushButton[danger="true"]:hover {{
    background-color: {Palette.DANGER};
    color: #ffffff;
}}

/* ─── Text inputs ───────────────────────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {Palette.BG_DEEPEST};
    color: {Palette.TEXT};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 5px;
    padding: 5px 8px;
    selection-background-color: {Palette.ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {Palette.ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    background-color: {Palette.BG_RAISED};
    color: {Palette.TEXT_FAINT};
}}
QLineEdit[invalid="true"] {{
    border-color: {Palette.DANGER};
}}

QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {Palette.TEXT_MUTED};
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background-color: {Palette.BG_OVERLAY};
    border: 1px solid {Palette.BORDER_STRONG};
    selection-background-color: {Palette.ACCENT};
    selection-color: #ffffff;
    outline: none;
    padding: 2px;
}}

QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    background-color: {Palette.BG_OVERLAY};
    border: none;
    width: 16px;
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background-color: {Palette.BG_HOVER};
}}

/* ─── Tabs ──────────────────────────────────────────────────────────────── */
QTabWidget::pane {{
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    background-color: {Palette.BG_RAISED};
    top: -1px;
}}
QTabBar::tab {{
    background-color: transparent;
    color: {Palette.TEXT_MUTED};
    padding: 8px 18px;
    margin-right: 2px;
    border: 1px solid transparent;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:hover {{
    color: {Palette.TEXT};
    background-color: {Palette.BG_RAISED};
}}
QTabBar::tab:selected {{
    color: {Palette.ACCENT};
    border-bottom: 2px solid {Palette.ACCENT};
    font-weight: 600;
}}

/* ─── Tables ────────────────────────────────────────────────────────────── */
QTableView, QTreeView, QListView {{
    background-color: {Palette.BG_DEEPEST};
    alternate-background-color: {Palette.BG_BASE};
    color: {Palette.TEXT};
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    gridline-color: {Palette.BORDER};
    selection-background-color: {Palette.ACCENT_SUBTLE};
    selection-color: {Palette.TEXT};
    outline: none;
}}
QTableView::item, QTreeView::item, QListView::item {{
    padding: 4px 6px;
    border: none;
}}
QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
    background-color: {Palette.ACCENT_SUBTLE};
    color: {Palette.TEXT};
}}
QHeaderView::section {{
    background-color: {Palette.BG_RAISED};
    color: {Palette.TEXT_MUTED};
    padding: 7px 8px;
    border: none;
    border-right: 1px solid {Palette.BORDER};
    border-bottom: 1px solid {Palette.BORDER};
    font-weight: 600;
}}
QHeaderView::section:hover {{
    background-color: {Palette.BG_HOVER};
    color: {Palette.TEXT};
}}
QTableCornerButton::section {{
    background-color: {Palette.BG_RAISED};
    border: none;
}}

/* ─── Scrollbars ────────────────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {Palette.BORDER_STRONG};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
    background: {Palette.TEXT_FAINT};
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 0;
}}
QScrollBar::handle:horizontal {{
    background: {Palette.BORDER_STRONG};
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {Palette.TEXT_FAINT};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    height: 0; width: 0;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: none;
}}

/* ─── Group boxes ───────────────────────────────────────────────────────── */
QGroupBox {{
    border: 1px solid {Palette.BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 6px;
    color: {Palette.TEXT_MUTED};
}}

/* ─── Menus & toolbars ──────────────────────────────────────────────────── */
QMenuBar {{
    background-color: {Palette.BG_DEEPEST};
    border-bottom: 1px solid {Palette.BORDER};
    padding: 2px;
}}
QMenuBar::item {{
    padding: 6px 12px;
    background: transparent;
    border-radius: 4px;
}}
QMenuBar::item:selected {{
    background-color: {Palette.BG_OVERLAY};
}}
QMenu {{
    background-color: {Palette.BG_OVERLAY};
    border: 1px solid {Palette.BORDER_STRONG};
    border-radius: 6px;
    padding: 5px;
}}
QMenu::item {{
    padding: 7px 26px 7px 14px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {Palette.ACCENT};
    color: #ffffff;
}}
QMenu::separator {{
    height: 1px;
    background-color: {Palette.BORDER};
    margin: 5px 8px;
}}

QToolBar {{
    background-color: {Palette.BG_DEEPEST};
    border-bottom: 1px solid {Palette.BORDER};
    padding: 4px 6px;
    spacing: 4px;
}}
QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 5px 10px;
    color: {Palette.TEXT};
}}
QToolButton:hover {{
    background-color: {Palette.BG_OVERLAY};
    border-color: {Palette.BORDER_STRONG};
}}
QToolButton:pressed, QToolButton:checked {{
    background-color: {Palette.ACCENT_SUBTLE};
    border-color: {Palette.ACCENT};
}}
QToolButton:disabled {{
    color: {Palette.TEXT_FAINT};
}}

/* ─── Status bar ────────────────────────────────────────────────────────── */
QStatusBar {{
    background-color: {Palette.BG_DEEPEST};
    border-top: 1px solid {Palette.BORDER};
    color: {Palette.TEXT_MUTED};
}}
QStatusBar::item {{
    border: none;
}}

/* ─── Misc controls ─────────────────────────────────────────────────────── */
QCheckBox, QRadioButton {{
    spacing: 8px;
    padding: 2px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {Palette.BORDER_STRONG};
    background-color: {Palette.BG_DEEPEST};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 9px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {Palette.ACCENT};
}}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {Palette.ACCENT};
    border-color: {Palette.ACCENT};
}}

QSplitter::handle {{
    background-color: {Palette.BORDER};
}}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:hover {{
    background-color: {Palette.ACCENT};
}}

QProgressBar {{
    background-color: {Palette.BG_DEEPEST};
    border: 1px solid {Palette.BORDER};
    border-radius: 4px;
    text-align: center;
    height: 6px;
}}
QProgressBar::chunk {{
    background-color: {Palette.ACCENT};
    border-radius: 3px;
}}

QLabel[heading="true"] {{
    font-size: 16px;
    font-weight: 600;
    color: {Palette.TEXT};
}}
QLabel[muted="true"] {{
    color: {Palette.TEXT_MUTED};
}}
QLabel[error="true"] {{
    color: {Palette.DANGER};
}}
"""


def apply_theme(app) -> None:
    """Apply the dark theme to a ``QApplication``."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPalette

    app.setStyle("Fusion")

    # Set the Qt palette too, not just the stylesheet: native-drawn chrome such
    # as tooltips and text-cursor colours ignores QSS.
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(Palette.BG_BASE))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(Palette.TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(Palette.BG_DEEPEST))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(Palette.BG_BASE))
    palette.setColor(QPalette.ColorRole.Text, QColor(Palette.TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(Palette.BG_OVERLAY))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(Palette.TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(Palette.ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(Palette.BG_OVERLAY))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(Palette.TEXT))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(Palette.TEXT_FAINT))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,
                     QColor(Palette.TEXT_FAINT))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText,
                     QColor(Palette.TEXT_FAINT))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
    app.setFont(ui_font())
