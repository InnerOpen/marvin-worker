"""What a worker says on its own output (`docker logs`, `kubectl logs`, the journal): one timestamped line per event, so a run can be
read back later. The job's record in Marvin keeps the short story; this is the machine's own."""

from datetime import UTC, datetime


def say(text: str) -> None:
    print(f"{datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')} {text}", flush=True)
