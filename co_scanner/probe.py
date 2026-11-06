"""稳定性探针：判断当前 CO 值下压测是否还活着。

P1 的判据是三路合一（对应 CoreCycler 的 CPULOAD / exit code / WHEA）：
  1. 进程树 CPU 时间占比跌破阈值   -> 复刻 CPULOAD，最灵敏
  2. 压测进程以非零码退出          -> 抓「算错了 / 检测到错误」
  3. 期间新增 WHEA 18/19 事件      -> 抓硬件层机器检查异常

为什么用进程树：y-cruncher.exe 只是启动器，计算在 Binaries\\24-ZN5 ~ Komari.exe 里。
只盯启动器 PID 会把「子进程已崩、启动器仍在等」判成活着。
"""
from __future__ import annotations

import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from . import winproc
from .whea import WheaSnapshot, snapshot


@dataclass
class ProbeResult:
    ok: bool
    reason: str
    detail: str = ""
    metrics: dict = field(default_factory=dict)


class Probe(ABC):
    @abstractmethod
    def run(self, core: int, value: int, dwell: float) -> ProbeResult:
        """在给定 CO 值下压测 core，返回是否稳定。"""


@dataclass
class MonitorConfig:
    """监控参数。默认值与本次实测的 CPULOAD 现象对齐。"""

    mask: int = 0                  # 亲和性掩码，0 表示不绑定
    cpu_ratio_min: float = 0.5     # CPU 时间占比下限，单线程满载约 1.0
    sample_interval: float = 0.5
    warmup: float = 4.0            # 启动阶段不判定，避免误杀
    slack: float = 25.0            # 超出计划时长多少秒后强制收尾并判成功
    respawn_repin: bool = True     # 子进程新起时补设亲和性
    min_progress: float = 0.75     # exit=0 但过早退出（低于该进度）判为失败


def run_monitored(
    argv: list[str],
    dwell: float,
    cfg: MonitorConfig,
    cwd: str | None = None,
    label: str = "",
) -> ProbeResult:
    """启动 argv，按三路判据监控 dwell 秒。"""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        return ProbeResult(False, "SPAWN_FAILED", f"无法启动压测程序：{exc}")

    pinned: set[int] = set()
    ratios: list[float] = []
    whea_before: WheaSnapshot = snapshot()
    started = time.monotonic()
    baseline: float | None = None
    last_sample = started
    pid = proc.pid
    tag = f" {label}" if label else ""

    def finish(ok: bool, reason: str, detail: str, code: int | None = None) -> ProbeResult:
        elapsed = time.monotonic() - started
        fresh = _whea_delta(whea_before)
        if fresh > 0:  # WHEA 优先于任何「通过」结论
            ok, reason = False, "WHEA"
            detail = f"期间出现 {fresh} 条 WHEA 事件；{detail}"
        return ProbeResult(
            ok, reason, detail + tag,
            _metrics(elapsed, ratios, code, fresh),
        )

    try:
        if cfg.mask:
            winproc.set_affinity(pid, cfg.mask)

        while True:
            now = time.monotonic()
            elapsed = now - started

            if elapsed >= dwell:
                return finish(True, "OK", f"存活 {elapsed:.1f}s")
            if elapsed > dwell + cfg.slack:
                return finish(True, "OK_OVER", f"超出计划时长（{elapsed:.1f}s），已收尾")

            time.sleep(cfg.sample_interval)
            now = time.monotonic()

            if proc.poll() is not None:
                code = proc.returncode
                ratio_done = (now - started) / dwell if dwell else 1.0
                if code == 0 and ratio_done >= cfg.min_progress:
                    # 压测程序自己跑完正常退出：-TL 比实际完成时间短时会出现，算通过
                    return finish(True, "OK_DONE", f"压测程序正常结束（{now - started:.1f}s）", code)
                if code == 0:
                    return finish(False, "EARLY_EXIT",
                                  f"只跑了 {now - started:.1f}s 就退出（计划 {dwell:.0f}s）", code)
                return finish(False, "EXIT_CODE",
                              f"压测程序异常退出 exit={code}，已跑 {now - started:.1f}s", code)

            total, alive = winproc.tree_cpu_seconds(pid)
            if not alive:
                return finish(False, "PROCESS_GONE", f"{now - started:.1f}s 时进程树已空")

            if cfg.mask and cfg.respawn_repin:
                for child in alive:
                    if child not in pinned:
                        pinned.add(child)
                        if child != pid:
                            winproc.set_affinity(child, cfg.mask)

            if baseline is None or (now - started) <= cfg.warmup:
                baseline = total
                last_sample = now
                continue

            window = max(now - last_sample, 0.05)
            ratio = (total - baseline) / window
            ratios.append(ratio)
            baseline = total
            last_sample = now

            if ratio < cfg.cpu_ratio_min:
                return finish(False, "CPULOAD",
                              f"CPU 占比 {ratio:.2f}（阈值 {cfg.cpu_ratio_min}），已跑 {now - started:.1f}s")
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


def _whea_delta(before: WheaSnapshot) -> int:
    if not before.available:
        return 0
    from .whea import count_since

    return count_since(before.newest_dt)


def _metrics(elapsed: float, ratios: list[float], exit_code: int | None, whea_new: int) -> dict:
    return {
        "elapsed": round(elapsed, 1),
        "avg_ratio": round(sum(ratios) / len(ratios), 3) if ratios else None,
        "min_ratio": round(min(ratios), 3) if ratios else None,
        "exit_code": exit_code,
        "whea_new": whea_new,
    }


@dataclass
class SimulatedProbe(Probe):
    """模拟：CO 值浅于等于该核下限时稳定，深于下限则崩。"""

    limits: dict[int, int]
    dwell_scale: float = 0.01

    def run(self, core: int, value: int, dwell: float) -> ProbeResult:
        time.sleep(max(0.0, dwell * self.dwell_scale))
        limit = self.limits.get(core, -50)
        if value >= limit:
            return ProbeResult(True, "OK", f"{value} >= limit {limit}")
        return ProbeResult(False, "CPULOAD", f"{value} < limit {limit}")


@dataclass
class ProcessProbe(Probe):
    """通用外部命令探针。

    cmd 支持 {core} 与 {seconds} 占位符；用 replace 而不是 format，
    因为压测命令行里常常带 {} 和 $，format 会直接抛异常。
    """

    cmd: list[str]
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    cwd: str | None = None

    def run(self, core: int, value: int, dwell: float) -> ProbeResult:
        argv = [
            part.replace("{core}", str(core)).replace("{seconds}", str(int(dwell)))
            for part in self.cmd
        ]
        return run_monitored(argv, dwell, self.monitor, self.cwd, label=f"core {core}")
