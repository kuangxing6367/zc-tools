#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ntp_sync.py - Windows 7+ (x86/x64) 单文件、零第三方依赖的系统时间同步工具。

时间源优先级
  1. NTP v4 over UDP/123（原生 UDP 报文，不走系统代理）
  2. 若 UDP 被封 / 全部 NTP 源不可达 -> 自动降级为 TCP：读取网站 HTTP 响应包里的
     Date 头（RFC 7231），并按 Age 头修正 CDN 缓存时间。默认站点为国内可直连的
     大站，不使用 time.is（其服务条款禁止第三方脚本抓取）。

写入系统时间
  kernel32.SetSystemTime(UTC) + 启用 SeSystemtimePrivilege，需要管理员权限。
  默认只查询不写入（dry-run），只有 --apply 或 sync.bat 才会真正改时间。

兼容性
  Python 3.7+ / Windows 7 SP1 及以后 / 32 位与 64 位 / 纯标准库。
  语法只使用 3.7 特性，标准库 API 均在 3.7 可用。

安全护栏
  --max-skew 默认 7200 秒：偏差超过 2 小时一律拒绝写入，避免一天/十几天的
  大幅跳变把 TLS 证书、域认证、计划任务全部打挂；确需大跳变须显式 --force。
"""

import argparse
import codecs
import datetime
import email.utils
import json
import logging
import logging.handlers
import math
import os
import socket
import statistics
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, as_completed

VERSION = '1.3.0'
IS_WINDOWS = (os.name == 'nt')

# 日志轮转默认值：单文件 1 MiB，保留 5 个历史文件，最多占用约 6 MiB。
# 服务每 30 分钟写 1~2 行，1 MiB 大约够存半年以上，历史最多回溯三年。
DEFAULT_LOG_MAX_BYTES = 1024 * 1024
DEFAULT_LOG_BACKUPS = 5

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
NTP_PORT = 123
NTP_DELTA = 2208988800.0      # 1900-01-01 -> 1970-01-01 的秒数
NTP_ERA = 4294967296.0        # 2**32，NTP 32 位秒的回绕周期（约 136 年）

# 中国授时源优先，国外源仅作兜底；--server 可完全覆盖
DEFAULT_NTP_SERVERS = (
    'ntp.ntsc.ac.cn',              # 中国科学院国家授时中心
    'ntp.aliyun.com',
    'time1.cloud.tencent.com',
    'cn.pool.ntp.org',
    'time.windows.com',
    'pool.ntp.org',
)

# UDP 被封时的 TCP 回退源：读 HTTP 响应包里的 Date 头（普通 HTTP 请求，不是抓包）。
# 明文 http 排第一位：系统时间若错到 2009 或 2087 年，HTTPS 证书有效期必然
# 覆盖不到当前时间，标准校验一定失败，而明文通道不受时间影响。
DEFAULT_HTTP_URLS = (
    'http://www.baidu.com/',
    'https://www.baidu.com/',
    'https://www.qq.com/',
)

# 时间基准：中国标准时间 UTC+8
CHINA_TZ_NAME = 'China Standard Time'
DEFAULT_TZ_OFFSET_HOURS = 8

UA = 'ntp-sync/%s (Windows system clock sync)' % VERSION

# 预热 IDNA codec。Python 3.7 embeddable 在冷启动直接并发 getaddrinfo 时，
# 多个线程会争抢首次 import encodings.idna，失败者抛 "unknown encoding: idna"。
# 主线程先查一次即可彻底消除该竞态。
codecs.lookup('idna')

Sample = namedtuple('Sample', 'source kind offset delay stratum age note')

LOG = logging.getLogger('ntp_sync')


class SyncError(Exception):
    """所有可预期的同步错误。"""


class NTPProtocolError(SyncError):
    """NTP 报文非法。"""


class PlatformError(SyncError):
    """当前平台不支持。"""


class PrivilegeError(SyncError):
    """权限不足。"""


# ---------------------------------------------------------------------------
# NTP 时间换算
# ---------------------------------------------------------------------------
def era_base(unix_ts):
    """依据本地时钟挑选 NTP era，正确处理 2036 年 32 位秒回绕。"""
    return math.floor((unix_ts + NTP_DELTA) / NTP_ERA) * NTP_ERA


def unix_to_ntp(unix_ts, era):
    """Unix 秒 -> (32 位秒, 32 位小数)，era 为本机所处的 NTP era 基准。"""
    value = unix_ts + NTP_DELTA - era
    sec = int(math.floor(value))
    frac = int((value - sec) * NTP_ERA)
    frac = max(0, min(0xFFFFFFFF, frac))
    return sec & 0xFFFFFFFF, frac


def ntp_to_unix(sec, frac, era):
    """NTP (秒, 小数) -> Unix 秒浮点。"""
    return sec + frac / NTP_ERA + era - NTP_DELTA


def build_ntp_request(t1, era):
    """构造 48 字节 NTP v4 客户端报文；把 t1 写进 Transmit Timestamp。"""
    pkt = bytearray(48)
    pkt[0] = 0x23                     # LI=0, VN=4, Mode=3 (client)
    sec, frac = unix_to_ntp(t1, era)
    struct.pack_into('!II', pkt, 40, sec, frac)
    return bytes(pkt), sec, frac


def parse_ntp_response(data, t1, t4, era, expect_xmt):
    """解析服务器应答，返回 (offset, delay, stratum, note)。

    offset 定义为「需要加到本地时钟上的校正量」：
        offset = ((t2 - t1) + (t3 - t4)) / 2
    """
    if len(data) < 48:
        raise NTPProtocolError('short datagram: %d bytes' % len(data))

    first = data[0]
    li = (first >> 6) & 0x3
    vn = (first >> 3) & 0x7
    mode = first & 0x7
    stratum = data[1]

    if mode != 4:
        raise NTPProtocolError('not a server reply (mode=%d)' % mode)
    if li == 3:
        raise NTPProtocolError('server clock not synchronised (LI=3)')
    if stratum == 0 or stratum > 15:
        raise NTPProtocolError('invalid stratum=%d (KoD / unsynchronised)' % stratum)

    w = struct.unpack('!12I', data[:48])
    org_sec, org_frac = w[6], w[7]           # Originate Timestamp
    rec_sec, rec_frac = w[8], w[9]           # Receive Timestamp   = t2
    xmt_sec, xmt_frac = w[10], w[11]         # Transmit Timestamp  = t3

    # 有些 SNTP 实现回 0；非零时必须与我们发出的 t1 一致，否则视为迟到/伪造包
    if (org_sec or org_frac) and (org_sec, org_frac) != expect_xmt:
        raise NTPProtocolError('originate timestamp mismatch (stale/spoofed reply)')

    t1_n = t1 + NTP_DELTA - era
    t4_n = t4 + NTP_DELTA - era
    t2_n = rec_sec + rec_frac / NTP_ERA
    t3_n = xmt_sec + xmt_frac / NTP_ERA

    offset = ((t2_n - t1_n) + (t3_n - t4_n)) / 2.0
    delay = (t4_n - t1_n) - (t3_n - t2_n)

    refid = ''
    if stratum == 1:
        try:
            refid = struct.pack('!I', w[3]).decode('ascii').strip('\x00')
        except Exception:
            refid = ''
    note = 'VN=%d stratum=%d%s' % (vn, stratum, (' ref=' + refid) if refid else '')
    return offset, delay, stratum, note


# ---------------------------------------------------------------------------
# UDP / NTP 查询
# ---------------------------------------------------------------------------
def resolve_udp(host, port):
    """返回 [(family, sockaddr)]，IPv4 优先。"""
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC,
                               socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    addrs = []
    for fam, _typ, _proto, _canon, sa in infos:
        if fam in (socket.AF_INET, socket.AF_INET6):
            addrs.append((fam, sa))
    addrs.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
    return addrs


def query_ntp_once(host, cfg):
    """向单个 NTP 服务器发一次 UDP 包；connect 后内核自动过滤非该来源的应答。"""
    addrs = resolve_udp(host, NTP_PORT)
    if not addrs:
        raise SyncError('DNS returned no usable address for %s' % host)

    last_err = None
    for fam, sa in addrs[:2]:
        sock = None
        try:
            sock = socket.socket(fam, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.settimeout(cfg.timeout)
            sock.connect(sa)

            t1 = time.time()
            p1 = time.perf_counter()
            era = era_base(t1)
            pkt, xmt_sec, xmt_frac = build_ntp_request(t1, era)
            sock.send(pkt)
            data = sock.recv(1024)
            # perf_counter 是单调钟，不受系统时间跳变影响，RTT 比 time.time() 精确
            t4 = t1 + (time.perf_counter() - p1)

            offset, delay, stratum, note = parse_ntp_response(
                data, t1, t4, era, (xmt_sec, xmt_frac))
            return Sample(host, 'ntp/udp', offset, delay, stratum, 0, note)
        except (socket.timeout, socket.error, OSError, NTPProtocolError,
                LookupError) as exc:
            last_err = exc
        finally:
            if sock is not None:
                sock.close()
    raise SyncError('%s: %s' % (host, last_err))


def query_ntp_pool(cfg):
    """并发查询所有 NTP 服务器，每源 --samples 次。"""
    jobs = []
    for host in cfg.servers:
        for _ in range(cfg.samples):
            jobs.append(host)

    samples = []
    errors = []
    if not jobs:
        return samples, errors

    workers = min(len(jobs), 12)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(query_ntp_once, host, cfg): host for host in jobs}
        for fut in as_completed(futures):
            host = futures[fut]
            try:
                samples.append(fut.result())
            except Exception as exc:
                errors.append('%s: %s' % (host, exc))
    return samples, errors


# ---------------------------------------------------------------------------
# TCP / HTTP Date 头查询（UDP 被封时的回退通道）
# ---------------------------------------------------------------------------
def _http_opener(cfg):
    """构造 HTTP(S) opener。

    默认**完全不校验证书**，这是刻意设计而不是偷懒：系统时间一旦错到
    2009 年或 2087 年，任何证书的 NotBefore/NotAfter 都覆盖不到当前时间，
    标准校验必然失败，于是形成「时间不对 -> 证书校验失败 -> 拿不到
    Date 头 -> 时间永远不对」的死锁。这里只读响应头里的 Date，不传输
    任何敏感数据，关掉校验的收益远大于风险。
    """
    handlers = []
    if not cfg.tls_verify:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def query_http_once(url, cfg):
    """读取 HTTP 响应包里的 Date 头，按 Age 修正缓存，估算本地时钟偏差。"""
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Accept': '*/*',
    })
    opener = _http_opener(cfg)
    resp = None
    t1 = time.time()
    p1 = time.perf_counter()
    try:
        try:
            resp = opener.open(req, timeout=cfg.timeout)
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 响应同样带有服务器 Date 头，依旧可用于对时
            resp = exc
        t4 = t1 + (time.perf_counter() - p1)

        date_hdr = resp.headers.get('Date')
        if not date_hdr:
            raise SyncError('no Date header in response')
        dt = email.utils.parsedate_to_datetime(date_hdr)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        server_unix = dt.timestamp()

        # RFC 7234: Date + Age 约为响应被缓存后的当前时刻；CDN 命中时必需修正
        age = 0.0
        age_hdr = resp.headers.get('Age')
        if age_hdr:
            try:
                age = max(0.0, float(age_hdr.strip()))
            except (TypeError, ValueError):
                age = 0.0
        server_unix += age

        # Date 对应服务器生成响应的时刻 ~= (t1 + t4)/2（本地显示时刻）
        offset = server_unix - (t1 + t4) / 2.0
        delay = t4 - t1
        note = 'HTTP Date + Age %.1fs, status=%s' % (age, getattr(resp, 'code', '?'))
        return Sample(url, 'http/tcp', offset, delay, 0, age, note)
    except (urllib.error.URLError, socket.timeout, OSError, ValueError) as exc:
        raise SyncError('%s: %s' % (url, exc))
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def query_http_pool(cfg):
    """并发抓取 TCP 时间源，每源 --samples 次。"""
    jobs = []
    for url in cfg.http_urls:
        for _ in range(cfg.samples):
            jobs.append(url)

    samples = []
    errors = []
    if not jobs:
        return samples, errors

    workers = min(len(jobs), 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(query_http_once, url, cfg): url for url in jobs}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                samples.append(fut.result())
            except Exception as exc:
                errors.append('%s: %s' % (url, exc))
    return samples, errors


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def aggregate(samples, top):
    """按 RTT 取最优的 top 个样本，offset 取中位数，剔除明显离群者。"""
    if not samples:
        raise SyncError('no usable time samples')

    ordered = sorted(samples, key=lambda s: max(0.0, s.delay))
    best = ordered[:max(1, min(top, len(ordered)))]
    offsets = [s.offset for s in best]
    median = statistics.median(offsets)

    # 用中位数做二次离群过滤（UDP 抖动 / CDN 缓存）
    sane = [s for s in best if abs(s.offset - median) <= 3.0]
    if not sane:
        sane = best[:1]

    spread = max(s.offset for s in sane) - min(s.offset for s in sane)
    return {
        'offset': statistics.median([s.offset for s in sane]),
        'spread': spread,
        'rtt': statistics.median([max(0.0, s.delay) for s in sane]),
        'used': sane,
        'all': samples,
    }


def collect(cfg):
    """NTP/UDP 优先；不足 min_sources 时自动降级到 TCP/HTTP Date。"""
    ntp_samples, ntp_errors = query_ntp_pool(cfg)
    for err in ntp_errors:
        LOG.debug('NTP miss: %s', err)

    if len(ntp_samples) >= cfg.min_sources:
        LOG.info('NTP/UDP OK: %d usable samples', len(ntp_samples))
        return aggregate(ntp_samples, cfg.top), 'ntp/udp', ntp_samples, ntp_errors

    LOG.warning('NTP/UDP unusable (%d samples, %d errors) -> falling back to TCP/HTTP Date',
                len(ntp_samples), len(ntp_errors))
    LOG.warning('Reason: UDP/123 may be blocked by firewall/ISP, or DNS failed')
    http_samples, http_errors = query_http_pool(cfg)
    for err in http_errors:
        LOG.debug('HTTP miss: %s', err)

    samples = ntp_samples + http_samples
    errors = ntp_errors + http_errors
    if len(samples) < cfg.min_sources:
        raise SyncError('all time sources failed (UDP/123 blocked and TCP/HTTP Date unreachable). '
                        'Details: %s' % '; '.join(errors[:6]))
    return aggregate(samples, cfg.top), 'http/tcp', samples, errors


# ---------------------------------------------------------------------------
# Windows 系统时间写入
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    class SYSTEMTIME(ctypes.Structure):
        _fields_ = [
            ('wYear', wintypes.WORD),
            ('wMonth', wintypes.WORD),
            ('wDayOfWeek', wintypes.WORD),
            ('wDay', wintypes.WORD),
            ('wHour', wintypes.WORD),
            ('wMinute', wintypes.WORD),
            ('wSecond', wintypes.WORD),
            ('wMilliseconds', wintypes.WORD),
        ]

    class LUID(ctypes.Structure):
        _fields_ = [('LowPart', wintypes.DWORD), ('HighPart', wintypes.LONG)]

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [('Luid', LUID), ('Attributes', wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [('PrivilegeCount', wintypes.DWORD),
                    ('Privileges', LUID_AND_ATTRIBUTES * 1)]

    TOKEN_ADJUST_PRIVILEGES = 0x0020
    TOKEN_QUERY = 0x0008
    SE_PRIVILEGE_ENABLED = 0x00000002
    ERROR_NOT_ALL_ASSIGNED = 1300

    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _adv = ctypes.WinDLL('advapi32', use_last_error=True)
    _sh = ctypes.WinDLL('shell32', use_last_error=True)

    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.SetSystemTime.argtypes = [ctypes.POINTER(SYSTEMTIME)]
    _k32.SetSystemTime.restype = wintypes.BOOL

    _adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.HANDLE)]
    _adv.OpenProcessToken.restype = wintypes.BOOL
    _adv.LookupPrivilegeValueW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                           ctypes.POINTER(LUID)]
    _adv.LookupPrivilegeValueW.restype = wintypes.BOOL
    _adv.AdjustTokenPrivileges.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                           ctypes.POINTER(TOKEN_PRIVILEGES),
                                           wintypes.DWORD, ctypes.c_void_p,
                                           ctypes.c_void_p]
    _adv.AdjustTokenPrivileges.restype = wintypes.BOOL
    _sh.IsUserAnAdmin.restype = wintypes.BOOL

    def is_admin():
        try:
            return bool(_sh.IsUserAnAdmin())
        except Exception:
            return False

    def enable_systemtime_privilege():
        """管理员令牌默认带 SeSystemtimePrivilege 但处于 disabled，必须显式启用。"""
        token = wintypes.HANDLE()
        if not _adv.OpenProcessToken(_k32.GetCurrentProcess(),
                                     TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                     ctypes.byref(token)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            luid = LUID()
            if not _adv.LookupPrivilegeValueW(None, 'SeSystemtimePrivilege',
                                              ctypes.byref(luid)):
                raise ctypes.WinError(ctypes.get_last_error())
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            if not _adv.AdjustTokenPrivileges(token, False, ctypes.byref(tp),
                                              ctypes.sizeof(tp), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == ERROR_NOT_ALL_ASSIGNED:
                raise PrivilegeError('SeSystemtimePrivilege not granted to this token')
        finally:
            _k32.CloseHandle(token)

    def systemtime_from_unix(unix_ts):
        dt = datetime.datetime.utcfromtimestamp(unix_ts)
        # SYSTEMTIME.wDayOfWeek: 0=Sunday；datetime.weekday(): 0=Monday
        return SYSTEMTIME(dt.year, dt.month, (dt.weekday() + 1) % 7, dt.day,
                          dt.hour, dt.minute, dt.second, dt.microsecond // 1000)

    def _win_set_system_time(unix_ts):
        st = systemtime_from_unix(unix_ts)
        if not _k32.SetSystemTime(ctypes.byref(st)):
            raise ctypes.WinError(ctypes.get_last_error())
else:
    def is_admin():
        return False

    def enable_systemtime_privilege():
        raise PlatformError('setting system time is only supported on Windows')

    def systemtime_from_unix(unix_ts):
        raise PlatformError('SYSTEMTIME is Windows-only')

    def _win_set_system_time(unix_ts):
        raise PlatformError('setting system time is only supported on Windows')


def require_write_access():
    """以「能否真正启用 SeSystemtimePrivilege」为准，而不是只看 IsUserAnAdmin。

    服务以 LocalSystem 运行时 IsUserAnAdmin() 并不可靠，直接尝试启用特权
    才是最准确的判定。
    """
    if not IS_WINDOWS:
        raise PlatformError('--apply requires Windows')
    try:
        enable_systemtime_privilege()
    except PrivilegeError:
        raise
    except Exception as exc:
        if is_admin():
            raise PrivilegeError('cannot enable SeSystemtimePrivilege: %s' % exc)
        raise PrivilegeError('administrator rights required; use sync.bat / --elevate / '
                             'run from an elevated command prompt')


def set_system_time(unix_ts, setter=None):
    """可注入 setter，便于 self-test 完整跑通写入链路而不真的改系统时间。"""
    fn = setter or _win_set_system_time
    fn(unix_ts)


def apply_offset(offset, cfg, setter=None):
    """把 offset 应用到系统时钟，返回写入前后的实测信息。"""
    if setter is None:
        require_write_access()
    p0 = time.perf_counter()
    before = time.time()
    set_system_time(before + offset, setter=setter)
    elapsed = time.perf_counter() - p0
    if setter is None:
        after = time.time()
        residual = after - (before + offset + elapsed)
    else:
        after = before + offset
        residual = 0.0
    return {'before': before, 'after': after, 'applied': offset,
            'elapsed': elapsed, 'residual': residual}


def sync_once(cfg, write=None, force=False):
    """查询 -> 校验护栏 -> 可选写入。CLI 与 Windows 服务共用这一条链路。

    返回 dict：chosen / mode / samples / errors / offset / write / refused / skipped。
    """
    chosen, mode, samples, errors = collect(cfg)
    result = {'chosen': chosen, 'mode': mode, 'samples': samples,
              'errors': errors, 'offset': chosen['offset'],
              'write': None, 'refused': None, 'skipped': None}

    if write is None:
        write = cfg.apply
    if not write:
        return result

    if abs(chosen['offset']) > cfg.max_skew and not (cfg.force or force):
        result['refused'] = 'skew'
        return result
    if abs(chosen['offset']) < cfg.tolerance:
        result['skipped'] = 'tolerance'
        return result

    result['write'] = apply_offset(chosen['offset'], cfg)
    return result


def relaunch_elevated():
    """通过 ShellExecuteW(runas) 以管理员重启自身，并去掉 --elevate 防止死循环。"""
    exe = sys.executable
    if exe.lower().endswith('pythonw.exe'):
        exe = os.path.join(os.path.dirname(exe), 'python.exe')
    args = [a for a in sys.argv[1:] if a != '--elevate']
    # 提权后是新控制台窗口，加个标记让窗口停住以便查看结果
    args.append('--elevated-child')
    if getattr(sys, 'frozen', False):
        params = subprocess.list2cmdline(args)
    else:
        params = subprocess.list2cmdline([os.path.abspath(__file__)] + args)
    _sh.ShellExecuteW.restype = ctypes.c_void_p
    workdir = os.path.dirname(os.path.abspath(__file__))
    rc = _sh.ShellExecuteW(None, 'runas', exe, params, workdir, 1)
    if int(rc or 0) <= 32:
        raise PrivilegeError('UAC elevation failed (ShellExecuteW returned %s)' % rc)
    return 0


# ---------------------------------------------------------------------------
# 自检 / 本地端到端调试
# ---------------------------------------------------------------------------
def utc_str(unix_ts):
    return datetime.datetime.utcfromtimestamp(unix_ts).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + ' UTC'


def beijing_str(unix_ts, hours=None):
    """UTC 时间戳 -> 中国标准时间（UTC+8）字符串。"""
    if hours is None:
        hours = DEFAULT_TZ_OFFSET_HOURS
    return (datetime.datetime.utcfromtimestamp(unix_ts + hours * 3600.0)
            .strftime('%Y-%m-%d %H:%M:%S.%f')[:-3] + ' UTC+8')


def local_str(unix_ts):
    """北京时间为主、UTC 为辅，避免把两个时区混在一起看错。"""
    return '%s  (%s)' % (beijing_str(unix_ts), utc_str(unix_ts))


def system_utc_offset_hours():
    """读取当前系统时区相对 UTC 的偏移（小时，含夏令时）。"""
    now = time.time()
    local = datetime.datetime.fromtimestamp(now)
    utc = datetime.datetime.utcfromtimestamp(now)
    return round((local - utc).total_seconds() / 3600.0, 2)


def ensure_china_timezone(apply=False):
    """检查/设置系统时区为中国标准时间。

    返回 (offset_hours, status)，status in {'ok','mismatch','set','failed','unsupported'}。
    """
    offset = system_utc_offset_hours()
    if abs(offset - DEFAULT_TZ_OFFSET_HOURS) < 1e-9:
        return offset, 'ok'
    if not apply:
        return offset, 'mismatch'
    if not IS_WINDOWS:
        return offset, 'unsupported'
    try:
        subprocess.check_output(['tzutil.exe', '/s', CHINA_TZ_NAME],
                                stderr=subprocess.STDOUT)
    except Exception as exc:
        LOG.debug('tzutil failed: %s', exc)
        return offset, 'failed'
    new_offset = system_utc_offset_hours()
    return new_offset, ('set' if abs(new_offset - DEFAULT_TZ_OFFSET_HOURS) < 1e-9
                        else 'failed')


def run_self_test():
    """纯离线逻辑自检：报文、时间换算、era、聚合、SYSTEMTIME、写入链路 mock。"""
    results = []

    def check(name, fn):
        try:
            detail = fn()
            results.append((name, True, detail or ''))
        except Exception as exc:
            results.append((name, False, '%s: %s' % (type(exc).__name__, exc)))

    def t_packet():
        era = era_base(1600000000.0)
        pkt, sec, frac = build_ntp_request(1600000000.0, era)
        assert len(pkt) == 48, len(pkt)
        assert pkt[0] == 0x23, hex(pkt[0])
        assert struct.unpack_from('!II', pkt, 40) == (sec, frac)
        return '48 bytes, LI/VN/Mode=0x23, xmt=%d.%d' % (sec, frac)

    def t_roundtrip():
        era = era_base(1600000000.0)
        for ts in (0.0, 1600000000.125, 1700000000.9999):
            sec, frac = unix_to_ntp(ts, era)
            back = ntp_to_unix(sec, frac, era)
            assert abs(back - ts) < 1e-6, (ts, back)
        return '3 timestamps round-trip within 1 microsecond'

    def t_era2036():
        # 2036 年回绕之后：era 自动切到 1，换算仍须无损
        ts = 2208988800.0 + 100000.0      # 2040 年附近
        era = era_base(ts)
        assert era == NTP_ERA, era
        sec, frac = unix_to_ntp(ts, era)
        assert abs(ntp_to_unix(sec, frac, era) - ts) < 1e-6
        return 'post-2036 era=1 handled'

    def t_ntp_parse():
        # 构造一个已知的应答：真实时间比本地快 5 秒，RTT=0.4s，上下行对称
        t1 = 1600000000.0
        t4 = t1 + 0.4
        era = era_base(t1)
        delta = 5.0                       # 本地时钟慢 5 秒
        up, down, proc = 0.1, 0.1, 0.2
        # 直接在 NTP 域构造，避免 era 混淆
        base = t1 + NTP_DELTA - era
        def to_pair(v):
            s = int(math.floor(v))
            f = int((v - s) * NTP_ERA)
            return s & 0xFFFFFFFF, f
        org_s, org_f = to_pair(base)
        t2_s, t2_f = to_pair(base + up + delta)
        t3_s, t3_f = to_pair(base + up + delta + proc)
        pkt = bytearray(48)
        pkt[0] = 0x24                     # LI=0, VN=4, Mode=4 (server)
        pkt[1] = 2                        # stratum
        struct.pack_into('!12I', pkt, 0,
                         0x24020000, 0, 0, 0,
                         0, 0, org_s, org_f,
                         t2_s, t2_f, t3_s, t3_f)
        offset, delay, stratum, note = parse_ntp_response(
            bytes(pkt), t1, t4, era, (org_s, org_f))
        assert abs(offset - delta) < 1e-6, (offset, delta)
        assert abs(delay - (up + down)) < 1e-6, delay
        assert stratum == 2
        return 'offset=%.6fs delay=%.6fs %s' % (offset, delay, note)

    def t_bad_packets():
        era = era_base(1600000000.0)
        for label, first, stratum in (('LI=3', 0xC4, 2), ('KoD', 0x24, 0),
                                      ('mode=3', 0x23, 2)):
            pkt = bytearray(48)
            pkt[0] = first
            pkt[1] = stratum
            try:
                parse_ntp_response(bytes(pkt), 1600000000.0, 1600000000.1,
                                   era, (0, 0))
            except NTPProtocolError:
                continue
            raise AssertionError('accepted bad packet: ' + label)
        return 'LI=3 / KoD / wrong-mode rejected'

    def t_aggregate():
        ss = [Sample('a', 'ntp/udp', 1.00, 0.02, 2, 0, ''),
              Sample('b', 'ntp/udp', 1.04, 0.03, 2, 0, ''),
              Sample('c', 'ntp/udp', 1.02, 0.04, 2, 0, ''),
              Sample('d', 'ntp/udp', 99.0, 0.90, 2, 0, '')]
        out = aggregate(ss, 3)
        assert abs(out['offset'] - 1.02) < 1e-9, out['offset']
        assert len(out['used']) == 3
        return 'median=%.3f, outlier excluded, used=%d' % (out['offset'], len(out['used']))

    def t_http_math():
        # Date 头为服务器生成时刻：offset = server - (t1+t4)/2
        t1, t4 = 1600000000.0, 1600000000.4
        dt = datetime.datetime.fromtimestamp(1600000003.2, datetime.timezone.utc)
        server = dt.timestamp()
        offset = server - (t1 + t4) / 2.0
        assert abs(offset - 3.0) < 1e-9, offset
        return 'HTTP Date offset=%.3fs' % offset

    def t_beijing():
        assert beijing_str(0) == '1970-01-01 08:00:00.000 UTC+8', beijing_str(0)
        assert beijing_str(1600000000) == '2020-09-13 20:26:40.000 UTC+8', beijing_str(1600000000)
        return 'UTC -> UTC+8 (China Standard Time) conversion OK'

    def t_systemtime():
        if not IS_WINDOWS:
            return 'skipped (not Windows)'
        st = systemtime_from_unix(1600000000.123)
        assert (st.wYear, st.wMonth, st.wDay) == (2020, 9, 13)
        assert st.wMilliseconds == 123
        return 'UTC 2020-09-13 12:26:40.123 -> SYSTEMTIME OK, wDayOfWeek=%d' % st.wDayOfWeek

    def t_apply_mock():
        calls = []
        cfg = type('C', (), {'tolerance': 0.5, 'max_skew': 7200.0})()
        # 直接验证注入 setter 的写入链路
        info = apply_offset(1.25, cfg, setter=lambda ts: calls.append(ts))
        assert len(calls) == 1
        assert abs(calls[0] - (info['before'] + 1.25)) < 1e-9
        return 'setter called once with before+offset, no real clock change'

    def t_rotation():
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix='ntprot_')
        try:
            path = os.path.join(tmp, 'r.log')
            h = SafeRotatingFileHandler(path, 1024, 2)
            lg = logging.getLogger('ntp_sync.selftest.rot')
            lg.handlers = [h]
            lg.setLevel(logging.INFO)
            lg.propagate = False
            for i in range(80):
                lg.info('padding %03d %s', i, 'p' * 40)
            h.flush()
            assert os.path.getsize(path) <= 1300, os.path.getsize(path)
            assert os.path.exists(path + '.1'), 'no rotated backup'
            assert not os.path.exists(path + '.3'), 'backups over limit'
            total = sum(os.path.getsize(p) for p in
                        [path, path + '.1', path + '.2'] if os.path.exists(p))
            return '1 KiB x 2 backups, %d bytes total, capped' % total
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    check('NTP request packet', t_packet)
    check('NTP timestamp round-trip', t_roundtrip)
    check('2036 NTP era rollover', t_era2036)
    check('NTP response / offset+delay math', t_ntp_parse)
    check('Malformed NTP reply rejection', t_bad_packets)
    check('Multi-source aggregation', t_aggregate)
    check('HTTP Date offset math', t_http_math)
    check('UTC+8 Beijing conversion', t_beijing)
    check('Log rotation (size-capped)', t_rotation)
    check('SYSTEMTIME conversion', t_systemtime)
    check('Apply path (mocked setter)', t_apply_mock)

    failed = 0
    for name, ok, detail in results:
        line = ('PASS' if ok else 'FAIL') + '  ' + name + ('  | ' + detail if detail else '')
        print(line)
        if not ok:
            failed += 1
    print('-' * 62)
    print('%d/%d checks passed; Python %s, %d-bit, platform=%s' %
          (len(results) - failed, len(results),
           '.'.join(str(v) for v in sys.version_info[:3]),
           struct.calcsize('P') * 8, sys.platform))
    return 0 if failed == 0 else 1


def run_selftest_apply(cfg):
    """真实写入调试：注入一个很小的偏差 -> 跑完整同步链路 -> 无条件恢复原时间。

    perf_counter 是单调钟，所以即便系统时间被改动，也能精确算出真实流逝时间，
    finally 中把时钟还原成「原时间 + 真实流逝」，保证零残留。
    """
    require_write_access()

    skew = max(-7200.0, min(7200.0, float(cfg.selftest_offset)))
    if skew == 0:
        skew = 90.0
    LOG.warning('SELFTEST-APPLY: will inject %+.1f s skew then restore (max +/-7200 s)', skew)

    t0 = time.time()
    p0 = time.perf_counter()
    report = {'injected_skew': skew, 'restored': False, 'sync_ok': False}
    changed = False
    try:
        set_system_time(t0 + skew)
        changed = True
        skewed = time.time()
        LOG.info('SELFTEST: local clock now %s (was %s)', utc_str(skewed), utc_str(t0))
        assert abs(skewed - (t0 + skew)) < 1.5, 'injection did not take effect'

        chosen, mode, samples, errors = collect(cfg)
        detected = chosen['offset']
        LOG.info('SELFTEST: %s detected offset %+.3f s from %d samples (expect ~%+.1f s)',
                 mode, detected, len(samples), -skew)
        if abs(detected) > cfg.max_skew and not cfg.force:
            raise SyncError('detected offset %.1fs exceeds --max-skew %.1fs' %
                            (detected, cfg.max_skew))
        report['original_skew'] = detected + skew
        if abs(detected + skew) > 30.0:
            raise SyncError('detected offset %+.3fs does not match injected %+.1fs' %
                            (detected, skew))

        info = apply_offset(detected, cfg)
        report['sync_ok'] = True
        report['mode'] = mode
        report['detected_offset'] = detected
        report['residual'] = info['residual']
        report['after_sync'] = utc_str(info['after'])
        LOG.info('SELFTEST: SetSystemTime succeeded, residual=%.4f s, clock=%s',
                 info['residual'], report['after_sync'])
    except Exception as exc:
        report['error'] = '%s: %s' % (type(exc).__name__, exc)
        LOG.error('SELFTEST: %s', report['error'])
    finally:
        # 无论成败都恢复；这一步必须成功，否则明确报错
        elapsed = time.perf_counter() - p0
        if changed:
            try:
                set_system_time(t0 + elapsed)
                report['restored'] = True
                LOG.info('SELFTEST: restored clock to %s (elapsed %.3f s)',
                         utc_str(t0 + elapsed), elapsed)
            except Exception as exc:
                report['restore_error'] = str(exc)
                LOG.error('SELFTEST: RESTORE FAILED - %s', exc)

    if cfg.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print('SELFTEST-APPLY REPORT')
        for key in sorted(report):
            print('  %-18s %s' % (key, report[key]))
    ok = (report['restored'] and report['sync_ok'] and 'error' not in report)
    print('RESULT: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog='ntp_sync.py',
        description='Sync Windows system clock via NTP/UDP, auto-fallback to TCP/HTTP Date.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            'Default is CHECK ONLY (no system time change).\n'
            '  sync.bat              -> apply with automatic UAC elevation\n'
            '  ntp_sync.py --apply   -> apply from an elevated prompt\n'
            '  ntp_sync.py --self-test        -> offline logic checks\n'
            '  ntp_sync.py --selftest-apply  -> real write test, small skew, auto restore\n'
        ))
    p.add_argument('--server', action='append', metavar='HOST',
                   help='NTP server (repeatable; overrides the default list)')
    p.add_argument('--http-url', action='append', metavar='URL',
                   help='TCP/HTTP Date source (repeatable; overrides the default list)')
    p.add_argument('--timeout', type=float, default=2.0, metavar='SEC',
                   help='per-request timeout (default 2.0)')
    p.add_argument('--samples', type=int, default=2, metavar='N',
                   help='queries per source (default 2)')
    p.add_argument('--min-sources', type=int, default=1, metavar='N',
                   help='min usable samples before accepting NTP (default 1)')
    p.add_argument('--top', type=int, default=3, metavar='N',
                   help='use the N lowest-RTT samples (default 3)')
    p.add_argument('--tolerance', type=float, default=0.5, metavar='SEC',
                   help='skip setting when |offset| is below this (default 0.5)')
    p.add_argument('--max-skew', type=float, default=7200.0, metavar='SEC',
                   help='REFUSE to set when |offset| exceeds this (default 7200 = 2h)')
    p.add_argument('--force', action='store_true',
                   help='allow a clock jump larger than --max-skew (dangerous)')
    p.add_argument('--apply', action='store_true',
                   help='actually write the system clock (needs administrator)')
    p.add_argument('--elevate', action='store_true',
                   help='restart itself as administrator via UAC if needed')
    p.add_argument('--verify-tls', dest='tls_verify', action='store_true',
                   help='verify TLS certificates (OFF by default: a wrong clock breaks '
                        'certificate validation and would deadlock the TCP fallback)')
    p.add_argument('--set-timezone', action='store_true',
                   help='set the Windows time zone to China Standard Time (UTC+8)')
    p.add_argument('--json', action='store_true', help='emit JSON on stdout')
    p.add_argument('--log-file', metavar='PATH',
                   help='append log to this file (UTF-8, size-rotated)')
    p.add_argument('--log-max-bytes', type=int, default=DEFAULT_LOG_MAX_BYTES,
                   metavar='N',
                   help='rotate the log file after N bytes (default 1 MiB, 0 disables)')
    p.add_argument('--log-backups', type=int, default=DEFAULT_LOG_BACKUPS,
                   metavar='N', help='number of rotated log files to keep (default 5)')
    p.add_argument('--verbose', action='store_true', help='debug logging')
    p.add_argument('--self-test', action='store_true', help='offline logic self-test')
    p.add_argument('--selftest-apply', action='store_true',
                   help='real SetSystemTime test with small injected skew + restore')
    p.add_argument('--selftest-offset', type=float, default=90.0, metavar='SEC',
                   help='injected skew for --selftest-apply, clamped to +/-7200 (default 90)')
    p.add_argument('--elevated-child', action='store_true',
                   help=argparse.SUPPRESS)
    p.add_argument('--version', action='version', version='ntp_sync ' + VERSION)
    return p


def parse_args(argv):
    p = build_parser()
    cfg = p.parse_args(argv)
    cfg.servers = cfg.server or list(DEFAULT_NTP_SERVERS)
    cfg.http_urls = cfg.http_url or list(DEFAULT_HTTP_URLS)
    if cfg.timeout <= 0:
        p.error('--timeout must be > 0')
    if cfg.samples < 1:
        p.error('--samples must be >= 1')
    if cfg.min_sources < 1:
        p.error('--min-sources must be >= 1')
    if cfg.top < 1:
        p.error('--top must be >= 1')
    if cfg.max_skew < 0:
        p.error('--max-skew must be >= 0')
    if cfg.log_max_bytes < 0:
        p.error('--log-max-bytes must be >= 0')
    if cfg.log_backups < 0:
        p.error('--log-backups must be >= 0')
    return cfg


def _shift_backups(path, backups):
    """把 path.1..path.N-1 依次后移一位，为新的 path.1 腾位置。"""
    for i in range(backups - 1, 0, -1):
        src = '%s.%d' % (path, i)
        dst = '%s.%d' % (path, i + 1)
        if not os.path.exists(src):
            continue
        try:
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(src, dst)
        except OSError:
            # 单个历史文件挪不动就跳过，不要因此中断整条日志链路
            continue


def rotate_log_file(path, backups=DEFAULT_LOG_BACKUPS):
    """轮转一次：path.1->path.2 ... path.1 腾空后再把 path 挪过去。

    返回 'rotated' / 'copied' / 'truncated' / 'missing' / 'empty'。
    顺序很关键：必须先把历史文件整体后移，最后才把当前文件落到 .1，
    否则会把刚生成的新备份又推开一格、.1 永远是空的。
    先试 os.rename（快、原子）；Windows 上只要有人（记事本、tail、杀毒）
    正开着文件，rename 就会因共享冲突失败，此时退化为「复制到 .1 + 截断
    原文件」，这条路只需要写权限，不需要删除权限。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return 'missing'
    if size <= 0:
        return 'empty'

    if backups <= 0:
        try:
            with open(path, 'r+b') as f:
                f.truncate(0)
            return 'truncated'
        except OSError:
            return 'missing'

    _shift_backups(path, backups)
    first = '%s.1' % path
    try:
        os.rename(path, first)
        return 'rotated'
    except OSError:
        pass

    try:
        import shutil
        shutil.copyfile(path, first)
        with open(path, 'r+b') as f:
            f.truncate(0)
        return 'copied'
    except OSError:
        return 'missing'


class SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """按大小轮转的 UTF-8 日志文件，占用中也不会把日志写死。

    标准库 RotatingFileHandler 在 Windows 上靠 os.rename 轮转，文件被别人
    打开时会抛 PermissionError；logging 内部把 handler 的异常吞掉，结果
    是日志静默停止增长 —— 对「装完就不管」的开机服务来说这是致命的。
    这里把轮转失败降级为「复制 + 截断」，并且任何异常都不外抛。
    """

    def __init__(self, filename, maxBytes=DEFAULT_LOG_MAX_BYTES,
                 backupCount=DEFAULT_LOG_BACKUPS, encoding='utf-8'):
        # 注意：errors= 参数在 Python 3.9 才进 RotatingFileHandler，
        # 这里不能用，否则 3.7 直接 TypeError。
        logging.handlers.RotatingFileHandler.__init__(
            self, filename, mode='a', maxBytes=int(maxBytes),
            backupCount=int(backupCount), encoding=encoding, delay=False)

    def doRollover(self):
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
        except Exception:
            self.stream = None
        try:
            if self.backupCount > 0:
                rotate_log_file(self.baseFilename, self.backupCount)
        except Exception:
            pass
        if not self.delay:
            try:
                self.stream = self._open()
            except Exception:
                self.stream = None

    def handleError(self, record):
        # 日志本身出错绝不能影响同步主流程，也不要在 stderr 上刷 traceback
        pass


