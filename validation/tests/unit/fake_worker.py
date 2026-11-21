"""A stand-alone stand-in for ``python -m dc_rules.worker`` that makes deadlines deterministic.

It speaks the same JSON-lines protocol and imports neither the engine nor ``dc_rules``. Behaviour
is chosen by the rule key of each unit:

* ``hang``: print the start marker, then burn CPU forever (a hostile rule loop, until killed);
* ``stall``: print the start marker, then sleep forever without using CPU (a wedged worker);
* ``slow``: print the start marker, sleep 0.45 s (no CPU, like a load-starved worker), then answer;
* ``die``: print the start marker, then exit abruptly (the parent sees end of file);
* ``doa``: exit before any start marker, the first time only (``FAKE_WORKER_FLAG`` is a flag
  file shared by every process), so a replacement worker answers normally;
* ``doa-always``: exit before any start marker, every time;
* ``end-early``: print the start marker, then end the job without a result for it or the rest;
* ``die-after-done``: answer ``pass``, then exit before the end marker;
* ``bad-start``, ``bad-done``, ``bad-marker``, ``garbage``, ``huge-line``: print the start marker,
  then a start marker for an unknown unit, a result for another unit, a line that is no marker,
  a line that is no JSON, or a line longer than the parent's stream limit;
* anything else: answer ``pass`` at once, with this process id in ``detail`` so a test can tell
  whether a unit ran in the same process as another.

With ``--close-stdin`` on the command line the worker closes its stdin before its ready line and
stays alive, so the parent's next job write hits a broken pipe.
"""

import json
import os
import sys
import time

# The parent's stream limit is 16 MiB; one byte more than that is a line it cannot read.
HUGE_LINE_BYTES = 16 * 1024 * 1024 + 1
MISBEHAVIOUR = {
    "bad-start": lambda unit_id: json.dumps({"start": unit_id + 1000}),
    "bad-done": lambda unit_id: json.dumps(
        {"done": unit_id, "result": {"id": unit_id + 1000, "outcome": "pass", "findings": []}}
    ),
    "bad-marker": lambda unit_id: json.dumps({"progress": unit_id}),
    "garbage": lambda unit_id: "this is not json",
    "huge-line": lambda unit_id: "x" * HUGE_LINE_BYTES,
}


def out(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def result(unit_id: int) -> dict:
    return {
        "done": unit_id,
        "result": {
            "id": unit_id,
            "outcome": "pass",
            "findings": [],
            "matched": 1,
            "detail": str(os.getpid()),
        },
    }


if "--close-stdin" in sys.argv:
    # Closed before the ready line, so the parent cannot write a job into the pipe first.
    os.close(0)
    out({"ready": True, "engine_version": "fake-engine", "dsl_version": "cel-1"})
    while True:
        time.sleep(60)
out({"ready": True, "engine_version": "fake-engine", "dsl_version": "cel-1"})
for line in sys.stdin:
    if not line.strip():
        continue
    job = json.loads(line)
    keys = {unit["rule"]["key"] for unit in job["units"]}
    if "doa-always" in keys:
        os._exit(1)
    if "doa" in keys:
        flag = os.environ["FAKE_WORKER_FLAG"]
        if not os.path.exists(flag):
            open(flag, "w").close()
            os._exit(1)
    for unit in job["units"]:
        out({"start": unit["id"]})
        key = unit["rule"]["key"]
        if key == "hang":
            while True:
                pass
        if key == "stall":
            while True:
                time.sleep(60)
        if key == "slow":
            time.sleep(0.45)
        if key == "die":
            os._exit(1)
        if key == "end-early":
            break
        if key in MISBEHAVIOUR:
            sys.stdout.write(MISBEHAVIOUR[key](unit["id"]) + "\n")
            sys.stdout.flush()
            while True:
                time.sleep(60)
        out(result(unit["id"]))
        if key == "die-after-done":
            os._exit(1)
    out({"end": True})
