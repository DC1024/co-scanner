"""报告生成：单文件 HTML，无任何外部资源。

内容分四块：
  1. 结论（一眼看到该填什么）
  2. 逐核数据 + 扫描轨迹热力图（证明结论从哪来）
  3. 全核叠加验证（证明结论在真实场景成立）
  4. BIOS 填写卡（照着抄，不用回头翻聊天记录）
"""
from __future__ import annotations

import html
from datetime import datetime

from . import __version__
from .scan import Checkpoint
from .verify import VerifyResult

_CSS = """
:root{
  --bg:#070b14; --panel:#0d1526; --panel2:#111c31; --line:#1e2c47;
  --text:#e6edf7; --muted:#8ba0bd; --cyan:#38e0ff; --cyan-dim:#0e7490;
  --ok:#3ddc97; --fail:#ff6b6b; --warn:#ffc857; --shadow:0 12px 32px rgba(0,0,0,.45);
}
*{box-sizing:border-box}
body{
  margin:0; background:radial-gradient(1200px 700px at 18% -8%, #12233d 0%, transparent 55%),
  radial-gradient(900px 600px at 88% 4%, #10283a 0%, transparent 50%), var(--bg);
  color:var(--text); font-family:"Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif;
  line-height:1.6; font-size:15px;
}
.wrap{max-width:1080px; margin:0 auto; padding:40px 28px 72px}
.mono{font-family:"JetBrains Mono","Cascadia Code",Consolas,"Courier New",monospace; font-variant-numeric:tabular-nums}

header{position:relative; padding-bottom:26px; border-bottom:1px solid var(--line); margin-bottom:30px}
.brand{display:flex; align-items:center; gap:10px; font-size:12px; letter-spacing:.18em;
  text-transform:uppercase; color:var(--cyan); font-weight:600}
.brand::before{content:""; width:26px; height:2px; background:var(--cyan); box-shadow:0 0 10px var(--cyan)}
h1{font-size:30px; margin:12px 0 6px; letter-spacing:-.3px; font-weight:650}
.meta{color:var(--muted); font-size:13.5px}
.badge{display:block; width:fit-content; margin-top:16px; padding:7px 16px; border-radius:999px;
  font-size:13px; font-weight:600; border:1px solid}
.badge.ok{color:var(--ok); border-color:rgba(61,220,151,.45); background:rgba(61,220,151,.09)}
.badge.warn{color:var(--warn); border-color:rgba(255,200,87,.45); background:rgba(255,200,87,.09)}
.badge.bad{color:var(--fail); border-color:rgba(255,107,107,.45); background:rgba(255,107,107,.09)}
.recline{margin-top:14px; padding:14px 20px; border:1px dashed rgba(56,224,255,.35);
  border-radius:10px; background:rgba(56,224,255,.03); font-size:22px; font-weight:600;
  letter-spacing:.06em; color:var(--cyan); display:block; width:fit-content}
.recline span{display:block; font-size:11px; letter-spacing:.16em; color:var(--muted);
  font-weight:500; margin-bottom:2px; font-family:"Segoe UI",sans-serif}

.stats{display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:14px; margin-bottom:34px}
.stat{background:linear-gradient(160deg,var(--panel2),var(--panel)); border:1px solid var(--line);
  border-radius:14px; padding:18px 20px; box-shadow:var(--shadow)}
.stat .k{font-size:12px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted)}
.stat .v{font-size:30px; font-weight:650; margin-top:6px; color:var(--cyan)}
.stat .s{font-size:12.5px; color:var(--muted); margin-top:4px}

h2{font-size:15px; letter-spacing:.14em; text-transform:uppercase; color:var(--cyan);
  margin:44px 0 16px; font-weight:600; display:flex; align-items:center; gap:12px}
h2::after{content:""; flex:1; height:1px; background:linear-gradient(90deg,var(--line),transparent)}

table{width:100%; border-collapse:collapse; background:var(--panel); border:1px solid var(--line);
  border-radius:12px; overflow:hidden; font-size:14px}
th{text-align:left; padding:12px 14px; font-size:11.5px; letter-spacing:.1em; text-transform:uppercase;
  color:var(--muted); background:var(--panel2); border-bottom:1px solid var(--line); font-weight:600}
td{padding:11px 14px; border-bottom:1px solid rgba(30,44,71,.55)}
tr:last-child td{border-bottom:none}
td.num{font-family:"JetBrains Mono",Consolas,monospace; font-variant-numeric:tabular-nums}
.pass{color:var(--ok)} .failc{color:var(--fail)} .dim{color:var(--muted)}
.tag{display:inline-block; padding:2px 9px; border-radius:6px; font-size:11.5px; font-weight:600}
.tag.ok{background:rgba(61,220,151,.12); color:var(--ok)}
.tag.bad{background:rgba(255,107,107,.12); color:var(--fail)}
.tag.floor{background:rgba(56,224,255,.12); color:var(--cyan)}
.tag.pending{background:rgba(139,160,189,.12); color:var(--muted)}

.heat{background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:20px}
.heat-row{display:flex; align-items:center; gap:10px; margin-bottom:9px}
.heat-label{width:74px; flex:none; font-size:12.5px; color:var(--muted)}
.heat-cells{display:flex; gap:3px; flex-wrap:nowrap; overflow-x:auto; padding:2px 0}
.cell{width:15px; height:22px; border-radius:3px; flex:none; position:relative}
.cell.p{background:linear-gradient(180deg,#3ddc97,#1f9e6d); box-shadow:0 0 8px rgba(61,220,151,.25)}
.cell.f{background:linear-gradient(180deg,#ff6b6b,#c0392b); box-shadow:0 0 8px rgba(255,107,107,.3)}
.cell.n{background:#16233a}
.cell.rec{outline:2px solid var(--cyan); outline-offset:1px}
.axis{display:flex; gap:3px; margin-left:84px; margin-top:10px}
.axis span{width:15px; flex:none; font-size:10px; color:#7d92b0; text-align:center; overflow:visible}
.caption{font-size:12.5px; color:var(--muted); margin-bottom:16px}
.legend{display:flex; gap:18px; margin-top:16px; font-size:12.5px; color:var(--muted); flex-wrap:wrap}
.legend i{display:inline-block; width:11px; height:11px; border-radius:3px; margin-right:6px; vertical-align:-1px}

.verify{background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:22px}
.kv{display:grid; grid-template-columns:repeat(4,1fr); gap:16px 24px}
.kv.wide{grid-template-columns:repeat(3,1fr)}
.kv div span{display:block; font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted)}
.kv div strong{font-size:17px; font-weight:600}
.note{margin-top:18px; padding:14px 16px; border-left:2px solid var(--cyan-dim);
  background:rgba(56,224,255,.04); border-radius:0 8px 8px 0; font-size:13.5px; color:#b9cbe2}
.note ul{margin:8px 0 0; padding-left:20px}
.note li{margin-bottom:5px}
.note strong{color:var(--text)}

.bios{background:linear-gradient(165deg,#0f1c30,#0a1120); border:1px solid var(--cyan-dim);
  border-radius:14px; padding:26px; box-shadow:var(--shadow)}
.bios table{background:transparent; border:none}
.bios th{background:transparent; border-bottom:1px solid var(--cyan-dim)}
.bios td{border-bottom:1px solid rgba(14,116,144,.28)}
.bios .big{font-size:26px; font-weight:700; color:var(--cyan)}
.bios ol{margin:16px 0 0; padding-left:20px; color:#b9cbe2; font-size:13.5px}
.bios li{margin-bottom:6px}
code{background:#0a1424; border:1px solid var(--line); padding:1px 6px; border-radius:5px;
  font-family:"JetBrains Mono",Consolas,monospace; font-size:12.5px; color:#9fe8ff}

footer{margin-top:52px; padding-top:22px; border-top:1px solid var(--line); color:#6d8099; font-size:12.5px}
footer strong{color:#94a9c4}

@media print{
  body{background:#fff; color:#111}
  .wrap{max-width:none; padding:0}
  .stat,.heat,.verify,.bios,table{box-shadow:none; background:#fff; border-color:#ccc}
  h1,h2,.brand,.stat .v,.bios .big{color:#111}
  .cell.n{background:#eee}
  .meta,.stat .k,.stat .s,td,.kv div span{color:#333}
  th{background:#f2f2f2; color:#333}
  .badge{border-color:#999; color:#111; background:#fff}
}
"""


