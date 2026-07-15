"""Small Windows filesystem-security helpers shared by local state owners."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
import re


@lru_cache(maxsize=1)
def _current_user_sid() -> str:
    import csv
    import subprocess

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=creation_flags,
    )
    try:
        sid = next(csv.reader([identity.stdout.strip()]))[-1].strip()
    except (IndexError, StopIteration) as exc:
        raise OSError("Could not determine the current Windows user SID") from exc
    if identity.returncode != 0 or not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise OSError("Could not determine the current Windows user SID")
    return sid


def secure_windows_path(path: Path, *, directory: bool = False) -> None:
    """Replace a Windows path DACL with current-user-only full control."""

    if os.name != "nt":
        return

    import ctypes
    from ctypes import wintypes

    sid = _current_user_sid()
    # Directories propagate the protected current-user ACE to children. Files
    # need no inheritance flags.
    ace_flags = "OICI" if directory else ""
    sddl = f"D:P(A;{ace_flags};FA;;;{sid})"

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    )
    get_dacl.restype = wintypes.BOOL
    set_named_security = advapi32.SetNamedSecurityInfoW
    set_named_security.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    set_named_security.restype = wintypes.DWORD
    local_free = kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    security_descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    if not convert(
        sddl,
        1,  # SDDL_REVISION_1
        ctypes.byref(security_descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if not get_dacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present:
            raise ctypes.WinError(ctypes.get_last_error())
        error = set_named_security(
            str(path),
            1,  # SE_FILE_OBJECT
            0x00000004 | 0x80000000,  # DACL + protected DACL
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        local_free(security_descriptor)
