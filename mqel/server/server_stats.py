"""Technische Kennzahlen für die Server-Seite des Webtools (admin.GetServerStats).

Der VPS ist ein Container: os.getloadavg und /proc/stat zeigen den Host.
CPU und Speicher daher aus der eigenen cgroup (v2, sonst v1), Load nur als
Host-Wert daneben. Auf Windows (Entwicklung) fehlt das meiste: dann None.
"""

import collections
import os
import shutil
import threading
import time

STARTED = time.time()
WINDOW = 300  # Sekunden für Requests/min und Antwortzeit


class RequestStats:
    """Dauer der letzten Requests (GameHandler._safe_handle), threadsicher."""

    def __init__(self):
        self.lock = threading.Lock()
        self.recent = collections.deque()  # (Ende monotonic, Dauer s)

    def add(self, seconds):
        now = time.monotonic()
        with self.lock:
            self.recent.append((now, seconds))
            while self.recent and self.recent[0][0] < now - WINDOW:
                self.recent.popleft()

    def summary(self):
        now = time.monotonic()
        with self.lock:
            durations = [d for t, d in self.recent if t >= now - WINDOW]
        durations.sort()
        n = len(durations)
        return {"PerMinute": round(n / (WINDOW / 60), 1),
                "AvgMs": round(1000 * sum(durations) / n, 1) if n else None,
                "P95Ms": round(1000 * durations[min(n - 1, int(n * 0.95))], 1) if n else None,
                "WindowMinutes": WINDOW // 60}


REQUESTS = RequestStats()


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _cpu_usage_seconds():
    """CPU-Zeit der cgroup in Sekunden (v2 cpu.stat, v1 cpuacct.usage)."""
    stat = _read("/sys/fs/cgroup/cpu.stat")
    if stat:
        for line in stat.splitlines():
            k, _, v = line.partition(" ")
            if k == "usage_usec":
                return int(v) / 1e6
    v1 = _read("/sys/fs/cgroup/cpuacct/cpuacct.usage") or _read("/sys/fs/cgroup/cpu,cpuacct/cpuacct.usage")
    return int(v1) / 1e9 if v1 else None


def _cpu_limit():
    """Kerne laut Quota der cgroup, sonst sichtbare CPUs."""
    v2 = _read("/sys/fs/cgroup/cpu.max")
    if v2 and not v2.startswith("max"):
        quota, period = v2.split()
        return int(quota) / int(period)
    quota = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") or _read("/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us")
    period = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us") or _read("/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us")
    if quota and period and int(quota) > 0:
        return int(quota) / int(period)
    affinity = getattr(os, "sched_getaffinity", None)  # nur Linux
    return len(affinity(0)) if affinity else (os.cpu_count() or 1)


class CpuSampler:
    """CPU-Zeit der cgroup alle STEP s, Mittel über 1 und 15 Minuten. Kurze
    Messungen taugen nicht: der Kernel schreibt cpu.stat verzögert in
    Schüben fort (rstat-Flush), 0,5 s zeigten 50 % statt 2 %."""
    STEP, KEEP = 10, 15 * 60

    def __init__(self):
        self.samples = collections.deque()  # (monotonic, CPU-Sekunden)

    def start(self):
        if _cpu_usage_seconds() is not None:
            threading.Thread(target=self._run, daemon=True, name="cpu-sampler").start()

    def _run(self):
        while True:
            self.samples.append((time.monotonic(), _cpu_usage_seconds()))
            while self.samples[0][0] < self.samples[-1][0] - self.KEEP:
                self.samples.popleft()
            time.sleep(self.STEP)

    def percent(self, seconds, cores):
        samples = list(self.samples)
        if len(samples) < 2:
            return None
        t1, u1 = samples[-1]
        t0, u0 = next((s for s in samples if s[0] >= t1 - seconds), samples[0])
        return round(100 * (u1 - u0) / (t1 - t0) / cores, 1) if t1 > t0 else None


CPU = CpuSampler()


def cpu():
    """Auslastung der cgroup in % ihres Limits (1 und 15 Minuten)."""
    cores = _cpu_limit()
    return {"Percent1": CPU.percent(60, cores), "Percent15": CPU.percent(15 * 60, cores),
            "Cores": round(cores, 2)}


def memory():
    """Belegt/Limit der cgroup; ohne Limit /proc/meminfo (bei LXC über lxcfs)."""
    used = _read("/sys/fs/cgroup/memory.current") or _read("/sys/fs/cgroup/memory/memory.usage_in_bytes")
    limit = _read("/sys/fs/cgroup/memory.max") or _read("/sys/fs/cgroup/memory/memory.limit_in_bytes")
    info = {}
    for line in (_read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.split()[0]) * 1024
    total = info.get("MemTotal")
    if used and limit and limit.isdigit() and (not total or int(limit) < total):
        total = int(limit)
        free = total - int(used)
        # Seitencache zählt zu memory.current, ist aber freigebbar
        for line in (_read("/sys/fs/cgroup/memory.stat") or "").splitlines():
            k, _, v = line.partition(" ")
            if k == "inactive_file":
                free += int(v)
    else:
        free = info.get("MemAvailable")
    return {"Total": total, "Free": free}


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def collect(state_dir):
    disk = shutil.disk_usage(state_dir)
    getloadavg = getattr(os, "getloadavg", None)  # nicht unter Windows
    load = getloadavg() if getloadavg else None
    host_up = _read("/proc/uptime")
    return {"Cpu": cpu(), "HostLoad": [round(x, 2) for x in load] if load else None,
            "Memory": memory(),
            "Disk": {"Total": disk.total, "Free": disk.free},
            "StateBytes": dir_size(state_dir),
            "Requests": REQUESTS.summary(),
            "UptimeSeconds": int(time.time() - STARTED),
            "HostUptimeSeconds": int(float(host_up.split()[0])) if host_up else None}