def _dwell_text(cfg: dict) -> str:
    coarse = cfg.get("dwell")
    fine = cfg.get("dwell_fine")
    cut = cfg.get("fine_from")
    if coarse is None:
        return "—"
    if fine and cut is not None:
        return f"{coarse:.0f} s（{cut:+d} 起 {fine:.0f} s）"
    return f"{coarse:.0f} s"


def _window(verify: VerifyResult) -> str:
    """把起止时间压成 12:10 → 12:30 这种紧凑形式。"""
    start = (verify.started or "").replace("T", " ")
    end = (verify.ended or "").replace("T", " ")
    if not start:
        return "—"
    return f"{start[11:16]} → {end[11:16]}" if len(end) >= 16 else start


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _fmt(value) -> str:
    if value is None:
        return "—"
    return f"{value:+d}" if isinstance(value, int) else str(value)


def _ccd_suffix(plat) -> str:
    ccd = getattr(plat, "ccd_count", 0)
    return f" / {ccd} CCD" if ccd else ""


def _verdict(cp: Checkpoint, verify: VerifyResult | None) -> tuple[str, str]:
    if verify is None:
        return "warn", "仅逐核扫描 · 未做全核叠加验证"
    if verify.ok:
        return "ok", "已通过全核叠加验证"
    return "bad", f"全核验证未通过 · {verify.reason}"


