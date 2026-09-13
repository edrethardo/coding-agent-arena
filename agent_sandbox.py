"""Put every agent invocation behind bin/agent-sandbox.

Both runners launch an agent the same way -- `[wrapper, workdir]`, task on stdin, answer
on stdout -- so both leak the same thing: the agent runs unsandboxed as the same uid and
can simply read the code that scores it. For the game benchmark that is
`benchmarks/<game>/grader.py` (the 24 checks and the exact `__state` field names they
read); for the agentic tier it is `tests_reference/refs_hard.py`, which contains the
reference solutions outright. Neither runner should have to remember to defend itself, so
the wrapping lives here and both call it.

`ARENA_SANDBOX=0` turns it off for debugging. It is recorded in the run's provenance,
because a number produced without the sandbox is not comparable to one produced with it.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX = os.path.join(HERE, "bin", "agent-sandbox")


def enabled():
    """Off only if asked explicitly, and only if the sandbox is actually usable."""
    if os.environ.get("ARENA_SANDBOX", "1") == "0":
        return False
    return os.access(SANDBOX, os.X_OK)


def argv(wrapper, workdir):
    """The command line to launch one agent round, sandboxed unless disabled."""
    if not enabled():
        return [wrapper, workdir]
    return [SANDBOX, wrapper, workdir]


def status():
    """One line for the record: what the launcher decided and why."""
    if os.environ.get("ARENA_SANDBOX", "1") == "0":
        return "off (ARENA_SANDBOX=0)"
    if not os.access(SANDBOX, os.X_OK):
        return f"off (missing or not executable: {SANDBOX})"
    return "on"
