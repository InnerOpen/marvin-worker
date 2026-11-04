import sys

from .dispatch import main as dispatch
from .serve import main as serve
from .serve import run_once

# `python -m marvin_worker` serves (a standing worker); `... run --kind K --job J --args '{...}'` does one job and exits;
# `... dispatch` watches Marvin's queue of `job` records and starts a worker for each.
command = sys.argv[1:2]
raise SystemExit(run_once(sys.argv[2:]) if command == ["run"] else dispatch() if command == ["dispatch"] else serve())
