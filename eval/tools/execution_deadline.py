"""Bound an external exec by the enclosing host call."""
import time


def deadline_command(command, deadline):
    if deadline is None:
        return list(command)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("enclosing tool deadline expired")
    # Reserve 0.2s for escalation and CPU meter/IPC completion.
    duration = max(0.001, remaining - 0.2)
    return ["timeout", "--signal=TERM", "--kill-after=0.1s", f"{duration:.6f}s", *command]
