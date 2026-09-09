"""
Pre-flight summaries, the confirm prompt, and the post-run verification report.

Everything writes through an `out` stream (the command's ``self.stdout``) so
output is capturable in tests and respects Django's ``--no-color``.
"""
import json


def _line(out, text=""):
    """Write one line. Explicit newline, because Django's OutputWrapper only adds one if absent."""
    out.write(f"{text}\n")


def confirm(out, prompt="Proceed? [y/N]: "):
    """
    Ask for an explicit y/yes. Anything else -- including no stdin at all --
    means no.

    The EOFError case is the common one in practice: ``kubectl exec`` without
    ``-it`` gives the command a closed stdin, and a migration that writes to
    production content must not treat "could not ask" as "yes".
    """
    _line(out, "")
    out.write(prompt)
    try:
        answer = input().strip().lower()
    except EOFError:
        _line(out, "")
        _line(out, "No stdin available to confirm on. Re-run with `kubectl exec -it` "
                   "(or `docker exec -it`), or pass --yes if you have already reviewed this plan.")
        return False
    return answer in ("y", "yes")


def print_libraries_preflight(out, env, mode, search_term, org, libraries, apply_mode):
    """
    Print the Phase 1 plan: every library in scope, every component, and the
    activityid each one will be wired to.

    Reports name-matched and content-bearing library counts separately, and
    surfaces duplicate activityids explicitly, rather than folding either into
    a single total.
    """
    _line(out, "=== LTI 1.3 Migration - Phase 1: Libraries - Pre-flight ===")
    _line(out, f"Environment: {env}   [auto-detected]")
    _line(out, f"Mode: {mode} (search term: {search_term!r})" + (f"   org={org}" if org else ""))
    _line(out, "")

    with_content = [lib for lib in libraries if lib["components"]]
    _line(out, f"Libraries matched by slug/org/title/description: {len(libraries)}")
    _line(out, f"Libraries containing lti_consumer components:    {len(with_content)}")
    _line(out, "")

    counts = {}
    for lib in with_content:
        comps = lib["components"]

        seen = {}
        for comp in comps:
            if comp["activityid"]:
                seen.setdefault(comp["activityid"], []).append(comp["display_name"])
        dupes = {aid: names for aid, names in seen.items() if len(names) > 1}
        dup_note = ""
        if dupes:
            described = "; ".join(
                f'"{names[0]}" x{len(names)} (activityid={aid})' for aid, names in dupes.items()
            )
            dup_note = f"   /!\\ DUPLICATE: {described}"

        _line(out, f"[{lib['slug']}] {lib['title']} -- {len(comps)} lti_consumer components{dup_note}")
        for comp in comps:
            counts[comp["status"]] = counts.get(comp["status"], 0) + 1
            short = comp["usage_key"].rsplit(":", 1)[-1][:14]
            name = comp["display_name"][:44]
            if comp["status"] == "skipped_already_migrated":
                _line(out, f"   {short:<14}  {name:<44}  already migrated, DL content present -- skip")
            elif comp["status"] == "skipped_conflict":
                _line(out, f"   {short:<14}  {name:<44}  /!\\ CONFLICT: {comp['error']}")
            elif comp["status"] == "failed":
                _line(out, f"   {short:<14}  {name:<44}  CANNOT MIGRATE: {comp['error']}")
            else:
                _line(out, f"   {short:<14}  {name:<44}  activityid={comp['activityid']}")

    _line(out, "")
    total = sum(counts.values())
    _line(out, f"TOTAL: {len(with_content)} libraries, {total} lti_consumer components")
    for status, count in sorted(counts.items()):
        _line(out, f"         {count:>4}  {status}")
    _line(out, "")
    _line(out, "Phase 1 will: edit OLX (config_type/external_config/lti_version/custom_parameters) "
               "-> write DL content -> verify -> publish each component.")
    _line(out, "Mode: " + ("APPLY -- changes WILL be written and published."
                           if apply_mode else "DRY-RUN -- nothing will be written."))