def make_log_handler(path, max_bytes=DEFAULT_LOG_MAX_BYTES,
                     backups=DEFAULT_LOG_BACKUPS):
    """创建轮转日志 handler；目录不存在就建，建不了就返回 None。"""
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        return SafeRotatingFileHandler(path, max_bytes, backups)
    except Exception as exc:
        sys.stderr.write('cannot open log file %s: %s\n' % (path, exc))
        return None


def setup_logging(cfg):
    handlers = [logging.StreamHandler(sys.stderr)]
    if cfg.log_file:
        fh = make_log_handler(cfg.log_file, cfg.log_max_bytes, cfg.log_backups)
        if fh is not None:
            handlers.append(fh)
    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.INFO,
        format='[%(levelname)s] %(message)s',
        handlers=handlers)


def fmt_signed(v, unit='s'):
    return '%+.3f %s' % (v, unit)


def print_report(chosen, mode, samples, errors, cfg, write_info):
    if cfg.json:
        out = {
            'version': VERSION,
            'mode': mode,
            'offset_seconds': chosen['offset'],
            'rtt_seconds': chosen['rtt'],
            'spread_seconds': chosen['spread'],
            'sample_count': len(samples),
            'max_skew_seconds': cfg.max_skew,
            'tz_offset_hours': system_utc_offset_hours(),
            'applied': bool(write_info),
            'samples': [{'source': s.source, 'kind': s.kind,
                         'offset': round(s.offset, 6), 'rtt': round(s.delay, 6),
                         'stratum': s.stratum, 'age': s.age, 'note': s.note}
                        for s in samples],
            'errors': errors,
        }
        if write_info:
            out['write'] = {k: round(v, 6) if isinstance(v, float) else v
                            for k, v in write_info.items()}
        print(json.dumps(out, indent=2, sort_keys=True))
        return

    print('')
    print('=' * 66)
    print(' ntp_sync %s   Python %s (%d-bit)   %s' %
          (VERSION, '.'.join(str(v) for v in sys.version_info[:3]),
           struct.calcsize('P') * 8, sys.platform))
    print('=' * 66)
    tz_offset = system_utc_offset_hours()
    tz_ok = abs(tz_offset - DEFAULT_TZ_OFFSET_HOURS) < 1e-9
    print(' Time standard : UTC+8 China Standard Time%s' %
          ('  [system TZ OK]' if tz_ok else
           '  [WARNING: system TZ is UTC%+g, use --set-timezone]' % tz_offset))
    print(' Time source   : %s%s' %
          (mode, '  [UDP blocked -> TCP fallback]' if mode == 'http/tcp' else ''))
    print(' Samples       : %d usable, %d errors' % (len(samples), len(errors)))
    print(' Best source   : %s (rtt %.1f ms)' %
          (chosen['used'][0].source, chosen['rtt'] * 1000.0))
    print(' Offset        : %s   (local clock is %s)' %
          (fmt_signed(chosen['offset']),
           'BEHIND' if chosen['offset'] > 0 else 'AHEAD'))
    print(' Spread        : %.3f s across selected samples' % chosen['spread'])
    print(' Skew guard    : |offset| %.3f s %s %.1f s -> %s' %
          (abs(chosen['offset']), '<=' if abs(chosen['offset']) <= cfg.max_skew else '>',
           cfg.max_skew,
           'OK' if abs(chosen['offset']) <= cfg.max_skew or cfg.force else 'REFUSED'))
    print('-' * 66)
    for s in sorted(samples, key=lambda x: max(0.0, x.delay)):
        print('  %-30s %-9s off %+8.3f s  rtt %7.1f ms  %s' %
              (s.source[:30], s.kind, s.offset, max(0.0, s.delay) * 1000.0, s.note))
    for err in errors[:5]:
        print('  ! %s' % err[:100])
    if errors:
        print('  (%d errors total)' % len(errors))
    print('-' * 66)
    if write_info:
        print(' WRITE         : before %s' % local_str(write_info['before']))
        print('                 after  %s' % local_str(write_info['after']))
        print('                 residual %.4f s' % write_info['residual'])
    else:
        print(' ACTION        : CHECK ONLY - system time NOT changed')
        print('                 use --apply / sync.bat to write the clock')
    print(' NOTE          : if the Windows Time service (w32time) is running,')
    print('                 it may overwrite a manually set clock later.')
    print('=' * 66)


