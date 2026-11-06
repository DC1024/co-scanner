"""WHEA 硬件错误检测。

AMD 平台上，CO 压得太深时除了「进程消失」，另一种典型失效是 CPU 报出可纠正/不可纠正
的机器检查异常（MCE），Windows 会记成 WHEA-Logger 的 18（可纠正）/ 19（致命）事件。
CoreCycler 的判据之一就是这两条。

实现上用 wevtutil 的 XML 输出，而不是 text：
  * text 输出的字段名是本地化的（中文系统叫「事件 ID」），解析会跟着系统语言碎掉
  * XML 里 SystemTime 是 ISO 8601，与语言无关
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

_PROVIDER = "Microsoft-Windows-WHEA-Logger"
_XPATH = (
    f"*[System[Provider[@Name='{_PROVIDER}'] and (EventID=18 or EventID=19)]]"
)
_TIME_RE = re.compile(r'SystemTime="([^"]+)"')


@dataclass
class WheaSnapshot:
    count: int                      # 最近窗口内的事件条数
    newest: str | None              # 最新一条的 SystemTime（原始字符串）
    newest_dt: datetime | None
    available: bool = True          # 查询本身是否成功

    def describe(self) -> str:
        if not self.available:
            return "WHEA 查询不可用"
        if not self.count:
            return "无 WHEA 事件"
        return f"{self.count} 条 WHEA 事件，最新 {self.newest}"


def _parse_time(raw: str) -> datetime | None:
    """把 2026-11-06T00:35:34.7280000Z 转成 datetime。手动截断，%f 只吃 6 位小数。"""
    try:
        head, _, tail = raw.partition(".")
        frac = (tail.rstrip("Z") + "000000")[:6]
        return datetime.strptime(f"{head}.{frac}", "%Y-%m-%dT%H:%M:%S.%f").replace(
            tzinfo=timezone.utc
        )
    except Exception:
        return None


def snapshot(limit: int = 50, timeout: float = 20.0) -> WheaSnapshot:
    """读取最近 limit 条 WHEA 事件。"""
    try:
        proc = subprocess.run(
            ["wevtutil", "qe", "System", f"/q:{_XPATH}", f"/c:{limit}", "/rd:true", "/f:xml"],
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return WheaSnapshot(0, None, None, available=False)

    if proc.returncode != 0:
        return WheaSnapshot(0, None, None, available=False)

    text = proc.stdout.decode("utf-8", "replace")
    times = _TIME_RE.findall(text)
    if not times:
        return WheaSnapshot(0, None, None, available=True)
    newest_raw = times[0]
    return WheaSnapshot(len(times), newest_raw, _parse_time(newest_raw), available=True)


def count_since(baseline: datetime | None, limit: int = 50) -> int:
    """统计 baseline 之后新增的 WHEA 事件条数。baseline 为 None 时返回总数。"""
    snap = snapshot(limit=limit)
    if not snap.available:
        return 0
    if baseline is None:
        return snap.count
    newest_dt = snap.newest_dt
    if newest_dt is None or newest_dt <= baseline:
        return 0

    # 逐条比对：把整段 XML 里的时间全部拿来做过滤
    try:
        proc = subprocess.run(
            ["wevtutil", "qe", "System", f"/q:{_XPATH}", f"/c:{limit}", "/rd:true", "/f:xml"],
            capture_output=True,
            timeout=20.0,
        )
        raw = proc.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return 0

    fresh = 0
    for stamp in _TIME_RE.findall(raw):
        dt = _parse_time(stamp)
        if dt and dt > baseline:
            fresh += 1
    return fresh
