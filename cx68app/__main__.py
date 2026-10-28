"""python3 -m cx68app: configurator and live travel HUD for the iCAN CX68."""
import signal
import sys


def main():
    from PySide6.QtWidgets import QApplication

    from . import theme
    from .main_window import MainWindow
    from .worker import start_worker

    app = QApplication(sys.argv)
    app.setApplicationName("cx68app")
    app.setDesktopFileName("cx68app")
    theme.apply(app)
    signal.signal(signal.SIGINT, signal.SIG_DFL)   # Ctrl+C in the terminal quits
    thread, worker = start_worker()
    win = MainWindow(worker)
    win.show()

    def shutdown():
        worker.stop()
        thread.quit()
        thread.wait(2000)

    app.aboutToQuit.connect(shutdown)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
