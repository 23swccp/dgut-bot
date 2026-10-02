"""Windows 原生文件选择窗口，不依赖网页服务或额外 GUI 框架。"""

from __future__ import annotations

import os
import sys


def choose_browser_file() -> str:
    """返回所选程序的完整路径；取消返回空字符串，窗口错误抛出 OSError。"""
    if sys.platform != "win32":
        return ""

    import ctypes
    from ctypes import wintypes

    class OpenFileName(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", ctypes.c_void_p),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    dialog = ctypes.WinDLL("comdlg32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetConsoleWindow.argtypes = []
    kernel.GetConsoleWindow.restype = wintypes.HWND
    dialog.GetOpenFileNameW.argtypes = [ctypes.POINTER(OpenFileName)]
    dialog.GetOpenFileNameW.restype = wintypes.BOOL
    dialog.CommDlgExtendedError.argtypes = []
    dialog.CommDlgExtendedError.restype = wintypes.DWORD

    filename = ctypes.create_unicode_buffer(32768)
    options = OpenFileName()
    options.lStructSize = ctypes.sizeof(options)
    options.hwndOwner = kernel.GetConsoleWindow()
    options.lpstrFilter = "Edge / Chrome 的 .exe 文件（推荐）\0msedge.exe;chrome.exe\0其他浏览器 .exe 文件 (*.exe)\0*.exe\0\0"
    options.nFilterIndex = 1
    options.lpstrFile = ctypes.cast(filename, wintypes.LPWSTR)
    options.nMaxFile = len(filename)
    options.lpstrInitialDir = os.environ.get("PROGRAMFILES")
    options.lpstrTitle = "请选择浏览器的 .exe 文件（如 msedge.exe 或 chrome.exe）"
    # 文件和目录必须存在；不改变进程工作目录，不添加到最近文件。
    options.Flags = 0x00001000 | 0x00000800 | 0x00000008 | 0x02000000 | 0x00080000
    if dialog.GetOpenFileNameW(ctypes.byref(options)):
        return filename.value
    error = dialog.CommDlgExtendedError()
    if error:
        raise OSError(f"文件选择窗口错误 0x{error:04X}")
    return ""