def main(argv=None):
    cfg = parse_args(argv)
    setup_logging(cfg)

    if cfg.self_test:
        return run_self_test()
    if cfg.selftest_apply:
        return run_selftest_apply(cfg)

    if cfg.elevate and IS_WINDOWS and not is_admin():
        LOG.info('not elevated -> requesting UAC')
        return relaunch_elevated()

    if cfg.set_timezone:
        tz_offset, tz_status = ensure_china_timezone(apply=True)
        if tz_status == 'set':
            LOG.info('system time zone set to %s (UTC+8)', CHINA_TZ_NAME)
        elif tz_status == 'ok':
            LOG.info('system time zone already UTC+8')
        else:
            LOG.warning('could not set time zone to %s (status=%s, offset=%+g)',
                        CHINA_TZ_NAME, tz_status, tz_offset)

    try:
        result = sync_once(cfg)
    except (PrivilegeError, PlatformError) as exc:
        LOG.error('cannot write system clock: %s', exc)
        if cfg.json:
            print(json.dumps({'error': str(exc)}, indent=2))
        return 4
    except OSError as exc:
        LOG.error('cannot write system clock: %s', exc)
        if cfg.json:
            print(json.dumps({'error': str(exc)}, indent=2))
        return 4
    except SyncError as exc:
        LOG.error('sync failed: %s', exc)
        if cfg.json:
            print(json.dumps({'error': str(exc)}, indent=2))
        return 2

    if result.get('refused') == 'skew':
        LOG.error('REFUSED: offset %.3f s exceeds --max-skew %.3f s. '
                  'Refusing a huge clock jump; use --force only if you really mean it.',
                  result['offset'], cfg.max_skew)
        if cfg.json:
            print(json.dumps({'error': 'skew guard refused',
                              'offset': result['offset'],
                              'max_skew': cfg.max_skew}, indent=2))
        return 3
    if result.get('skipped') == 'tolerance':
        LOG.info('offset %.3f s < tolerance %.3f s, clock already accurate; nothing to do',
                 result['offset'], cfg.tolerance)

    print_report(result['chosen'], result['mode'], result['samples'],
                 result['errors'], cfg, result['write'])
    if cfg.elevated_child and sys.stdin.isatty():
        try:
            input('\nPress Enter to close this window... ')
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        LOG.warning('interrupted')
        sys.exit(130)
    except Exception as exc:
        LOG.error('unexpected: %s: %s', type(exc).__name__, exc)
        sys.exit(1)
