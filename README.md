# co-scanner

AMD Zen 3/4/5 的逐核 **Curve Optimizer 极限扫描器**。由浅入深自动探出每个核心能承受多深的负压，
再做全核叠加验证，最后输出一份可以直接照抄进 BIOS 的填写卡。

纯标准库实现，不依赖任何第三方 Python 包，也不打包任何第三方二进制。

## 它解决什么问题

现有工具各有缺口：

| 工具 | 自动下探 | 逐核 | 短板 |
|---|---|---|---|
| Ryzen Master | ✗ | ✓ | 全靠手动，没有搜索算法 |
| **CoreCycler** | ✗ **只能从起点往浅退** | ✓ | `startValues` 是起点，`incrementBy` 只会退不会进，**永远测不出真实极限** |
| Hydra | ✓ | ✓ | 闭源、付费、与 HWiNFO 冲突、诊断耗时数小时 |
| 本工具 | ✓ 由浅入深 | ✓ | 需管理员权限，无图形界面 |

本工具补的就是「自动下探 + 全核叠加验证」这一段：

```
for 每个核:
    for CO 从 -20 逐档往深走:
        只给该核设 CO，其余核归零
        压测（y-cruncher 单核，粗扫 30s / 精扫 120s）
        判活：CPU 时间占比 + 退出码 + WHEA
        失败 → 回退 2 档作为建议值，锁定该核
所有核的建议值一次性写入，全核满载验证 20 分钟
生成 HTML 报告 + BIOS 填写卡
```

## 为什么「全核叠加验证」不能省

这不是可选项，是从实测里换来的教训：

单核逐测时其余核 CO 归零，PPT 余量大、温度低，结果天然偏乐观。六核全部挂上负压后，
空闲核也在低压下待着、被测核能 boost 到更高频点，原本 10 轮全过的 Core 0，
在真实叠加场景下第 6 次测试就崩了（`CPULOAD`，进程直接消失，无 WHEA、无蓝屏）。
**只做单核扫描就写 BIOS，等于拿假数据赌稳定性。**

## 平台支持

| Zen | 型号 | CO 范围 | 备注 |
|---|---|---|---|
| Zen 5 | 9000 系（9600X ~ 9950X3D）、Threadripper 9000、Ryzen AI 300 | **-50 ~ +30** | |
| Zen 4 | 7000 系、8000G（APU）、Threadripper 7000 | -30 ~ +30 | |
| Zen 3 | 5000 系（含 5000G）、Threadripper PRO 5000 | -30 ~ +30 | |
| Zen 2 / Zen+ / Zen | 3000 ~ 1000 系 | — | 识别得到，但没有 PBO2 Curve Optimizer，会明确拒绝 |

- 核数不限：6 / 8 / 12 / 16 核、单 CCD 与双 CCD 都支持，报告会自动适配
- 型号识别不了（工程样品、新型号）时用 `--assume-zen 5` 手动指定
- 双 CCD 会额外提示「两个 CCD 体质可能不同，建议分核设值」
- APU / X3D 会给出针对性的注意事项

## 环境要求

- Windows 10/11，AMD Ryzen 5000 及以上
- **管理员权限**（读写 SMU 必需，不加会直接报错而不是静默失败）
- `ryzen-smu-cli.exe` —— CoreCycler 自带：`C:\CoreCycler-*\tools\ryzen-smu-cli\`
- `y-cruncher.exe` —— CoreCycler 自带：`C:\CoreCycler-*\test_programs\y-cruncher\`
- Python 3.10+

这两个第三方程序**不随本仓库分发**，也不建议打包进 exe —— 本程序本身就在加载底层驱动改电压，
再叠一层 PyInstaller 壳，杀软误报率接近 100%。

## 安装

```powershell
git clone <repo> co-scanner
cd co-scanner

# 方式一：直接跑，不安装
python -m co_scanner doctor

# 方式二：装成一个命令
pip install -e .
co-scanner doctor
```

路径自动找不到时，用 `--smu` / `--ycruncher` 指定，或建一个配置文件（见下）。

## 快速开始

**所有命令都要在管理员终端里跑**（右键开始菜单 → 终端(管理员)）。

```powershell
# 0. 环境体检：把开扫前能踩的坑一次性排掉
python -m co_scanner doctor

# 1. 看一眼当前 CO 和核编号映射
python -m co_scanner detect

# 2. 逐核扫描（默认约 1.5~2 小时，随时可 Ctrl+C，重跑自动续跑）
python -m co_scanner scan --report .\co-report.html

# 3. 全核叠加验证（默认 20 分钟）
python -m co_scanner verify --report .\co-report.html

