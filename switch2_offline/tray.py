# SPDX-License-Identifier: GPL-2.0-or-later
"""Notification-area (tray) icon, using only ctypes.

A hidden window on its own thread owns the icon and receives its clicks.
The callbacks run on that thread, so the GUI only queues events from them.
"""
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes as w
from pathlib import Path

CLASS_NAME = "Switch2OfflineEthernetTray"
WM_DESTROY, WM_CLOSE, WM_NULL, WM_APP = 0x0002, 0x0010, 0x0000, 0x8000
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
CALLBACK = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MENU_OPEN, MENU_EXIT = 1, 2

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                ("lpszClassName", w.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", w.HICON)]


def _api():
    """Private copies of the DLLs, so these signatures don't affect ctypes.windll."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = [
        (user32.DefWindowProcW, LRESULT, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
        (user32.RegisterClassW, w.ATOM, [ctypes.POINTER(WNDCLASSW)]),
        (user32.UnregisterClassW, w.BOOL, [w.LPCWSTR, w.HINSTANCE]),
        (user32.CreateWindowExW, w.HWND, [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND,
                                          w.HMENU, w.HINSTANCE, w.LPVOID]),
        (user32.DestroyWindow, w.BOOL, [w.HWND]),
        (user32.FindWindowW, w.HWND, [w.LPCWSTR, w.LPCWSTR]),
        (user32.PostMessageW, w.BOOL, [w.HWND, w.UINT, w.WPARAM, w.LPARAM]),
        (user32.PostQuitMessage, None, [ctypes.c_int]),
        (user32.GetMessageW, w.BOOL, [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]),
        (user32.TranslateMessage, w.BOOL, [ctypes.POINTER(w.MSG)]),
        (user32.DispatchMessageW, LRESULT, [ctypes.POINTER(w.MSG)]),
        (user32.RegisterWindowMessageW, w.UINT, [w.LPCWSTR]),
        (user32.GetSystemMetrics, ctypes.c_int, [ctypes.c_int]),
        (user32.LoadImageW, w.HANDLE, [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]),
        (user32.LoadIconW, w.HICON, [w.HINSTANCE, w.LPVOID]),
        (user32.DestroyIcon, w.BOOL, [w.HICON]),
        (user32.CreatePopupMenu, w.HMENU, []),
        (user32.AppendMenuW, w.BOOL, [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]),
        (user32.SetMenuDefaultItem, w.BOOL, [w.HMENU, w.UINT, w.UINT]),
        (user32.TrackPopupMenu, ctypes.c_int, [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, w.HWND, w.LPVOID]),
        (user32.DestroyMenu, w.BOOL, [w.HMENU]),
        (user32.GetCursorPos, w.BOOL, [ctypes.POINTER(w.POINT)]),
        (user32.SetForegroundWindow, w.BOOL, [w.HWND]),
        (user32.MessageBoxW, ctypes.c_int, [w.HWND, w.LPCWSTR, w.LPCWSTR, w.UINT]),
        (user32.ChangeWindowMessageFilterEx, w.BOOL, [w.HWND, w.UINT, w.DWORD, w.LPVOID]),
        (shell32.Shell_NotifyIconW, w.BOOL, [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]),
        (kernel32.GetModuleHandleW, w.HMODULE, [w.LPCWSTR]),
    ]
    for func, restype, argtypes in signatures:
        func.restype, func.argtypes = restype, argtypes
    return user32, shell32, kernel32


class TrayIcon:
    """show() adds the icon, hide() removes it. A left click calls on_open();
    the right-click menu offers Open and Exit (on_exit())."""

    def __init__(self, tooltip: str, icon: Path, on_open, on_exit):
        self.tooltip, self.icon, self.on_open, self.on_exit = tooltip, icon, on_open, on_exit
        self.hwnd = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.user32, self.shell32, self.kernel32 = _api()

    @property
    def visible(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def show(self) -> bool:
        if self.visible:
            return True
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return self.hwnd is not None

    def hide(self) -> None:
        if self.hwnd:
            self.user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self._thread:
            self._thread.join(2)

    def set_tooltip(self, text: str) -> None:
        with self._lock:
            self.tooltip = text[:127]
            if self.hwnd:
                self._notify(NIM_MODIFY)

    # ------------------------------------------------------- tray thread
    def _notify(self, action: int) -> bool:
        data = NOTIFYICONDATAW(cbSize=ctypes.sizeof(NOTIFYICONDATAW), hWnd=self.hwnd, uID=1,
                               uFlags=NIF_MESSAGE | NIF_ICON | NIF_TIP,
                               uCallbackMessage=CALLBACK, hIcon=self._hicon, szTip=self.tooltip)
        return bool(self.shell32.Shell_NotifyIconW(action, ctypes.byref(data)))

    def _run(self) -> None:
        u = self.user32
        hinstance = self.kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._window_proc)  # keep a reference: Windows calls it
        u.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=self._proc, hInstance=hinstance,
                                                lpszClassName=CLASS_NAME)))
        self._taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")
        size = u.GetSystemMetrics(49)  # SM_CXSMICON
        self._own_icon = u.LoadImageW(None, str(self.icon), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
        self._hicon = self._own_icon or u.LoadIconW(None, ctypes.c_void_p(32512))  # IDI_APPLICATION
        # A hidden top-level window (not message-only) also gets TaskbarCreated
        # when Explorer restarts, so the icon can be added again.
        hwnd = u.CreateWindowExW(0, CLASS_NAME, CLASS_NAME, 0, 0, 0, 0, 0, None, None, hinstance, None)
        if hwnd:
            # The exe runs elevated; let Explorer's clicks and TaskbarCreated through.
            for message_id in (CALLBACK, self._taskbar_created):
                u.ChangeWindowMessageFilterEx(hwnd, message_id, 1, None)  # MSGFLT_ALLOW
            with self._lock:
                self.hwnd = hwnd
                self._notify(NIM_ADD)
        self._ready.set()
        msg = w.MSG()
        while hwnd and u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        if self._own_icon:
            u.DestroyIcon(self._own_icon)
        u.UnregisterClassW(CLASS_NAME, hinstance)

    def _window_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == CALLBACK:
                event = lparam & 0xFFFF
                if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.on_open()
                elif event == WM_RBUTTONUP:
                    self._menu(hwnd)
                return 0
            if msg == self._taskbar_created:
                with self._lock:
                    self._notify(NIM_ADD)
                return 0
            if msg == WM_CLOSE:
                self.user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                with self._lock:
                    self._notify(NIM_DELETE)
                    self.hwnd = None
                self.user32.PostQuitMessage(0)
                return 0
        except Exception:  # never let an exception cross into Windows
            pass
        return self.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _menu(self, hwnd) -> None:
        u = self.user32
        menu = u.CreatePopupMenu()
        u.AppendMenuW(menu, 0, MENU_OPEN, "Open")
        u.AppendMenuW(menu, 0x800, 0, None)  # MF_SEPARATOR
        u.AppendMenuW(menu, 0, MENU_EXIT, "Exit…")
        u.SetMenuDefaultItem(menu, MENU_OPEN, 0)
        point = w.POINT()
        u.GetCursorPos(ctypes.byref(point))
        u.SetForegroundWindow(hwnd)  # so the menu closes when clicking elsewhere
        choice = u.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, hwnd, None)  # RETURNCMD|RIGHTBUTTON
        u.PostMessageW(hwnd, WM_NULL, 0, 0)
        u.DestroyMenu(menu)
        if choice == MENU_OPEN:
            self.on_open()
        elif choice == MENU_EXIT:
            self.on_exit()


def open_running_copy() -> bool:
    """Ask a copy that is hidden in the tray to show its window."""
    user32, _, _ = _api()
    hwnd = user32.FindWindowW(CLASS_NAME, None)
    return bool(hwnd and user32.PostMessageW(hwnd, CALLBACK, 0, WM_LBUTTONUP))


def message(title: str, text: str) -> None:
    _api()[0].MessageBoxW(None, text, title, 0x40)  # MB_ICONINFORMATION
