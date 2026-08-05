"""Dark theme and shared colour helpers."""

from __future__ import annotations

from PySide6.QtGui import QColor

BG = "#14161c"
BG_ALT = "#1a1d25"
BG_RAISED = "#20242e"
BORDER = "#2c313d"
FG = "#d7dce5"
FG_DIM = "#8b93a3"
ACCENT = "#4f9cf9"
ACCENT_DIM = "#2d5c96"
GOOD = "#3fb950"
BAD = "#f05d5d"
WARN = "#e3b341"

# Diverging ramp for metric heat-shading (bad -> neutral -> good).
HEAT_LOW = QColor(240, 93, 93)
HEAT_MID = QColor(60, 66, 80)
HEAT_HIGH = QColor(63, 185, 80)


def heat_color(frac: float, alpha: int = 70) -> QColor:
    """Map 0..1 onto the diverging ramp, 0.5 being neutral."""
    frac = max(0.0, min(1.0, frac))
    if frac < 0.5:
        lo, hi, t = HEAT_LOW, HEAT_MID, frac * 2.0
    else:
        lo, hi, t = HEAT_MID, HEAT_HIGH, (frac - 0.5) * 2.0
    c = QColor(
        int(lo.red() + (hi.red() - lo.red()) * t),
        int(lo.green() + (hi.green() - lo.green()) * t),
        int(lo.blue() + (hi.blue() - lo.blue()) * t),
    )
    c.setAlpha(alpha)
    return c


STYLESHEET = f"""
QWidget {{
    background: {BG};
    color: {FG};
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
    font-size: 12px;
}}
QMainWindow, QDialog {{ background: {BG}; }}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 8px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {FG_DIM};
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 1px;
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QPlainTextEdit, QTextEdit {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 6px;
    selection-background-color: {ACCENT_DIM};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QDateEdit:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM};
}}

QPushButton {{
    background: {BG_RAISED};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 6px 12px;
}}
QPushButton:hover {{ background: #2a3040; border-color: {ACCENT_DIM}; }}
QPushButton:pressed {{ background: {ACCENT_DIM}; }}
QPushButton:disabled {{ color: {FG_DIM}; background: {BG_ALT}; }}
QPushButton#primary {{
    background: {ACCENT_DIM};
    border-color: {ACCENT};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {ACCENT}; color: #0b0d12; }}
QPushButton#danger:hover {{ background: {BAD}; color: #0b0d12; }}

QHeaderView::section {{
    background: {BG_RAISED};
    color: {FG_DIM};
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    padding: 5px 6px;
    font-weight: 600;
    font-size: 11px;
}}
QHeaderView::section:hover {{ color: {FG}; }}

QTableView {{
    background: {BG_ALT};
    alternate-background-color: #171a21;
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 4px;
    selection-background-color: {ACCENT_DIM};
    selection-color: #ffffff;
}}
QTableView::item {{ padding: 2px 4px; }}

QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 4px; top: -1px; }}
QTabBar::tab {{
    background: transparent;
    color: {FG_DIM};
    padding: 7px 14px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {FG}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {FG}; }}

QScrollBar:vertical {{ background: {BG}; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #3a4152; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #4c5468; }}
QScrollBar:horizontal {{ background: {BG}; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #3a4152; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QProgressBar {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
    text-align: center;
    height: 16px;
}}
QProgressBar::chunk {{ background: {ACCENT_DIM}; border-radius: 3px; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}

QListWidget {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 4px;
}}
QListWidget::item {{ padding: 3px 5px; }}
QListWidget::item:selected {{ background: {ACCENT_DIM}; }}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 13px; height: 13px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background: {BG_ALT};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QStatusBar {{ background: {BG_RAISED}; border-top: 1px solid {BORDER}; color: {FG_DIM}; }}
QToolTip {{
    background: {BG_RAISED};
    color: {FG};
    border: 1px solid {ACCENT_DIM};
    padding: 4px;
}}
QToolBar#brandBar {{
    background: {BG_RAISED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 2px 4px;
    spacing: 0px;
}}
QToolBar#brandBar::separator {{ width: 0; height: 0; }}
/* Labels inside the toolbar otherwise paint their own panel background. */
QToolBar#brandBar QLabel {{ background: transparent; }}
QLabel#brandLogo {{ background: transparent; }}

QLabel#h1 {{ font-size: 15px; font-weight: 700; }}
QLabel#dim {{ color: {FG_DIM}; }}
QLabel#metricValue {{ font-size: 18px; font-weight: 700; }}
QLabel#metricLabel {{ color: {FG_DIM}; font-size: 10px; text-transform: uppercase;
                      letter-spacing: 0.5px; }}
QFrame#card {{
    background: {BG_ALT};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
/* The metric strip scrolls sideways; it must not paint its own panel. */
QScrollArea#statRow, QWidget#statRowInner {{ background: transparent; border: none; }}
"""