def _stats(cp: Checkpoint, verify: VerifyResult | None) -> str:
    recs = [c.recommend for c in cp.cores.values() if c.recommend is not None]
    deepest = [c.deepest_stable for c in cp.cores.values() if c.deepest_stable is not None]
    steps = sum(c.steps for c in cp.cores.values())
    cores = len(cp.cores)

    if recs:
        lo, hi = min(recs), max(recs)
        span = f"{lo:+d}" if lo == hi else f"{hi:+d} ~ {lo:+d}"
    else:
        span = "—"

    cards = [
        ("建议值区间", span, "逐核不同，按下方 BIOS 卡逐个填"),
        ("最深稳定", f"{min(deepest):+d}" if deepest else "—", "所有核里压得最深且通过的档位"),
        ("压测档数", str(steps), f"{cores} 核 × 平均 {steps / cores:.1f} 档" if cores else ""),
    ]
    if verify is not None:
        cards.append(
            ("全核满载时长", f"{verify.metrics.get('actual_seconds', 0) / 60:.0f} min",
             f"{cores} 核同时满载，验证叠加稳定性")
        )
    out = ['<section class="stats">']
    for key, value, sub in cards:
        out.append(
            f'<div class="stat"><div class="k">{_esc(key)}</div>'
            f'<div class="v mono">{_esc(value)}</div>'
            f'<div class="s">{_esc(sub)}</div></div>'
        )
    out.append("</section>")
    return "".join(out)


def _platform_block(plat) -> str:
    """平台相关的操作提示。没有提示时不输出任何东西。"""
    notes = getattr(plat, "notes", None) or []
    if not notes:
        return ""
    items = "".join(f"<li>{_esc(n)}</li>" for n in notes)
    flags = []
    if getattr(plat, "x3d", False):
        flags.append("X3D")
    if getattr(plat, "is_apu", False):
        flags.append("APU")
    if getattr(plat, "hedt", False):
        flags.append("HEDT")
    tag = f"（{' / '.join(flags)}）" if flags else ""
    return (
        f'<div class="verify"><div class="note" style="margin-top:0">'
        f"<strong>平台提示{tag}</strong><ul>{items}</ul></div></div>"
    )


