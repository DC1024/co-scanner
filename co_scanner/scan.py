"""逐核下探扫描器：状态机 + 断点续跑 + 冻结检测。

扫描方向固定为「由浅入深」。原因：起点越深，一次冻结就丢掉整轮数据；
从 -20 往下走，每一步都有前一步的结果兜底。

**冻结检测（inflight 机制）**
Zen 5 的 CO 过深通常是整机冻结而不是蓝屏，冻结期间没有任何代码在跑，
软件无法自救。所以每测一档之前，先把「正在测哪个核、哪个值」写进断点文件。
如果下次启动时发现 inflight 还在，说明上一轮就死在这一档 —— 直接把它判为失败，
从中断处继续，而不是从头再跳一次同一个坑。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Callable

from .probe import Probe, ProbeResult
from .smu import SmuBackend, SmuError


@dataclass
class ScanConfig:
    """两段式时长：浅档快筛，深档慢验。

    浅段用 30 秒是为了快速排除明显不稳的档位（CO 过深通常在 1 分钟内就崩，
    本次实测 Core 0 在 -40 上 1 分半暴露）；深段拉到 120 秒是因为漏判的代价
    远高于多花的时间 —— 一个假「通过」会直接写进 BIOS。
    """

    start: int = -20
    step: int = 1
    safety: int = 2
    dwell: float = 30.0
    dwell_fine: float = 120.0
    fine_from: int | None = -35
    verify_write: bool = True
    warmup: float = 180.0
    # 只扫指定核（None = 全部）。调试或分批复扫时用。
    cores: tuple[int, ...] | None = None
    # 强制重扫指定核，忽略断点里已有的结果
    retest: tuple[int, ...] | None = None

    def dwell_for(self, value: int) -> float:
        if self.fine_from is not None and value <= self.fine_from:
            return self.dwell_fine
        return self.dwell

    def selects(self, core: int) -> bool:
        return self.cores is None or core in self.cores


@dataclass
class CoreState:
    core: int
    physical: int | None = None
    tested: dict[str, bool] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)
    deepest_stable: int | None = None
    failed_at: int | None = None
    recommend: int | None = None
    status: str = "pending"  # pending / scanning / done / floor
    last_reason: str = ""

    @property
    def steps(self) -> int:
        return len(self.tested)

    def next_value(self, start: int, step: int) -> int:
        """从已有结果推算下一档该测的值。"""
        passed = [int(v) for v, ok in self.tested.items() if ok]
        if passed:
            return min(passed) - step
        return start


@dataclass
class Checkpoint:
    cpu: str = ""
    zen: int = 0
    physical_cores: int = 0
    co_min: int = -50
    co_max: int = 30
    config: dict = field(default_factory=dict)
    cores: dict[str, CoreState] = field(default_factory=dict)
    started: str = ""
    updated: str = ""
    finished: bool = False
    inflight: dict | None = None
    origin: list[int] = field(default_factory=list)

    def save(self, path: str) -> None:
        payload = asdict(self)
        payload["updated"] = datetime.now().isoformat(timespec="seconds")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: str) -> "Checkpoint | None":
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        cores = {
            key: CoreState(**value) for key, value in payload.pop("cores", {}).items()
        }
        cp = cls(**payload)
        cp.cores = cores
        return cp


class CheckpointMismatch(RuntimeError):
    """断点与当前机器对不上，继续跑会得到错位的结果。"""


class Scanner:
    def __init__(
        self,
        smu: SmuBackend,
        probe: Probe,
        config: ScanConfig,
        co_min: int,
        co_max: int,
        checkpoint_path: str,
        cpu: str = "",
        zen: int = 0,
        physical_cores: int = 0,
        on_event: Callable[[str], None] = print,
        warmup_fn: Callable[[float], None] | None = None,
    ) -> None:
        self.smu = smu
        self.probe = probe
        self.cfg = config
        self.co_min = co_min
        self.co_max = co_max
        self.path = checkpoint_path
        self.cpu = cpu
        self.zen = zen
        self.physical_cores = physical_cores
        self.log = on_event
        self.warmup_fn = warmup_fn
        self._origin: list[int] = []
        self._remaining = 0.0
        self.cp = Checkpoint()

    # ---------- 底层动作 ----------

    def _apply_single(self, core: int, value: int) -> dict[int, int]:
        """只给目标核设 CO，其余核归零（等价于 CoreCycler 的 setVoltageOnlyForTestedCore=1）。"""
        n = len(self._origin)
        offsets = [0] * n
        offsets[core] = value
        mapping = self.smu.write(offsets)

        if self.cfg.verify_write:
            actual = self.smu.read()
            if actual[core] != value:
                raise SmuError(
                    f"写入未生效：core {core} 期望 {value}，读回 {actual[core]}。"
                    "常见原因是驱动被安全软件拦截。"
                )
        return mapping

    def _restore_origin(self) -> None:
        if not self._origin:
            return
        try:
            self.smu.write(self._origin)
            self.log(f"[restore] 已恢复扫描前的 CO：{self._origin}")
        except SmuError as exc:
            self.log(f"[restore] 恢复失败（{exc}），请手动确认当前 CO 值。")

    # ---------- 主流程 ----------

    def run(self, resume: bool = True) -> Checkpoint:
        self._origin = self.smu.read()
        n = len(self._origin)
        self.log(f"[init] 当前 CO = {self._origin}，共 {n} 个逻辑核")

        existing = Checkpoint.load(self.path) if resume else None
        if existing and not existing.finished:
            reason = self._incompatible(existing, n)
            if reason:
                raise CheckpointMismatch(
                    f"断点与当前机器不匹配：{reason}。\n"
                    f"断点文件：{self.path}\n"
                    "换机器或换了 CPU 之后不能接着用旧断点，否则核号会错位。"
                    "确认要重来时加 --reset（旧断点会被归档而不是删除）。"
                )
            self.cp = existing
            self.log(f"[resume] 从断点继续：{self.path}")
            self._settle_inflight()
        else:
            self.cp = Checkpoint(
                cpu=self.cpu,
                zen=self.zen,
                physical_cores=self.physical_cores,
                config=asdict(self.cfg),
                co_min=self.co_min,
                co_max=self.co_max,
                started=datetime.now().isoformat(timespec="seconds"),
                origin=list(self._origin),
            )
            for core in range(n):
                self.cp.cores[str(core)] = CoreState(core=core)
            self._save()

        # 断点建立之后才有核状态，补上后加的核（例如中途换了 SMU 返回的核数）
        for core in range(n):
            self.cp.cores.setdefault(str(core), CoreState(core=core))
        self._apply_retest()

        pending = [
            c
            for c in self.cp.cores.values()
            if c.status in ("pending", "scanning") and self.cfg.selects(c.core)
        ]
        skipped = [
            c.core
            for c in self.cp.cores.values()
            if c.status in ("pending", "scanning") and not self.cfg.selects(c.core)
        ]
        if skipped:
            self.log(f"[skip] 按 --cores 跳过的核：{skipped}（它们仍会在断点里保持待扫状态）")
        if not pending:
            if self._all_done():
                self.log("[done] 所有核都已完成，无需重扫。")
                self.cp.finished = True
                self._save()
            else:
                self.log("[done] 本次选中的核已完成；其余核仍是待扫状态，去掉 --cores 可继续。")
            return self.cp

        if self.cfg.warmup > 0 and self.warmup_fn:
            self.log(f"[warmup] 预热 {self.cfg.warmup:.0f}s，把温度拉到工作区间再开始判定")
            try:
                self.warmup_fn(self.cfg.warmup)
            except Exception as exc:  # 预热失败不该阻断整个流程
                self.log(f"[warmup] 预热失败（{exc}），继续扫描")

        total_steps = sum(self._estimate_steps(c) for c in pending)
        self._remaining = sum(self._estimate_seconds(c) for c in pending)
        est_minutes = self._remaining / 60.0
        fine = f"，{self.cfg.fine_from:+d} 之后每档 {self.cfg.dwell_fine:.0f}s" \
            if self.cfg.fine_from is not None else ""
        self.log(
            f"[plan] 待扫 {len(pending)} 核，最多 {total_steps} 档，"
            f"每档 {self.cfg.dwell:.0f}s{fine}，预计 {est_minutes:.0f} 分钟"
        )

        started = time.monotonic()
        done_steps = 0
        try:
            for state in sorted(pending, key=lambda c: c.core):
                done_steps = self._scan_core(state, done_steps, total_steps, started)
        except KeyboardInterrupt:
            self.log("\n[abort] 用户中断，断点已保存。")
            self.cp.inflight = None
            self._save()
            raise
        finally:
            self._restore_origin()

        self.cp.finished = self._all_done()
        self.cp.inflight = None
        self._save()
        return self.cp

    def _settle_inflight(self) -> None:
        """上次运行死在某一档上：把那一档判为失败，然后从中断处继续。"""
        mark = self.cp.inflight
        if not mark:
            return
        core = int(mark.get("core", -1))
        value = int(mark.get("value", 0))
        state = self.cp.cores.get(str(core))
        self.log(
            f"[freeze] 上次运行在 core {core} 的 {value:+d} 档中断（{mark.get('started', '?')}），"
            "判定该档失败"
        )
        if state is not None:
            state.tested[str(value)] = False
            state.notes[str(value)] = "系统在测试期间中断（冻结或掉电）"
            state.failed_at = value
            passed = [int(v) for v, ok in state.tested.items() if ok]
            base = min(passed) if passed else value
            state.deepest_stable = base if passed else None
            state.recommend = min(base + self.cfg.safety, self.co_max)
            state.status = "done"
            state.last_reason = "冻结中断"
        self.cp.inflight = None
        self._save()

    def _incompatible(self, cp: Checkpoint, smu_cores: int) -> str | None:
        """断点能不能在现在这台机器上接着用。返回不兼容原因，None 表示可以。"""
        if cp.cpu and self.cpu and cp.cpu != self.cpu:
            return f"断点来自 {cp.cpu}，当前是 {self.cpu}"
        if len(cp.cores) != smu_cores:
            return f"断点里有 {len(cp.cores)} 个核，当前 SMU 返回 {smu_cores} 个"
        if len(cp.origin) not in (0, smu_cores):
            return f"断点记录的原始 CO 有 {len(cp.origin)} 个值，当前是 {smu_cores} 个"
        return None

    def _all_done(self) -> bool:
        return all(c.status in ("done", "floor") for c in self.cp.cores.values())

    def _apply_retest(self) -> None:
        """把 --retest 指定的核清空重扫。"""
        targets = self.cfg.retest or ()
        if not targets:
            return
        for core in targets:
            state = self.cp.cores.get(str(core))
            if state is None:
                self.log(f"[retest] 核 {core} 不存在，忽略")
                continue
            state.tested.clear()
            state.notes.clear()
            state.deepest_stable = None
            state.failed_at = None
            state.recommend = None
            state.status = "pending"
            state.last_reason = ""
        self.log(f"[retest] 已清空这些核的历史结果：{list(targets)}")
        self.cp.finished = False
        self._save()

    def _estimate_steps(self, state: CoreState) -> int:
        start = state.next_value(self.cfg.start, self.cfg.step)
        return max(1, (start - self.co_min) // self.cfg.step + 1)

    def _estimate_seconds(self, state: CoreState) -> float:
        start = state.next_value(self.cfg.start, self.cfg.step)
        total = 0.0
        value = start
        while value >= self.co_min:
            total += self.cfg.dwell_for(value) + 3.0  # +3 秒给 SMU 读写和启动开销
            value -= self.cfg.step
        return total

    def _scan_core(self, state: CoreState, done: int, total: int, wall0: float) -> int:
        state.status = "scanning"
        value = state.next_value(self.cfg.start, self.cfg.step)

        while value >= self.co_min:
            mapping = self._apply_single(state.core, value)
            state.physical = mapping.get(state.core, state.physical)

            # 先落盘再压测：万一这一档把系统测死，下次能从中断处接着走
            self.cp.inflight = {
                "core": state.core,
                "value": value,
                "started": datetime.now().isoformat(timespec="seconds"),
            }
            self._save()

            dwell = self.cfg.dwell_for(value)
            result: ProbeResult = self.probe.run(state.core, value, dwell)
            self.cp.inflight = None

            state.tested[str(value)] = result.ok
            state.notes[str(value)] = f"{result.reason}: {result.detail}"
            state.last_reason = state.notes[str(value)]

            done += 1
            self._remaining = max(0.0, getattr(self, "_remaining", 0.0) - (dwell + 3.0))
            eta = self._remaining
            flag = "OK  " if result.ok else "FAIL"
            self.log(
                f"[core {state.core}] {value:+d} {flag} {dwell:.0f}s "
                f"({done}/{total}, ETA {eta / 60:.0f}min) {state.last_reason}"
            )

            if result.ok:
                state.deepest_stable = value
                self._save()
                value -= self.cfg.step
                continue

            # 失败：回退安全余量，锁定该核
            state.failed_at = value
            base = state.deepest_stable if state.deepest_stable is not None else value
            state.recommend = min(base + self.cfg.safety, self.co_max)
            state.status = "done"
            self.log(f"[core {state.core}] 锁定：极限 ≥ {base:+d}，建议 {state.recommend:+d}")
            self._save()
            return done

        state.status = "floor"
        state.recommend = self.co_min + self.cfg.safety
        self.log(f"[core {state.core}] 触底 {self.co_min:+d} 仍稳定，建议 {state.recommend:+d}")
        self._save()
        return done

    def _save(self) -> None:
        self.cp.save(self.path)


def recommendations(cp: Checkpoint) -> list[int]:
    out = []
    for key in sorted(cp.cores, key=int):
        state = cp.cores[key]
        out.append(state.recommend if state.recommend is not None else 0)
    return out


def summarize(cp: Checkpoint) -> str:
    lines = ["", "=" * 62, "结果汇总", "=" * 62]
    lines.append(f"{'核':<6}{'物理核':<8}{'最深稳定':<10}{'失败点':<10}{'建议':<8}{'状态'}")
    lines.append("-" * 62)
    for key in sorted(cp.cores, key=int):
        c = cp.cores[key]
        lines.append(
            f"{c.core:<6}{str(c.physical):<8}"
            f"{str(c.deepest_stable):<10}{str(c.failed_at):<10}"
            f"{str(c.recommend):<8}{c.status}"
        )
    lines.append("-" * 62)
    recs = recommendations(cp)
    if recs and all(c.recommend is not None for c in cp.cores.values()):
        lines.append("BIOS Per Core 填写值：" + ",".join(str(v) for v in recs))
    else:
        pending = [c.core for c in cp.cores.values() if c.recommend is None]
        lines.append(f"尚未完成：核 {pending} 还没有建议值，现在还不是最终结果。")
    return "\n".join(lines)
