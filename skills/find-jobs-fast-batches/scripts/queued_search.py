"""Cap concurrent native OpenCLI searches on one shared browser bridge, then
exec the run's owned_search.py with the passed arguments unchanged.

Evidence (find-500 run): 8-9 simultaneous foreground opens -> 'opencli open
timed out after 90.0s'; a lone open on the same bridge took 6.2s.

usage: <imx_python> queued_search.py --run-dir RUN [--slots 4] -- <owned_search.py args>
The passthrough args must include --profile <live profile>. Shared slot locks
live in RUN/coordination/native-slots/ so every worker in the run shares them.
"""
import argparse
import fcntl
import os
import sys
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--run-dir", required=True)
ap.add_argument("--slots", type=int, default=4)
ap.add_argument("--poll", type=float, default=3.0)
ap.add_argument("rest", nargs=argparse.REMAINDER)
a = ap.parse_args()
rest = a.rest[1:] if a.rest[:1] == ["--"] else a.rest
run = Path(a.run_dir).expanduser().resolve()
target = run / "owned_search.py"
if not target.is_file():
    ap.error(f"{target} not found")
if "--profile" not in rest or rest.index("--profile") + 1 >= len(rest):
    ap.error("pass the live browser profile explicitly: --profile <name>")
if a.slots < 1:
    ap.error("--slots must be >= 1")
locks = run / "coordination" / "native-slots"
locks.mkdir(parents=True, exist_ok=True)
start, held = time.time(), None
while held is None:
    for i in range(1, a.slots + 1):
        fd = os.open(locks / f"slot{i}", os.O_RDONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = fd
            break
        except BlockingIOError:
            os.close(fd)
    if held is None:
        time.sleep(a.poll)
print(f"[queue] slot acquired after {time.time() - start:.0f}s", file=sys.stderr, flush=True)
os.set_inheritable(held, True)  # lock stays held until the exec'd search exits
os.execv(sys.executable, [sys.executable, str(target), *rest])
