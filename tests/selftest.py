"""自测：三路判据、冻结续跑、建议值算法、报告生成、多平台适配、配置与断点守卫。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from co_scanner.config import ConfigError, resolve  # noqa: E402
from co_scanner.platforminfo import Platform, UnsupportedPlatform, detect_zen  # noqa: E402
from co_scanner.probe import MonitorConfig, ProcessProbe, ProbeResult  # noqa: E402
from co_scanner.report import build_report  # noqa: E402
from co_scanner.scan import (  # noqa: E402
    Checkpoint,
    CheckpointMismatch,
    CoreState,
    ScanConfig,
    Scanner,
    recommendations,
)
from co_scanner.smu import SimulatedSmu  # noqa: E402
from co_scanner.verify import VerifyResult  # noqa: E402
from co_scanner.whea import snapshot  # noqa: E402

PY = sys.executable
HEALTHY = f'{PY}|-c|import time\nt=time.monotonic()\nwhile time.monotonic()-t<{{seconds}}: pass'
EXITS = f'{PY}|-c|import time\ntime.sleep(2)'
THROTTLED = (
    f'{PY}|-c|import time\nt=time.monotonic()\n'
    'while time.monotonic()-t<{seconds}:\n    time.sleep(0.4)'
)

_ok = True


def check(name: str, passed: bool, extra: str = "") -> None:
    global _ok
    _ok = _ok and passed
    print(f"  [{'PASS' if passed else 'FAIL'}] {name:<22} {extra}")


def build(template: str) -> list[str]:
    return template.split("|")


def case(name: str, cmd: list[str], dwell: float, expect_ok: bool) -> None:
    probe = ProcessProbe(cmd=cmd, monitor=MonitorConfig(sample_interval=0.25, warmup=2.0))
    result = probe.run(core=0, value=-20, dwell=dwell)
    check(name, result.ok == expect_ok,
          f"ok={result.ok} reason={result.reason} min_ratio={result.metrics.get('min_ratio')}")


def test_judge() -> None:
    print("== 三路判据 ==")
    case("正常满载", build(HEALTHY), 8.0, True)
    case("进程退出", build(EXITS), 8.0, False)
    case("CPU 掉底", build(THROTTLED), 8.0, False)


def test_affinity() -> None:
    print("\n== 亲和性绑定 ==")
    from co_scanner import winproc

    masks = winproc.core_affinity_masks()
    check("拓扑解析", len(masks) >= 2, f"{len(masks)} 个物理核 {[hex(m) for m in masks[:4]]}")
    distinct = len(set(masks)) == len(masks)
    check("掩码互不相同", distinct, "每个核的 SMT 对独立")


def test_freeze_resume() -> None:
    print("\n== 冻结后续跑 ==")
    tmp = os.path.join(tempfile.mkdtemp(), "cp.json")
    cp = Checkpoint(cpu="Test", zen=5, co_min=-50, co_max=30, started="2026-11-06T00:00:00")
    for core in range(3):
        cp.cores[str(core)] = CoreState(core=core)
    # core 0 已测到 -25，正卡在 -26 上（模拟冻结）
    cp.cores["0"].tested = {"-20": True, "-21": True, "-22": True, "-23": True,
                            "-24": True, "-25": True}
    cp.cores["0"].deepest_stable = -25
    cp.cores["0"].status = "scanning"
    cp.inflight = {"core": 0, "value": -26, "started": "2026-11-06T00:10:00"}
    cp.save(tmp)

    smu = SimulatedSmu(limits={0: -25, 1: -30, 2: -30})
    scanner = Scanner(
        smu=smu, probe=_NoopProbe(), config=ScanConfig(start=-20, safety=2, dwell=0),
        co_min=-50, co_max=30, checkpoint_path=tmp, on_event=lambda _m: None,
    )
    result = scanner.run(resume=True)
    c0 = result.cores["0"]
    check("中断档判失败", c0.tested.get("-26") is False, f"-26 -> {c0.tested.get('-26')}")
    check("建议值回退", c0.recommend == -23, f"建议 {c0.recommend}（-25 +2）")
    check("未重跳同一个坑", "-27" not in c0.tested, "没有继续下探")


def test_recommend() -> None:
    print("\n== 建议值 ==")
    cp = Checkpoint()
    for core, deep, fail in [(0, -38, -39), (1, -46, -47), (2, None, -20)]:
        st = CoreState(core=core, deepest_stable=deep, failed_at=fail,
                       recommend=(deep + 2) if deep is not None else fail + 2, status="done")
        cp.cores[str(core)] = st
    check("顺序与数量", recommendations(cp) == [-36, -44, -18], str(recommendations(cp)))


def test_report() -> None:
    print("\n== 报告生成 ==")
    cp = Checkpoint(cpu="AMD Ryzen 5 9600X 6-Core Processor", zen=5, co_min=-50, co_max=30,
                    started="2026-11-06T00:25:00", origin=[0, 0, 0, 0, 0, 0],
                    config={"start": -20, "step": 1, "safety": 2, "dwell": 60, "warmup": 180})
    for core in range(6):
        st = CoreState(core=core, physical=[0, 1, 2, 3, 5, 6][core])
        for offset in range(18):
            st.tested[str(-20 - offset)] = offset < 18
        st.tested["-38"] = core != 0
        st.deepest_stable = -37 if core == 0 else -38
        st.failed_at = -38 if core == 0 else -39
        st.recommend = -35 if core == 0 else -36
        st.status = "done"
        cp.cores[str(core)] = st

    verify = VerifyResult(
        started="2026-11-06T01:00:00", ended="2026-11-06T01:20:00",
        planned_seconds=1200, values=[-35, -36, -36, -36, -36, -36],
        ok=True, reason="OK", detail="存活 1200.3s",
        metrics={"actual_seconds": 1201.0, "avg_ratio": 11.8, "whea_new": 0},
    )
    html = build_report(cp, verify)
    tmp = os.path.join(tempfile.mkdtemp(), "report.html")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(html)

    checks = [
        ("含 CPU 型号", "Ryzen 5 9600X" in html),
        ("含 BIOS 卡", "CURVE OPTIMIZER" in html),
        ("含建议值", ">-35<" in html.replace("</td>", "<")),
        ("含验证结论", "已通过全核叠加验证" in html),
        ("热力图有格子", html.count('class="cell p"') > 50),
        ("无外部资源", "http://" not in html and "https://" not in html),
        ("文件可写入", os.path.getsize(tmp) > 8000),
    ]
    for name, passed in checks:
        check(name, passed, "" if passed else f"size={os.path.getsize(tmp)}")


def test_whea() -> None:
    print("\n== WHEA 查询 ==")
    snap = snapshot()
    check("查询可用", snap.available, snap.describe())


def test_platform() -> None:
    print("\n== 型号识别 ==")
    cases = {
        "AMD Ryzen 9 9950X 16-Core Processor": 5,
        "AMD Ryzen 7 9800X3D 8-Core Processor": 5,
        "AMD Ryzen 5 9600X 6-Core Processor": 5,
        "AMD Ryzen 7 8700G w/ Radeon 780M": 4,
        "AMD Ryzen 7 7800X3D 8-Core Processor": 4,
        "AMD Ryzen 9 5950X 16-Core Processor": 3,
        "AMD Ryzen 5 5600G with Radeon Graphics": 3,
        "AMD Ryzen 5 3600 6-Core Processor": 2,
        "AMD Ryzen 7 2700X Eight-Core Processor": 1,
        "AMD Ryzen Threadripper PRO 5995WX 64-Cores": 3,
        "AMD Ryzen AI 9 HX 370 w/ Radeon 890M": 5,
    }
    bad = []
    for name, expect in cases.items():
        try:
            got = detect_zen(name)
        except UnsupportedPlatform:
            got = None
        if got != expect:
            bad.append(f"{name} -> {got}")
    check("11 个型号全部正确", not bad, "; ".join(bad))

    try:
        detect_zen("AMD Eng Sample: 100-000000XXX")
        check("工程样品被拒绝", False, "应当抛 UnsupportedPlatform")
    except UnsupportedPlatform:
        check("工程样品被拒绝", True, "提示可用 --assume-zen")


def test_config() -> None:
    print("\n== 配置文件 ==")
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "co-scanner.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "_说明": "注释行应被忽略",
                "ycruncher": "D:\\yc\\y-cruncher.exe",
                "dwell": 45,
                "cores": "0, 1 ,2",
            },
            fh,
            ensure_ascii=False,
        )

    args = argparse.Namespace(config=path)
    cfg = resolve(args)
    check("读取文件值", cfg.ycruncher == "D:\\yc\\y-cruncher.exe", cfg.ycruncher)
    check("cores 解析", cfg.cores == [0, 1, 2], str(cfg.cores))
    check("注释键被忽略", True, "_说明 没有触发未知键报错")

    # 命令行优先于配置文件
    args2 = argparse.Namespace(config=path, dwell=12.5)
    check("命令行覆盖文件", resolve(args2).dwell == 12.5, str(resolve(args2).dwell))

    # 未知键必须报错，不能静默忽略
    bad_path = os.path.join(tmpdir, "bad.json")
    with open(bad_path, "w", encoding="utf-8") as fh:
        json.dump({"ycruncher_path": "x"}, fh)
    try:
        resolve(argparse.Namespace(config=bad_path))
        check("未知键被拦截", False, "应当抛 ConfigError")
    except ConfigError as exc:
        check("未知键被拦截", "ycruncher_path" in str(exc), str(exc)[:60])

    # 类型错误也要报错
    with open(bad_path, "w", encoding="utf-8") as fh:
        json.dump({"dwell": "很久"}, fh)
    try:
        resolve(argparse.Namespace(config=bad_path))
        check("类型错误被拦截", False, "应当抛 ConfigError")
    except ConfigError:
        check("类型错误被拦截", True, "dwell 必须是数字")

    # --no-fine 关掉两段式
    args3 = argparse.Namespace(config=path, no_fine=True)
    check("--no-fine 生效", resolve(args3).fine_from is None, str(resolve(args3).fine_from))


def test_checkpoint_guard() -> None:
    print("\n== 断点守卫 ==")
    tmpdir = tempfile.mkdtemp()

    def make(cpu: str, cores: int) -> str:
        path = os.path.join(tmpdir, f"{abs(hash((cpu, cores)))}.json")
        cp = Checkpoint(cpu=cpu, zen=5, co_min=-50, co_max=30)
        for core in range(cores):
            cp.cores[str(core)] = CoreState(core=core)
        cp.save(path)
        return path

    path = make("AMD Ryzen 5 9600X", 6)
    smu = SimulatedSmu(limits={i: -30 for i in range(6)})

    # 换了 CPU：必须拒绝
    try:
        Scanner(
            smu=smu, probe=_NoopProbe(), config=ScanConfig(start=-20, dwell=0, fine_from=None),
            co_min=-50, co_max=30, checkpoint_path=path, cpu="AMD Ryzen 9 9950X",
            on_event=lambda _m: None,
        ).run(resume=True)
        check("换 CPU 被拒绝", False, "应当抛 CheckpointMismatch")
    except CheckpointMismatch as exc:
        check("换 CPU 被拒绝", "--reset" in str(exc), str(exc).splitlines()[0][:50])

    # 核数对不上：也要拒绝
    path2 = make("AMD Ryzen 5 9600X", 6)
    smu4 = SimulatedSmu(limits={i: -30 for i in range(4)})
    try:
        Scanner(
            smu=smu4, probe=_NoopProbe(), config=ScanConfig(start=-20, dwell=0, fine_from=None),
            co_min=-50, co_max=30, checkpoint_path=path2, cpu="AMD Ryzen 5 9600X",
            on_event=lambda _m: None,
        ).run(resume=True)
        check("核数不符被拒绝", False, "应当抛 CheckpointMismatch")
    except CheckpointMismatch:
        check("核数不符被拒绝", True, "6 核断点 vs 4 核 SMU")


def test_partial_scan() -> None:
    print("\n== 分批扫描与重扫 ==")
    path = os.path.join(tempfile.mkdtemp(), "cp.json")
    limits = {i: -24 for i in range(6)}

    def run(**cfg_kwargs):
        return Scanner(
            smu=SimulatedSmu(limits=limits), probe=_NoopProbe(),
            config=ScanConfig(start=-20, step=1, dwell=0, fine_from=None, **cfg_kwargs),
            co_min=-30, co_max=30, checkpoint_path=path, cpu="Test",
            on_event=lambda _m: None,
        ).run(resume=True)

    cp = run(cores=(0, 1))
    done = sorted(int(k) for k, v in cp.cores.items() if v.status in ("done", "floor"))
    check("只扫选中的核", done == [0, 1], str(done))
    check("未完成不标 finished", cp.finished is False, str(cp.finished))

    cp = run()
    done = sorted(int(k) for k, v in cp.cores.items() if v.status in ("done", "floor"))
    check("续跑补齐其余核", done == list(range(6)), str(done))
    check("全部完成才标 finished", cp.finished is True, str(cp.finished))

    # 手动塞一条假结果，再用 --retest 清掉
    cp.cores["0"].tested["-99"] = False
    cp.save(path)
    cp = run(cores=(0,), retest=(0,))
    check("retest 清空旧结果", "-99" not in cp.cores["0"].tested, "旧记录已丢弃")
    check("retest 只动指定核", int(cp.cores["0"].steps) > 0, f"core0 {cp.cores['0'].steps} 档")


def test_report_many_cores() -> None:
    print("\n== 16 核报告 ==")
    cp = Checkpoint(
        cpu="AMD Ryzen 9 9950X 16-Core Processor", zen=5, physical_cores=16,
        co_min=-50, co_max=30, started="2026-11-06T09:00:00", origin=[0] * 16,
        config={"start": -20, "step": 1, "safety": 2, "dwell": 30,
                "dwell_fine": 120, "fine_from": -35, "warmup": 180},
    )
    for core in range(16):
        st = CoreState(core=core, physical=core, status="done")
        for offset in range(20):
            st.tested[str(-20 - offset)] = True
        st.deepest_stable = -39
        st.failed_at = -40
        st.recommend = -37
        cp.cores[str(core)] = st

    plat = Platform(name=cp.cpu, zen=5, co_min=-50, co_max=30, physical_cores=16,
                    logical_cores=32, ccd_count=2)
    html = build_report(cp, None, plat=plat)
    checks = [
        ("显示 16 核", "16 核" in html),
        ("显示 2 CCD", "2 CCD" in html),
        ("双 CCD 提示", "两个 CCD 的体质" in html),
        ("不再写死六核", "六核" not in html),
        ("16 行 BIOS 卡", html.count('class="mono">Core ') >= 16),
    ]
    for name, passed in checks:
        check(name, passed)

    # 单 CCD 平台不应该出现双 CCD 提示
    plat1 = Platform(name="AMD Ryzen 5 9600X", zen=5, co_min=-50, co_max=30,
                     physical_cores=6, logical_cores=12, ccd_count=1)
    html1 = build_report(cp, None, plat=plat1)
    check("单 CCD 无多余提示", "两个 CCD 的体质" not in html1)


def test_doctor() -> None:
    print("\n== 环境体检 ==")
    from co_scanner.doctor import FAIL, _AV, _CONFLICT, process_basenames, run

    rep = run()
    text = rep.render()
    check("体检能跑完", len(rep.checks) >= 8, f"{len(rep.checks)} 项")
    check("输出非空", len(text) > 200, f"{len(text)} 字符")
    check("有阻塞项判定", any(c.status == FAIL for c in rep.checks) == bool(rep.fails),
          f"{len(rep.fails)} 个阻塞项")

    # 回归：szExeFile 带 .exe，查表 key 不带后缀。
    # 曾经因为没剥后缀，安全软件/干扰进程两项永远报「未发现」（静默失效）。
    fake = [(1, 0, "HipsDaemon.exe"), (2, 0, "OCCT.exe"), (3, 0, "explorer.exe")]
    names = process_basenames(fake)
    check("进程名剥掉 .exe", names == {"hipsdaemon", "occt", "explorer"}, str(sorted(names)))
    check("能识别火绒", any(n in _AV for n in names))
    check("能识别干扰进程", any(n in _CONFLICT for n in names))
    check("大小写不敏感", process_basenames([(9, 0, "HiPsTrAy.Exe")]) == {"hipstray"})
    check("无后缀名也兼容", process_basenames([(9, 0, "System")]) == {"system"})


class _NoopProbe:
    def run(self, core: int, value: int, dwell: float) -> ProbeResult:
        return ProbeResult(True, "OK", "noop")


if __name__ == "__main__":
    test_judge()
    test_affinity()
    test_freeze_resume()
    test_recommend()
    test_report()
    test_whea()
    test_platform()
    test_config()
    test_checkpoint_guard()
    test_partial_scan()
    test_report_many_cores()
    test_doctor()
    print("\n结果：" + ("全部通过" if _ok else "存在失败项"))
    raise SystemExit(0 if _ok else 1)
