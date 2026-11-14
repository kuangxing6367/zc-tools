# zc-tools

> 闲得没事，按需拼出来的一些小工具。  
> A grab-bag of small Windows utilities, hacked together whenever something got annoying enough.

**License: WTFPL v2** — 想怎么用怎么用，改、抄、卖、改个名当成自己的都行。见 [LICENSE](LICENSE)。

---

## 仓库怎么摆的

**根目录放成品，`src/` 放源码。** 根目录下的 exe / zip 都是构建好的最终产物，
直接下载就能跑，不用装 Python、不用编译。

```
zc-tools/
├─ ntp_sync_setup.exe            ← 成品：时间同步服务安装包
├─ ntp_sync_win7_x86_py37.zip    ← 成品：时间同步绿色包
├─ tray_mon_win7_x86.zip         ← 成品：任务栏监控绿色包
├─ src/
│  ├─ ntp-sync/                  时间同步服务源码
│  └─ tray-mon/                  任务栏监控源码
├─ LICENSE
└─ .gitignore
```

成品文件名**不带版本号**，这样挂到别处当下载源时链接永远是活的。
版本变更只更新下面表格和 SHA-256。

---

## 直接下载

| 文件 | 是什么 | 版本 | 体积 | 目标机器 |
|---|---|---|---|---|
| [ntp_sync_setup.exe](ntp_sync_setup.exe) | 时间同步**安装包**，装完开机未登录即对时 | 1.2.0 | 7.2 MB | Win7 SP1+ / 32 位 |
| [ntp_sync_win7_x86_py37.zip](ntp_sync_win7_x86_py37.zip) | 时间同步**绿色包**，自带 Python 3.7.9 x86 | 1.2.0 | 6.4 MB | Win7 SP1+ / 32 位 |
| [tray_mon_win7_x86.zip](tray_mon_win7_x86.zip) | 任务栏性能监控绿色包，自带 Python 3.7.9 x86 | 1.0.0 | 6.4 MB | Win7 SP1+ / 32 位 |

### 直链（脚本可直接拉）

```
https://raw.githubusercontent.com/kuangxing6367/zc-tools/main/ntp_sync_setup.exe
https://raw.githubusercontent.com/kuangxing6367/zc-tools/main/ntp_sync_win7_x86_py37.zip
https://raw.githubusercontent.com/kuangxing6367/zc-tools/main/tray_mon_win7_x86.zip
```

国内直连 raw 慢的话，前面套一层代理：

```
https://gh-proxy.org/https://raw.githubusercontent.com/kuangxing6367/zc-tools/main/tray_mon_win7_x86.zip
```

### SHA-256

```
1f59b26ac3db8d5c5550db8ca36f82d2aacb29a82f0de4f27fc46b53735853e0  ntp_sync_setup.exe
542766a730f1cea54ddf06541116fe85cf584aa21977a3ccebc9549ea0d0190c  ntp_sync_win7_x86_py37.zip
850095762ff749a0183a1ab0b073141d243ad1da8df2518b25297d030dd8088a  tray_mon_win7_x86.zip
```

Windows 上校验：`certutil -hashfile 文件名 SHA256`

---

## 三个成品分别怎么用

### 1. `ntp_sync_setup.exe` —— 时间同步（推荐）

双击 → 下一步 → 装完自动注册成 Windows 服务，**开机不等登录就对时**，
时间标准锁在中国标准时间 UTC+8。日志按大小轮转（默认 1 MiB × 5）。

静默安装：

```cmd
ntp_sync_setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
```

细节（前置补丁、验证方法、故障排查、卸载）都在
[src/ntp-sync/安装说明.md](src/ntp-sync/安装说明.md)。

### 2. `ntp_sync_win7_x86_py37.zip` —— 时间同步绿色版

解压后 `python.exe` 已就位，直接用：

```cmd
python.exe ntp_sync.py --check           :: 只看偏移，不写系统
python.exe ntp_sync.py --apply --elevate :: 提权写入
install_service.bat                       :: 注册成服务
```

### 3. `tray_mon_win7_x86.zip` —— 任务栏监控

解压后双击 `启动托盘监控.bat`。CPU、内存、上下行网速、磁盘占用贴在任务栏
托盘区左侧，托盘图标显示 CPU 百分比，右键有 14 项菜单（明暗主题、采样间隔、
开机自启等）。跑不起来先双击 `诊断.bat`。

详见 [src/tray-mon/说明.md](src/tray-mon/说明.md)。

---

## 共同的设计取向

- **不引第三方依赖**：只用 `ctypes` 直接怼 Win32 API，避免 Win7 上装 pi/pywin32 的麻烦
- **Win7 SP1 + 32 位优先**：语法锁在 Python 3.7 子集，`python.exe` 用官方 embeddable 包
- **默认安全**：能只读就不写，能不动系统就不动系统（对时工具默认 dry-run，真写要显式 `--apply`）
- **日志可轮转**：无人值守的东西，日志不能无限长大
- **踩过的坑写在注释里**：为什么这么写，比代码本身更值钱

---

## 改代码之后

1. 在 `src/<工具>/` 里改
2. 按各目录说明重新构建（Inno Setup 编译 / 复制 embeddable 运行时）
3. 用新产物覆盖根目录同名文件
4. 更新本文的版本号、体积、SHA-256

---

## License

```
        DO WHAT THE FUCK YOU WANT TO PUBLIC LICENSE
                Version 2, December 2004
```
