"""压测程序集成：y-cruncher。

选择 y-cruncher 而不是 Prime95 的理由（本次实测依据）：
  * Zen 5 有官方 AVX512 二进制（24-ZN5 ~ Komari），压测强度更贴近真实负载
  * 命令行可控：stress -D:{每轮秒数} -TL:{总时限} <算法>，退出码可信
  * 启动开销可压到 1 秒内：关键是 -M 限制内存，默认会自动吃掉几个 GB、启动要 40 秒

参数语义（实测确认）：
  -D:5 -TL:5 SFTv4  ->  总耗时约 5.8 秒，exit=0
  -D:5 不给 -TL     ->  会一直跑下去，永远不停
  所以 D 和 TL 必须同时给，且相等。
"""
from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass, field, replace

from . import winproc
from .probe import MonitorConfig, Probe, ProbeResult, run_monitored

# y-cruncher 一般在 CoreCycler 的 test_programs 下，也允许独立安装
_SEARCH_PATTERNS = [
    r"C:\CoreCycler*\test_programs\y-cruncher\y-cruncher.exe",
    r"C:\Program Files\CoreCycler*\test_programs\y-cruncher\y-cruncher.exe",
    r"C:\Program Files (x86)\CoreCycler*\test_programs\y-cruncher\y-cruncher.exe",
    r"C:\Program Files\y-cruncher*\y-cruncher.exe",
    r"C:\y-cruncher*\y-cruncher.exe",
]


class StressError(RuntimeError):
    """压测程序不可用。"""


def find_ycruncher() -> str | None:
    env = os.environ.get("CO_SCANNER_YCRUNCHER")
    if env and os.path.isfile(env):
        return env
    for pattern in _SEARCH_PATTERNS:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    found = shutil.which("y-cruncher") or shutil.which("y-cruncher.exe")
    return found


def available_algorithms(exe: str) -> list[str]:
    """从同目录的 Command Lines.txt 里读算法名，读不到就用内置默认。"""
    doc = os.path.join(os.path.dirname(exe), "Command Lines.txt")
    known = ["BKT", "BBP", "SFTv4", "SNT", "SVT", "FFTv4", "N63", "VT3"]
    if not os.path.isfile(doc):
        return known
    try:
        text = open(doc, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return known
    hits = [name for name in known if name in text]
    return hits or known


@dataclass
class YCruncherProbe(Probe):
    """按核绑定的 y-cruncher 压测探针。"""

    exe: str
    algorithm: str = "SFTv4"
    memory: str = "15M"
    pin: bool = True
    masks: list[int] = field(default_factory=list)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    cwd: str | None = None

    def build_argv(self, dwell: float) -> list[str]:
        seconds = max(1, int(round(dwell)))
        return [
            self.exe,
            "skip-warnings",
            "pause:0",
            "stress",
            f"-M:{self.memory}",
            f"-D:{seconds}",
            f"-TL:{seconds}",
            self.algorithm,
        ]

    def run(self, core: int, value: int, dwell: float) -> ProbeResult:
        cfg = replace(self.monitor)
        if self.pin and self.masks:
            cfg.mask = winproc.mask_for_core(core, self.masks)
        return run_monitored(
            self.build_argv(dwell), dwell, cfg, self.cwd, label=f"core {core}"
        )


@dataclass
class AllCoreProbe:
    """全核压测：不绑定亲和性，让所有核同时吃满。"""

    exe: str
    algorithm: str = "SFTv4"
    memory: str = "2G"
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    cwd: str | None = None

    def argv(self, seconds: float) -> list[str]:
        secs = max(1, int(round(seconds)))
        return [
            self.exe,
            "skip-warnings",
            "pause:0",
            "stress",
            f"-M:{self.memory}",
            f"-D:{secs}",
            f"-TL:{secs}",
            self.algorithm,
        ]

    def run(self, seconds: float) -> ProbeResult:
        cfg = replace(self.monitor)
        cfg.mask = 0
        cfg.slack = max(cfg.slack, 60.0)
        return run_monitored(self.argv(seconds), seconds, cfg, self.cwd, label="all-core")
