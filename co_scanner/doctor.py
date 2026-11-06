"""环境体检：在真正开扫之前把能踩的坑一次性排掉。

为什么要单独做这一步：
扫描一旦开跑就是 1.5~2 小时，而且中途「CO 没写进去」这类故障不会报错 ——
它会安静地跑完全程，最后输出一份看起来正常、实际上全是 0 的结果。
这种情况靠日志很难发现，只能在开跑前确认每个环节都真的能工作。

判定分四档：
  ok   正常
  warn 能用但有隐患，建议处理
  fail 阻塞项，不解决跑出来的结果是假的
  skip 当前条件下无法检查（多数是缺管理员权限）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from . import winproc
from .platforminfo import Platform, UnsupportedPlatform, detect
from .stress import find_ycruncher
from .smu import CliSmu, NotElevated, SmuError, find_smu_cli, is_admin

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", SKIP: "[skip]"}

# 这些进程会抢 CPU 或锁住 SMU，压测前必须清掉
_CONFLICT = {
    "occt": "OCCT",
    "corecycler": "CoreCycler",
    "y-cruncher": "y-cruncher",
    "prime95": "Prime95",
    "cinebench": "Cinebench",
    "aida64": "AIDA64",
    "hwinfo64": "HWiNFO（会读 MSR，但一般不冲突）",
}

# 常见国产/第三方安全软件，它们的驱动层常常拦截底层 I/O 驱动加载
_AV = {
    "hipsdaemon": "火绒",
    "hipstray": "火绒",
    "hipsmain": "火绒",
    "usysdiag": "火绒",
    "zhudongfangyu": "360 安全卫士",
    "360tray": "360 安全卫士",
    "qqpctray": "腾讯电脑管家",
    "qqpcmgr": "腾讯电脑管家",
    "kxetray": "金山毒霸",
}

# 电源计划 GUID
_POWER_PLANS = {
    "381b4222-f694-41f0-9685-ff5bb260df2e": ("平衡", WARN),
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": ("高性能", OK),
    "a1841308-3541-4fab-bc81-f71556f20b4a": ("节能", FAIL),
    "e9a42b02-d5df-448d-aa00-03f14749eb61": ("卓越性能", OK),
}

# y-cruncher 各代对应的专用二进制（Zen 3 没有专用版，走 ZN2 的 AVX2 路径）
_YC_TAG = {1: "ZN1", 2: "ZN2", 3: "ZN2", 4: "ZN4", 5: "ZN5"}


@dataclass
class Check:
    key: str
    title: str
    status: str
    detail: str
    hint: str = ""

    @property
    def blocked(self) -> bool:
        return self.status == FAIL


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    platform: Platform | None = None

    def add(self, key: str, title: str, status: str, detail: str, hint: str = "") -> None:
        self.checks.append(Check(key, title, status, detail, hint))

    @property
    def fails(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warns(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    def render(self, verbose: bool = False) -> str:
        def width_of(text: str) -> int:
            """中文按两字宽算，否则表格会歪。"""
            return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

        width = max((width_of(c.title) for c in self.checks), default=10)
        lines = []
        for c in self.checks:
            pad = width - width_of(c.title)
            lines.append(f"{_MARK.get(c.status, '[????]')} {c.title}{' ' * (pad + 2)}{c.detail}")
            if c.hint and (verbose or c.status in (FAIL, WARN)):
                lines.append(f"          → {c.hint}")
        return "\n".join(lines)


def _os_check(rep: Report) -> None:
    try:
        ver = sys.getwindowsversion()
    except Exception:
        rep.add("os", "操作系统", FAIL, "无法读取 Windows 版本")
        return
    label = f"Windows {'11' if ver.build >= 22000 else '10'} build {ver.build}"
    if ver.major < 10:
        rep.add("os", "操作系统", FAIL, label, "需要 Windows 10 及以上。")
    else:
        rep.add("os", "操作系统", OK, label)


def _admin_check(rep: Report) -> bool:
    admin = is_admin()
    if admin:
        rep.add("admin", "管理员权限", OK, "已提权")
    else:
        rep.add(
            "admin", "管理员权限", FAIL, "当前不是管理员",
            "右键开始菜单 → 终端(管理员)。读写 SMU 必须提权，否则后面所有硬件检查都会跳过。",
        )
    return admin


def _cpu_check(rep: Report, plat: Platform | None, err: str = "") -> None:
    if plat is None:
        rep.add("cpu", "CPU 识别", FAIL, err or "未识别", "可用 --assume-zen 手动指定代数。")
        return
    status = WARN if plat.forced else OK
    detail = (
        f"{plat.name} · {plat.zen_label} · "
        f"{plat.physical_cores} 核 {plat.logical_cores} 线程 · {plat.ccd_count} CCD"
    )
    rep.add("cpu", "CPU 识别", status, detail)
    for note in plat.notes:
        rep.add("cpu-note", "平台提示", WARN, note)


def _smu_check(rep: Report, plat: Platform | None, exe: str | None, admin: bool) -> list[int] | None:
    if not exe or not os.path.isfile(exe):
        rep.add(
            "smu-cli", "SMU 工具", FAIL, exe or "未找到 ryzen-smu-cli.exe",
            "它随 CoreCycler 一起分发：<CoreCycler>\\tools\\ryzen-smu-cli\\ryzen-smu-cli.exe。"
            "也可以用 --smu 或配置文件里的 smu_cli 指定。",
        )
        return None
    size = os.path.getsize(exe) / 1024
    rep.add("smu-cli", "SMU 工具", OK, f"{exe}（{size:.0f} KB）")

    if not admin:
        rep.add("smu-read", "SMU 读取", SKIP, "跳过（需要管理员）")
        return None

    try:
        smu = CliSmu(exe)
        values = smu.read()
    except NotElevated as exc:
        rep.add("smu-read", "SMU 读取", FAIL, str(exc))
        return None
    except SmuError as exc:
        rep.add(
            "smu-read", "SMU 读取", FAIL, str(exc).splitlines()[0][:160],
            "多半是安全软件的驱动拦截，或者主板 BIOS 锁了 SMU 接口。"
            "先把火绒/360 的「系统防护」临时关掉再试。",
        )
        return None

    # 读取成功说明底层驱动已加载，可以顺手确认核数是否对得上
    if plat and len(values) != plat.physical_cores:
        rep.add(
            "smu-cores", "核数一致性", WARN,
            f"SMU 返回 {len(values)} 个核，系统报告 {plat.physical_cores} 个物理核",
            "两者不一致时，扫描出来的结果可能对不上 BIOS 里的核编号，"
            "建议先在 detect 里核对「逻辑核 -> 物理核」映射。",
        )
    else:
        rep.add("smu-cores", "核数一致性", OK, f"{len(values)} 个核与系统报告一致")

    rep.add("smu-read", "SMU 读取", OK, f"当前 CO = {','.join(str(v) for v in values)}")

    # 原样回写，验证写通路（同时拿到映射）。写同样的值，不改变任何状态。
    try:
        mapping = smu.write(values)
        rep.add(
            "smu-write", "SMU 写入", OK,
            "回写成功：" + ", ".join(f"{k}->{v}" for k, v in sorted(mapping.items())),
        )
    except SmuError as exc:
        rep.add(
            "smu-write", "SMU 写入", FAIL, str(exc).splitlines()[0][:160],
            "能读不能写，通常是驱动的写保护或安全软件拦截。",
        )
    return values


def _stress_check(rep: Report, plat: Platform | None, exe: str | None) -> None:
    if not exe or not os.path.isfile(exe):
        rep.add(
            "yc", "压测程序", FAIL, exe or "未找到 y-cruncher.exe",
            "同样随 CoreCycler 分发：<CoreCycler>\\test_programs\\y-cruncher\\y-cruncher.exe。"
            "也可以用 --ycruncher 或配置文件里的 ycruncher 指定。",
        )
        return

    root = os.path.dirname(exe)
    rep.add("yc", "压测程序", OK, exe)

    bin_dir = os.path.join(root, "Binaries")
    if not os.path.isdir(bin_dir):
        rep.add("yc-bin", "y-cruncher 二进制", WARN, "没有 Binaries 目录",
                "可能不是完整安装，stress 启动时会失败。")
        return

    names = [n for n in os.listdir(bin_dir) if n.lower().endswith(".exe")]
    tag = _YC_TAG.get(plat.zen) if plat else None
    hit = next((n for n in names if tag and tag in n), None)
    if hit:
        rep.add("yc-bin", "y-cruncher 二进制", OK, f"{hit}（{plat.zen_label} 专用）")
    elif names:
        rep.add(
            "yc-bin", "y-cruncher 二进制", WARN,
            f"没找到 {tag} 专用二进制，共 {len(names)} 个",
            "会回落到通用 x86 版本，压测强度偏低，可能漏判。",
        )
    else:
        rep.add("yc-bin", "y-cruncher 二进制", FAIL, "Binaries 目录是空的")


def _whea_check(rep: Report) -> None:
    from .whea import snapshot

    snap = snapshot(limit=20)
    if not snap.available:
        rep.add(
            "whea", "WHEA 事件", WARN, "事件日志查询失败",
            "不影响扫描，但少了一路判据。通常需要管理员权限。",
        )
    elif snap.count:
        rep.add(
            "whea", "WHEA 事件", WARN,
            f"最近有 {snap.count} 条硬件错误，最新 {snap.newest}",
            "如果这些是超频测试期间产生的，说明当前设置已经不稳定，先恢复默认再扫。",
        )
    else:
        rep.add("whea", "WHEA 事件", OK, "近期无 WHEA 18/19 事件")


def process_basenames(procs: list[tuple[int, int, str]]) -> set[str]:
    """把 szExeFile 归一成不带后缀的小写名，供 _CONFLICT / _AV 查表。

    踩坑记录：enumerate_processes 返回的是 Toolhelp 的 szExeFile，**带 .exe 后缀**。
    早先这里直接 lower() 得到 "hipsdaemon.exe"，与表里的 "hipsdaemon" 永不相等，
    于是「安全软件」和「干扰进程」两项**永远报正常** —— 而且不报错。
    火绒明明在跑，doctor 却说「未发现」，这属于最危险的那类静默失效。
    """
    return {os.path.splitext(name)[0].lower() for _pid, _ppid, name in procs}


def _process_check(rep: Report) -> None:
    procs = winproc.enumerate_processes()
    names = process_basenames(procs)

    conflicts = sorted(
        f"{_CONFLICT[n]} ({n}.exe)" for n in names if n in _CONFLICT
    )
    if conflicts:
        rep.add(
            "procs", "干扰进程", WARN, "、".join(conflicts),
            "压测前关掉它们：既抢 CPU 影响判定，也可能锁住 SMU。",
        )
    else:
        rep.add("procs", "干扰进程", OK, f"无冲突进程（当前 {len(procs)} 个进程）")

    avs = sorted({_AV[n] for n in names if n in _AV})
    if avs:
        rep.add(
            "av", "安全软件", WARN, "、".join(avs),
            "它们的驱动层可能拦截 SMU 写入。如果上面的 SMU 读写失败，"
            "临时关掉「系统防护」再试；只是关掉托盘图标通常不够。",
        )
    else:
        rep.add("av", "安全软件", OK, "未发现常见第三方安全软件")


def _power_check(rep: Report) -> None:
    try:
        proc = subprocess.run(
            ["powercfg", "/getactivescheme"],
            capture_output=True, timeout=10,
        )
        text = proc.stdout.decode("gbk", "replace") + proc.stderr.decode("gbk", "replace")
    except (OSError, subprocess.TimeoutExpired):
        rep.add("power", "电源计划", SKIP, "查询失败")
        return

    match = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", text)
    if not match:
        rep.add("power", "电源计划", SKIP, "无法解析当前方案")
        return
    guid = match.group(1).lower()
    name, status = _POWER_PLANS.get(guid, ("自定义", WARN))
    hint = ""
    if status == FAIL:
        hint = "节能方案会把频率压下来，压测结果没有意义。切到「高性能」。"
    elif status == WARN and name == "平衡":
        hint = "平衡方案在低负载时更容易触发降频。想要结果稳，切到「高性能」。"
    rep.add("power", "电源计划", status, name, hint)


def run(settings=None) -> Report:
    rep = Report()
    _os_check(rep)
    admin = _admin_check(rep)

    plat = None
    err = ""
    assume = getattr(settings, "assume_zen", None)
    try:
        plat = detect(assume_zen=assume)
    except UnsupportedPlatform as exc:
        err = str(exc)
    rep.platform = plat
    _cpu_check(rep, plat, err)

    exe = getattr(settings, "smu_cli", None) or find_smu_cli()
    _smu_check(rep, plat, exe, admin)

    yc = getattr(settings, "ycruncher", None) or find_ycruncher()
    _stress_check(rep, plat, yc)

    _whea_check(rep)
    _process_check(rep)
    _power_check(rep)

    rep.add(
        "shaper", "Curve Shaper", SKIP, "本工具不涉及（Zen 5 特性，SMU 工具无对应参数）",
        "Curve Shaper 只能在 BIOS 里逐温度/频率区间设置，本轮扫描只处理全局 Curve Optimizer。"
        "两者叠加时以更保守的那个为准。",
    )
    return rep
