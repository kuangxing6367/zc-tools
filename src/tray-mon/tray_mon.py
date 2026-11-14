#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
tray_mon.py - 任务栏 / 托盘实时性能监控（Windows 7+，32/64 位，Python 3.7+）

把 CPU、内存、网络速率、磁盘占用实时贴在 Windows 任务栏右下角（和系统时钟
并排显示），并在通知区域放一个百分比托盘图标与右键菜单。形态参考
TrafficMonitor：不占桌面、不弹窗、常驻任务栏，眼角扫一下就知道机器状态。

用法
  pythonw.exe tray_mon.py              正常启动（无控制台窗口）
  python.exe  tray_mon.py --probe      只采样一次并打印结果，用于排错
  python.exe  tray_mon.py --no-dock    不嵌任务栏，改为右上角悬浮窗
  python.exe  tray_mon.py --exit-after 5   跑 5 秒后自动退出（自测用）

右键菜单（任务栏面板 / 托盘图标都可以）
  显示项开关（网络 / CPU / 内存 / 磁盘）、刷新间隔、深浅主题、
  停靠与悬浮切换、开机自启、退出

配置保存在脚本同目录 tray_mon.json，退出时自动保存。

实现说明
  全程只用 ctypes 调 user32/gdi32/shell32/iphlpapi，零第三方依赖。
  数据来源：GetSystemTimes(CPU)、GlobalMemoryStatusEx(内存)、
  GetIfTable(网卡收发计数，处理 32 位回绕)、GetDiskFreeSpaceEx(磁盘)。
  界面用 GDI 双缓冲自绘；每段文字按「模板串」预留固定宽度，
  数字变化时不会让面板左右抖动。
