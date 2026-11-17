"""Protected local credentials and a cross-process refresh lock. Never logs secrets."""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile
import threading


def assert_private(path: Path) -> None:
    if os.name != "nt":
        if path.stat().st_mode & 0o077:
            raise PermissionError("Credential path must have owner-only permissions")
        return
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    def sid_text(pointer):
        result = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(pointer, ctypes.byref(result)):
            raise PermissionError("Could not inspect credential ACL identity")
        try:
            return result.value
        finally:
            kernel.LocalFree(ctypes.cast(result, ctypes.c_void_p))

    descriptor, owner, dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    status = advapi.GetNamedSecurityInfoW(str(path), 1, 0x1 | 0x4,
        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if status:
        raise PermissionError("Could not inspect credential file permissions")
    try:
        if not dacl.value:
            raise PermissionError("Unrestricted credential ACL rejected")
        allowed = {sid_text(owner), "S-1-5-18", "S-1-5-32-544"}

        class AclSize(ctypes.Structure):
            _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]

        info = AclSize()
        if not advapi.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            raise PermissionError("Could not inspect credential ACL entries")
        for index in range(info.count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                raise PermissionError("Could not inspect credential ACL entry")
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 0 and sid_text(ctypes.c_void_p(ace.value + 8)) not in allowed:
                raise PermissionError("Credential ACL grants access to another principal")
            if kind in (5, 9, 11):
                raise PermissionError("Unsupported allow ACE on credential path")
    finally:
        kernel.LocalFree(descriptor)


class TokenStore:
    def __init__(self, path):
        self.path = Path(path)
        self.thread_lock = threading.RLock()

    def read(self):
        if not self.path.exists():
            return {}
        assert_private(self.path.parent)
        assert_private(self.path)
        return json.loads(self.path.read_text(encoding="utf-8"))

    @contextmanager
    def locked(self):
        with self.thread_lock:
            lock_path = self.path.parent / "local-test.lock"
            with lock_path.open("a+b") as handle:
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle, fcntl.LOCK_UN)

    def write(self, record):
        assert_private(self.path.parent)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                              prefix="siwc-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                json.dump(record, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
