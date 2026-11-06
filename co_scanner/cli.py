"""co-scanner 命令行入口。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime

from . import __version__
from .config import ConfigError, Settings, resolve, write_sample
from .platforminfo import UnsupportedPlatform, detect
from .probe import MonitorConfig, ProcessProbe, SimulatedProbe
from .report import write_report
from .scan import CheckpointMismatch, ScanConfig, Scanner, recommendations, summarize
from .smu import CliSmu, NotElevated, SimulatedSmu, SmuError, find_smu_cli, is_admin
from .stress import AllCoreProbe, StressError, YCruncherProbe, find_ycruncher
from .verify import AllCoreVerifier, VerifyConfig, VerifyResult

BAR = "=" * 62


def _print_platform(plat) -> None:
    print(BAR)
    print(f"CPU       : {plat.name}")
    print(f"架构      : {plat.zen_label}")
    print(f"核心      : {plat.physical_cores} 物理 / {plat.logical_cores} 逻辑"
          f"{'（SMT 关闭）' if not plat.smt else ''}")
    print(f"CCD       : {plat.ccd_count}")
    print(f"CO 范围   : {plat.co_min} ~ {plat.co_max}")
    if plat.x3d or plat.is_apu or plat.hedt:
        tags = [
            name
            for name, on in (("X3D", plat.x3d), ("APU", plat.is_apu), ("HEDT", plat.hedt))
            if on
        ]
        print(f"标记      : {' / '.join(tags)}")
    print(BAR)
    for note in plat.notes:
        print(f"  · {note}")
    if plat.notes:
        print(BAR)


def _default_checkpoint(plat) -> str:
    return os.path.join(os.getcwd(), f"co-scan-{plat.zen}zen.json")


def _archive(path: str) -> str | None:
    """把旧断点改名归档，而不是直接删掉 —— 那是几小时的测试结果。"""
    if not os.path.exists(path):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = f"{path}.bak-{stamp}"
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target


def _resolve(args) -> Settings | None:
    try:
        return resolve(args)
    except ConfigError as exc:
        print(f"[错误] 配置问题：{exc}")
        return None


# ---------------- detect ----------------

def cmd_detect(args) -> int:
    cfg = _resolve(args)
    if cfg is None:
        return 2
    try:
        plat = detect(assume_zen=cfg.assume_zen)
    except UnsupportedPlatform as exc:
        print(f"[错误] {exc}")
        return 2
    _print_platform(plat)
    print(f"管理员    : {'是' if is_admin() else '否'}")
    if cfg.source:
        print(f"配置文件  : {cfg.source}")

    exe = cfg.smu_cli or find_smu_cli()
    if not exe:
        print("SMU 工具  : 未找到 ryzen-smu-cli.exe")
        print("            可用 --smu 指定，或设置环境变量 CO_SCANNER_SMU_CLI")
        return 2
    print(f"SMU 工具  : {exe}")

    yc = cfg.ycruncher or find_ycruncher()
    print(f"y-cruncher: {yc or '未找到（可用 --ycruncher 指定）'}")

    if not is_admin():
        print("\n[跳过] 读取当前 CO 需要管理员权限，请以管理员身份重新运行。")
        return 0

    try:
        smu = CliSmu(exe)
        offsets = smu.read()
    except NotElevated as exc:
        print(f"\n[错误] {exc}")
        return 3
    except SmuError as exc:
        print(f"\n[错误] {exc}")
        return 4

    print(f"当前 CO   : {offsets}")
    print(f"核数量    : {len(offsets)}（以 SMU 返回为准）")
    mapping = smu.write(offsets)  # 原样回写，顺便拿到 逻辑核 -> 物理核 映射
    print("物理核映射: " + ", ".join(f"{k}->{v}" for k, v in sorted(mapping.items())))
    return 0


# ---------------- doctor ----------------

def cmd_doctor(args) -> int:
    from .doctor import SKIP, run as run_doctor

    cfg = _resolve(args)
    if cfg is None:
        return 2
    try:
        rep = run_doctor(cfg)
    except ConfigError as exc:
        print(f"[错误] {exc}")
        return 2

    print(BAR)
    print(f"环境体检 · co-scanner {__version__}")
    print(BAR)
    print(rep.render(verbose=args.verbose))
    print(BAR)
    if rep.fails:
        print(f"有 {len(rep.fails)} 个阻塞项必须解决，否则跑出来的结果不可信。")
        return 1
    if rep.warns:
        print(f"体检通过，有 {len(rep.warns)} 条提醒。可以开扫。")
    else:
        print("体检全部通过，可以开扫。")
    return 0


# ---------------- init ----------------

def cmd_init(args) -> int:
    path = args.out or os.path.join(os.getcwd(), "co-scanner.json")
    try:
        write_sample(path, force=args.force)
    except ConfigError as exc:
        print(f"[错误] {exc}")
        return 2
    print(f"配置模板已写入 {path}")
    print("按本机实际路径改好之后，scan / verify 会自动读取当前目录下的这个文件。")
    return 0


# ---------------- scan ----------------

def _build_backend(args, cfg: Settings):
    """返回 (smu, probe, warmup_fn)。"""
    if args.simulate:
        limits = {}
        if args.sim_limits:
            for idx, raw in enumerate(args.sim_limits.split(",")):
                limits[idx] = int(raw)
        else:
            limits = {0: -39, 1: -46, 2: -50, 3: -44, 4: -50, 5: -41}
        print(f"[模拟] 各核稳定下限：{limits}")
        return SimulatedSmu(limits=limits), SimulatedProbe(limits=limits), None

    exe = cfg.smu_cli or find_smu_cli()
    if not exe:
        raise SmuError("未找到 ryzen-smu-cli.exe，可用 --smu 指定路径。")
    if not is_admin():
        raise NotElevated("真实扫描需要管理员权限，请以管理员身份重新运行。")
    smu = CliSmu(exe)

    if args.stress_burner:
        print(
            "[警告] --stress-burner 用的是 Python 空转，只用于验证 SMU 链路，"
            "不具备任何稳定性判定能力。真实结果必须用 y-cruncher。"
        )
        probe = ProcessProbe(cmd=[
            sys.executable, "-c",
            "import time\nt=time.monotonic()\nwhile time.monotonic()-t<{seconds}: pass",
        ])
        return smu, probe, None

    if args.stress:
        probe = ProcessProbe(cmd=args.stress.split())
        return smu, probe, None

    yc = cfg.ycruncher or find_ycruncher()
    if not yc or not os.path.isfile(yc):
        raise StressError(
            "未找到 y-cruncher。可用 --ycruncher 指定 y-cruncher.exe 的路径，"
            "或先用 --stress \"命令\" 接入别的压测程序。"
        )
    from . import winproc

    masks = winproc.core_affinity_masks()
    probe = YCruncherProbe(
        exe=yc,
        algorithm=cfg.algorithm,
        memory=cfg.memory,
        masks=masks,
        monitor=MonitorConfig(sample_interval=0.5),
    )
    warm = AllCoreProbe(exe=yc, algorithm=cfg.algorithm, memory=cfg.warmup_memory)

    def warmup(seconds: float) -> None:
        result = warm.run(seconds)
        print(f"[warmup] {result.detail}")

    return smu, probe, warmup


def _parse_core_list(raw: str, plat) -> tuple[int, ...]:
    values = tuple(int(p) for p in raw.replace("，", ",").split(",") if p.strip())
    bad = [c for c in values if c < 0 or c >= plat.physical_cores]
    if bad:
        raise ConfigError(
            f"核号 {bad} 超出范围，本机只有 0~{plat.physical_cores - 1}"
        )
    return values


def cmd_scan(args) -> int:
    cfg = _resolve(args)
    if cfg is None:
        return 2
    try:
        plat = detect(assume_zen=cfg.assume_zen)
    except UnsupportedPlatform as exc:
        print(f"[错误] {exc}")
        return 2
    _print_platform(plat)

    co_min = args.min if args.min is not None else plat.co_min
    if not (plat.co_min <= co_min <= plat.co_max):
        print(f"[错误] --min {co_min} 超出该平台 CO 范围 {plat.co_min}~{plat.co_max}")
        return 2

    try:
        cores = cfg.cores
        if args.cores:
            cores = _parse_core_list(args.cores, plat)
        elif cores:
            bad = [c for c in cores if c < 0 or c >= plat.physical_cores]
            if bad:
                raise ConfigError(
                    f"配置文件里的 cores {bad} 超出范围 0~{plat.physical_cores - 1}"
                )
        retest = _parse_core_list(args.retest, plat) if args.retest else None
    except ConfigError as exc:
        print(f"[错误] {exc}")
        return 2

    scan_cfg = ScanConfig(
        start=cfg.start,
        step=cfg.step,
        safety=cfg.safety,
        dwell=cfg.dwell,
        dwell_fine=cfg.dwell_fine,
        fine_from=cfg.fine_from,
        warmup=cfg.warmup,
        cores=tuple(cores) if cores else None,
        retest=retest,
    )
    out_path = args.checkpoint or _default_checkpoint(plat)
    if args.reset:
        archived = _archive(out_path)
        if archived:
            print(f"[reset] 旧断点已归档为 {archived}")
        else:
            print("[reset] 没有旧断点需要归档")

    try:
        smu, probe, warmup_fn = _build_backend(args, cfg)
    except (SmuError, NotElevated, StressError) as exc:
        print(f"[错误] {exc}")
        return 3

    scanner = Scanner(
        smu=smu,
        probe=probe,
        config=scan_cfg,
        co_min=co_min,
        co_max=plat.co_max,
        checkpoint_path=out_path,
        cpu=plat.name,
        zen=plat.zen,
        physical_cores=plat.physical_cores,
        warmup_fn=warmup_fn,
    )

    try:
        cp = scanner.run(resume=not args.no_resume)
    except CheckpointMismatch as exc:
        print(f"[错误] {exc}")
        return 6
    except (SmuError, NotElevated) as exc:
        print(f"[错误] {exc}")
        return 4
    except KeyboardInterrupt:
        return 130

    print(summarize(cp))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(asdict(cp), fh, ensure_ascii=False, indent=2)
        print(f"\nJSON 已写入 {args.json}")
    if args.report:
        write_report(args.report, cp, plat=plat)
        print(f"报告已写入 {args.report}")
    return 0


# ---------------- verify ----------------

def _load_checkpoint(path: str):
    from .scan import Checkpoint

    cp = Checkpoint.load(path)
    if cp is None:
        print(f"[错误] 找不到断点文件 {path}，请先运行 scan。")
        return None
    return cp


def _parse_values(raw: str) -> list[int] | None:
    try:
        return [int(v) for v in raw.replace("，", ",").split(",") if v.strip()]
    except ValueError:
        print(f"[错误] --values 里必须全是整数，实际是 {raw!r}")
        return None


def _manual_checkpoint(plat, values: list[int]):
    """没有扫描断点时，按 --values 造一个只有建议值的断点，好让报告能生成。"""
    from .scan import Checkpoint, CoreState

    cp = Checkpoint(
        cpu=plat.name,
        zen=plat.zen,
        physical_cores=plat.physical_cores,
        co_min=plat.co_min,
        co_max=plat.co_max,
        started=datetime.now().isoformat(timespec="seconds"),
        origin=[0] * len(values),
    )
    for core, value in enumerate(values):
        cp.cores[str(core)] = CoreState(
            core=core, status="done", recommend=value, deepest_stable=value
        )
    return cp


def cmd_verify(args) -> int:
    from .scan import Checkpoint

    cfg = _resolve(args)
    if cfg is None:
        return 2
    try:
        plat = detect(assume_zen=cfg.assume_zen)
    except UnsupportedPlatform as exc:
        print(f"[错误] {exc}")
        return 2
    _print_platform(plat)

    cp_path = args.checkpoint or _default_checkpoint(plat)
    cp = Checkpoint.load(cp_path)
    manual = _parse_values(args.values) if args.values else None
    if args.values and manual is None:
        return 2

    if cp is None and manual is None:
        print(
            f"[错误] 找不到断点文件 {cp_path}，请先运行 scan，"
            "或用 --values 手动指定各核的值（例如 --values -38,-40,-40,-40,-40,-40）。"
        )
        return 2
    if cp is None:
        cp = _manual_checkpoint(plat, manual)
        print(
            "[提示] 没有扫描断点，按 --values 手动值做全核验证。"
            "报告里不会有逐核扫描数据，这是预期行为。"
        )
    elif cp.cpu and cp.cpu != plat.name:
        print(
            f"[警告] 断点来自 {cp.cpu}，当前是 {plat.name}。核号顺序可能不同，"
            "建议重新扫描而不是沿用旧结果。"
        )

    if len(cp.cores) != plat.physical_cores:
        print(
            f"[错误] 断点里有 {len(cp.cores)} 个核，本机是 {plat.physical_cores} 个物理核。"
            "核数对不上时写进去的值会错位，请重新扫描或换一份断点。"
        )
        return 2

    if manual is not None:
        values = manual
    else:
        missing = [c.core for c in cp.cores.values() if c.recommend is None]
        if missing:
            print(f"[错误] 这些核还没有扫描结果：{missing}，请先跑完 scan，或用 --values 手动指定。")
            return 2
        values = recommendations(cp)
    if not values or len(values) != len(cp.cores):
        print(f"[错误] 值数量与核心数不匹配：给了 {len(values)} 个，断点里有 {len(cp.cores)} 个核")
        return 2
    out_of_range = [v for v in values if not (plat.co_min <= v <= plat.co_max)]
    if out_of_range:
        print(
            f"[错误] 有值超出 CO 范围 {plat.co_min}~{plat.co_max}："
            + ", ".join(str(v) for v in out_of_range)
        )
        return 2

    print(BAR)
    print(f"全核叠加验证 · {plat.name}（{plat.physical_cores} 核）")
    print(f"写入 CO    : {','.join(str(v) for v in values)}")
    print(f"时长       : {args.duration / 60:.0f} 分钟")
    print(BAR)

    if not is_admin():
        print("[错误] 全核验证需要管理员权限，请以管理员身份重新运行。")
        return 3

    exe = cfg.smu_cli or find_smu_cli()
    if not exe:
        print("[错误] 未找到 ryzen-smu-cli.exe，可用 --smu 指定。")
        return 3
    yc = cfg.ycruncher or find_ycruncher()
    if not yc or not os.path.isfile(yc):
        print("[错误] 未找到 y-cruncher，可用 --ycruncher 指定。")
        return 3

    try:
        smu = CliSmu(exe)
    except (SmuError, NotElevated) as exc:
        print(f"[错误] {exc}")
        return 3

    stress = AllCoreProbe(
        exe=yc,
        algorithm=cfg.algorithm,
        memory=args.memory or "2G",
        monitor=MonitorConfig(sample_interval=1.0, warmup=10.0),
    )
    verifier = AllCoreVerifier(
        smu=smu,
        stress=stress,
        config=VerifyConfig(
            duration=args.duration,
            restore_after=not args.no_restore,
        ),
        marker_path=cp_path + ".verify",
    )

    try:
        result = verifier.run(values)
    except (SmuError, NotElevated) as exc:
        print(f"[错误] {exc}")
        return 4
    except KeyboardInterrupt:
        return 130

    result.metrics["logical_cores"] = plat.logical_cores
    result.metrics["physical_cores"] = plat.physical_cores
    avg = result.metrics.get("avg_ratio")
    if isinstance(avg, (int, float)) and plat.logical_cores:
        if avg < 0.7 * plat.logical_cores:
            print(
                f"[warn] 平均 CPU 时间占比只有 {avg:.2f}，逻辑核 {plat.logical_cores} 个，"
                "说明没有吃满全部核心，这次「通过」的说服力有限。"
            )

    out = args.out or (cp_path.replace(".json", "") + "-verify.json")
    result.save(out)
    print(f"\n[verify] 结果已写入 {out}")

    if args.report:
        write_report(args.report, cp, result, plat=plat)
        print(f"[verify] 报告已写入 {args.report}")
    return 0 if result.ok else 5


# ---------------- report ----------------

def cmd_report(args) -> int:
    cfg = _resolve(args)
    if cfg is None:
        return 2

    plat = None
    try:
        plat = detect(assume_zen=cfg.assume_zen)
    except UnsupportedPlatform as exc:
        if not args.checkpoint:
            print(f"[错误] {exc}")
            return 2

    cp_path = args.checkpoint or _default_checkpoint(plat)
    cp = _load_checkpoint(cp_path)
    if cp is None:
        return 2

    # 断点不是本机跑的就不要拿本机的拓扑信息去渲染，会张冠李戴
    if plat is not None and cp.cpu and plat.name != cp.cpu:
        print(f"[提示] 断点来自 {cp.cpu}，与当前机器不同，报告不展示平台结构信息。")
        plat = None

    verify = None
    if args.verify:
        verify = VerifyResult.load(args.verify)
        if verify is None:
            print(f"[警告] 找不到验证结果 {args.verify}，报告将标记为未验证。")

    out = args.out or os.path.join(os.path.dirname(os.path.abspath(cp_path)), "co-report.html")
    write_report(out, cp, verify, plat=plat)
    print(f"报告已写入 {out}")
    return 0


# ---------------- parser ----------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="co-scanner",
        description="AMD Zen 3/4/5 逐核 Curve Optimizer 极限扫描与全核验证",
    )
    parser.add_argument("--version", action="version", version=f"co-scanner {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # 所有子命令共用的配置项。默认值留空，让配置文件能生效。
    def add_common(p, *, stress_paths=True):
        p.add_argument("--config", help="配置文件路径（默认读当前目录的 co-scanner.json）")
        p.add_argument("--assume-zen", type=int, metavar="N",
                       help="型号识别不了时手动指定 Zen 代数（3/4/5）")
        if stress_paths:
            p.add_argument("--smu", dest="smu_cli", help="ryzen-smu-cli.exe 路径")
            p.add_argument("--ycruncher", dest="ycruncher", help="y-cruncher.exe 路径")
            p.add_argument("--algorithm", help="y-cruncher 算法（默认 SFTv4）")
            p.add_argument("--memory", help="单核压测内存（默认 15M）")
            p.add_argument("--warmup-memory", help="预热用内存（默认 2G）")
            p.add_argument("--start", type=int, help="起始 CO（默认 -20）")
            p.add_argument("--step", type=int, help="每档步长（默认 1）")
            p.add_argument("--safety", type=int, help="建议值回退档数（默认 2）")
            p.add_argument("--dwell", type=float, help="浅段每档压测秒数（默认 30）")
            p.add_argument("--dwell-fine", type=float, help="深段每档压测秒数（默认 120）")
            p.add_argument("--fine-from", type=int, help="从哪个值开始用深段时长（默认 -35）")
            p.add_argument("--warmup", type=float, help="扫描前全核预热秒数（默认 180）")

    p_detect = sub.add_parser("detect", help="识别平台并读取当前 CO")
    add_common(p_detect)
    p_detect.set_defaults(func=cmd_detect)

    p_doctor = sub.add_parser("doctor", help="环境体检：开扫前把能踩的坑排掉")
    add_common(p_doctor)
    p_doctor.add_argument("-v", "--verbose", action="store_true", help="显示全部提示")
    p_doctor.set_defaults(func=cmd_doctor)

    p_init = sub.add_parser("init", help="生成配置文件模板")
    p_init.add_argument("-o", "--out", help="输出路径（默认 ./co-scanner.json）")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    p_init.set_defaults(func=cmd_init)

    p_scan = sub.add_parser("scan", help="逐核下探扫描")
    add_common(p_scan)
    p_scan.add_argument("--min", type=int, help="最深 CO（默认取平台下限）")
    p_scan.add_argument("--no-fine", action="store_true", help="关闭两段式，全程用 --dwell")
    p_scan.add_argument("--cores", help="只扫这些核，如 0,1,2（默认全部）")
    p_scan.add_argument("--retest", help="强制重扫这些核，忽略断点里的旧结果")
    p_scan.add_argument("--stress", help="自定义压测命令，支持 {core} {seconds} 占位符")
    p_scan.add_argument("--stress-burner", action="store_true",
                        help="内置空转压测，仅用于验证 SMU 链路")
    p_scan.add_argument("--simulate", action="store_true", help="用内存模拟后端自测")
    p_scan.add_argument("--sim-limits", help="模拟用的各核稳定下限，如 -39,-46,-50,-44,-50,-41")
    p_scan.add_argument("--checkpoint", help="断点文件路径")
    p_scan.add_argument("--json", dest="json", help="结果 JSON 输出路径")
    p_scan.add_argument("--report", help="扫描结束后生成 HTML 报告")
    p_scan.add_argument("--no-resume", action="store_true", help="忽略已有断点重新开始")
    p_scan.add_argument("--reset", action="store_true", help="归档旧断点后重新开始")
    p_scan.set_defaults(func=cmd_scan)

    p_verify = sub.add_parser("verify", help="全核叠加验证")
    add_common(p_verify, stress_paths=False)
    p_verify.add_argument("--smu", dest="smu_cli", help="ryzen-smu-cli.exe 路径")
    p_verify.add_argument("--ycruncher", dest="ycruncher", help="y-cruncher.exe 路径")
    p_verify.add_argument("--checkpoint", help="扫描断点文件路径")
    p_verify.add_argument("--values", help="手动指定各核的值，如 -38,-40,-40,-40,-40,-40")
    p_verify.add_argument("--duration", type=float, default=1200.0, help="满载秒数（默认 1200）")
    p_verify.add_argument("--memory", help="全核压测内存（默认 2G）")
    p_verify.add_argument("--no-restore", action="store_true", help="验证后不恢复原 CO")
    p_verify.add_argument("--out", help="验证结果 JSON 输出路径")
    p_verify.add_argument("--report", help="验证结束后生成 HTML 报告")
    p_verify.set_defaults(func=cmd_verify)

    p_report = sub.add_parser("report", help="生成 HTML 报告")
    p_report.add_argument("--config", help="配置文件路径")
    p_report.add_argument("--assume-zen", type=int, metavar="N", help="手动指定 Zen 代数")
    p_report.add_argument("--checkpoint", help="扫描断点文件路径")
    p_report.add_argument("--verify", help="验证结果 JSON 路径")
    p_report.add_argument("-o", "--out", help="输出 HTML 路径")
    p_report.set_defaults(func=cmd_report)

    return parser


# 取值可能以负号开头的选项。argparse 会把这些值当成选项名而报
# "expected one argument"，`--values -38,-40` 是最典型的踩法
# （单个 `-38` 能过，带逗号就不行了，因为不匹配它的负数字面量规则）。
_NEGATIVE_VALUE_OPTS = {
    "--values",
    "--start",
    "--min",
    "--fine-from",
    "--assume-zen",
    "--sim-limits",
}


def _normalize_argv(argv: list[str]) -> list[str]:
    """把 `--values -38,-40` 改写成 `--values=-38,-40`，让 argparse 能正确接收。"""
    import re

    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _NEGATIVE_VALUE_OPTS and i + 1 < len(argv):
            nxt = argv[i + 1]
            if re.match(r"^-\d", nxt):
                out.append(f"{token}={nxt}")
                i += 2
                continue
        out.append(token)
        i += 1
    return out


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except Exception:
            pass
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(_normalize_argv(raw))
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"[错误] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
