"""Stream cgroup-v2 measurements; this small probe is identical for all variants."""

from __future__ import annotations

import json
import time
from pathlib import Path

CGROUP = Path("/sys/fs/cgroup")

while True:
    stats = dict(line.split() for line in (CGROUP / "memory.stat").read_text().splitlines())
    events = dict(line.split() for line in (CGROUP / "memory.events").read_text().splitlines())
    current = int((CGROUP / "memory.current").read_text())
    print(
        json.dumps(
            {
                "time": time.time(),
                "current": current,
                "working_set": max(0, current - int(stats["inactive_file"])),
                "anon": int(stats["anon"]),
                "file": int(stats["file"]),
                "oom_kill": int(events["oom_kill"]),
            }
        ),
        flush=True,
    )
    time.sleep(1)