"""

import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

VERSION = '1.0.0'
IS_WINDOWS = (os.name == 'nt')

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, 'frozen', False)
                                           else __file__))
CONF_PATH = os.path.join(BASE_DIR, 'tray_mon.json')

APP_NAME = 'TrayMon'
APP_TITLE = '任务栏性能监控'
WND_CLASS = 'TrayMonPanelWnd'

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_NCLBUTTONDOWN = 0x00A1
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
WM_EXITSIZEMOVE = 0x0232
WM_APP = 0x8000
HTCAPTION = 2

WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_TOPMOST = 0x00000008

SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
SWP_NOSIZE = 0x0001
HWND_TOPMOST = -1

DT_CENTER = 0x0001
DT_LEFT = 0x0000
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_NOPREFIX = 0x0800

TRANSPARENT = 1
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
SPI_GETWORKAREA = 0x0030

MF_STRING = 0x0000
MF_CHECKED = 0x0008
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

SM_CXSMICON = 49
IDC_ARROW = 32512

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

ID_TRAY = 1
TIMER_ID = 1
TRAY_CALLBACK = WM_APP + 1

CMD_QUIT = 1001
CMD_TOGGLE_DOCK = 1002
CMD_THEME = 1003
CMD_AUTOSTART = 1004
CMD_INTERVAL_BASE = 1010
CMD_ITEM_BASE = 1020

INTERVAL_CHOICES = (1.0, 2.0, 5.0)
ITEM_KEYS = ('net', 'cpu', 'mem', 'disk')
ITEM_NAMES = {'net': '网络速率', 'cpu': 'CPU 占用',
              'mem': '内存占用', 'disk': '磁盘占用'}

# ---------------------------------------------------------------------------
# 主题
# ---------------------------------------------------------------------------
THEMES = {
    'dark': {
        'bg': (0x1C, 0x1C, 0x1E),
        'label': (0x8E, 0x94, 0x9E),
        'net_down': (0x4F, 0xC3, 0xF7),
        'net_up': (0x81, 0xC7, 0x84),
        'cpu': (0xFF, 0xB7, 0x4D),
        'mem': (0xBA, 0x68, 0xC8),
        'disk': (0x90, 0xA4, 0xAE),
        'warn': (0xE5, 0x73, 0x73),
        'text': (0xF2, 0xF2, 0xF4),
    },
    'light': {
        'bg': (0xF3, 0xF3, 0xF5),
        'label': (0x5F, 0x63, 0x68),
        'net_down': (0x02, 0x77, 0xBD),
        'net_up': (0x2E, 0x7D, 0x32),
        'cpu': (0xE6, 0x7E, 0x00),
        'mem': (0x7B, 0x1F, 0xA2),
        'disk': (0x54, 0x6E, 0x7A),
        'warn': (0xC6, 0x28, 0x28),
        'text': (0x20, 0x20, 0x22),
    },
}

# ---------------------------------------------------------------------------
# Win32 结构体
# ---------------------------------------------------------------------------
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.UINT),
                ('style', wintypes.UINT),
                ('lpfnWndProc', WNDPROC),
                ('cbClsExtra', ctypes.c_int),
                ('cbWndExtra', ctypes.c_int),
                ('hInstance', wintypes.HINSTANCE),
                ('hIcon', wintypes.HICON),
                ('hCursor', wintypes.HANDLE),
                ('hbrBackground', wintypes.HBRUSH),
                ('lpszMenuName', wintypes.LPCWSTR),
                ('lpszClassName', wintypes.LPCWSTR),
                ('hIconSm', wintypes.HICON)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [('hdc', wintypes.HDC),
                ('fErase', wintypes.BOOL),
                ('rcPaint', wintypes.RECT),
                ('fRestore', wintypes.BOOL),
                ('fIncUpdate', wintypes.BOOL),
                ('rgbReserved', ctypes.c_byte * 32)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD),
                ('hWnd', wintypes.HWND),
                ('uID', wintypes.UINT),
                ('uFlags', wintypes.UINT),
                ('uCallbackMessage', wintypes.UINT),
                ('hIcon', wintypes.HICON),
                ('szTip', wintypes.WCHAR * 128),
                ('dwState', wintypes.DWORD),
                ('dwStateMask', wintypes.DWORD),
                ('szInfo', wintypes.WCHAR * 256),
                ('uVersion', wintypes.UINT),
                ('szInfoTitle', wintypes.WCHAR * 64),
                ('dwInfoFlags', wintypes.DWORD),
                ('guidItem', ctypes.c_byte * 16),
                ('hBalloonIcon', wintypes.HICON)]


class ICONINFO(ctypes.Structure):
    _fields_ = [('fIcon', wintypes.BOOL),
                ('xHotspot', wintypes.DWORD),
                ('yHotspot', wintypes.DWORD),
                ('hbmMask', wintypes.HBITMAP),
                ('hbmColor', wintypes.HBITMAP)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD),
                ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG),
                ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD),
                ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD),
                ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG),
                ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', BITMAPINFOHEADER),
                ('bmiColors', wintypes.DWORD * 3)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [('dwLength', wintypes.DWORD),
                ('dwMemoryLoad', wintypes.DWORD),
                ('ullTotalPhys', ctypes.c_ulonglong),
                ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong),
                ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong),
                ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


class MIB_IFROW(ctypes.Structure):
    _fields_ = [('wszName', wintypes.WCHAR * 256),
                ('dwIndex', wintypes.DWORD),
                ('dwType', wintypes.DWORD),
                ('dwMtu', wintypes.DWORD),
                ('dwSpeed', wintypes.DWORD),
                ('dwPhysAddrLen', wintypes.DWORD),
                ('bPhysAddr', ctypes.c_ubyte * 8),
                ('dwAdminStatus', wintypes.DWORD),
                ('dwOperStatus', wintypes.DWORD),
                ('dwLastChange', wintypes.DWORD),
                ('dwInOctets', wintypes.DWORD),
                ('dwInUcastPkts', wintypes.DWORD),
                ('dwInNUcastPkts', wintypes.DWORD),
                ('dwInDiscards', wintypes.DWORD),
                ('dwInErrors', wintypes.DWORD),
                ('dwInUnknownProtos', wintypes.DWORD),
                ('dwOutOctets', wintypes.DWORD),
                ('dwOutUcastPkts', wintypes.DWORD),
                ('dwOutNUcastPkts', wintypes.DWORD),
                ('dwOutDiscards', wintypes.DWORD),
                ('dwOutErrors', wintypes.DWORD),
                ('dwOutQLen', wintypes.DWORD),
                ('dwDescrLen', wintypes.DWORD),
                ('bDescr', ctypes.c_ubyte * 256)]


IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
# MIB_IFROW.dwOperStatus 用的是「内部状态」枚举，不是 IF_OPER_STATUS：
# 0=NonOperational 1=Unreachable 2=Disconnected 3=Connecting
# 4=Connected 5=Operational。只有 4/5 才代表网卡真的在工作。
IF_OPER_INTERNAL_CONNECTED = 4
IF_OPER_INTERNAL_OPERATIONAL = 5

# ---------------------------------------------------------------------------
# API 绑定
# ---------------------------------------------------------------------------
user32 = ctypes.WinDLL('user32', use_last_error=True)
gdi32 = ctypes.WinDLL('gdi32', use_last_error=True)
shell32 = ctypes.WinDLL('shell32', use_last_error=True)
iphlpapi = ctypes.WinDLL('iphlpapi', use_last_error=True)
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND,
                                 wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowExW.restype = wintypes.HWND
user32.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
user32.SetParent.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                wintypes.UINT]
user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.InvalidateRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT),
                                  wintypes.BOOL]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT,
                            wintypes.LPVOID]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t,
                               wintypes.LPCWSTR]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                  ctypes.POINTER(wintypes.RECT)]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                wintypes.WPARAM, wintypes.LPARAM]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                            wintypes.HBRUSH]
user32.DrawTextW.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(wintypes.RECT), wintypes.UINT]
user32.DestroyIcon.argtypes = [wintypes.HICON]
user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]
user32.CreateIconIndirect.restype = wintypes.HICON
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                         wintypes.LPVOID, wintypes.UINT]
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.ReleaseCapture.argtypes = []

if hasattr(user32, 'GetDpiForWindow'):
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.DWORD]
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                              wintypes.LPCWSTR]
gdi32.CreateFontW.restype = wintypes.HFONT
gdi32.GetTextExtentPoint32W.argtypes = [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
                                        ctypes.POINTER(wintypes.SIZE)]
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BITMAPINFO),
                                   wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                               wintypes.UINT, wintypes.LPVOID]
gdi32.CreateBitmap.restype = wintypes.HBITMAP
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.GetDeviceCaps.restype = ctypes.c_int

shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD,
                                      ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL

kernel32.GetSystemTimes.argtypes = [ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME),
                                    ctypes.POINTER(wintypes.FILETIME)]
kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
kernel32.GetDiskFreeSpaceExW.argtypes = [wintypes.LPCWSTR,
                                         ctypes.POINTER(ctypes.c_ulonglong),
                                         ctypes.POINTER(ctypes.c_ulonglong),
                                         ctypes.POINTER(ctypes.c_ulonglong)]
iphlpapi.GetIfTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                                wintypes.BOOL]
iphlpapi.GetIfTable.restype = wintypes.DWORD


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def rgb(t):
    """(r,g,b) -> GDI COLORREF (0x00BBGGRR)"""
    return t[0] | (t[1] << 8) | (t[2] << 16)


def clamp(v, lo=0.0, hi=100.0):
    return lo if v < lo else (hi if v > hi else v)


def ft_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def fmt_rate(bps):
    """字节/秒 -> 人类可读文本。

    数字宽度刻意补齐到固定字符数（%4d / %4.1f / %5.2f）：GDI 文字是左对齐画的，
    宽度一变整个面板就会左右抖，补位之后每秒刷新看着是稳的。
    """
    if bps < 0:
        bps = 0.0
    if bps < 1024.0:
        return '%4d B/s' % int(bps)
    if bps < 1024.0 * 1024:
        return '%5.1f KB/s' % (bps / 1024.0)
    if bps < 1024.0 ** 3:
        return '%6.2f MB/s' % (bps / 1024.0 ** 2)
    return '%6.2f GB/s' % (bps / 1024.0 ** 3)


def fmt_bytes(n):
    if n >= 1024.0 ** 3:
        return '%.1f GB' % (n / 1024.0 ** 3)
    return '%.0f MB' % (n / 1024.0 ** 2)


# ---------------------------------------------------------------------------
# 采样
# ---------------------------------------------------------------------------
class Sampler(object):
    def __init__(self, disk_letter='C'):
        self.disk_letter = disk_letter
        self._cpu_prev = None
        self._net_prev = None       # (in, out, t)
        self.cpu = 0.0
        self.mem_percent = 0.0
        self.mem_used = 0.0
        self.mem_total = 0.0
        self.dl_bps = 0.0
        self.ul_bps = 0.0
        self.disk_percent = 0.0

    # --- CPU ---
    def _sample_cpu(self):
        idle = wintypes.FILETIME()
        kern = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                       ctypes.byref(user)):
            return self.cpu
        i, k, u = ft_to_int(idle), ft_to_int(kern), ft_to_int(user)
        total = k + u                      # kernel 时间已包含 idle
        if self._cpu_prev is not None:
            di = i - self._cpu_prev[0]
            dt = total - self._cpu_prev[1]
            if dt > 0:
                self.cpu = clamp(100.0 * (1.0 - float(di) / float(dt)))
        self._cpu_prev = (i, total)
        return self.cpu

    # --- 内存 ---
    def _sample_mem(self):
        ms = MEMORYSTATUSEX()
        ms.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            self.mem_percent = float(ms.dwMemoryLoad)
            self.mem_total = float(ms.ullTotalPhys)
            self.mem_used = self.mem_total - float(ms.ullAvailPhys)
        return self.mem_percent

    # --- 网络 ---
    def if_counters(self):
        """返回 (收到的字节, 发出的字节) 累计值。

        两个坑必须处理，否则读出来的网速是错的：
        1) GetIfTable 按「IP 地址」而不是按「网卡」出行，同一张物理网卡在有多个
           IP（IPv4+IPv6+虚拟交换机）时会重复出现 N 行且计数完全相同，直接求和
           会把流量放大 N 倍。这里按 MAC 去重，没有 MAC 的退回用索引。
        2) dwOperStatus 用的是内部状态枚举（4=Connected, 5=Operational），
           不是 IF_OPER_STATUS 的 1=Up，写成 ==1 会一行都匹配不到。
        """
        size = wintypes.DWORD(0)
        iphlpapi.GetIfTable(None, ctypes.byref(size), True)
        if size.value <= 0:
            return 0, 0
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetIfTable(buf, ctypes.byref(size), True) != 0:
            return 0, 0
        count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD)).contents.value
        row_size = ctypes.sizeof(MIB_IFROW)
        base = ctypes.addressof(buf) + ctypes.sizeof(wintypes.DWORD)
        total_in = total_out = 0
        seen = set()
        for idx in range(count):
            row = MIB_IFROW.from_address(base + idx * row_size)
            if row.dwType in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
                continue
            if row.dwOperStatus not in (IF_OPER_INTERNAL_CONNECTED,
                                        IF_OPER_INTERNAL_OPERATIONAL):
                continue
            mac = bytes(bytearray(row.bPhysAddr[:row.dwPhysAddrLen]))
            key = mac if mac.strip(b'\x00') else ('idx', row.dwIndex)
            if key in seen:
                continue
            seen.add(key)
            total_in += row.dwInOctets
            total_out += row.dwOutOctets
        return total_in, total_out

    def _sample_net(self):
        cur_in, cur_out = self.if_counters()
        now = time.time()
        if self._net_prev is None:
            self._net_prev = (cur_in, cur_out, now)
            return 0.0, 0.0
        prev_in, prev_out, prev_t = self._net_prev
        dt = now - prev_t
        if dt <= 0:
            return self.dl_bps, self.ul_bps
        mask = 0xFFFFFFFF               # 32 位计数器会回绕（4 GiB）
        d_in = (cur_in - prev_in) & mask
        d_out = (cur_out - prev_out) & mask
        if d_in > 0x7FFFFFFF:           # 更像计数器重置而不是回绕
            d_in = 0
        if d_out > 0x7FFFFFFF:
            d_out = 0
        self.dl_bps = d_in / dt
        self.ul_bps = d_out / dt
        self._net_prev = (cur_in, cur_out, now)
        return self.dl_bps, self.ul_bps

    # --- 磁盘 ---
    def _sample_disk(self):
        root = u'%s:\\' % self.disk_letter
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        total_free = ctypes.c_ulonglong(0)
        if kernel32.GetDiskFreeSpaceExW(root, ctypes.byref(free),
                                        ctypes.byref(total),
                                        ctypes.byref(total_free)):
            if total.value:
                self.disk_percent = clamp(
                    100.0 * (1.0 - float(total_free.value) / float(total.value)))
        return self.disk_percent

    def sample(self):
        self._sample_cpu()
        self._sample_mem()
        self._sample_net()
        self._sample_disk()
        return self

    def snapshot(self):
        return {
            'cpu': round(self.cpu, 1),
            'mem_percent': round(self.mem_percent, 1),
            'mem_used_bytes': int(self.mem_used),
            'mem_total_bytes': int(self.mem_total),
            'down_bps': round(self.dl_bps, 1),
            'up_bps': round(self.ul_bps, 1),
            'disk_percent': round(self.disk_percent, 1),
        }


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class Monitor(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.sampler = Sampler(cfg.get('disk_letter', 'C'))
        self.hwnd = None
        self.hinst = kernel32.GetModuleHandleW(None)
        self._wndproc_ref = None
        self._msg_taskbar_created = user32.RegisterWindowMessageW(u'TaskbarCreated')
        self._font = None
        self._font_px = 0
        self._brushes = {}
        self.hwnd_tray = None          # 专门收托盘消息的隐藏顶层窗口
        self._tray_icon = None
        self._tray_added = False
        self._tray_retry = 0
        self._width = 0
        self._height = 0
        self._x = 0
        self._y = 0
        self._docked_now = False
        self._rebuilding = False
        self._frames = 0
        self.rect_report = None

    # ---------------- 窗口 ----------------
    def _register_class(self):
        if self._wndproc_ref is not None:
            return
        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = self.hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))
        wc.lpszClassName = WND_CLASS
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            err = ctypes.get_last_error()
            if err not in (0, 1410):        # 1410 = class already exists
                raise ctypes.WinError(err)

    def _create_tray_host(self):
        """托盘消息必须挂在一个**顶层**窗口上。

        实测：把任务栏当父窗口的 WS_CHILD 窗口传给 Shell_NotifyIconW，
        NIM_ADD 直接返回 0（GetLastError=1460 ERROR_TIMEOUT），图标根本注册不上；
        WS_POPUP 顶层窗口就正常。所以托盘用一个不可见的顶层 popup 当接收器。
        """
        self.hwnd_tray = user32.CreateWindowExW(
            WS_EX_TOOLWINDOW, WND_CLASS, u'TrayMonTrayHost', WS_POPUP,
            0, 0, 0, 0, None, None, self.hinst, None)
        return self.hwnd_tray

    def _create_window(self):
        self._register_class()
        # 顺序很重要：先建托盘宿主、先注册图标，再把面板贴到任务栏上。
        # 如果面板已经 SetParent 到任务栏，此刻调 Shell_NotifyIconW 会超时
        # （GetLastError=1460），因为 explorer 在等我们这边的窗口响应，
        # 而我们的线程正卡在 NIM_ADD 里等 explorer 回话。
        if self.cfg.get('tray_icon', True):
            if not self.hwnd_tray:
                self._create_tray_host()
            if not self._tray_added:
                self._add_tray_icon()

        tray, bar, notify = self._taskbar_rects()
        want_dock = bool(self.cfg.get('dock', True))
        can_dock = (tray is not None and bar is not None and notify is not None
                    and (bar.right - bar.left) > (bar.bottom - bar.top))
        docked = want_dock and can_dock
        style = (WS_CHILD if docked else WS_POPUP) | WS_VISIBLE
        ex = WS_EX_TOOLWINDOW | (0 if docked else WS_EX_TOPMOST)
        parent = tray if docked else None
        self.hwnd = user32.CreateWindowExW(ex, WND_CLASS, APP_TITLE, style,
                                           0, 0, 200, 30, parent, None,
                                           self.hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self._docked_now = docked
        self._lay_out()
        user32.SetTimer(self.hwnd, TIMER_ID,
                        max(250, int(float(self.cfg.get('interval', 1.0)) * 1000)),
                        None)

    def _destroy_window(self):
        self._remove_tray_icon()
        if self.hwnd:
            user32.KillTimer(self.hwnd, TIMER_ID)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.hwnd_tray:
            user32.DestroyWindow(self.hwnd_tray)
            self.hwnd_tray = None

    # ---------------- 布局 ----------------
    def _dpi(self):
        dpi = 96
        if self.hwnd and hasattr(user32, 'GetDpiForWindow'):
            try:
                dpi = int(user32.GetDpiForWindow(self.hwnd)) or 96
            except Exception:
                dpi = 96
        return dpi

    def _taskbar_rects(self):
        tray = user32.FindWindowW(u'Shell_TrayWnd', None)
        if not tray:
            return None, None, None
        notify = user32.FindWindowExW(tray, None, u'TrayNotifyWnd', None)
        bar = wintypes.RECT()
        user32.GetWindowRect(tray, ctypes.byref(bar))
        notify_rect = None
        if notify:
            notify_rect = wintypes.RECT()
            user32.GetWindowRect(notify, ctypes.byref(notify_rect))
        return tray, bar, notify_rect

    def _metrics(self):
        s = self.sampler
        return {
            'net_down': fmt_rate(s.dl_bps),
            'net_up': fmt_rate(s.ul_bps),
            'cpu': int(round(s.cpu)),
            'mem': int(round(s.mem_percent)),
            'disk': int(round(s.disk_percent)),
        }

    def _font_spec(self):
        dpi = self._dpi()
        px = int(self.cfg.get('font_size') or 0)
        if px <= 0:
            px = max(11, int(round(12 * dpi / 96.0)))
        return px, dpi

    def _ensure_font(self, px):
        if self._font and self._font_px == px:
            return self._font
        if self._font:
            gdi32.DeleteObject(self._font)
        face = self.cfg.get('font_face') or u'Segoe UI'
        h = gdi32.CreateFontW(-px, 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 5, 0, face)
        if not h:
            h = gdi32.CreateFontW(-px, 0, 0, 0, 500, 0, 0, 0, 1, 0, 0, 5, 0,
                                  u'Tahoma')
        self._font = h
        self._font_px = px
        return h

    def _brush(self, color):
        key = rgb(color)
        b = self._brushes.get(key)
        if b is None:
            b = gdi32.CreateSolidBrush(key)
            self._brushes[key] = b
        return b

    def _tokens(self, m):
        """每项 = (标签, 标签色, 数值文本, 数值色)，宽度按实际文本测。"""
        th = THEMES[self.cfg.get('theme', 'dark')]
        items = [i for i in self.cfg.get('items', list(ITEM_KEYS)) if i in ITEM_KEYS]
        out = []
        if 'net' in items:
            out.append((u'\u2193', th['label'], m['net_down'], th['net_down']))
            out.append((u'\u2191', th['label'], m['net_up'], th['net_up']))
        for key, label, color_key in (('cpu', u'CPU', 'cpu'),
                                      ('mem', u'MEM', 'mem'),
                                      ('disk', u'DSK', 'disk')):
            if key not in items:
                continue
            pct = m[key]
            color = th['warn'] if pct >= 90 else th[color_key]
            out.append((label, th['label'], u'%d%%' % pct, color))
        return out, th

    def _lay_out(self):
        """算宽度、贴到任务栏（或悬浮），必要时调整窗口尺寸与位置。"""
        if not self.hwnd:
            return
        px, dpi = self._font_spec()
        font = self._ensure_font(px)
        tokens, _th = self._tokens(self._metrics())

        hdc = user32.GetDC(self.hwnd)
        old = gdi32.SelectObject(hdc, font)
        gap = max(6, int(round(8 * dpi / 96.0)))
        pad = max(6, int(round(8 * dpi / 96.0)))
        small = max(3, int(round(4 * dpi / 96.0)))
        width = pad * 2
        for label, _lc, value, _vc in tokens:
            lsz = wintypes.SIZE()
            gdi32.GetTextExtentPoint32W(hdc, label, len(label), ctypes.byref(lsz))
            vsz = wintypes.SIZE()
            gdi32.GetTextExtentPoint32W(hdc, value, len(value), ctypes.byref(vsz))
            width += lsz.cx + small + vsz.cx + gap
        if tokens:
            width -= gap
        gdi32.SelectObject(hdc, old)
        user32.ReleaseDC(self.hwnd, hdc)
        width = max(60, int(width))

        tray, bar, notify = self._taskbar_rects()
        docked = bool(self.cfg.get('dock', True))
        if docked and bar is not None and notify is not None:
            bar_h = bar.bottom - bar.top
            if (bar.right - bar.left) > bar_h:
                height = max(px + 6, bar_h)
                x = notify.left - width - max(2, int(round(3 * dpi / 96.0)))
                if x < bar.left:
                    x = bar.left
                y = bar.top + (bar_h - height) // 2
                if not self._docked_now:
                    user32.SetParent(self.hwnd, tray)
                    self._docked_now = True
                user32.SetWindowPos(self.hwnd, None, x - bar.left, y - bar.top,
                                    width, height,
                                    SWP_NOACTIVATE | SWP_SHOWWINDOW)
                self._width, self._height, self._x, self._y = width, height, x, y
                return

        # 悬浮模式
        height = px + max(8, int(round(10 * dpi / 96.0)))
        wa = wintypes.RECT()
        user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0)
        x = self.cfg.get('float_x')
        y = self.cfg.get('float_y')
        if x is None or y is None:
            x = wa.right - width - 12
            y = wa.top + 12
        x = max(wa.left, min(int(x), wa.right - width))
        y = max(wa.top, min(int(y), wa.bottom - height))
        if self._docked_now or self._x != x or self._y != y:
            user32.SetParent(self.hwnd, None)
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, width, height,
                                SWP_NOACTIVATE | SWP_SHOWWINDOW)
            self._docked_now = False
        else:
            user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, width, height,
                                SWP_NOACTIVATE | SWP_SHOWWINDOW)
        self._width, self._height, self._x, self._y = width, height, x, y

    # ---------------- 绘制 ----------------
    def _paint(self):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self.hwnd, ctypes.byref(ps))
        rc = wintypes.RECT()
        user32.GetClientRect(self.hwnd, ctypes.byref(rc))
        w, h = max(1, rc.right), max(1, rc.bottom)

        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old_bmp = gdi32.SelectObject(mem, bmp)

        m = self._metrics()
        tokens, th = self._tokens(m)

        user32.FillRect(mem, ctypes.byref(wintypes.RECT(0, 0, w, h)),
                        self._brush(th['bg']))

        px, dpi = self._font_spec()
        font = self._ensure_font(px)
        old_font = gdi32.SelectObject(mem, font)
        gdi32.SetBkMode(mem, TRANSPARENT)

        gap = max(6, int(round(8 * dpi / 96.0)))
        pad = max(6, int(round(8 * dpi / 96.0)))
        small = max(3, int(round(4 * dpi / 96.0)))
        x = pad
        for label, lcolor, value, vcolor in tokens:
            lsz = wintypes.SIZE()
            gdi32.GetTextExtentPoint32W(mem, label, len(label), ctypes.byref(lsz))
            vsz = wintypes.SIZE()
            gdi32.GetTextExtentPoint32W(mem, value, len(value), ctypes.byref(vsz))

            r = wintypes.RECT(x, 0, x + lsz.cx + small, h)
            gdi32.SetTextColor(mem, rgb(lcolor))
            user32.DrawTextW(mem, label, len(label), ctypes.byref(r),
                             DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)

            vx = x + lsz.cx + small
            r2 = wintypes.RECT(vx, 0, vx + vsz.cx + 2, h)
            gdi32.SetTextColor(mem, rgb(vcolor))
            user32.DrawTextW(mem, value, len(value), ctypes.byref(r2),
                             DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
            x += lsz.cx + small + vsz.cx + gap

        # 底部一条 CPU 负载线
        bar_h = max(1, int(round(2 * dpi / 96.0)))
        frac = clamp(m['cpu']) / 100.0
        if frac > 0:
            bw = int(round(w * frac))
            color = th['warn'] if m['cpu'] >= 90 else th['cpu']
            user32.FillRect(mem, ctypes.byref(wintypes.RECT(0, h - bar_h, bw, h)),
                            self._brush(color))

        gdi32.SelectObject(mem, old_font)
        gdi32.BitBlt(hdc, 0, 0, w, h, mem, 0, 0, SRCCOPY)
        gdi32.SelectObject(mem, old_bmp)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.EndPaint(self.hwnd, ctypes.byref(ps))

        self._update_tray_icon()
        self._frames += 1
        rep = {'x': self._x, 'y': self._y, 'w': self._width,
               'h': self._height, 'docked': self._docked_now,
               'tray_icon_added': self._tray_added}
        # 调试用：窗口真实屏幕位置 + 任务栏/托盘区矩形，便于和截图对齐
        try:
            wr = wintypes.RECT()
            user32.GetWindowRect(self.hwnd, ctypes.byref(wr))
            rep['win_rect'] = [wr.left, wr.top, wr.right, wr.bottom]
            tray, bar, notify = self._taskbar_rects()
            if bar is not None:
                rep['taskbar'] = [bar.left, bar.top, bar.right, bar.bottom]
            if notify is not None:
                rep['notify'] = [notify.left, notify.top, notify.right, notify.bottom]
            rep['screen'] = [user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)]
            rep['dpi'] = self._dpi()
        except Exception:
            pass
        self.rect_report = rep

    # ---------------- 托盘图标 ----------------
    def _draw_icon_dib(self, text, frac, th):
        """16x16 图标：深底 + 白字 + 底部负载条。返回 (hbitmap, size, bits_ptr)。

        GDI 只在 RGB 三通道上画，alpha 通道不会被写，所以画完必须自己把 alpha
        补成不透明，否则 CreateIconIndirect 拿到的是「全透明图标」（第一次做
        成了纯透明，任务栏里什么都看不见）。整体做成不透明方块即可，跟任务栏
        深色底融为一体，也更像系统自带图标。
        """
        size = user32.GetSystemMetrics(SM_CXSMICON) or 16
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = size
        bmi.bmiHeader.biHeight = -size           # 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = DIB_RGB_COLORS
        bits = ctypes.c_void_p()
        hdc = gdi32.CreateCompatibleDC(None)
        dib = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                     ctypes.byref(bits), None, 0)
        old = gdi32.SelectObject(hdc, dib)

        # 底色
        user32.FillRect(hdc, ctypes.byref(wintypes.RECT(0, 0, size, size)),
                        self._brush(th['bg']))

        # 百分比数字：底部留出负载条的位置
        # 字号压到 0.62 倍、字重 600：16px 里两三位数字才不会被裁掉；
        # 质量用 ANTIALIASED(4) 而不是 CLEARTYPE(5)，否则字边缘会出现彩色
        # 次像素条纹，在 16px 尺寸上非常脏。
        text_h = size - max(2, int(round(size * 0.2)))
        font_px = max(8, int(round(size * 0.62)))
        icon_font = gdi32.CreateFontW(-font_px, 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 4,
                                      0, u'Segoe UI')
        old_font = gdi32.SelectObject(hdc, icon_font)
        gdi32.SetBkMode(hdc, TRANSPARENT)
        gdi32.SetTextColor(hdc, rgb(th['text']))
        r = wintypes.RECT(0, 0, size, text_h)
        user32.DrawTextW(hdc, text, len(text), ctypes.byref(r),
                         DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX)
        gdi32.SelectObject(hdc, old_font)
        gdi32.DeleteObject(icon_font)

        bar_h = max(2, int(round(size * 0.2)))
        bw = int(round(size * clamp(frac) / 100.0))
        if bw > 0:
            color = th['warn'] if frac >= 90 else th['cpu']
            user32.FillRect(hdc,
                            ctypes.byref(wintypes.RECT(0, size - bar_h, bw, size)),
                            self._brush(color))
        gdi32.SelectObject(hdc, old)
        gdi32.DeleteDC(hdc)

        n = size * size
        buf = ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte * (n * 4))).contents
        for i in range(n):
            buf[i * 4 + 3] = 255
        return dib, size, bits

    def _build_icon(self):
        th = THEMES[self.cfg.get('theme', 'dark')]
        text = u'%d' % int(round(clamp(self.sampler.cpu)))
        dib, size, _bits = self._draw_icon_dib(text, self.sampler.cpu, th)
        mask = gdi32.CreateBitmap(size, size, 1, 1, None)
        ii = ICONINFO()
        ii.fIcon = 1
        ii.xHotspot = 0
        ii.yHotspot = 0
        ii.hbmMask = mask
        ii.hbmColor = dib
        hicon = user32.CreateIconIndirect(ctypes.byref(ii))
        gdi32.DeleteObject(dib)
        gdi32.DeleteObject(mask)
        return hicon

    def _nid(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd_tray or self.hwnd
        nid.uID = ID_TRAY
        return nid

    def _add_tray_icon(self):
        if not (self.hwnd_tray or self.hwnd):
            return
        hicon = self._build_icon()
        nid = self._nid()
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = TRAY_CALLBACK
        nid.hIcon = hicon
        nid.szTip = self._tooltip()
        ctypes.set_last_error(0)
        if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            self._tray_added = True
            if self._tray_icon:
                user32.DestroyIcon(self._tray_icon)
            self._tray_icon = hicon
        else:
            sys.stderr.write('tray icon NIM_ADD failed (hwnd=%s hicon=%s err=%s)\n'
                             % (nid.hWnd, hicon, ctypes.get_last_error()))
            if hicon:
                user32.DestroyIcon(hicon)

    def _remove_tray_icon(self):
        if self._tray_added and (self.hwnd_tray or self.hwnd):
            nid = self._nid()
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._tray_added = False
        if self._tray_icon:
            user32.DestroyIcon(self._tray_icon)
            self._tray_icon = None

    def _tooltip(self):
        s = self.sampler
        items = self.cfg.get('items', [])
        parts = []
        if 'net' in items:
            parts.append(u'\u2193 %s  \u2191 %s' % (fmt_rate(s.dl_bps),
                                                    fmt_rate(s.ul_bps)))
        if 'cpu' in items:
            parts.append(u'CPU %d%%' % int(round(s.cpu)))
        if 'mem' in items:
            parts.append(u'MEM %d%% (%s / %s)' % (int(round(s.mem_percent)),
                                                  fmt_bytes(s.mem_used),
                                                  fmt_bytes(s.mem_total)))
        if 'disk' in items:
            parts.append(u'DSK %d%%' % int(round(s.disk_percent)))
        return (u'  |  '.join(parts) or APP_TITLE)[:127]

    def _update_tray_icon(self):
        if not self._tray_added or not self.cfg.get('tray_icon', True):
            return
        hicon = self._build_icon()
        nid = self._nid()
        nid.uFlags = NIF_ICON | NIF_TIP
        nid.hIcon = hicon
        nid.szTip = self._tooltip()
        if shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)):
            if self._tray_icon:
                user32.DestroyIcon(self._tray_icon)
            self._tray_icon = hicon
        elif hicon:
            user32.DestroyIcon(hicon)

    # ---------------- 菜单 ----------------
    def _build_menu(self):
        """构建右键菜单（不含弹出），方便无界面测试检查条目。"""
        menu = user32.CreatePopupMenu()
        items = self.cfg.get('items', [])
        for i, key in enumerate(ITEM_KEYS):
            flags = MF_STRING | (MF_CHECKED if key in items else 0)
            user32.AppendMenuW(menu, flags, CMD_ITEM_BASE + i,
                               u'显示 %s' % ITEM_NAMES[key])
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        cur = float(self.cfg.get('interval', 1.0))
        for i, sec in enumerate(INTERVAL_CHOICES):
            flags = MF_STRING | (MF_CHECKED if abs(cur - sec) < 0.01 else 0)
            user32.AppendMenuW(menu, flags, CMD_INTERVAL_BASE + i,
                               u'刷新间隔 %d 秒' % int(sec))
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if
                                              self.cfg.get('theme') == 'dark' else 0),
                           CMD_THEME, u'深色主题')
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if self.cfg.get('dock', True)
                                              else 0),
                           CMD_TOGGLE_DOCK, u'停靠在任务栏')
        user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if
                                              self.cfg.get('autostart') else 0),
                           CMD_AUTOSTART, u'开机自动启动')
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, CMD_QUIT, u'退出')
        return menu

    def _show_menu(self):
        menu = self._build_menu()
        # 用顶层托盘窗口当 owner：面板是任务栏的子窗口，
        # SetForegroundWindow/TrackPopupMenu 对子窗口不起作用，菜单会点不没。
        owner = self.hwnd_tray or self.hwnd
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(owner)
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                                    pt.x, pt.y, 0, owner, None)
        user32.PostMessageW(owner, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd:
            self._on_menu(cmd)

    def _on_menu(self, cmd):
        if cmd == CMD_QUIT:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        elif CMD_ITEM_BASE <= cmd < CMD_ITEM_BASE + len(ITEM_KEYS):
            key = ITEM_KEYS[cmd - CMD_ITEM_BASE]
            items = list(self.cfg.get('items', list(ITEM_KEYS)))
            if key in items:
                if len(items) > 1:
                    items.remove(key)
            else:
                items.append(key)
            self.cfg['items'] = [k for k in ITEM_KEYS if k in items]
            self._refresh()
        elif CMD_INTERVAL_BASE <= cmd < CMD_INTERVAL_BASE + len(INTERVAL_CHOICES):
            self.cfg['interval'] = INTERVAL_CHOICES[cmd - CMD_INTERVAL_BASE]
            user32.KillTimer(self.hwnd, TIMER_ID)
            user32.SetTimer(self.hwnd, TIMER_ID,
                            max(250, int(self.cfg['interval'] * 1000)), None)
        elif cmd == CMD_THEME:
            self.cfg['theme'] = 'light' if self.cfg.get('theme') == 'dark' else 'dark'
            self._refresh()
        elif cmd == CMD_TOGGLE_DOCK:
            self.cfg['dock'] = not bool(self.cfg.get('dock', True))
            self._rebuild()
        elif cmd == CMD_AUTOSTART:
            set_autostart(not bool(self.cfg.get('autostart')))
            self.cfg['autostart'] = is_autostart_enabled()
            self._refresh()

    def _refresh(self):
        if self.hwnd:
            self._lay_out()
            user32.InvalidateRect(self.hwnd, None, True)

    def _rebuild(self):
        self._rebuilding = True
        try:
            self._destroy_window()
            self._create_window()
        finally:
            self._rebuilding = False
        self._refresh()

    # ---------------- 消息 ----------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        is_tray_host = (self.hwnd_tray is not None and hwnd == self.hwnd_tray)
        if msg == WM_TIMER and wparam == TIMER_ID and not is_tray_host:
            # 托盘图标没注册上就再试试（explorer 刚重启/忙的时候会超时）
            if (self.cfg.get('tray_icon', True) and not self._tray_added
                    and self._tray_retry < 6):
                self._tray_retry += 1
                self._add_tray_icon()
            self.sampler.sample()
            self._lay_out()
            user32.InvalidateRect(hwnd, None, False)
            return 0
        if msg == WM_PAINT:
            if is_tray_host:
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
            self._paint()
            return 0
        if msg == TRAY_CALLBACK:
            if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU, WM_LBUTTONUP):
                self._show_menu()
            return 0
        if msg in (WM_RBUTTONUP, WM_CONTEXTMENU):
            self._show_menu()
            return 0
        if msg == WM_LBUTTONDOWN:
            if not self._docked_now:        # 悬浮模式：按住可以拖
                user32.ReleaseCapture()
                user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, 0)
            return 0
        if msg == WM_LBUTTONUP:
            if self._docked_now:
                self._show_menu()
            return 0
        if msg == WM_EXITSIZEMOVE:
            if not self._docked_now:
                rc = wintypes.RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rc))
                self.cfg['float_x'] = rc.left
                self.cfg['float_y'] = rc.top
            return 0
        if msg == self._msg_taskbar_created:
            # explorer 重启过：托盘图标与停靠都要重做
            self._tray_added = False
            if self._tray_icon:
                user32.DestroyIcon(self._tray_icon)
                self._tray_icon = None
            self._remove_tray_icon()
            if self.cfg.get('tray_icon', True):
                self._add_tray_icon()
            self._refresh()
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.KillTimer(hwnd, TIMER_ID)
            self._remove_tray_icon()
            if is_tray_host:
                return 0
            if not self._rebuilding:
                user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------------- 主循环 ----------------
    def run(self, exit_after=None):
        self._create_window()
        self.sampler.sample()
        self._refresh()
        self._paint_now()
        if self.cfg.get('dump_rect'):
            print(json.dumps(self.rect_report or {}, ensure_ascii=False))
            sys.stdout.flush()
        deadline = time.time() + float(exit_after) if exit_after else None
        msg = wintypes.MSG()
        while True:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
            if deadline is not None and time.time() >= deadline:
                break
        self._shutdown()
        return 0

    def _paint_now(self):
        """主动画一帧（不依赖 WM_PAINT），用于启动即时显示与自测。"""
        if not self.hwnd:
            return
        self._paint()

    def _shutdown(self):
        save_config(self.cfg)
        self._destroy_window()
        for b in self._brushes.values():
            gdi32.DeleteObject(b)
        self._brushes.clear()
        if self._font:
            gdi32.DeleteObject(self._font)
            self._font = None


# ---------------------------------------------------------------------------
# 开机自启 / 配置
# ---------------------------------------------------------------------------
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def _launch_command():
    if getattr(sys, 'frozen', False):
        return u'"%s"' % sys.executable
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), 'pythonw.exe')
    if os.path.isfile(pyw):
        exe = pyw
    return u'"%s" "%s"' % (exe, os.path.abspath(__file__))


def is_autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, APP_NAME)
            return bool(val)
    except Exception:
        return False


def set_autostart(enable):
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as exc:
        sys.stderr.write('autostart failed: %s\n' % exc)
        return False


DEFAULT_CFG = {
    'interval': 1.0,
    'items': list(ITEM_KEYS),
    'theme': 'dark',
    'dock': True,
    'tray_icon': True,
    'autostart': False,
    'font_size': 0,
    'font_face': 'Segoe UI',
    'disk_letter': 'C',
    'float_x': None,
    'float_y': None,
}


def load_config():
    cfg = dict(DEFAULT_CFG)
    if os.path.isfile(CONF_PATH):
        try:
            with open(CONF_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except Exception as exc:
            sys.stderr.write('bad %s: %s\n' % (CONF_PATH, exc))
    items = [i for i in cfg.get('items', []) if i in ITEM_KEYS]
    cfg['items'] = items or list(ITEM_KEYS)
    return cfg


def save_config(cfg):
    try:
        data = {k: cfg.get(k) for k in DEFAULT_CFG}
        with open(CONF_PATH, 'w', encoding='utf-8') as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as exc:
        sys.stderr.write('cannot save config: %s\n' % exc)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def build_parser():
    import argparse
    p = argparse.ArgumentParser(
        prog='tray_mon.py',
        description='任务栏 / 托盘实时性能监控（CPU、内存、网速、磁盘）')
    p.add_argument('--interval', type=float, metavar='SEC',
                   help='刷新间隔秒数（默认 1）')
    p.add_argument('--items', metavar='LIST',
                   help='显示项，逗号分隔：net,cpu,mem,disk（默认全显）')
    p.add_argument('--theme', choices=('dark', 'light'), help='主题')
    p.add_argument('--no-dock', action='store_true',
                   help='不嵌任务栏，改成右上角悬浮窗')
    p.add_argument('--no-tray', action='store_true', help='不创建通知区图标')
    p.add_argument('--font-size', type=int, metavar='PX',
                   help='字号像素（默认按 DPI 自动）')
    p.add_argument('--font-face', metavar='NAME', help='字体名（默认 Segoe UI）')
    p.add_argument('--disk', metavar='LETTER', help='磁盘盘符（默认 C）')
    p.add_argument('--probe', action='store_true',
                   help='只采样一次并打印 JSON，不显示界面')
    p.add_argument('--dump-rect', action='store_true',
                   help='启动时打印窗口矩形（调试 / 截图定位用）')
    p.add_argument('--dump-icon', metavar='PATH',
                   help='把托盘图标放大 8 倍导出为 BMP 后退出')
    p.add_argument('--exit-after', type=float, metavar='SEC',
                   help='运行 SEC 秒后自动退出（自测用）')
    p.add_argument('--version', action='version', version='tray_mon ' + VERSION)
    return p


def do_probe(cfg):
    s = Sampler(cfg.get('disk_letter', 'C'))
    s.sample()
    time.sleep(max(0.35, float(cfg.get('interval', 1.0))))
    s.sample()
    snap = s.snapshot()
    snap['down_text'] = fmt_rate(s.dl_bps)
    snap['up_text'] = fmt_rate(s.ul_bps)
    snap['mem_text'] = '%s / %s' % (fmt_bytes(s.mem_used), fmt_bytes(s.mem_total))
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


def do_dump_icon(cfg, path):
    """把托盘图标按 8 倍放大导出 BMP（32bpp），方便肉眼核对绘制细节。"""
    mon = Monitor(cfg)
    mon.sampler.sample()
    time.sleep(0.6)                      # 采两次才有真实的 CPU 占用
    mon.sampler.sample()
    th = THEMES[cfg.get('theme', 'dark')]
    text = u'%d' % int(round(clamp(mon.sampler.cpu)))
    dib, size, bits = mon._draw_icon_dib(text, mon.sampler.cpu, th)
    scale = 8
    n = size * size
    src = ctypes.cast(bits, ctypes.POINTER(ctypes.c_ubyte * (n * 4))).contents
    out_w = out_h = size * scale
    row = out_w * 3
    pad_row = (4 - (row % 4)) % 4
    data = bytearray()
    for y in range(out_h - 1, -1, -1):            # BMP 自下而上
        line = bytearray()
        for x in range(out_w):
            o = ((y // scale) * size + (x // scale)) * 4
            b, g, r, a = src[o], src[o + 1], src[o + 2], src[o + 3]
            if a == 0:                            # 透明处画棋盘格
                checker = 200 if ((x // 4 + y // 4) % 2) else 150
                b = g = r = checker
            line += bytes((b, g, r))
        line += b'\x00' * pad_row
        data += line
    header = bytearray()
    header += b'BM'
    header += (14 + 40 + len(data)).to_bytes(4, 'little')
    header += b'\x00\x00\x00\x00'
    header += (54).to_bytes(4, 'little')
    header += (40).to_bytes(4, 'little')
    header += out_w.to_bytes(4, 'little', signed=True)
    header += out_h.to_bytes(4, 'little', signed=True)
    header += (1).to_bytes(2, 'little')
    header += (24).to_bytes(2, 'little')
    header += (0).to_bytes(4, 'little')
    header += len(data).to_bytes(4, 'little')
    header += (2835).to_bytes(4, 'little')
    header += (2835).to_bytes(4, 'little')
    header += (0).to_bytes(4, 'little')
    header += (0).to_bytes(4, 'little')
    with open(path, 'wb') as f:
        f.write(bytes(header) + bytes(data))
    gdi32.DeleteObject(dib)
    print('icon %dx%d (scale x%d) -> %s' % (size, size, scale, path))
    return 0


def apply_cli_overrides(cfg, args):
    if args.interval:
        cfg['interval'] = max(0.25, float(args.interval))
    if args.items:
        want = [p.strip().lower() for p in args.items.split(',') if p.strip()]
        cfg['items'] = [k for k in ITEM_KEYS if k in want] or list(ITEM_KEYS)
    if args.theme:
        cfg['theme'] = args.theme
    if args.no_dock:
        cfg['dock'] = False
    if args.no_tray:
        cfg['tray_icon'] = False
    if args.font_size:
        cfg['font_size'] = int(args.font_size)
    if args.font_face:
        cfg['font_face'] = args.font_face
    if args.disk:
        cfg['disk_letter'] = args.disk.strip().rstrip(':').upper() or 'C'
    if args.dump_rect:
        cfg['dump_rect'] = True
    return cfg


def main(argv=None):
    args = build_parser().parse_args(argv)
    cfg = load_config()
    cfg = apply_cli_overrides(cfg, args)
    if args.probe:
        return do_probe(cfg)
    if not IS_WINDOWS:
        sys.stderr.write('tray_mon requires Windows\n')
        return 2
    if args.dump_icon:
        return do_dump_icon(cfg, args.dump_icon)
    cfg['autostart'] = is_autostart_enabled()
    mon = Monitor(cfg)
    try:
        return mon.run(exit_after=args.exit_after)
    except KeyboardInterrupt:
        mon._shutdown()
        return 0


if __name__ == '__main__':
    sys.exit(main())