def _core_table(cp: Checkpoint) -> str:
    rows = [
        "<table><thead><tr>",
        "<th>核</th><th>物理核</th><th>测试档数</th><th>最深稳定</th>",
        "<th>失败点</th><th>建议值</th><th>状态</th>",
        "</tr></thead><tbody>",
    ]
    status_text = {
        "done": ("ok", "已锁定"),
        "floor": ("floor", "触底"),
        "scanning": ("pending", "未完成"),
        "pending": ("pending", "未扫描"),
    }
    for key in sorted(cp.cores, key=int):
        c = cp.cores[key]
        cls, text = status_text.get(c.status, ("pending", c.status))
        rows.append(
            "<tr>"
            f'<td class="num">Core {c.core}</td>'
            f'<td class="num dim">{c.physical if c.physical is not None else "—"}</td>'
            f'<td class="num dim">{c.steps}</td>'
            f'<td class="num pass">{_fmt(c.deepest_stable)}</td>'
            f'<td class="num failc">{_fmt(c.failed_at)}</td>'
            f'<td class="num"><strong>{_fmt(c.recommend)}</strong></td>'
            f'<td><span class="tag {cls}">{text}</span></td>'
            "</tr>"
        )
    rows.append("</tbody></table>")
    return "".join(rows)


def _heatmap(cp: Checkpoint) -> str:
    all_values: list[int] = []
    for c in cp.cores.values():
        all_values.extend(int(v) for v in c.tested)
    if not all_values:
        return '<div class="heat dim">没有扫描数据</div>'

    low, high = min(all_values), max(all_values)
    columns = list(range(high, low - 1, -1))
    out = [
        '<div class="heat">',
        '<div class="caption">横轴为 CO 值，从左（浅）到右（深）；每一格代表一档压测。'
        "蓝框是该核的建议值。</div>",
    ]
    for key in sorted(cp.cores, key=int):
        c = cp.cores[key]
        cells = []
        for value in columns:
            state = c.tested.get(str(value))
            cls = "n" if state is None else ("p" if state else "f")
            if c.recommend is not None and value == c.recommend:
                cls += " rec"
            note = c.notes.get(str(value), "未测试")
            cells.append(f'<div class="cell {cls}" title="{_esc(f"{value:+d} · {note}")}"></div>')
        out.append(
            f'<div class="heat-row"><div class="heat-label mono">C{c.core} '
            f'<span class="dim">{_fmt(c.recommend)}</span></div>'
            f'<div class="heat-cells">{"".join(cells)}</div></div>'
        )

    axis = []
    for idx, value in enumerate(columns):
        label = str(value) if idx % 5 == 0 or idx == len(columns) - 1 else ""
        axis.append(f"<span>{label}</span>")
    out.append(f'<div class="axis">{"".join(axis)}</div>')
    out.append(
        '<div class="legend">'
        '<span><i style="background:#3ddc97"></i>该档通过</span>'
        '<span><i style="background:#ff6b6b"></i>该档失败</span>'
        '<span><i style="background:#16233a"></i>未测试</span>'
        '<span><i style="background:transparent;outline:2px solid #38e0ff"></i>建议值</span>'
        "</div></div>"
    )
    return "".join(out)


