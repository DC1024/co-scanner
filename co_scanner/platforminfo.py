r"""平台识别：CPU 型号、Zen 代数、Curve Optimizer 取值范围、CCD 结构。

只用标准库（winreg + ctypes），不引入 psutil 等外部依赖。

设计取舍：型号表用「数字首位 + 后缀」判定而不是逐型号枚举。
AMD 的命名规则在这些年里是稳定的 —— 四位数字的首位就是代次，
所以 `\b9\d{3}[A-Z]` 永远匹配 Zen 5 桌面/线程撕裂者。真正需要单独
处理的是那些跨代共用的段位（8000 系是 Zen 4 的 APU，不是 Zen 5），
以及没有规律的特殊型号（Ryzen AI 系列）。识别不了时用 --assume-zen 兜底，
绝不硬猜。
"""
from __future__ import annotations

import os
import re
import winreg
from dataclasses import asdict, dataclass


class UnsupportedPlatform(RuntimeError):
    """不是本工具支持的平台，或无法判定。"""


# 顺序敏感：先匹配的规则优先。
# 8000 系列是 Phoenix（Zen 4 架构），如果按「首位 8」去猜会落到 Zen 4 是对的，
# 但必须排在 9 之后，避免 8 开头被当成本代产品。
_SERIES_RULES: list[tuple[str, int]] = [
    (r"Ryzen AI", 5),           # Strix Point / Krackan，无四位数字型号
    (r"\b9\d{3}[A-Z]?", 5),     # 9600X 9700X 9800X3D 9900X 9950X / TR 9970X
    (r"\b8\d{3}[A-Z]?", 4),     # 8700G 8600G 8500G / 8845HS
    (r"\b7\d{3}[A-Z]?", 4),     # 7600X 7800X3D 7950X / TR 7960X
    (r"\b5\d{3}[A-Z]?", 3),     # 5600X 5800X3D 5700G / TR PRO 5995WX
    (r"\b4\d{3}[A-Z]?", 2),     # 4700G 4600G（Renoir）
    (r"\b3\d{3}[A-Z]?", 2),     # 3600 3900X
    (r"\b[12]\d{3}[A-Z]?", 1),  # 1600 2600 2700X
]

# Zen 代数 -> (CO 下限, CO 上限)。
# Zen 3 / 4 的 PBO2 只开放 -30；Zen 5 扩到 -50（Windows 端 BIOS 也按此显示）。
_CO_RANGE: dict[int, tuple[int, int]] = {
    3: (-30, 30),
    4: (-30, 30),
    5: (-50, 30),
}

# 内置的 Zen 版本 -> 展示名
_ZEN_ALIAS = {
    1: "Zen / Zen+",
    2: "Zen 2",
    3: "Zen 3",
    4: "Zen 4",
    5: "Zen 5",
}


@dataclass(frozen=True)
class Platform:
    name: str
    zen: int
    co_min: int
    co_max: int
    physical_cores: int
    logical_cores: int
    ccd_count: int = 1
    is_apu: bool = False
    x3d: bool = False
    hedt: bool = False
    forced: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def zen_label(self) -> str:
        return _ZEN_ALIAS.get(self.zen, f"Zen {self.zen}")

    @property
    def smt(self) -> bool:
        return self.physical_cores > 0 and self.logical_cores >= self.physical_cores * 2

    @property
    def notes(self) -> list[str]:
        """平台相关的操作提示。打印在 detect / doctor / 报告里。"""
        out: list[str] = []
        if self.forced:
            out.append(
                "Zen 代数是用 --assume-zen 指定的，不是从型号名识别出来的。"
                "CO 范围按该代数取值，请自行确认是否正确。"
            )
        if self.ccd_count >= 2:
            out.append(
                f"双 CCD（{self.ccd_count} 个 L3 实例）：两个 CCD 的体质可能明显不同，"
                "建议按核分别设值，不要图省事用统一值。"
            )
        if self.is_apu:
            out.append(
                "APU 平台：CO 同时影响核显的电压曲线，若出现花屏或驱动重置，先回退一档再测。"
            )
        if self.x3d:
            out.append(
                "X3D 型号：3D 缓存堆叠在 CCD 上方，散热路径更长。"
                "CO 过深时更容易因触顶降频而掩盖不稳，建议深段时长再加 30 秒。"
            )
        if self.zen == 4:
            out.append("Zen 4 的 CO 下限是 -30，扫到 -30 即为到底，不会再有更深的档位。")
        if self.zen == 3:
            out.append("Zen 3 的 CO 范围是 -30 ~ +30。")
        if self.physical_cores >= 12:
            out.append(
                f"{self.physical_cores} 核全扫一遍耗时较长，可先用 --cores 0,1 挑两个核试跑，"
                "确认流程和判定都正常之后再放开。"
            )
        if self.physical_cores and not self.smt:
            out.append("检测到 SMT 已关闭或为混合核心配置，单核压测的 CPU 占比会与默认值不同。")
        return out


def cpu_name() -> str:
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return str(value).strip()
    except OSError:
        return "Unknown"


def detect_zen(name: str) -> int:
    for pattern, zen in _SERIES_RULES:
        if re.search(pattern, name):
            return zen
    raise UnsupportedPlatform(
        f"无法从型号名判断 Zen 代数：{name!r}。"
        "支持 Zen 1 ~ Zen 5 的桌面与线程撕裂者型号；"
        "如果是新型号或工程样品，用 --assume-zen 4 这类方式手动指定代数。"
    )


def _looks_like_apu(name: str) -> bool:
    if re.search(r"Ryzen AI", name, re.I):
        return True
    # 型号以 G 结尾（5600G / 8700G 等）即为带核显的 APU
    return bool(re.search(r"\b\d{4}[A-Z]?G\b", name, re.I))


def _ccd_count(zen: int) -> int:
    """CCD 数量由 L3 实例数推断；Zen 2 及更早每个 CCX 一份 L3，需要除以 2。"""
    from . import winproc

    groups = winproc.l3_group_count()
    if groups <= 0:
        return 1
    if zen >= 3:
        return groups
    return max(1, (groups + 1) // 2)


def detect(assume_zen: int | None = None) -> Platform:
    from . import winproc

    name = cpu_name()
    forced = False
    if assume_zen is not None:
        if assume_zen not in _CO_RANGE:
            raise UnsupportedPlatform(
                f"指定的 Zen {assume_zen} 不支持 PBO2 Curve Optimizer。"
                "该功能从 Zen 3 开始提供（Zen 1/2 没有）。"
            )
        zen = assume_zen
        forced = True
    else:
        zen = detect_zen(name)

    if zen not in _CO_RANGE:
        raise UnsupportedPlatform(
            f"{_ZEN_ALIAS.get(zen, f'Zen {zen}')} 不支持 PBO2 Curve Optimizer"
            "（该功能从 Zen 3 开始提供）。"
        )

    physical = winproc.physical_core_count()
    logical = os.cpu_count() or 0
    if physical <= 0:
        # 拓扑查询失败时退化为「逻辑核数的一半」，至少让工具能继续往下走
        physical = max(1, logical // 2)

    co_min, co_max = _CO_RANGE[zen]
    upper = name.upper()
    return Platform(
        name=name,
        zen=zen,
        co_min=co_min,
        co_max=co_max,
        physical_cores=physical,
        logical_cores=logical,
        ccd_count=_ccd_count(zen),
        is_apu=_looks_like_apu(name),
        x3d="X3D" in upper,
        hedt="THREADRIPPER" in upper,
        forced=forced,
    )
