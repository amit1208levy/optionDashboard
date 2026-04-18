"""Palette and global stylesheet for the Options Dashboard."""

BG       = "#0b0d14"
BG_ALT   = "#0f1117"
CARD     = "#161928"
CARD_ALT = "#1a1d2e"
BORDER   = "#262a40"
BORDER_H = "#3a3f5c"
PURPLE   = "#8b5cf6"
PURPLE2  = "#7c3aed"
PURPLE3  = "#6d28d9"
ACCENT   = "#a78bfa"
TEXT     = "#e2e8f0"
TEXT_DIM = "#cbd5e1"
MUTED    = "#64748b"
LABEL    = "#94a3b8"
GREEN    = "#4ade80"
GREEN_D  = "#16a34a"
RED      = "#f87171"
RED_D    = "#dc2626"
YELLOW   = "#fbbf24"
BLUE     = "#60a5fa"
TEAL     = "#2dd4bf"

BASE_STYLE = f"""
QWidget           {{ background: {BG}; color: {TEXT};
                     font-family: -apple-system, "Helvetica Neue", "SF Pro Display", Arial, sans-serif; }}
QScrollArea       {{ border: none; background: {BG}; }}
QScrollBar:vertical   {{ background: transparent; width: 8px; border-radius: 4px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER_H}; border-radius: 4px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {MUTED}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
QTableWidget      {{ background: transparent; border: none; gridline-color: {BORDER};
                     selection-background-color: #232740; outline: 0; }}
QTableWidget::item {{ padding: 8px 12px; border: none; }}
QHeaderView::section {{ background: transparent; color: {MUTED}; border: none;
                         border-bottom: 1px solid {BORDER}; padding: 8px 12px;
                         font-size: 11px; font-weight: bold; letter-spacing: 0.5px; }}
QComboBox         {{ background: {CARD}; border: 1px solid {BORDER}; border-radius: 8px;
                     color: {TEXT}; font-size: 13px; padding: 6px 12px; }}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {BORDER};
                                color: {TEXT}; selection-background-color: {BORDER_H}; padding: 4px; }}
QLineEdit         {{ background: {BG_ALT}; border: 1px solid {BORDER}; border-radius: 8px;
                     color: {TEXT}; font-size: 14px; padding: 8px 12px; }}
QLineEdit:focus   {{ border: 1px solid {PURPLE}; }}
QPushButton       {{ background: {PURPLE2}; color: white; border: none; border-radius: 8px;
                     font-size: 13px; font-weight: bold; padding: 8px 14px; }}
QPushButton:hover {{ background: {PURPLE3}; }}
QPushButton:disabled {{ background: #374151; color: #6b7280; }}
QPushButton[flat="true"] {{ background: transparent; color: {MUTED};
                             border: 1px solid {BORDER}; }}
QPushButton[flat="true"]:hover {{ color: {TEXT}; border-color: {BORDER_H}; }}
QPushButton[ghost="true"] {{ background: transparent; color: {MUTED}; border: none;
                              padding: 4px 8px; font-weight: normal; }}
QPushButton[ghost="true"]:hover {{ color: {ACCENT}; }}
"""
