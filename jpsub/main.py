# -*- coding: utf-8 -*-
"""程序入口：python -m jpsub.main"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
import traceback
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from . import APP_NAME, __version__
from .core.config import Config
from .paths import ASSETS, CONFIG_PATH, LOG_PATH, ensure_dirs

MUTEX_NAME = "Local\\yisheng_white_release_single_instance"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9
RDW_INVALIDATE = 0x0001
RDW_ALLCHILDREN = 0x0080
RDW_UPDATENOW = 0x0100
WAIT_EXISTING_WINDOW_S = 4.0   # 已有实例还在加载时，等它把窗口显示出来的最长时间

_MUTEX_HANDLE = None           # 进程存活期间一直持有，互斥体才不会提前消失
_T_START = time.perf_counter()


def _log(msg: str) -> None:
    """追加一行启动日志（排错用），失败也不影响使用。"""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"+{time.perf_counter() - _T_START:5.2f}s  {msg}\n")
    except Exception:  # noqa: BLE001
        pass


def _existing_window() -> tuple[str, int]:
    """单实例检测。

    ("new", 0)       —— 没有别的实例，正常启动。
    ("existing", hwnd) —— 已有实例在运行，这是它的主窗口句柄。
    ("stale", 0)     —— 有互斥体但始终没有窗口（旧进程卡住了），照常启动新窗口。
    """
    global _MUTEX_HANDLE
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    _MUTEX_HANDLE = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
        return "new", 0

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    deadline = time.time() + WAIT_EXISTING_WINDOW_S
    while time.time() < deadline:
        hwnd = user32.FindWindowW(None, APP_NAME)
        if hwnd:
            return "existing", int(hwnd)
        time.sleep(0.1)
    return "stale", 0


def _bring_to_front(hwnd: int) -> None:
    """把已有窗口拉到前台并强制重绘一次（透明无边框窗口偶尔会停在空白画面）。"""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
        user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        user32.RedrawWindow(ctypes.c_void_p(hwnd), None, None,
                            RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW)
    except Exception:  # noqa: BLE001
        pass


def _refresh_window(win) -> None:
    """逼窗口把画面真正画到屏幕上：个别显卡/主题下首帧会停在空白。"""
    try:
        win.update()
        win.repaint()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RedrawWindow(ctypes.c_void_p(int(win.winId())), None, None,
                            RDW_INVALIDATE | RDW_UPDATENOW)
    except Exception:  # noqa: BLE001
        pass


def _excepthook(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 未处理异常 =====\n{text}")
    except Exception:
        pass
    sys.__excepthook__(exc_type, exc, tb)


def _report_fatal(exc_type, exc, tb) -> None:
    """启动阶段的致命错误：写日志 + 弹窗，避免双击后窗口一闪就没。"""
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动失败 =====\n{text}")
    except Exception:
        pass
    try:
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        QtWidgets.QMessageBox.critical(
            None,
            f"{APP_NAME} 启动失败",
            "程序遇到错误，无法继续运行：\n\n"
            f"{exc_type.__name__}: {exc}\n\n"
            f"完整错误信息已保存到：\n{LOG_PATH}",
        )
    except Exception:
        pass


def _run() -> int:
    try:
        return main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 - 兜底，保证错误可见
        _report_fatal(*sys.exc_info())
        return 1


def _selftest(cfg: Config) -> int:
    from .core.asr import FinalASR
    from .paths import MODELS

    checks = [
        ("日语高速识别 Parakeet", FinalASR.available("parakeet")),
        ("silero_vad.onnx", (MODELS / "silero_vad.onnx").exists()),
    ]
    for name, ok in checks:
        print(f"{'OK ' if ok else '-- '}  {name}")
    failed = not (FinalASR.available("parakeet") and (MODELS / "silero_vad.onnx").is_file())
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jpsub", description=f"{APP_NAME} v{__version__}")
    ap.add_argument("--config", type=str, default=None, help="指定配置文件路径")
    ap.add_argument("--screenshot", type=str, default=None, help="渲染主界面截图到指定路径后退出（用于自检）")
    ap.add_argument("--drawer", action="store_true", help="截图时展开设置面板")
    ap.add_argument("--drawer-scroll", type=int, default=0, help="截图前把设置面板滚动到指定像素")
    ap.add_argument("--overlay-shot", type=str, default=None, help="渲染悬浮字幕窗截图到指定路径后退出")
    ap.add_argument("--selftest", action="store_true", help="检查模型是否齐全")
    args = ap.parse_args(argv)

    ensure_dirs()
    sys.excepthook = _excepthook
    cfg = Config(Path(args.config) if args.config else CONFIG_PATH)

    if args.selftest:
        return _selftest(cfg)

    if args.screenshot is None and args.overlay_shot is None:
        state, hwnd = _existing_window()
        if state == "existing":
            _bring_to_front(hwnd)
            _log("已有实例在运行，已把它的窗口带到前台")
            print("程序已在运行，已把窗口带到前台。")
            return 0
        if state == "stale":
            _log("上一次的进程没有留下窗口（可能已卡死），按新启动处理")

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    icon_path = ASSETS / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QtGui.QIcon(str(icon_path)))

    _log(f"开始构建主窗口（v{__version__}）")
    from .ui.window import MainWindow

    win = MainWindow(cfg)
    win.show()
    _log(f"主窗口已显示（进程启动后 {time.perf_counter() - _T_START:.2f} 秒）")
    # 首帧兜底：窗口显示后再强制重绘几次，避免个别机器上停在空白画面
    for delay_ms in (0, 250, 1200):
        QtCore.QTimer.singleShot(delay_ms, lambda: _refresh_window(win))

    if args.overlay_shot:
        def _shot_overlay() -> None:
            try:
                ov = win._ensure_overlay()
                ov.set_content([
                    ("ちょっと待って、今から新しいゲームやるよ！", "等一下，我现在要开始玩新游戏了！"),
                    ("このキャラ、マジで可愛くない？", "这个角色，是不是超级可爱？"),
                ], live_index=1)
                ov.setVisible(True)

                def _grab() -> None:
                    ov.grab().save(args.overlay_shot)
                    print(f"截图已保存: {args.overlay_shot}")
                    app.quit()

                QtCore.QTimer.singleShot(600, _grab)
            except Exception as e:  # noqa: BLE001
                print(f"截图失败: {e}")
                app.quit()

        QtCore.QTimer.singleShot(900, _shot_overlay)
        return app.exec()

    if args.screenshot:
        if args.drawer:
            win.btn_settings.setChecked(True)
        if args.drawer_scroll:
            QtCore.QTimer.singleShot(
                400, lambda: win.settings.verticalScrollBar().setValue(int(args.drawer_scroll)))

        def _shot() -> None:
            try:
                pm = win.grab()
                pm.save(args.screenshot)
                print(f"截图已保存: {args.screenshot}")
            except Exception as e:  # noqa: BLE001
                print(f"截图失败: {e}")
            app.quit()

        QtCore.QTimer.singleShot(1200, _shot)

    return app.exec()


if __name__ == "__main__":
    sys.exit(_run())
