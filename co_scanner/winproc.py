"""Windows 底层：CPU 拓扑亲和性、进程树 CPU 时间。纯 ctypes，无第三方依赖。

为什么需要进程树而不是单个 PID：
  y-cruncher.exe 是个启动器，真正的计算跑在 Binaries\\24-ZN5 ~ Komari.exe 里。
  只盯启动器会把「子进程已经崩了、启动器还在等」误判成「活着」。
"""
from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SET_INFORMATION = 0x0200
TH32CS_SNAPPROCESS = 0x00000002
RELATION_PROCESSOR_CORE = 0
RELATION_CACHE = 2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# PROCESSOR_CACHE_TYPE 的枚举值是 CacheUnified=0 起头的。
# 这里踩过一次坑：按 CacheUnified=2 去找 L3，结果一个实例都数不到，
# 反而把 L1 数据缓存（Type=CacheData=2）当成了候选。
CACHE_UNIFIED = 0


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL


def process_cpu_seconds(pid: int) -> float | None:
    """进程累计 CPU 时间（秒）。进程不存在或无权访问返回 None。"""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (kernel.dwHighDateTime << 32 | kernel.dwLowDateTime) + (
            user.dwHighDateTime << 32 | user.dwLowDateTime
        )
        return ticks / 1e7
    finally:
        kernel32.CloseHandle(handle)


def enumerate_processes() -> list[tuple[int, int, str]]:
    """返回 [(pid, ppid, exe_name), ...]。"""
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return []
    rows: list[tuple[int, int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            rows.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return rows


def process_tree(pid: int) -> list[int]:
    """pid 及其全部后代进程。"""
    rows = enumerate_processes()
    children: dict[int, list[int]] = {}
    for child, parent, _name in rows:
        children.setdefault(parent, []).append(child)

    out = [pid]
    stack = [pid]
    seen = {pid}
    while stack:
        cur = stack.pop()
        for child in children.get(cur, []):
            if child in seen:
                continue
            seen.add(child)
            out.append(child)
            stack.append(child)
    return out


def tree_cpu_seconds(pid: int) -> tuple[float, list[int]]:
    """进程树的累计 CPU 时间合计，以及树内的 pid 列表。"""
    pids = process_tree(pid)
    total = 0.0
    alive: list[int] = []
    for child in pids:
        value = process_cpu_seconds(child)
        if value is None:
            continue
        total += value
        alive.append(child)
    return total, alive


def set_affinity(pid: int, mask: int, retries: int = 20, delay: float = 0.02) -> bool:
    """把进程绑到指定亲和性掩码。进程刚创建时可能还没法打开，做有限重试。"""
    import time

    for _ in range(retries):
        handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION, False, pid)
        if handle:
            try:
                if kernel32.SetProcessAffinityMask(handle, ctypes.c_size_t(mask)):
                    return True
            finally:
                kernel32.CloseHandle(handle)
        time.sleep(delay)
    return False


def _walk_relations(relationship: int):
    """遍历 GetLogicalProcessorInformationEx 返回的记录，产出 (offset, size, raw)。

    条目头部固定 8 字节：DWORD Relationship + DWORD Size，之后是联合体。
    """
    fn = kernel32.GetLogicalProcessorInformationEx
    fn.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    fn.restype = wintypes.BOOL

    length = wintypes.DWORD(0)
    fn(relationship, None, ctypes.byref(length))
    if length.value == 0:
        return
    buf = ctypes.create_string_buffer(length.value)
    if not fn(relationship, buf, ctypes.byref(length)):
        return

    raw = buf.raw
    offset = 0
    while offset + 8 <= length.value:
        rel, size = struct.unpack_from("<II", raw, offset)
        if size == 0 or offset + size > length.value:
            break
        yield offset, size, raw
        offset += size


def core_affinity_masks() -> list[int]:
    """每个物理核的亲和性掩码（含其 SMT 兄弟线程），按核序号排列。

    SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX 布局：
        0: Relationship (DWORD)   4: Size (DWORD)
        8: union { PROCESSOR_RELATIONSHIP ... }
    PROCESSOR_RELATIONSHIP 相对联合体起点：
        0: Flags (BYTE)  1: EfficiencyClass (BYTE)  2: Reserved[20]
        22: GroupCount (WORD)  24: GROUP_AFFINITY GroupMask[]（Mask 占 8 字节）
    合起来，第 0 组的掩码在条目起点 + 32。实测 9600X 得到
    0x3 / 0xC / 0x30 / 0xC0 / 0x300 / 0xC00，正好是六个 SMT 对。
    """
    masks: list[int] = []
    for offset, size, raw in _walk_relations(RELATION_PROCESSOR_CORE):
        if offset + 40 <= len(raw):
            mask, _group = struct.unpack_from("<QH", raw, offset + 32)
            masks.append(mask)
    return masks


def physical_core_count() -> int:
    """物理核数量。只数条目，不解析内容。"""
    return sum(1 for _ in _walk_relations(RELATION_PROCESSOR_CORE))


def l3_group_count() -> int:
    """L3 缓存实例数，用来推断 CCD 数量。

    CACHE_RELATIONSHIP 相对联合体起点：
        0: Level (BYTE)  1: Associativity  2: LineSize (WORD)
        4: CacheSize (DWORD)  8: Type (DWORD, 0=Unified)
        12: Reserved[18]  30: GroupCount  32: GroupMask
    Zen 3 及之后每个 CCD 有且只有一个统一 L3，所以实例数 = CCD 数。
    Zen 2 及更早是每个 CCX 一份 L3（一个 CCD 内含两个 CCX），需要除以 2。
    """
    count = 0
    for offset, _size, raw in _walk_relations(RELATION_CACHE):
        base = offset + 8
        if base + 36 > len(raw):
            continue
        level = raw[base]
        cache_type = struct.unpack_from("<I", raw, base + 8)[0]
        if level == 3 and cache_type == CACHE_UNIFIED:
            count += 1
    return count


def mask_for_core(core: int, masks: list[int]) -> int:
    """取第 core 个物理核的掩码；越界时回绕，避免多 CCD 编号差异导致 IndexError。"""
    if not masks:
        return 0
    return masks[core % len(masks)]
