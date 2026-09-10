from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .core import process_token


def _ancestor_named(
    pid: int, processes: dict[int, tuple[int, str]], executable: str
) -> int | None:
    seen = set()
    while pid and pid not in seen:
        seen.add(pid)
        parent, name = processes.get(pid, (0, ""))
        if name.casefold() == executable.casefold():
            return pid
        pid = parent
    return None


def _windows_codex_ancestor(pid: int, executable: str) -> int | None:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return None
    processes = {}
    entry = ProcessEntry(dwSize=ctypes.sizeof(ProcessEntry))
    try:
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            processes[entry.th32ProcessID] = (
                entry.th32ParentProcessID,
                entry.szExeFile,
            )
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return _ancestor_named(pid, processes, executable)


def hook_target(
    payload: dict,
    *,
    codex_bin: Path | None = None,
    codex_home: Path | None = None,
    sqlite_home: Path | None = None,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("hook input must be a JSON object")
    thread_id = payload.get("session_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError("hook input is missing session_id")
    pid = os.getppid()
    if sys.platform == "win32":
        ancestor = _windows_codex_ancestor(
            pid, codex_bin.name if codex_bin is not None else "codex.exe"
        )
        if ancestor is None:
            raise ValueError("could not identify the Codex process")
        pid = ancestor
    token = process_token(pid)
    if token is None:
        raise ValueError("Codex process is not active")
    if codex_bin is None and sys.platform.startswith("linux"):
        codex_bin = Path(f"/proc/{pid}/exe")
    elif codex_bin is None:
        executable = shutil.which("codex")
        if executable is None:
            raise ValueError("could not locate the Codex executable")
        codex_bin = Path(executable).absolute()
    codex_home = codex_home or Path(
        os.environ.get("CODEX_HOME", Path.home() / ".codex")
    )
    sqlite_home = sqlite_home or Path(os.environ.get("CODEX_SQLITE_HOME", codex_home))
    codex_home = codex_home.expanduser()
    sqlite_home = sqlite_home.expanduser()
    surface = payload.get("thread_source") or os.environ.get("CODEX_SURFACE") or "codex"
    return {
        "thread_id": thread_id,
        "surface": str(surface),
        "codex_bin": codex_bin,
        "codex_home": codex_home.absolute(),
        "sqlite_home": sqlite_home.absolute(),
        "pid": pid,
        "token": token,
    }
