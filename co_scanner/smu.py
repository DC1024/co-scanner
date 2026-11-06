"""SMU 后端：读写 Curve Optimizer 偏移。

两种实现：
  * CliSmu     —— 调用 ryzen-smu-cli.exe（真实硬件，需要管理员权限）
  * SimulatedSmu —— 内存模拟，用于无权限环境下的算法自测

真实实现的输出格式（本机实测）：
    > ryzen-smu-cli.exe --get-offsets-terse
    Current PBO offsets:
    -38,-40,-40,-40,-40,-40

    > ryzen-smu-cli.exe --offset -38,-40,-40,-40,-40,-40
    Set logical core 0, physical core 0 offset to -38!
    ...
    Set logical core 4, physical core 5 offset to -40!
    Set logical core 5, physical core 6 offset to -40!
"""
from __future__ import annotations

import ctypes
import glob
import os
import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class SmuError(RuntimeError):
    """SMU 访问失败。"""


class NotElevated(SmuError):
    """当前进程不是管理员。"""


_NOT_ADMIN_HINT = re.compile(r"must be run as an administrator", re.I)
_WRITE_LINE = re.compile(
    r"logical core\s+(\d+),\s*physical core\s+(\d+)\s+offset to\s+(-?\d+)", re.I
)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def find_smu_cli() -> str | None:
    """按优先级查找 ryzen-smu-cli.exe。"""
    env = os.environ.get("CO_SCANNER_SMU_CLI")
    if env and os.path.isfile(env):
        return env

    patterns = [
        r"C:\CoreCycler*\tools\ryzen-smu-cli\ryzen-smu-cli.exe",
        r"C:\Program Files\CoreCycler*\tools\ryzen-smu-cli\ryzen-smu-cli.exe",
        os.path.join(os.getcwd(), r"tools\ryzen-smu-cli\ryzen-smu-cli.exe"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     r"tools\ryzen-smu-cli\ryzen-smu-cli.exe"),
    ]
    for pattern in patterns:
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None


def parse_offsets(text: str) -> list[int]:
    """从 --get-offsets-terse 输出里抠出偏移数组。"""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2 and all(re.fullmatch(r"-?\d+", p) for p in parts):
            return [int(p) for p in parts]
    raise SmuError(f"无法从输出解析 CO 偏移：{text.strip()[:200]!r}")


class SmuBackend(ABC):
    name: str = "abstract"

    @abstractmethod
    def read(self) -> list[int]:
        """读取每个逻辑核当前的 CO 偏移。"""

    @abstractmethod
    def write(self, offsets: list[int]) -> dict[int, int]:
        """写入 CO 偏移，返回 逻辑核 -> 物理核 的映射。"""

    @property
    def core_count(self) -> int:
        return len(self.read())


@dataclass
class CliSmu(SmuBackend):
    exe: str
    timeout: float = 30.0
    name: str = "ryzen-smu-cli"

    def __post_init__(self) -> None:
        if not os.path.isfile(self.exe):
            raise SmuError(f"找不到 ryzen-smu-cli.exe：{self.exe}")

    def _run(self, *args: str) -> str:
        try:
            proc = subprocess.run(
                [self.exe, *args],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise SmuError(f"SMU 命令超时（{self.timeout:.0f}s）：{' '.join(args)}") from exc

        output = (proc.stdout or "") + (proc.stderr or "")
        if _NOT_ADMIN_HINT.search(output):
            raise NotElevated(
                "ryzen-smu-cli 需要管理员权限。请以管理员身份重新运行本工具。"
            )
        if proc.returncode != 0:
            raise SmuError(
                f"SMU 命令失败（exit={proc.returncode}）：{' '.join(args)}\n{output.strip()[:400]}"
            )
        return output

    def read(self) -> list[int]:
        return parse_offsets(self._run("--get-offsets-terse"))

    def write(self, offsets: list[int]) -> dict[int, int]:
        payload = ",".join(str(int(v)) for v in offsets)
        output = self._run("--offset", payload)
        mapping: dict[int, int] = {}
        for logical, physical, value in _WRITE_LINE.findall(output):
            mapping[int(logical)] = int(physical)
        if not mapping:
            raise SmuError(
                f"写入后未解析到核映射，说明写入可能没生效。原始输出：{output.strip()[:300]!r}"
            )
        return mapping


@dataclass
class SimulatedSmu(SmuBackend):
    """内存模拟：每个逻辑核有各自的稳定下限，超过就写不进去（模拟冻结前的失败）。"""

    limits: dict[int, int] = field(default_factory=lambda: {i: -45 for i in range(6)})
    mapping: dict[int, int] = field(
        default_factory=lambda: {0: 0, 1: 1, 2: 2, 3: 3, 4: 5, 5: 6}
    )
    name: str = "simulated"
    _values: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self._values:
            self._values = [0] * len(self.limits)

    def read(self) -> list[int]:
        return list(self._values)

    def write(self, offsets: list[int]) -> dict[int, int]:
        if len(offsets) != len(self._values):
            raise SmuError(f"核数量不匹配：给了 {len(offsets)} 个，实际 {len(self._values)} 个")
        self._values = [int(v) for v in offsets]
        return dict(self.mapping)
