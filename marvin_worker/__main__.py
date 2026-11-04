import sys

from .serve import main, run_once

# `python -m marvin_worker` serves; `python -m marvin_worker run --kind K --job J --args '{...}'` does one job and exits.
raise SystemExit(run_once(sys.argv[2:]) if sys.argv[1:2] == ["run"] else main())
