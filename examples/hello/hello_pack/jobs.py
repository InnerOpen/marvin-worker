"""A complete, working kind of job to copy: `hello` greets someone and says how long their name is.

Everything a kind needs is here: the decorator that registers it and declares its contract (what it takes, gives and needs), the function
`(job, ctx) -> fields`, a note in the log, and the fields the worker writes onto the job's record when it ends."""

from marvin_worker.jobs import JobError, job_kind


@job_kind(
    "hello",
    description="Greet someone",
    inputs={
        "name": {"type": "string", "description": "who to greet"},
        "shout": {"type": "boolean", "required": False, "description": "greet in capitals"},  # optional: say required False, or the job is refused without it
    },
    produces=[],  # no file; a kind that makes one lists its media type here, e.g. ["audio/mpeg"]
    needs=[],  # programs the machine must have, e.g. ["ffmpeg"]; `python -m marvin_worker kinds` says whether they are there
)
def hello(job, ctx):
    args = job.get("args") or {}  # already checked against the contract above: `name` is there and a string
    name = args["name"].strip()
    if not name:
        raise JobError("name is empty: say who to greet")  # a JobError is the plain-words way to fail: it lands on the record's log
    greeting = f"Hello, {name}!"
    ctx.note(f"greeting {name}")  # appears on the worker's own log at once and on the record's log when the job ends
    # ctx.work is a scratch folder for files, removed afterwards; ctx.marvin("platform", "entries", "list") runs the Marvin CLI as the worker's token
    return {"log": greeting.upper() if args.get("shout") else greeting, "result": {"greeting": greeting, "letters": len(name)}}  # merged onto the job's record