def print_activityid_table(out, entries):
    """
    Print a lookup table of activityid -> LTI 1.1 library block, covering
    every component Phase 1 has not yet migrated (``status == "pending"``).

    The join between a course-side block and its library source is by
    usage_key, which is not human-legible -- this table is what makes the
    Phase 2 matches below it checkable at a glance instead of by memory.
    """
    pending = [entry for entry in entries if entry.get("status") == "pending"]
    _line(out, "=== Activity ID -> LTI 1.1 Library Block ===")
    if not pending:
        _line(out, "(none -- no pending LTI 1.1 components)")
        _line(out, "")
        return
    _line(out, f"{'activityid':<38}  {'library block':<55}  display_name")
    for entry in pending:
        _line(out, f"{entry['activityid']:<38}  {entry['usage_key']:<55}  {entry['display_name']}")
    _line(out, "")


def print_courses_preflight(out, env, mode, courses, unreadable, apply_mode):
    """
    Print the Phase 2 plan: which courses and blocks will be touched, and what
    each one will have written.
    """
    _line(out, "=== LTI 1.3 Migration - Phase 2: Courses - Pre-flight ===")
    _line(out, f"Environment: {env}   Mode: {mode}")
    _line(out, "")

    counts = {}
    total_blocks = 0
    _line(out, f"Courses affected: {len(courses)}")
    _line(out, "")
    for course_id, blocks in courses.items():
        _line(out, course_id)
        total_blocks += len(blocks)
        for block in blocks:
            short = block["block_location"].rsplit(":", 1)[-1][:14]
            if block["conflict"]:
                counts["skipped_conflict"] = counts.get("skipped_conflict", 0) + 1
                _line(out, f"   {short:<14}  upstream={block['upstream_short']}  /!\\ CONFLICT: existing DL "
                           f"content points at {block['existing_activityid']}, not {block['activityid']} -- skip")
            elif block["already_migrated"]:
                counts["skipped_already_migrated"] = counts.get("skipped_already_migrated", 0) + 1
                _line(out, f"   {short:<14}  upstream={block['upstream_short']}  "
                           f"already on 1.3 with matching DL content -- skip")
            else:
                counts["pending"] = counts.get("pending", 0) + 1
                _line(out, f"   {short:<14}  upstream={block['upstream_short']}  "
                           f"accept changes -> DL content (activityid={block['activityid']}) -> publish unit")
            _line(out, json.dumps(block, indent=2, default=str))

    _line(out, "")
    _line(out, f"TOTAL: {len(courses)} courses, {total_blocks} blocks")
    for status, count in sorted(counts.items()):
        _line(out, f"         {count:>4}  {status}")

    if unreadable:
        _line(out, "")
        _line(out, f"/!\\ {len(unreadable)} course(s) could not be listed and are NOT covered by the totals above:")
        for note in unreadable:
            _line(out, f"      {note}")

    _line(out, "")
    _line(out, "Note: publishing happens at the parent unit, so any *other* unpublished draft edit "
               "in the same unit goes live too.")
    _line(out, "Note: Accept Changes also syncs has_score from the library (non-customizable field).")
    _line(out, "Mode: " + ("APPLY -- changes WILL be written and published."
                           if apply_mode else "DRY-RUN -- nothing will be written."))


def print_state(out, state):
    """
    Dump the full state to stdout.

    This is the durable record: the state *file* lives in the pod's ephemeral
    working directory, and once a component is migrated its cleared
    custom_parameters no longer hold the activityid. Printing it means the
    operator's own terminal log is a usable backup.
    """
    _line(out, "=== State (copy this out of your terminal log; the state file is on ephemeral pod storage) ===")
    _line(out, json.dumps(state, indent=2, default=str))


def print_verification_report(out, label, results):
    """Print pass/fail per item for one phase, listing every failure in full."""
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    _line(out, f"=== Post-run Verification - {label} ===")
    _line(out, f"Checked: {len(results)}")
    _line(out, f"  PASS  {len(passed)}")
    _line(out, f"  FAIL  {len(failed)}")
    for result in failed:
        _line(out, f"    FAIL  {result['key']}")
        _line(out, f"          {result['detail']}")