def _verify_block(verify: VerifyResult | None) -> str:
    if verify is None:
        return (
            '<div class="verify"><div class="note">'
            "本轮没有做全核叠加验证。逐核扫描时其余核的 CO 归零，功耗与温度环境都偏乐观，"
            "结论不能直接当作真实场景的稳定性依据。建议补跑 <code>co-scanner verify</code>。"
            "</div></div>"
        )

    metrics = verify.metrics or {}
    avg = metrics.get("avg_ratio")
    cores = metrics.get("logical_cores")
    if isinstance(avg, (int, float)):
        avg_text = f"{avg:.1f} / {cores}" if cores else f"{avg:.2f}"
    else:
        avg_text = "—"
    whea = metrics.get("whea_new", 0)
    status = "通过" if verify.ok else "失败"
    status_cls = "pass" if verify.ok else "failc"
    actual = metrics.get("actual_seconds", 0)

    extra = ""
    if verify.interrupted_before:
        extra = (
            '<div class="note" style="border-left-color:#ffc857;background:rgba(255,200,87,.05)">'
            "上次全核验证未正常结束（很可能发生冻结或掉电），本次已重新完整执行。</div>"
        )

    return f"""
<div class="verify">
  <div class="kv wide">
    <div><span>判定</span><strong class="{status_cls}">{status} · {_esc(verify.reason)}</strong></div>
    <div><span>满载时长</span><strong class="mono">{actual / 60:.1f} min</strong></div>
    <div><span>写入的 CO</span><strong class="mono">{_esc(",".join(str(v) for v in verify.values))}</strong></div>
    <div><span>CPU 占用 / 逻辑核</span><strong class="mono">{avg_text}</strong></div>
    <div><span>WHEA 新增</span><strong class="mono {'pass' if not whea else 'failc'}">{whea}</strong></div>
    <div><span>执行时段</span><strong class="mono">{_esc(_window(verify))}</strong></div>
  </div>
  <div class="note">{_esc(verify.detail)}</div>
  {extra}
</div>"""


def _bios_card(cp: Checkpoint, verify: VerifyResult | None) -> str:
    rows = ["<table><thead><tr><th>BIOS 中显示</th><th>Sign</th><th>Magnitude</th>"
            "<th>对应物理核</th></tr></thead><tbody>"]
    for key in sorted(cp.cores, key=int):
        c = cp.cores[key]
        value = c.recommend if c.recommend is not None else 0
        rows.append(
            "<tr>"
            f'<td class="mono">Core {c.core}</td>'
            '<td class="dim">Negative</td>'
            f'<td class="mono big">{abs(value)}</td>'
            f'<td class="mono dim">{c.physical if c.physical is not None else "—"}</td>'
            "</tr>"
        )
    rows.append("</tbody></table>")

    values = ",".join(
        str(c.recommend if c.recommend is not None else 0)
        for c in (cp.cores[k] for k in sorted(cp.cores, key=int))
    )
    caution = ""
    if verify is None:
        caution = (
            '<li><strong style="color:#ffc857">尚未做全核叠加验证</strong>，'
            "建议先跑 <code>co-scanner verify</code> 再写入。</li>"
        )
    return f"""
<div class="bios">
  <div class="mono dim" style="font-size:12px;letter-spacing:.14em;margin-bottom:14px">
    ADVANCED → AMD OVERCLOCKING → PRECISION BOOST OVERDRIVE → CURVE OPTIMIZER → PER CORE
  </div>
  {"".join(rows)}
  <ol>
    <li>PBO 必须设为 <code>Advanced</code>，选 <code>Enabled</code> 看不到 Curve Optimizer。</li>
    <li><code>PBO Limits</code> 保持 <code>Auto</code>。本轮所有验证都在默认 88 W PPT 下完成，
        改成 Motherboard 会让结论全部失效。</li>
    <li><code>Max CPU Boost Clock Override</code> 保持 <code>0 / Disabled</code>。</li>
    <li>保存后重启，用 <code>co-scanner detect</code> 回读，确认是 {_esc(values)}。</li>
    {caution}
  </ol>
</div>"""


