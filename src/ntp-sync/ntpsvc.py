#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ntpsvc.py - 纯 ctypes 的 Windows 服务宿主，把 ntp_sync 装成开机自启服务。

零第三方依赖：不需要 pywin32、NSSM、srvany，只用标准库 + advapi32。
服务以 LocalSystem 运行，在用户登录之前（尚未进入桌面时）就开始对时。

两套回退策略，专门对付「网络服务比时间服务晚启动 / 开机压根没网」：
  1. 启动阶段还没成功过时，失败后每 5 秒重试一次，最多 60 次（共 5 分钟）；
  2. 全程监听网络状态，一旦从「无网」变「有网」（或上次同步失败后网络可用），
     立即补一次同步，不用干等 interval（默认 30 分钟）到点。
启动重试耗尽后转入 interval 周期模式，但网络监听不会关。

子命令：
  install [--interval 1800]   注册为自动启动服务并立即启动（需管理员）
  uninstall                   停止并删除服务（需管理员）
  start / stop / status       控制与查询
  set-tz                      把系统时区设为中国标准时间 UTC+8（需管理员）
  run                         服务入口，由 SCM 调用，不要手动执行
  debug                       前台跑同步循环，便于排错（Ctrl+C 退出）
"""

import argparse
import ctypes
import io
import logging
import os
import socket
import sys
import threading
import time
from ctypes import wintypes

import ntp_sync

SERVICE_NAME = 'NtpSync'
SERVICE_DISPLAY = 'NTP Time Sync (China Standard Time)'
SERVICE_DESC = ('Syncs the system clock to China Standard Time (UTC+8) at boot, '
                'before logon, over NTP/UDP with a TCP/HTTP Date fallback.')

SERVICE_TYPE = 0x00000010            # SERVICE_WIN32_OWN_PROCESS
SERVICE_AUTO_START = 0x00000002
SERVICE_ERROR_NORMAL = 0x00000001
SERVICE_ACCEPT_STOP = 0x00000001
SERVICE_ACCEPT_SHUTDOWN = 0x00000004
SERVICE_CONTROL_STOP = 0x00000001
SERVICE_CONTROL_INTERROGATE = 0x00000004
SERVICE_CONTROL_SHUTDOWN = 0x00000005
SERVICE_STOPPED = 0x00000001
SERVICE_START_PENDING = 0x00000002
SERVICE_STOP_PENDING = 0x00000003
SERVICE_RUNNING = 0x00000004
SC_MANAGER_ALL_ACCESS = 0x000F003F
SERVICE_ALL_ACCESS = 0x000F01FF
ERROR_SERVICE_EXISTS = 1073
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_ALREADY_RUNNING = 1056

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(BASE_DIR, 'service.log')
CONF_PATH = os.path.join(BASE_DIR, 'service.ini')
PYTHON_EXE = os.path.join(BASE_DIR, 'python.exe')
SELF_PY = os.path.join(BASE_DIR, 'ntpsvc.py')

DEFAULT_INTERVAL = 1800
# 开机阶段：网络常常比服务晚起来，失败后密集重试 5s x 60 = 5 分钟。
DEFAULT_BOOT_RETRY_INTERVAL = 5.0
DEFAULT_BOOT_RETRY_LIMIT = 60
# 网络监听：每 3 秒探一次；恢复后 30 秒内不重复触发，防止网络抖动刷屏。
DEFAULT_NET_CHECK_INTERVAL = 3.0
NET_RECOVERY_COOLDOWN = 30.0

# 服务日志轮转：1 MiB x 5 份历史 + 当前文件，最坏占用约 6 MiB。
# 30 分钟一行，1 MiB 能存半年以上，不必担心无人值守的机器把盘写满。
DEFAULT_LOG_MAX_BYTES = ntp_sync.DEFAULT_LOG_MAX_BYTES
DEFAULT_LOG_BACKUPS = ntp_sync.DEFAULT_LOG_BACKUPS
STATE_NAMES = {1: 'STOPPED', 2: 'START_PENDING', 3: 'STOP_PENDING',
               4: 'RUNNING', 5: 'CONTINUE_PENDING', 6: 'PAUSE_PENDING',
               7: 'PAUSED'}


class SERVICE_STATUS(ctypes.Structure):
    _fields_ = [('dwServiceType', wintypes.DWORD),
                ('dwCurrentState', wintypes.DWORD),
                ('dwControlsAccepted', wintypes.DWORD),
                ('dwWin32ExitCode', wintypes.DWORD),
                ('dwServiceSpecificExitCode', wintypes.DWORD),
                ('dwCheckPoint', wintypes.DWORD),
                ('dwWaitHint', wintypes.DWORD)]


SERVICE_STATUS_HANDLE = ctypes.c_void_p
SERVICE_MAIN_FUNCTION = ctypes.WINFUNCTYPE(None, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.LPWSTR))
HANDLER_FUNCTION = ctypes.WINFUNCTYPE(None, wintypes.DWORD)


class SERVICE_TABLE_ENTRY(ctypes.Structure):
    _fields_ = [('lpServiceName', wintypes.LPWSTR),
                ('lpServiceProc', ctypes.c_void_p)]


class SERVICE_DESCRIPTION(ctypes.Structure):
    _fields_ = [('lpDescription', wintypes.LPWSTR)]


_adv = ctypes.WinDLL('advapi32', use_last_error=True)
_adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_adv.OpenSCManagerW.restype = wintypes.HANDLE
_adv.CreateServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.LPCWSTR]
_adv.CreateServiceW.restype = wintypes.HANDLE
_adv.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD]
_adv.OpenServiceW.restype = wintypes.HANDLE
_adv.StartServiceW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
_adv.StartServiceW.restype = wintypes.BOOL
_adv.ControlService.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                ctypes.POINTER(SERVICE_STATUS)]
_adv.ControlService.restype = wintypes.BOOL
_adv.QueryServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(SERVICE_STATUS)]
_adv.QueryServiceStatus.restype = wintypes.BOOL
_adv.DeleteService.argtypes = [wintypes.HANDLE]
_adv.DeleteService.restype = wintypes.BOOL
_adv.CloseServiceHandle.argtypes = [wintypes.HANDLE]
_adv.CloseServiceHandle.restype = wintypes.BOOL
_adv.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(SERVICE_TABLE_ENTRY)]
_adv.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
_adv.RegisterServiceCtrlHandlerW.argtypes = [wintypes.LPCWSTR, HANDLER_FUNCTION]
_adv.RegisterServiceCtrlHandlerW.restype = SERVICE_STATUS_HANDLE
_adv.SetServiceStatus.argtypes = [SERVICE_STATUS_HANDLE, ctypes.POINTER(SERVICE_STATUS)]
_adv.SetServiceStatus.restype = wintypes.BOOL
_adv.ChangeServiceConfig2W.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
_adv.ChangeServiceConfig2W.restype = wintypes.BOOL

_status_handle = None
_status = SERVICE_STATUS()
_stop_event = threading.Event()
_handler_ref = None          # 必须保活，否则 ctypes 回调被 GC 后服务会崩


# ---------------------------------------------------------------------------
# 服务状态上报
# ---------------------------------------------------------------------------
def _report(state, controls=0, win32_exit=0, hint=0, checkpoint=0):
    _status.dwServiceType = SERVICE_TYPE
    _status.dwCurrentState = state
    _status.dwControlsAccepted = controls
    _status.dwWin32ExitCode = win32_exit
    _status.dwServiceSpecificExitCode = 0
    _status.dwCheckPoint = checkpoint
    _status.dwWaitHint = hint
    if _status_handle:
        _adv.SetServiceStatus(_status_handle, ctypes.byref(_status))


def _handler(control):
    if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
        _report(SERVICE_STOP_PENDING, 0, hint=8000)
        _stop_event.set()


# ---------------------------------------------------------------------------
# 网络可用性探测
# ---------------------------------------------------------------------------
# InternetGetConnectedState 只能说明「有链路」，不能说明 DNS/外网可用；
# 开机时网卡往往已经 up 但 DHCP/DNS 还没好，所以再叠一层 DNS 解析。
# 解析放进守护线程，避免 DNS 卡住时阻塞服务停止。
_wininet = None
if sys.platform == 'win32':
    try:
        _wininet = ctypes.WinDLL('wininet', use_last_error=True)
        _wininet.InternetGetConnectedState.argtypes = [
            ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        _wininet.InternetGetConnectedState.restype = wintypes.BOOL
    except Exception:
        _wininet = None

_net_probe_lock = threading.Lock()
_net_probe_busy = False


def _dns_reachable(host, timeout=2.0):
    """DNS 解析探测。返回 True / False / None（上次还在跑，本次不确定）。"""
    global _net_probe_busy
    with _net_probe_lock:
        if _net_probe_busy:
            return None
        _net_probe_busy = True
    box = []

    def probe():
        global _net_probe_busy
        try:
            socket.getaddrinfo(host, 123, 0, socket.SOCK_DGRAM)
            box.append(True)
        except Exception:
            box.append(False)
        finally:
            with _net_probe_lock:
                _net_probe_busy = False

    t = threading.Thread(target=probe, name='ntp-net-probe')
    t.daemon = True
    t.start()
    t.join(timeout)
    return box[0] if box else None


def network_ready(cfg=None):
    """返回 True（有网）/ False（无网）/ None（不确定，不触发状态变化）。"""
    if _wininet is not None:
        flags = wintypes.DWORD(0)
        try:
            if not _wininet.InternetGetConnectedState(ctypes.byref(flags), 0):
                return False
        except Exception:
            pass  # API 不可用就只看 DNS
    servers = getattr(cfg, 'ntp_servers', None) if cfg is not None else None
    if not servers:
        servers = ntp_sync.DEFAULT_NTP_SERVERS
    host = servers[0] if servers else None
    if not host:
        return None
    return _dns_reachable(host)


# ---------------------------------------------------------------------------
# 日志与配置
# ---------------------------------------------------------------------------
def _setup_service_logging(console=False):
    logger = logging.getLogger('ntp_sync')
    logger.setLevel(logging.INFO)
    for h in list(logger.handlers):
        logger.removeHandler(h)
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    ini = _load_ini()
    fh = ntp_sync.make_log_handler(LOG_PATH, ini['log_max_bytes'],
                                   ini['log_backups'])
    if fh is not None:
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    if console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    logger.propagate = False
    return logger


def _log_files(backups=None):
    """当前日志文件 + 轮转出来的历史文件，用于 status 展示。"""
    if backups is None:
        backups = _load_ini()['log_backups']
    names = [LOG_PATH] + ['%s.%d' % (LOG_PATH, i) for i in range(1, backups + 1)]
    return [n for n in names if os.path.exists(n)]


def _load_ini():
    cfg = {'interval': DEFAULT_INTERVAL, 'max_skew': 7200.0, 'tolerance': 0.5,
           'boot_retry_interval': DEFAULT_BOOT_RETRY_INTERVAL,
           'boot_retry_limit': DEFAULT_BOOT_RETRY_LIMIT,
           'net_check_interval': DEFAULT_NET_CHECK_INTERVAL,
           'log_max_bytes': DEFAULT_LOG_MAX_BYTES,
           'log_backups': DEFAULT_LOG_BACKUPS}
    if os.path.isfile(CONF_PATH):
        try:
            import configparser
            cp = configparser.ConfigParser()
            cp.read(CONF_PATH, encoding='utf-8')
            if cp.has_section('sync'):
                cfg['interval'] = cp.getint('sync', 'interval', fallback=cfg['interval'])
                cfg['max_skew'] = cp.getfloat('sync', 'max_skew', fallback=cfg['max_skew'])
                cfg['tolerance'] = cp.getfloat('sync', 'tolerance', fallback=cfg['tolerance'])
                cfg['boot_retry_interval'] = cp.getfloat(
                    'sync', 'boot_retry_interval', fallback=cfg['boot_retry_interval'])
                cfg['boot_retry_limit'] = cp.getint(
                    'sync', 'boot_retry_limit', fallback=cfg['boot_retry_limit'])
                cfg['net_check_interval'] = cp.getfloat(
                    'sync', 'net_check_interval', fallback=cfg['net_check_interval'])
            if cp.has_section('log'):
                cfg['log_max_bytes'] = cp.getint(
                    'log', 'max_bytes', fallback=cfg['log_max_bytes'])
                cfg['log_backups'] = cp.getint(
                    'log', 'backups', fallback=cfg['log_backups'])
        except Exception as exc:
            logging.getLogger('ntp_sync').warning('bad service.ini: %s', exc)
    if cfg['log_max_bytes'] < 0:
        cfg['log_max_bytes'] = DEFAULT_LOG_MAX_BYTES
    if cfg['log_backups'] < 0:
        cfg['log_backups'] = DEFAULT_LOG_BACKUPS
    # 参数兜底：配置文件被人手滑改成 0 / 负数 / 离谱大时不能把服务卡死
    cfg['boot_retry_interval'] = max(0.1, min(float(cfg['boot_retry_interval']), 3600.0))
    cfg['boot_retry_limit'] = max(1, min(int(cfg['boot_retry_limit']), 10000))
    cfg['net_check_interval'] = max(0.5, min(float(cfg['net_check_interval']), 300.0))
    return cfg


def _service_cfg():
    ini = _load_ini()
    cfg = ntp_sync.parse_args([])
    cfg.apply = True
    cfg.log_file = None
    cfg.interval = max(60, int(ini['interval']))
    cfg.max_skew = float(ini['max_skew'])
    cfg.tolerance = float(ini['tolerance'])
    cfg.boot_retry_interval = float(ini['boot_retry_interval'])
    cfg.boot_retry_limit = int(ini['boot_retry_limit'])
    cfg.net_check_interval = float(ini['net_check_interval'])
    return cfg


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------
def _guard_log_size(log):
    """双保险：即使日志被外部进程追加过，也保证单文件不超过 max_bytes。

    RotatingFileHandler 只在自己写的时候检查大小；服务 30 分钟才写一行，
    中间如果有人往里灌东西，就要靠这里在每轮任务开头兜一下。
    """
    ini = _load_ini()
    max_bytes = ini['log_max_bytes']
    if max_bytes <= 0:
        return
    handled = False
    for h in list(log.handlers):
        if not isinstance(h, ntp_sync.SafeRotatingFileHandler):
            continue
        handled = True
        try:
            if os.path.getsize(h.baseFilename) < max_bytes:
                continue
        except OSError:
            continue
        # 走 handler 自己的轮转，它会先关流再重开，句柄不会失效
        try:
            h.doRollover()
            log.info('log rotated at %d bytes, keeping %d backups',
                     max_bytes, ini['log_backups'])
        except Exception:
            pass
    if not handled:
        # 日志文件没能挂上 handler（比如目录只读）时，至少别让文件无限长大
        try:
            if os.path.getsize(LOG_PATH) >= max_bytes:
                ntp_sync.rotate_log_file(LOG_PATH, ini['log_backups'])
        except OSError:
            pass


def _run_loop(cfg, log, sync_fn, net_probe_fn=None, clock=None,
              sleeper=None, guard=None, stop_event=None):
    """同步调度核心。依赖全部可注入，便于本地用假时钟/假网络快速验证。

    两个阶段：
      boot   —— 还没成功过，失败后每 boot_interval 秒重试，最多 boot_limit 次
      steady —— 成功后按 interval 周期同步，同时持续监听网络恢复
    """
    stop_event = stop_event or _stop_event
    clock = clock or time.time
    sleeper = sleeper or stop_event.wait
    net_probe_fn = net_probe_fn or network_ready
    guard = guard or (lambda: None)

    interval = float(cfg.interval)
    boot_interval = float(getattr(cfg, 'boot_retry_interval',
                                  DEFAULT_BOOT_RETRY_INTERVAL))
    boot_limit = int(getattr(cfg, 'boot_retry_limit', DEFAULT_BOOT_RETRY_LIMIT))
    net_interval = float(getattr(cfg, 'net_check_interval',
                                 DEFAULT_NET_CHECK_INTERVAL))

    phase = 'boot'
    boot_attempts = 0
    next_sync = 0.0
    next_net_check = 0.0
    net_up = None
    last_net_trigger = -1e12
    last_sync_failed = False

    while not stop_event.is_set():
        now = clock()

        # ---- 网络状态监测：两个阶段都开着 ----
        by_network = False
        if now >= next_net_check:
            next_net_check = now + net_interval
            try:
                up = net_probe_fn(cfg)
            except Exception as exc:
                log.debug('network probe error: %s', exc)
                up = None
            if up is not None:
                recovered = (net_up is False and up is True)
                # DNS 一直通但 NTP 曾失败的情况，也算「网络侧恢复了」
                needs_retry = (phase == 'steady' and last_sync_failed and up is True)
                if ((recovered or needs_retry)
                        and now - last_net_trigger >= NET_RECOVERY_COOLDOWN):
                    by_network = True
                    last_net_trigger = now
                    log.info('network is available (previous=%s), '
                             'triggering an extra sync',
                             'down' if net_up is False else 'unsynced')
                net_up = up

        if not by_network and now < next_sync:
            # 用小步睡眠代替一次长 sleep，网络探测和停止信号才能及时响应
            sleeper(min(0.5, max(0.01, min(next_sync, next_net_check) - now)))
            continue

        guard()
        try:
            # boot 阶段允许大跨度修正（CMOS 掉电可能差好几年）；
            # 一旦成功就关掉 force，运行期不发生大跳变。
            result = sync_fn(cfg, write=True, force=(phase == 'boot'))
        except Exception as exc:
            last_sync_failed = True
            if phase == 'boot':
                boot_attempts += 1
                log.warning('boot sync attempt %d/%d failed (%s: %s); retry in %.1f s',
                            boot_attempts, boot_limit,
                            type(exc).__name__, exc, boot_interval)
                if boot_attempts >= boot_limit:
                    log.warning('boot retries exhausted (%d x %.1fs); '
                                'switching to %ds interval mode, '
                                'network watch stays on',
                                boot_limit, boot_interval, interval)
                    phase = 'steady'
                    next_sync = now + interval
                else:
                    next_sync = now + boot_interval
            else:
                delay = min(interval, max(30.0, boot_interval))
                log.warning('sync failed (%s: %s); retry in %.1f s',
                            type(exc).__name__, exc, delay)
                next_sync = now + delay
            continue

        last_sync_failed = False
        phase = 'steady'
        boot_attempts = 0
        if result.get('refused') == 'skew':
            log.error('skew guard refused: offset %+.1f s exceeds %.1f s; '
                      'run manually with --force if the clock is really that wrong',
                      result['offset'], cfg.max_skew)
        elif result.get('write'):
            w = result['write']
            log.info('clock synced via %s: offset %+.3f s, residual %+.4f s, '
                     'now %s', result['mode'], w['applied'], w['residual'],
                     ntp_sync.beijing_str(w['after']))
        else:
            log.info('clock already within %.1f s (%+.3f s via %s), '
                     'next check in %d s', cfg.tolerance, result['offset'],
                     result['mode'], cfg.interval)
        next_sync = now + interval


def _worker(sync_fn=None, net_probe_fn=None, clock=None, sleeper=None,
            guard=None, stop_event=None, cfg=None):
    log = logging.getLogger('ntp_sync')
    if not log.handlers:
        _setup_service_logging()
    if cfg is None:
        try:
            cfg = _service_cfg()
        except Exception as exc:
            log.error('cannot build config: %s', exc)
            return

    ini = _load_ini()
    log.info('worker started (interval=%ds max_skew=%.0fs tolerance=%.1fs '
             'boot_retry=%.1fs x %d net_check=%.1fs '
             'log=%s max=%dB backups=%d)',
             cfg.interval, cfg.max_skew, cfg.tolerance,
             cfg.boot_retry_interval, cfg.boot_retry_limit,
             cfg.net_check_interval, LOG_PATH,
             ini['log_max_bytes'], ini['log_backups'])

    offset_h, tz_status = ntp_sync.ensure_china_timezone(apply=False)
    if tz_status == 'ok':
        log.info('system time zone is UTC+8 (China Standard Time)')
    else:
        log.warning('system time zone is UTC%+g, expected UTC+8; '
                    'run: python ntpsvc.py set-tz', offset_h)

    _run_loop(cfg, log, sync_fn or ntp_sync.sync_once,
              net_probe_fn or network_ready, clock or time.time,
              sleeper or _stop_event.wait,
              guard or (lambda: _guard_log_size(log)),
              stop_event or _stop_event)
    log.info('worker stopped')


# ---------------------------------------------------------------------------
# 服务入口
# ---------------------------------------------------------------------------
@SERVICE_MAIN_FUNCTION
def _service_main(argc, argv):
    global _status_handle, _handler_ref
    _handler_ref = HANDLER_FUNCTION(_handler)
    _status_handle = _adv.RegisterServiceCtrlHandlerW(SERVICE_NAME, _handler_ref)
    if not _status_handle:
        return
    _report(SERVICE_START_PENDING, hint=5000)
    _report(SERVICE_RUNNING, SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN)
    _setup_service_logging()
    t = threading.Thread(target=_worker, name='ntp-sync-worker')
    t.daemon = True
    t.start()
    while not _stop_event.is_set():
        time.sleep(0.4)
    _report(SERVICE_STOP_PENDING, 0, hint=8000)
    t.join(timeout=15)
    _report(SERVICE_STOPPED, 0)


def run_service():
    table = (SERVICE_TABLE_ENTRY * 2)()
    table[0].lpServiceName = SERVICE_NAME
    table[0].lpServiceProc = ctypes.cast(_service_main, ctypes.c_void_p)
    table[1].lpServiceName = None
    table[1].lpServiceProc = None
    if not _adv.StartServiceCtrlDispatcherW(table):
        raise ctypes.WinError(ctypes.get_last_error())


# ---------------------------------------------------------------------------
# 安装 / 卸载 / 控制
# ---------------------------------------------------------------------------
def _open_scm():
    h = _adv.OpenSCManagerW(None, None, SC_MANAGER_ALL_ACCESS)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def _open_service(scm, access=SERVICE_ALL_ACCESS):
    h = _adv.OpenServiceW(scm, SERVICE_NAME, access)
    if not h:
        err = ctypes.get_last_error()
        if err == ERROR_SERVICE_DOES_NOT_EXIST:
            return None
        raise ctypes.WinError(err)
    return h


def _wait_state(svc, wanted, timeout=20.0):
    st = SERVICE_STATUS()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _adv.QueryServiceStatus(svc, ctypes.byref(st)):
            return None
        if st.dwCurrentState == wanted:
            return st
        time.sleep(0.2)
    return st


def service_status():
    scm = _open_scm()
    try:
        svc = _open_service(scm, 0x0004)      # SERVICE_QUERY_STATUS
        if not svc:
            return None
        try:
            st = SERVICE_STATUS()
            if not _adv.QueryServiceStatus(svc, ctypes.byref(st)):
                raise ctypes.WinError(ctypes.get_last_error())
            return st
        finally:
            _adv.CloseServiceHandle(svc)
    finally:
        _adv.CloseServiceHandle(scm)


def _write_ini(interval, log_max_bytes=DEFAULT_LOG_MAX_BYTES,
               log_backups=DEFAULT_LOG_BACKUPS,
               boot_retry_interval=DEFAULT_BOOT_RETRY_INTERVAL,
               boot_retry_limit=DEFAULT_BOOT_RETRY_LIMIT,
               net_check_interval=DEFAULT_NET_CHECK_INTERVAL):
    with io.open(CONF_PATH, 'w', encoding='utf-8') as f:
        f.write(u'[sync]\n')
        f.write(u'interval = %d\n' % interval)
        f.write(u'max_skew = 7200\n')
        f.write(u'tolerance = 0.5\n')
        f.write(u'; while the first sync has not succeeded: retry every N seconds\n')
        f.write(u'boot_retry_interval = %g\n' % boot_retry_interval)
        f.write(u'; give up dense retries after this many attempts (5 x 60 = 5 min)\n')
        f.write(u'boot_retry_limit = %d\n' % boot_retry_limit)
        f.write(u'; watch network availability every N seconds; sync on recovery\n')
        f.write(u'net_check_interval = %g\n' % net_check_interval)
        f.write(u'\n[log]\n')
        f.write(u'; rotate service.log after max_bytes, keep `backups` old files\n')
        f.write(u'; set max_bytes = 0 to disable rotation\n')
        f.write(u'max_bytes = %d\n' % log_max_bytes)
        f.write(u'backups = %d\n' % log_backups)


def install(interval=DEFAULT_INTERVAL, log_max_bytes=DEFAULT_LOG_MAX_BYTES,
            log_backups=DEFAULT_LOG_BACKUPS,
            boot_retry_interval=DEFAULT_BOOT_RETRY_INTERVAL,
            boot_retry_limit=DEFAULT_BOOT_RETRY_LIMIT,
            net_check_interval=DEFAULT_NET_CHECK_INTERVAL):
    if not ntp_sync.is_admin():
        raise ntp_sync.PrivilegeError('install requires an elevated prompt')
    if not os.path.isfile(PYTHON_EXE):
        raise RuntimeError('python.exe not found next to ntpsvc.py: %s' % PYTHON_EXE)

    _write_ini(interval, log_max_bytes, log_backups,
               boot_retry_interval, boot_retry_limit, net_check_interval)
    binpath = u'"%s" "%s" run' % (PYTHON_EXE, SELF_PY)

    scm = _open_scm()
    try:
        svc = _open_service(scm)
        if svc:
            # 已存在：先停再删，等 SCM 真正释放名字
            st = SERVICE_STATUS()
            _adv.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(st))
            _wait_state(svc, SERVICE_STOPPED, 15)
            _adv.DeleteService(svc)
            _adv.CloseServiceHandle(svc)
            time.sleep(1.5)

        handle = None
        for attempt in range(6):
            handle = _adv.CreateServiceW(
                scm, SERVICE_NAME, SERVICE_DISPLAY, SERVICE_ALL_ACCESS,
                SERVICE_TYPE, SERVICE_AUTO_START, SERVICE_ERROR_NORMAL,
                binpath, None, None, None, None, None)
            if handle:
                break
            err = ctypes.get_last_error()
            if err == ERROR_SERVICE_EXISTS:
                time.sleep(1.0)
                continue
            raise ctypes.WinError(err)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())

        try:
            desc = SERVICE_DESCRIPTION()
            desc.lpDescription = ctypes.c_wchar_p(SERVICE_DESC)
            _adv.ChangeServiceConfig2W(handle, 1, ctypes.byref(desc))
        finally:
            _adv.CloseServiceHandle(handle)
    finally:
        _adv.CloseServiceHandle(scm)

    print('service installed, binPath = %s' % binpath)
    start_service()


def uninstall():
    if not ntp_sync.is_admin():
        raise ntp_sync.PrivilegeError('uninstall requires an elevated prompt')
    scm = _open_scm()
    try:
        svc = _open_service(scm)
        if not svc:
            print('service is not installed')
            return
        try:
            st = SERVICE_STATUS()
            _adv.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(st))
            _wait_state(svc, SERVICE_STOPPED, 20)
            if not _adv.DeleteService(svc):
                raise ctypes.WinError(ctypes.get_last_error())
            print('service stopped and marked for deletion')
        finally:
            _adv.CloseServiceHandle(svc)
    finally:
        _adv.CloseServiceHandle(scm)
    time.sleep(2.0)


def start_service():
    scm = _open_scm()
    try:
        svc = _open_service(scm)
        if not svc:
            raise RuntimeError('service is not installed')
        try:
            ok = _adv.StartServiceW(svc, 0, None)
            if not ok:
                err = ctypes.get_last_error()
                if err != ERROR_SERVICE_ALREADY_RUNNING:
                    raise ctypes.WinError(err)
            st = _wait_state(svc, SERVICE_RUNNING, 20)
            print('service state: %s' % STATE_NAMES.get(getattr(st, 'dwCurrentState', 0), '?'))
        finally:
            _adv.CloseServiceHandle(svc)
    finally:
        _adv.CloseServiceHandle(scm)


def stop_service():
    scm = _open_scm()
    try:
        svc = _open_service(scm)
        if not svc:
            raise RuntimeError('service is not installed')
        try:
            st = SERVICE_STATUS()
            _adv.ControlService(svc, SERVICE_CONTROL_STOP, ctypes.byref(st))
            st = _wait_state(svc, SERVICE_STOPPED, 20)
            print('service state: %s' % STATE_NAMES.get(getattr(st, 'dwCurrentState', 0), '?'))
        finally:
            _adv.CloseServiceHandle(svc)
    finally:
        _adv.CloseServiceHandle(scm)


def print_status():
    st = service_status()
    if st is None:
        print('service "%s" is not installed' % SERVICE_NAME)
        return
    print('service   : %s' % SERVICE_NAME)
    print('state     : %s' % STATE_NAMES.get(st.dwCurrentState, str(st.dwCurrentState)))
    print('accepts   : 0x%x' % st.dwControlsAccepted)
    print('exit code : %d' % st.dwWin32ExitCode)
    print('log file  : %s' % LOG_PATH)
    ini = _load_ini()
    print('retry     : boot %.1fs x %d, net check %.1fs' %
          (ini['boot_retry_interval'], ini['boot_retry_limit'],
           ini['net_check_interval']))
    if ini['log_max_bytes'] > 0:
        print('log rotate: %d bytes x %d backups' %
              (ini['log_max_bytes'], ini['log_backups']))
        for name in _log_files():
            try:
                print('  %-24s %10d bytes' % (name, os.path.getsize(name)))
            except OSError:
                pass
    else:
        print('log rotate: disabled (max_bytes = 0)')


def debug_console():
    _setup_service_logging(console=True)
    print('running sync loop in foreground (Ctrl+C to stop), log=%s' % LOG_PATH)
    try:
        _worker()
    except KeyboardInterrupt:
        print('\ninterrupted')


def set_timezone():
    offset, status = ntp_sync.ensure_china_timezone(apply=True)
    if status == 'ok':
        print('time zone already UTC+8')
    elif status == 'set':
        print('time zone set to China Standard Time (UTC+8)')
    else:
        print('failed to set time zone (status=%s, offset=%+g)' % (status, offset))
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog='ntpsvc.py',
                                 description='Windows service host for ntp_sync.')
    sub = ap.add_subparsers(dest='cmd')
    p_install = sub.add_parser('install', help='install and start the service')
    p_install.add_argument('--interval', type=int, default=DEFAULT_INTERVAL,
                           help='seconds between syncs (default 1800)')
    p_install.add_argument('--log-max-bytes', type=int, default=DEFAULT_LOG_MAX_BYTES,
                           help='rotate service.log after N bytes (default 1 MiB, 0 = off)')
    p_install.add_argument('--log-backups', type=int, default=DEFAULT_LOG_BACKUPS,
                           help='rotated service.log files to keep (default 5)')
    p_install.add_argument('--boot-retry-interval', type=float,
                           default=DEFAULT_BOOT_RETRY_INTERVAL,
                           help='retry every N seconds until the first sync succeeds '
                                '(default 5)')
    p_install.add_argument('--boot-retry-limit', type=int,
                           default=DEFAULT_BOOT_RETRY_LIMIT,
                           help='max dense retries at boot (default 60)')
    p_install.add_argument('--net-check-interval', type=float,
                           default=DEFAULT_NET_CHECK_INTERVAL,
                           help='check network every N seconds and sync on recovery '
                                '(default 3)')
    sub.add_parser('uninstall', help='stop and delete the service')
    sub.add_parser('start', help='start the service')
    sub.add_parser('stop', help='stop the service')
    sub.add_parser('status', help='show service state')
    sub.add_parser('set-tz', help='set time zone to China Standard Time')
    sub.add_parser('run', help='service entry point (SCM only)')
    sub.add_parser('debug', help='run the sync loop in the foreground')
    args = ap.parse_args(argv)

    try:
        if args.cmd == 'install':
            install(args.interval, args.log_max_bytes, args.log_backups,
                    args.boot_retry_interval, args.boot_retry_limit,
                    args.net_check_interval)
        elif args.cmd == 'uninstall':
            uninstall()
        elif args.cmd == 'start':
            start_service()
        elif args.cmd == 'stop':
            stop_service()
        elif args.cmd == 'status':
            print_status()
        elif args.cmd == 'set-tz':
            return set_timezone()
        elif args.cmd == 'debug':
            debug_console()
        elif args.cmd == 'run':
            run_service()
        else:
            ap.print_help()
            return 2
    except ntp_sync.SyncError as exc:
        print('error: %s' % exc)
        return 1
    except OSError as exc:
        print('error: %s' % exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
