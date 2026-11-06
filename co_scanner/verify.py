"""全核叠加验证。

为什么这一步不能省（本次实测的直接教训）：
单核逐测时其余核 CO 归零，PPT 余量大、温度低。等六核全部挂上负压，
空闲核也在低压下待着、测试核能 boost 到更高点，原本"全过"的 Core 0
在第 6 次测试就崩了。**单核结果天然偏乐观，只有全核叠加才是真实场景。**

所以扫描得到建议值之后，必须再走一遍这里：一次性把六核值全写进去，
跑一段足够长的全核满载，确认没有核在「邻居也降压」的环境下塌掉。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime

from .probe import ProbeResult
from .smu import SmuBackend, SmuError
from .stress import AllCoreProbe


@dataclass
class VerifyConfig:
    duration: float = 1200.0
    memory: str = "2G"
    algorithm: str = "SFTv4"
    restore_after: bool = True


@dataclass
class VerifyResult:
    started: str = ""
    ended: str = ""
    planned_seconds: float = 0.0
    values: list[int] = field(default_factory=list)
    origin: list[int] = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    detail: str = ""
    metrics: dict = field(default_factory=dict)
    readback: list[int] = field(default_factory=list)
    interrupted_before: bool = False

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path: str) -> "VerifyResult | None":
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return cls(**json.load(fh))


class AllCoreVerifier:
    def __init__(
        self,
        smu: SmuBackend,
        stress: AllCoreProbe,
        config: VerifyConfig,
        marker_path: str,
        on_event=print,
    ) -> None:
        self.smu = smu
        self.stress = stress
        self.cfg = config
        self.marker = marker_path
        self.log = on_event

    def run(self, values: list[int]) -> VerifyResult:
        result = VerifyResult(
            started=datetime.now().isoformat(timespec="seconds"),
            planned_seconds=self.cfg.duration,
            values=list(values),
        )

        if os.path.exists(self.marker):
            result.interrupted_before = True
            self.log("[warn] 检测到上一次全核验证没有正常结束（很可能是冻结/掉电）。")
            try:
                os.remove(self.marker)
            except OSError:
                pass

        result.origin = self.smu.read()
        self.log(f"[verify] 写入全核 CO：{values}（原值 {result.origin}）")
        self.smu.write(values)
        result.readback = self.smu.read()
        if result.readback != values:
            result.ok = False
            result.reason = "WRITE_MISMATCH"
            result.detail = f"写入后回读不一致：期望 {values}，实际 {result.readback}"
            result.ended = datetime.now().isoformat(timespec="seconds")
            return result

        self._write_marker(values)
        started = time.monotonic()
        try:
            probe_result: ProbeResult = self.stress.run(self.cfg.duration)
        except KeyboardInterrupt:
            self.log("\n[abort] 用户中断全核验证。")
            self._clear_marker()
            if self.cfg.restore_after:
                self._restore(result.origin)
            raise

        result.ended = datetime.now().isoformat(timespec="seconds")
        result.ok = probe_result.ok
        result.reason = probe_result.reason
        result.detail = probe_result.detail
        result.metrics = dict(probe_result.metrics)
        result.metrics["actual_seconds"] = round(time.monotonic() - started, 1)
        self._clear_marker()

        if self.cfg.restore_after:
            self._restore(result.origin)

        verdict = "通过" if result.ok else f"失败（{result.reason}）"
        self.log(f"[verify] 全核 {self.cfg.duration:.0f}s 结果：{verdict} — {result.detail}")
        return result

    def _restore(self, origin: list[int]) -> None:
        if not origin:
            return
        try:
            self.smu.write(origin)
            self.log(f"[verify] 已恢复原 CO：{origin}")
        except SmuError as exc:
            self.log(f"[verify] 恢复失败（{exc}），请手动确认当前 CO。")

    def _write_marker(self, values: list[int]) -> None:
        try:
            with open(self.marker, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "values": values,
                        "started": datetime.now().isoformat(timespec="seconds"),
                    },
                    fh,
                    ensure_ascii=False,
                )
        except OSError:
            pass

    def _clear_marker(self) -> None:
        try:
            os.remove(self.marker)
        except OSError:
            pass
