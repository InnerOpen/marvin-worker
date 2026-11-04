import sys

from .dispatch import main as dispatch
from .runner import main as runner
from .serve import main as serve
from .serve import run_once

# `python -m marvin_worker` serves (a standing worker that is POSTed jobs); `... run --kind K --job J --args '{...}'` does one job and exits;
# `... dispatch` watches Marvin's queue and starts a worker for each job; `... work` is a runner: it registers what it can do and takes
# the queued jobs of those kinds itself.
command = sys.argv[1:2]
raise SystemExit(run_once(sys.argv[2:]) if command == ["run"] else dispatch() if command == ["dispatch"] else runner() if command == ["work"] else serve())