# 4. 只重新生成报告
python -m co_scanner report --verify .\co-scan-5zen-verify.json -o .\co-report.html
```

`doctor` 是最值得先跑的一步。扫描一旦开跑就是一两小时，而「CO 没写进去」这类故障不会报错 ——
它会安静地跑完全程，输出一份看起来正常、实际上全是 0 的结果。

## 配置文件

把路径和默认参数固定下来，不用每次敲一长串：

```powershell
python -m co_scanner init            # 生成 ./co-scanner.json
```

```json
{
  "smu_cli": "C:\\CoreCycler-v0.11.0.3\\tools\\ryzen-smu-cli\\ryzen-smu-cli.exe",
  "ycruncher": "C:\\CoreCycler-v0.11.0.3\\test_programs\\y-cruncher\\y-cruncher.exe",
  "dwell": 30,
  "dwell_fine": 120,
  "fine_from": -35
}
```

搜索顺序：`--config` 指定 → 当前目录 `co-scanner.json` → `%APPDATA%\co-scanner\config.json`。

优先级：**命令行 > 配置文件 > 环境变量 > 自动查找**。

配置文件会被严格校验：写错键名（比如 `ycruncher_path`）会直接报错并列出可用项，
而不是静默忽略让你以为配好了。以 `_` 开头的键当作注释。

## 命令

| 命令 | 作用 |
|---|---|
| `doctor` | 环境体检：管理员、SMU 读写、y-cruncher、WHEA、电源计划、干扰进程、安全软件 |
| `detect` | 识别平台 + 读当前 CO + 拿「逻辑核 → 物理核」映射 |
| `init` | 生成配置文件模板 |
| `scan` | 逐核下探扫描（可中断、可续跑、可分批） |
| `verify` | 全核叠加验证 |
| `report` | 由断点 / 验证结果生成 HTML 报告 |

### 扫描参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--start` | -20 | 起始 CO，从浅往深扫 |
| `--min` | 平台下限 | 最深扫到哪（Zen 5 为 -50，Zen 3/4 为 -30） |
| `--step` | 1 | 每档步长 |
| `--safety` | 2 | 失败后回退几档作为建议值 |
| `--dwell` | 30 | 浅段每档压测秒数 |
| `--dwell-fine` | 120 | 深段每档压测秒数 |
| `--fine-from` | -35 | 从哪个值开始用深段时长 |
| `--warmup` | 180 | 扫描前全核预热秒数（冷机开扫会得到偏乐观的结果） |
| `--cores` | 全部 | 只扫这些核，如 `0,1,2` |
| `--retest` | — | 强制重扫这些核，忽略断点里的旧结果 |
| `--algorithm` | SFTv4 | y-cruncher 算法 |

核数多（12/16 核）时建议先 `--cores 0,1` 挑两个核试跑，确认流程和判定都正常再放开。

## 三路判据

| 判据 | 抓什么 | 实现 |
|---|---|---|
| CPU 时间占比跌破阈值 | 压测进程塌了 / 卡死 | 监控**进程树**（`y-cruncher.exe` 只是启动器，计算在 `Binaries\` 里） |
| 非零退出码 | 算错数 / 自检失败 | y-cruncher stress 的退出码；exit=0 但进度 < 75% 判 `EARLY_EXIT` |
| WHEA 18/19 事件 | 硬件层机器检查异常 | `wevtutil` 查 System 日志，按 Provider 精确过滤 |

三者任一命中即判该档失败。

## 断点与续跑

断点写在 `co-scan-<zen>zen.json`，重新执行同一条命令会自动续跑。

**跨平台守卫**：断点里记了 CPU 型号、Zen 代数、核数量。换机器或换 CPU 后接着用旧断点，
核号会错位，结果是假的 —— 所以这种情况会直接拒绝，并提示用 `--reset`。
`--reset` 会把旧断点**改名归档**而不是删除，几小时的测试结果不会丢。

**冻结检测**：每测一档之前，先把「正在测哪个核、哪个值」写进断点。如果下次启动时发现这个标记还在，
说明上一轮就死在这一档，直接把它判为失败、从中断处继续，而不是从头再跳一次同一个坑。

## 冻结与安全

Zen 5 的 CO 过深通常表现为**整机冻结**而不是蓝屏 —— 冻结期间没有任何代码在跑，
软件无法自救，只能长按电源键 10 秒强制关机。

工具能做的只有一件事：**让你重启后不用从头再来**。

其他风险控制：

- 重启后 CO 回到 BIOS 中保存的值，**硬件本身没有风险**
- 默认每档 30/120 秒、步长 1，不会一次跳太深
- 建议值默认回退 2 档，不贴着极限用
- 扫描结束自动把 CO 恢复成扫描前的值

## 已知边界

- **不写 BIOS**：物理上做不到，只生成填写卡
- **不碰内存 / EXPO / PBO Limits**：所有验证都在 BIOS 当前功耗档位下完成，
  改 PBO Limits 会让结论全部失效，必须在新档位下重跑
- **不涉及 Curve Shaper**：Zen 5 的特性，但 `ryzen-smu-cli` 没有对应参数，
  只能在 BIOS 里逐温度/频率区间手动设。`doctor` 会明确标出这一点
- **部分非一线主板会锁 SMU 接口**，工具会明确报错而不是静默失败
- **平台适配中尚未实机验证的部分**：Zen 3 / Zen 4、8/12/16 核、双 CCD 的代码路径已实现并有单测覆盖，
  但只在 Ryzen 5 9600X（Zen 5，6 核单 CCD）上做过完整实跑。其他平台首次使用时，
  建议先 `--cores 0` 单核试跑一轮

## 目录

```
co_scanner/
  platforminfo.py   CPU 型号 -> Zen 代数、核数、CCD、CO 范围、平台提示
  config.py         配置文件读取、校验与优先级合并
  doctor.py         环境体检
  winproc.py        亲和性掩码、L3 实例数（推 CCD）、进程树 CPU 时间（ctypes）
  smu.py            SMU 读写抽象（CliSmu / SimulatedSmu）
  stress.py         y-cruncher 发现与调用
  probe.py          三路判据监控
  whea.py           WHEA 事件查询
  scan.py           逐核状态机 + 断点 + 冻结检测 + 分批/重扫
  verify.py         全核叠加验证
  report.py         单文件 HTML 报告（无外部资源）
  cli.py            命令行入口
tests/selftest.py   不依赖硬件的自测（40+ 项）
```

## 许可

MIT。超频与降压存在数据损坏和硬件的潜在风险，使用即表示自行承担。
`ryzen-smu-cli` 与 `y-cruncher` 由各自作者提供，遵循各自的许可协议。
