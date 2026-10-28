"""Dark theme: near-black surfaces, white text, one medium-light blue accent."""
from PySide6.QtGui import QColor, QPalette

BG = "#0e1014"
SURFACE = "#161920"
RAISED = "#1d2129"
BORDER = "#2a2f3a"
TEXT = "#eef1f6"
MUTED = "#8b93a3"
ACCENT = "#5aa9ff"
ACCENT_HI = "#86c0ff"
ACCENT_LO = "#2d6fbf"


def qc(hex_, alpha=255):
    c = QColor(hex_)
    c.setAlpha(alpha)
    return c


def apply(app):
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in ((QPalette.Window, BG), (QPalette.Base, SURFACE), (QPalette.AlternateBase, RAISED),
                        (QPalette.WindowText, TEXT), (QPalette.Text, TEXT), (QPalette.ButtonText, TEXT),
                        (QPalette.Button, RAISED), (QPalette.Highlight, ACCENT), (QPalette.HighlightedText, BG),
                        (QPalette.ToolTipBase, RAISED), (QPalette.ToolTipText, TEXT), (QPalette.PlaceholderText, MUTED)):
        pal.setColor(role, QColor(color))
    app.setPalette(pal)
    app.setStyleSheet(f"""
        QWidget {{ color: {TEXT}; font-size: 13px; }}
        QMainWindow, QWidget#page {{ background: {BG}; }}
        QStatusBar {{ background: {BG}; color: {MUTED}; border-top: 1px solid {BORDER}; }}
        QStatusBar::item {{ border: none; }}

        QTabWidget::pane {{ border: none; }}
        QTabBar {{ background: transparent; }}
        QTabWidget::tab-bar {{ left: 18px; }}
        QTabBar::tab {{ background: transparent; color: {MUTED}; padding: 7px 18px; margin: 10px 4px 6px 0;
                       border-radius: 15px; font-weight: 600; }}
        QTabBar::tab:hover {{ color: {TEXT}; background: {RAISED}; }}
        QTabBar::tab:selected {{ color: {BG}; background: {ACCENT}; }}

        QPushButton {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 15px; padding: 6px 16px;
                      font-weight: 600; }}
        QPushButton:hover {{ border-color: {ACCENT}; }}
        QPushButton:pressed {{ background: {SURFACE}; }}
        QPushButton:checked {{ background: {ACCENT}; color: {BG}; border-color: {ACCENT}; }}
        QPushButton#primary {{ background: {ACCENT}; color: {BG}; border-color: {ACCENT}; }}
        QPushButton#primary:hover {{ background: {ACCENT_HI}; }}
        QPushButton#primary:disabled {{ color: #4b5260; background: {SURFACE}; border-color: {SURFACE}; }}

        QComboBox {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 15px; padding: 5px 14px; }}
        QComboBox:hover {{ border-color: {ACCENT}; }}
        QComboBox::drop-down {{ border: none; width: 22px; }}
        QComboBox QAbstractItemView {{ background: {RAISED}; border: 1px solid {BORDER}; selection-background-color: {ACCENT};
                                       selection-color: {BG}; outline: none; }}

        QDoubleSpinBox {{ background: {RAISED}; border: 1px solid {BORDER}; border-radius: 10px; padding: 5px 8px;
                          min-width: 110px; }}
        QDoubleSpinBox:hover, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
        QDoubleSpinBox:disabled {{ color: #4b5260; background: {SURFACE}; }}
        QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 18px; border: none; background: transparent; }}
        QPushButton:disabled {{ color: #4b5260; background: {SURFACE}; border-color: {SURFACE}; }}
        QFormLayout QLabel, QLabel {{ background: transparent; }}

        QCheckBox {{ spacing: 8px; color: {MUTED}; }}
        QCheckBox:checked {{ color: {TEXT}; }}
        QCheckBox::indicator {{ width: 34px; height: 18px; border-radius: 9px; background: {RAISED}; border: 1px solid {BORDER}; }}
        QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

        QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 16px; }}
        QLabel#cardValue {{ font-size: 24px; font-weight: 700; color: {TEXT}; }}
        QLabel#cardUnit {{ color: {MUTED}; font-size: 12px; }}
        QLabel#cardTitle {{ color: {MUTED}; font-size: 11px; font-weight: 600; letter-spacing: 1px; }}
        QLabel#muted {{ color: {MUTED}; }}
        QLabel#heading {{ font-size: 20px; font-weight: 700; }}

        QPlainTextEdit {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 14px; padding: 10px;
                         font-family: monospace; }}
        QToolTip {{ background: {RAISED}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 8px; padding: 6px; }}
    """)
