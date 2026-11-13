# zc-tools

> 闲得没事，按需拼出来的一些小工具。  
> A grab-bag of small Windows utilities, hacked together whenever something got annoying enough.

**License: WTFPL v2** — 想怎么用怎么用，改、抄、卖、改个名当成自己的都行。
见 [LICENSE](LICENSE)。

---

## 里面有什么

| 工具 | 一句话 | 平台 |
|---|---|---|
| [ntp-sync](ntp-sync/) | 开机未登录就把系统时间校准到中国标准时间（UTC+8）的 Windows 服务 + 安装包 | Win7 SP1+ / x86 |
| [tray-mon](tray-mon/) | 把 CPU、内存、网速、磁盘占用贴在任务栏右下角的监控条（含托盘图标与右键菜单） | Win7 SP1+ / x86 |

两个都是**纯 Python 标准库 + ctypes**，零第三方依赖，自带 32 位 Python 3.7.9
免安装运行时（打包产物不进仓库，见各工具目录说明）。

---

## 目录约定

```
zc-tools/
├─ ntp-sync/      时间同步服务（源码 + Inno Setup 打包脚本 + 安装说明）
├─ tray-mon/      任务栏性能监控（源码 + 启动脚本 + 使用说明）
├─ LICENSE        WTFPL Version 2
└─ .gitignore     打包产物、运行时、构建中间件都不入库
```

仓库里**只放源码和文档**。带 Python 运行时的绿色包、`setup.exe`、`*.zip` 都是
构建产物，靠各自目录里的脚本/说明现场生成。

---

## 共同的设计取向

- **不引第三方依赖**：只用 `ctypes` 直接怼 Win32 API，避免 Win7 上装 pi/pywin32 的麻烦
- **Win7 SP1 + 32 位优先**：语法锁在 Python 3.7 子集，`python.exe` 用官方 embeddable 包
- **默认安全**：能只读就不写，能不动系统就不动系统（对时工具默认 dry-run，真写要显式 `--apply`）
- **日志可轮转**：无人值守的东西，日志不能无限长大
- **踩过的坑写在注释里**：为什么这么写，比代码本身更值钱

---

## 快速上手

```cmd
:: 时间同步（装成开机自启服务）
cd ntp-sync
:: 详见 ntp-sync/安装说明.md

:: 任务栏性能监控
cd tray-mon
python.exe tray_mon.py
:: 详见 tray-mon/说明.md
```

---

## License

```
        DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
                Version 2, December 2004
```