def _appendix(cp: Checkpoint, verify: VerifyResult | None, plat=None) -> str:
    cfg = cp.config or {}
    items = [
        ("处理器", cp.cpu or "—"),
        ("Zen 代数", f"Zen {cp.zen}" if cp.zen else "—"),
        ("物理核 / CCD", f"{cp.physical_cores or '—'} / {getattr(plat, 'ccd_count', '—')}"),
        ("CO 允许范围", f"{cp.co_min} ~ {cp.co_max}"),
        ("起始值", _fmt(cfg.get("start"))),
        ("步长 / 安全余量", f"{cfg.get('step', '—')} / {cfg.get('safety', '—')}"),
        ("每档压测", _dwell_text(cfg)),
        ("预热", f"{float(cfg.get('warmup') or 0):.0f} s"),
        ("扫描开始", (cp.started or "—").replace("T", " ")),
        ("扫描结束", (cp.updated or "—").replace("T", " ")),
        ("扫描前 CO", ",".join(str(v) for v in cp.origin) or "—"),
        ("工具版本", f"co-scanner {__version__}"),
    ]
    out = ['<div class="kv wide">']
    for key, value in items:
        out.append(f"<div><span>{_esc(key)}</span><strong class=\"mono\">{_esc(value)}</strong></div>")
    out.append("</div>")
    return "".join(out)


def build_report(cp: Checkpoint, verify: VerifyResult | None = None, plat=None) -> str:
    cls, text = _verdict(cp, verify)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    recs = [c.recommend for c in cp.cores.values() if c.recommend is not None]
    avg_rec = f"{sum(recs) / len(recs):+.1f}" if recs else "—"
    ordered = [cp.cores[k] for k in sorted(cp.cores, key=int)]
    rec_line = " · ".join(str(c.recommend) if c.recommend is not None else "?" for c in ordered)
    # 16 核时这行会接近 70 个字符，字号不收缩就会挤成两行且观感很重
    rec_size = "22px" if len(ordered) <= 8 else ("18px" if len(ordered) <= 12 else "15px")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CO 扫描报告 · {_esc(cp.cpu or "AMD Ryzen")}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">

<header>
  <div class="brand">CO Scanner · 逐核 Curve Optimizer 扫描报告</div>
  <h1>{_esc(cp.cpu or "AMD Ryzen")}</h1>
  <div class="meta">{_esc(getattr(plat, "zen_label", f"Zen {cp.zen}"))} ·
    {len(cp.cores)} 核{_ccd_suffix(plat)} · 平均建议 {_esc(avg_rec)} ·
    生成于 {_esc(generated)}</div>
  <div class="badge {cls}">{_esc(text)}</div>
  <div class="recline mono" style="font-size:{rec_size}"><span>BIOS PER CORE 建议值</span>{_esc(rec_line)}</div>
</header>

{_stats(cp, verify)}

{_platform_block(plat)}

<h2>逐核结果</h2>
{_core_table(cp)}

<h2>扫描轨迹</h2>
{_heatmap(cp)}

<h2>全核叠加验证</h2>
{_verify_block(verify)}

<h2>BIOS 填写卡</h2>
{_bios_card(cp, verify)}

<h2>运行环境</h2>
{_appendix(cp, verify, plat)}

<footer>
  <p><strong>判据说明</strong>：每一档同时看三件事 —— 压测进程树的 CPU 时间占比是否跌破阈值、
  压测程序是否以非零码退出、期间是否新增 WHEA（事件 18/19）。三者任一命中即判该档失败，
  并回退 {_esc((cp.config or {}).get("safety", 2))} 档作为建议值。</p>
  <p><strong>风险提示</strong>：Curve Optimizer 负压过深时，Zen 5 通常表现为整机冻结而非蓝屏，
  自动恢复机制接不上，需要长按电源键强制关机。重启后 CO 会回到 BIOS 中保存的值，硬件本身没有风险。
  本报告只反映测试期间的稳定性，不构成任何形式的担保。</p>
</footer>

</div>
</body>
</html>"""


def write_report(
    path: str, cp: Checkpoint, verify: VerifyResult | None = None, plat=None
) -> str:
    content = build_report(cp, verify, plat=plat)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return path
