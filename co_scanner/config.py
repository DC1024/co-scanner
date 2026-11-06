"""配置文件：把路径和默认参数从代码里挪出来。

优先级：命令行参数 > 配置文件 > 环境变量 > 自动查找。

为什么配置文件要严格校验键名：
JSON 没有注释也不报拼写错误，`"ycruncher_path": "..."` 这种写错了不会
有任何提示，用户会以为配好了、结果工具还在用自动查找的旧路径。
所以未知键一律报出来，而不是静默忽略。

搜索顺序（先命中先用）：
  1. --config 指定的路径
  2. 当前目录 co-scanner.json
  3. %APPDATA%\\co-scanner\\config.json
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict

CONFIG_FILENAME = "co-scanner.json"

# 所有合法键。新增配置项时必须同步这个集合，否则会被判为未知键。
KNOWN_KEYS = {
    "smu_cli",
    "ycruncher",
    "algorithm",
    "memory",
    "warmup_memory",
    "start",
    "step",
    "safety",
    "dwell",
    "dwell_fine",
    "fine_from",
    "warmup",
    "assume_zen",
    "cores",
}

# 每个键期望的类型，用于给出「应该是数字还是字符串」的明确报错
_TYPES: dict[str, type | tuple[type, ...]] = {
    "smu_cli": str,
    "ycruncher": str,
    "algorithm": str,
    "memory": str,
    "warmup_memory": str,
    "start": int,
    "step": int,
    "safety": int,
    "dwell": (int, float),
    "dwell_fine": (int, float),
    "fine_from": int,
    "warmup": (int, float),
    "assume_zen": int,
    "cores": (list, str),
}


class ConfigError(ValueError):
    """配置文件有问题。"""


def appdata_config_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "co-scanner", "config.json")


def default_search_paths() -> list[str]:
    return [os.path.join(os.getcwd(), CONFIG_FILENAME), appdata_config_path()]


def find_config() -> str | None:
    for path in default_search_paths():
        if os.path.isfile(path):
            return path
    return None


def load_file(path: str) -> dict:
    """读取并校验配置文件。返回原始字典（已做类型检查）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} 不是合法的 JSON：第 {exc.lineno} 行 {exc.msg}") from exc
    except OSError as exc:
        raise ConfigError(f"读不了配置文件 {path}：{exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"{path} 的顶层必须是对象（{{ ... }}），实际是 {type(data).__name__}")

    # 以下划线开头的键当作注释用，不参与校验（JSON 不支持注释的折中办法）
    unknown = sorted(k for k in set(data) - KNOWN_KEYS if not k.startswith("_"))
    if unknown:
        raise ConfigError(
            f"{path} 里有无法识别的配置项：{', '.join(unknown)}。"
            f"可用项：{', '.join(sorted(KNOWN_KEYS))}"
        )

    for key, value in data.items():
        if key.startswith("_"):
            continue
        expect = _TYPES.get(key)
        if expect is None or value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, expect):
            names = (
                expect.__name__
                if isinstance(expect, type)
                else "/".join(t.__name__ for t in expect)
            )
            raise ConfigError(
                f"{path} 的 {key} 应该是 {names}，实际是 {type(value).__name__}（{value!r}）"
            )
    return data


def parse_cores(value) -> list[int] | None:
    """把 "0,1,2" 或 [0, 1, 2] 统一成 list[int]。空值返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
        if not parts:
            return None
        try:
            return [int(p) for p in parts]
        except ValueError as exc:
            raise ConfigError(f"cores 里的 {value!r} 不是合法的核号列表") from exc
    if isinstance(value, list):
        try:
            return [int(v) for v in value]
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"cores 里必须全是整数，实际是 {value!r}") from exc
    raise ConfigError(f"cores 不支持的类型：{type(value).__name__}")


@dataclass
class Settings:
    """命令行的最终生效值（已合并配置文件）。"""

    smu_cli: str | None = None
    ycruncher: str | None = None
    algorithm: str = "SFTv4"
    memory: str = "15M"
    warmup_memory: str = "2G"
    start: int = -20
    step: int = 1
    safety: int = 2
    dwell: float = 30.0
    dwell_fine: float = 120.0
    fine_from: int | None = -35
    warmup: float = 180.0
    assume_zen: int | None = None
    cores: list[int] | None = None
    source: str = field(default="", compare=False)

    def as_dict(self) -> dict:
        return asdict(self)


def resolve(args, path: str | None = None) -> Settings:
    """把命令行参数合并到配置文件默认值上。

    args 里凡是没有出现的属性都视为「用户没指定」，回落到配置文件。
    这样 argparse 的默认值就不会悄悄盖掉配置文件。
    """
    explicit = getattr(args, "config", None) or path
    cfg_path = explicit if explicit else find_config()
    data: dict = {}
    if cfg_path:
        if not os.path.isfile(cfg_path):
            raise ConfigError(f"指定的配置文件不存在：{cfg_path}")
        data = load_file(cfg_path)

    def pick(name: str, default):
        cli_value = getattr(args, name, None)
        if cli_value is not None:
            return cli_value
        if name in data and data[name] is not None:
            return data[name]
        return default

    settings = Settings(
        smu_cli=pick("smu_cli", None),
        ycruncher=pick("ycruncher", None),
        algorithm=pick("algorithm", "SFTv4"),
        memory=pick("memory", "15M"),
        warmup_memory=pick("warmup_memory", "2G"),
        start=pick("start", -20),
        step=pick("step", 1),
        safety=pick("safety", 2),
        dwell=float(pick("dwell", 30.0)),
        dwell_fine=float(pick("dwell_fine", 120.0)),
        fine_from=pick("fine_from", -35),
        warmup=float(pick("warmup", 180.0)),
        assume_zen=pick("assume_zen", None),
        cores=parse_cores(pick("cores", None)),
        source=cfg_path or "",
    )

    if getattr(args, "no_fine", False):
        settings.fine_from = None
    if settings.step <= 0:
        raise ConfigError("step 必须大于 0")
    if settings.safety < 0:
        raise ConfigError("safety 不能为负数")
    if settings.dwell <= 0 or settings.dwell_fine <= 0:
        raise ConfigError("dwell / dwell_fine 必须是正数")
    return settings


SAMPLE = {
    "_说明": "把本文件复制为 co-scanner.json 放在工作目录，或放到 %APPDATA%\\co-scanner\\config.json",
    "smu_cli": "C:\\CoreCycler-v0.11.0.3\\tools\\ryzen-smu-cli\\ryzen-smu-cli.exe",
    "ycruncher": "C:\\CoreCycler-v0.11.0.3\\test_programs\\y-cruncher\\y-cruncher.exe",
    "algorithm": "SFTv4",
    "memory": "15M",
    "warmup_memory": "2G",
    "start": -20,
    "step": 1,
    "safety": 2,
    "dwell": 30,
    "dwell_fine": 120,
    "fine_from": -35,
    "warmup": 180,
}


def write_sample(path: str, force: bool = False) -> str:
    if os.path.exists(path) and not force:
        raise ConfigError(f"{path} 已存在，加 --force 才会覆盖")
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(SAMPLE, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path
