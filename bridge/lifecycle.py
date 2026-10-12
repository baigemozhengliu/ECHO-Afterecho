"""Windows host lifetime tracking and containment of all service descendants."""
import ctypes
from ctypes import wintypes as w
import os
import threading

k = ctypes.WinDLL('kernel32', use_last_error=True)
for name, restype, args in [
    ('OpenProcess', w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
    ('CloseHandle', w.BOOL, [w.HANDLE]),
    ('WaitForSingleObject', w.DWORD, [w.HANDLE, w.DWORD]),
    ('CreateToolhelp32Snapshot', w.HANDLE, [w.DWORD, w.DWORD]),
    ('CreateJobObjectW', w.HANDLE, [w.LPVOID, w.LPCWSTR]),
    ('SetInformationJobObject', w.BOOL, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]),
    ('AssignProcessToJobObject', w.BOOL, [w.HANDLE, w.HANDLE]),
    ('GetCurrentProcess', w.HANDLE, []),
]:
    fn = getattr(k, name); fn.restype = restype; fn.argtypes = args

class ProcessEntry(ctypes.Structure):
    _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('pid', w.DWORD),
                ('heap', ctypes.c_size_t), ('module', w.DWORD), ('threads', w.DWORD),
                ('parent', w.DWORD), ('priority', w.LONG), ('flags', w.DWORD),
                ('exe', w.WCHAR * 260)]

for name in ('Process32FirstW', 'Process32NextW'):
    getattr(k, name).argtypes = [w.HANDLE, ctypes.POINTER(ProcessEntry)]
    getattr(k, name).restype = w.BOOL

class Limits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD),
                ('scheduling', w.DWORD)]

class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', Limits), ('io', ctypes.c_ulonglong * 6),
                ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

def echo_main_pid(start_pid):
    """Walk from the trusted runtime to the outermost ECHO process, not Steam."""
    snapshot = k.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entries = {}
    try:
        entry = ProcessEntry(); entry.size = ctypes.sizeof(entry)
        more = k.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            entries[entry.pid] = (entry.parent, entry.exe.casefold())
            more = k.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    selected = None; seen = set(); pid = start_pid
    while pid in entries and pid not in seen:
        seen.add(pid)
        parent, exe = entries[pid]
        if exe == 'echo.exe': selected = pid
        pid = parent
    if selected is None:
        raise RuntimeError('ECHO 已退出，请重新启动')
    return selected

class HostProcess:
    def __init__(self, pid):
        # Keep the original process handle open: PID reuse cannot extend its life.
        self.handle = k.OpenProcess(0x100000, False, pid)
        if not self.handle:
            raise RuntimeError('ECHO 已退出，请重新启动')

    def alive(self):
        result = k.WaitForSingleObject(self.handle, 0)
        if result not in (0, 258): raise ctypes.WinError(ctypes.get_last_error())
        return result == 258

    def close(self):
        if self.handle: k.CloseHandle(self.handle); self.handle = None

class ServiceLifetime:
    def __init__(self, pid, grace_seconds=10):
        self.host = HostProcess(pid)
        self.stopping = threading.Event()
        self.job = k.CreateJobObjectW(None, None)
        limits = ExtendedLimits(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self.job or not k.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Assign before spawning anything. venv launchers and their children inherit it.
        # Only this process owns the non-inheritable job handle.
        if not k.AssignProcessToJobObject(self.job, k.GetCurrentProcess()):
            raise ctypes.WinError(ctypes.get_last_error())
        def watch():
            try:
                while self.host.alive(): self.stopping.wait(.2)
            finally:
                self.stopping.set()
                # The service requests graceful shutdown independently. Bound hung workers,
                # dependency installers, and a supervisor killed unexpectedly via the job.
                threading.Event().wait(grace_seconds)
                os._exit(0)
        threading.Thread(target=watch, name='echo-lifetime', daemon=True).start()

    def running(self):
        return self.host.alive() and not self.stopping.is_set()
