"""
Run state: an audit and resume log for one migration run.

Now that both phases live in a single command, this is no longer a handoff
mechanism between two invocations -- it is a record of what was done, so a run
interrupted part-way can be reasoned about afterwards. It is written
incrementally (after discovery, and after each phase) rather than only at the
end, and also echoed to stdout, because the file itself sits on the pod's
ephemeral working directory.

It is stamped with env and mode, and reloading a file stamped for a different
env or mode is refused: a stage state file replayed on prod would otherwise
carry ``forge-stage.muzzylane.com`` URLs into production content items.
"""
import json
import os

from django.core.management.base import CommandError


def default_state_path(env, mode):
    """Default state filename, including env so prod and stage cannot collide."""
    return f"lti13_state_{env}_{mode}.json"


def new_state(env, mode):
    """Return an empty state stamped with the environment and mode of this run."""
    return {"env": env, "mode": mode, "libraries": [], "courses": {}, "unreadable_courses": []}


def load_state(path, env, mode):
    """
    Load an existing state file, or return a fresh one if there is none.

    Refuses a file belonging to a different environment or mode rather than
    silently mixing runs together.
    """
    if not os.path.exists(path):
        return new_state(env, mode)

    with open(path, encoding="utf-8") as handle:
        try:
            state = json.load(handle)
        except ValueError as exc:
            raise CommandError(f"State file {path!r} is not valid JSON: {exc}") from exc

    if not isinstance(state, dict) or "libraries" not in state:
        raise CommandError(f"State file {path!r} does not look like a migration state file.")
    if state.get("env") != env or state.get("mode") != mode:
        raise CommandError(
            f"State file {path!r} is stamped env={state.get('env')!r} mode={state.get('mode')!r}, "
            f"but this run is env={env!r} mode={mode!r}. Refusing to reuse it -- "
            f"pass --state-file to point somewhere else."
        )
    state.setdefault("courses", {})
    state.setdefault("unreadable_courses", [])
    return state


def save_state(path, state):
    """Write the state file atomically, so an interrupted write cannot corrupt it."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, default=str)
    os.replace(tmp_path, path)
