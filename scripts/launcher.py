"""Portable launcher: bundled Python and packages; no relocated venv executable."""
from __future__ import annotations
import ctypes
import importlib
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]

def bootstrap():
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    candidates = [ROOT / 'runtime/packages', ROOT / '.venv/Lib/site-packages']
    packages = next((p for p in candidates if (p / 'PySide6').is_dir()), None)
    if packages is None:
        raise RuntimeError('缺少随附依赖。请完整解压软件，不能只复制启动文件。')
    sys.path.insert(0, str(packages))
    sys.dont_write_bytecode = True

def check():
    print('离线检查运行环境（不会安装或下载任何东西）', flush=True)
    print(f'位置：{ROOT}，Python：{sys.version.split()[0]}', flush=True)
    failures = []
    for module in ('PySide6.QtWidgets', 'numpy', 'soundcard', 'soxr', 'sherpa_onnx',
                   'ctranslate2', 'faster_whisper', 'httpx', 'sentencepiece'):
        try:
            importlib.import_module(module)
            print(f'OK  {module}', flush=True)
        except Exception as exc:
            failures.append(module)
            print(f'ERR {module}: {exc}', flush=True)
    if failures:
        print('依赖不完整或系统不受支持。请查看日志并重新完整解压发行包。')
        return 1
    from jpsub.core.config import Config
    from jpsub.main import _selftest
    return _selftest(Config())

def main():
    bootstrap()
    if '--check' in sys.argv:
        return check()
    if '--foreground' in sys.argv:
        sys.argv.remove('--foreground')
        sys.argv[0] = 'jpsub'
        runpy.run_module('jpsub.main', run_name='__main__')
        return 0
    ctypes.WinDLL('kernel32').SetConsoleTitleW('译声 · 启动检查')
    importlib.import_module('PySide6.QtWidgets')
    print('正在打开软件……首次加载可能需要几秒。', flush=True)
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    with (ROOT / 'startup.log').open('a', encoding='utf-8') as log:
        child = subprocess.Popen([str(pythonw), '-X', 'utf8', str(Path(__file__).resolve()),
                                  '--foreground'], cwd=ROOT, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, creationflags=0x08000000)
    user32 = ctypes.WinDLL('user32')
    user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        hwnd = user32.FindWindowW(None, '译声实时翻译')
        if hwnd and user32.IsWindowVisible(hwnd):
            return 0
        code = child.poll()
        if code is not None:
            if code == 0:
                return 0
            break
        time.sleep(0.1)
    print(f'启动未完成。请查看 {ROOT / "startup.log"}，或使用排错模式。', flush=True)
    return 1

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
