"""
EDL Muzzy Lane LTI 1.1 -> 1.3 migration, both phases in one command.

    manage.py cms lti13_migrate --mode {test,actual} --as-user <user> [--apply]

Runs as two phases with a separate pre-flight summary and confirmation in
front of each, so the course-side plan can be reviewed (and declined) after
seeing what the library side actually did:

    Step 0  resolve the run's user, auto-detect prod vs stage
    Step 1  discover the libraries in scope for --mode
    Step 2  read each lti_consumer component, capture its Muzzy Lane activityid
    Step 3  PHASE 1 PRE-FLIGHT  ->  confirm
    Step 4  migrate each component: OLX -> DL content -> verify -> publish
    Step 5  discover the course-side blocks synced from those components
    Step 6  PHASE 2 PRE-FLIGHT  ->  confirm
    Step 7  per block: accept changes -> DL content -> verify -> publish unit
    Step 8  independent verification, re-derived from scratch

Both phases are read-only unless --apply is passed. Phase 1 must complete
before Phase 2 within the same run, because Studio's sync reads the upstream
at LatestVersion.PUBLISHED -- an unpublished library edit is invisible to the
course side. Keeping them in one command is what guarantees that ordering.

See lti_consumer/edl_migration/README.rst for the full runbook.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from lti_consumer.edl_migration import config, course_ops, library_ops, report, verify
from lti_consumer.edl_migration import state as state_module


class Command(BaseCommand):
    """Migrate EDL's Muzzy Lane content libraries and their courses from LTI 1.1 to LTI 1.3."""

    help = (
        "Migrate matched content libraries' lti_consumer components to LTI 1.3 with deep-linking "
        "content, then propagate to every course block synced from them."
    )

    def add_arguments(self, parser):
        """Declare the command line. --mode and --as-user are the only required arguments."""
        parser.add_argument(
            "--mode", required=True, choices=["test", "actual"],
            help="test = rehearse against test libraries; actual = the real 'auto' assessment libraries",
        )
        parser.add_argument(
            "--as-user", required=True,
            help="username or email to attribute this run to. Not auto-picked: which libraries are even "
                 "visible, and whether they can be read as an author, depends on this user's permissions.",
        )
        parser.add_argument("--org", default=None, help="restrict the library search to one org (e.g. EDL)")
        parser.add_argument(
            "--library", dest="library_scope", default=None,
            help="restrict to one library slug, for the 'one family at a time' rollout",
        )
        parser.add_argument("--course", dest="course_scope", default=None, help="restrict phase 2 to one course id")
        parser.add_argument("--apply", action="store_true", help="write and publish changes (default: dry run)")
        parser.add_argument("--state-file", dest="state_file", default=None, help="override the state file path")
        parser.add_argument(
            "--yes", action="store_true",
            help="skip both confirmation prompts. Only for a plan already reviewed via a dry run.",
        )

    def handle(self, *args, **options):
        """Run both phases, confirming before each."""
        out = self.stdout
        apply_mode = options["apply"]
        mode = options["mode"]

        # --- Step 0: who is this running as, and which environment are we in? -------------
        user = self._resolve_user(options["as_user"])
        env, env_cfg = config.detect_env()
        state_path = options["state_file"] or state_module.default_state_path(env, mode)
        state = state_module.load_state(state_path, env, mode)

        # --- Steps 1-2: discover libraries and read every component (read-only) ----------
        libraries = library_ops.discover_libraries(
            user, mode, org=options["org"], library_scope=options["library_scope"],
        )
        if options["library_scope"] and not libraries:
            raise CommandError(
                f"--library {options['library_scope']!r} matched no library that also matches "
                f"--mode {mode} (search term {config.MODE_SEARCH_TERMS[mode]!r})."
            )

        report_libraries = []
        entries = []
        for lib in libraries:
            components = [
                library_ops.inspect_component(lib["lib_key"], component, env_cfg)
                for component in lib["components"]
            ]
            entries.extend(components)
            report_libraries.append({"slug": lib["slug"], "title": lib["title"], "components": components})

        state["libraries"] = entries
        state_module.save_state(state_path, state)

        # --- Step 3: phase 1 pre-flight, then confirm ------------------------------------
        report.print_libraries_preflight(
            out, env, mode, config.MODE_SEARCH_TERMS[mode], options["org"], report_libraries, apply_mode,
        )
        if not options["yes"] and not report.confirm(out):
            out.write(self.style.WARNING("Aborted at phase 1. No changes made.\n"))
            return

        # --- Step 4: phase 1 writes ------------------------------------------------------
        if apply_mode:
            entries = self._apply_phase_1(out, entries, user, env_cfg, state, state_path)

        # --- Step 5: discover the course-side blocks -------------------------------------
        # In apply mode only components that are genuinely on 1.3 with correct DL content
        # can be safely synced from; in a dry run we include pending ones so the phase 2
        # plan is still previewable.
        eligible_statuses = ("done", "skipped_already_migrated") if apply_mode else (
            "done", "skipped_already_migrated", "pending"
        )
        eligible = [entry for entry in entries if entry.get("status") in eligible_statuses]
        courses, unreadable = course_ops.discover_course_blocks(eligible)
        if options["course_scope"]:
            if options["course_scope"] not in courses:
                raise CommandError(
                    f"--course {options['course_scope']!r} has no blocks linked to the libraries in scope."
                )
            courses = {options["course_scope"]: courses[options["course_scope"]]}

        state["courses"] = courses
        state["unreadable_courses"] = unreadable
        state_module.save_state(state_path, state)
        report.print_state(out, state)

        # --- Step 6: phase 2 pre-flight, then confirm ------------------------------------
        report.print_activityid_table(out, entries)
        report.print_courses_preflight(out, env, mode, courses, unreadable, apply_mode)
        if not options["yes"] and not report.confirm(out):
            out.write(self.style.WARNING("Aborted at phase 2. Phase 1 changes above stand.\n"))
            return

        if not apply_mode:
            out.write(self.style.SUCCESS(
                f"\nDry run complete. Nothing was written. State file: {state_path}\n"
            ))
            return

        # --- Step 7: phase 2 writes ------------------------------------------------------
        self._apply_phase_2(out, courses, user, env_cfg, state, state_path)

        # --- Step 8: independent verification, re-derived from scratch -------------------
        report.print_state(out, state)
        report.print_verification_report(out, "Libraries", verify.verify_library_entries(state["libraries"]))
        report.print_verification_report(out, "Courses", verify.verify_course_blocks(state["courses"]))

    def _resolve_user(self, as_user):
        """Look up --as-user by username, then by email."""
        user_model = get_user_model()
        for field in ("username", "email"):
            try:
                return user_model.objects.get(**{field: as_user})
            except user_model.DoesNotExist:
                continue
        raise CommandError(f"No user found for --as-user={as_user!r}")

    def _apply_phase_1(self, out, entries, user, env_cfg, state, state_path):
        """
        Migrate every pending component, saving state after each one.

        Each component is wrapped individually: an unexpected failure on one
        must not abandon the run and lose the record of everything already
        written -- especially because a migrated component's cleared
        custom_parameters no longer hold its activityid.
        """
        out.write("\nPhase 1: migrating library components...\n")
        applied = []
        for entry in entries:
            if entry.get("status") != "pending":
                applied.append(entry)
                continue
            try:
                result = library_ops.migrate_component(entry, user, env_cfg)
            except Exception as exc:  # pylint: disable=broad-except
                result = dict(entry, status="failed", error=f"{type(exc).__name__}: {exc}")
            applied.append(result)
            state["libraries"] = applied + entries[len(applied):]
            state_module.save_state(state_path, state)
            out.write(f"  {result['status']:<26} {result['usage_key']}\n")
        state["libraries"] = applied
        state_module.save_state(state_path, state)
        return applied

    def _apply_phase_2(self, out, courses, user, env_cfg, state, state_path):
        """Migrate every pending course block, saving state after each one."""
        out.write("\nPhase 2: migrating course blocks...\n")
        for course_id, blocks in courses.items():
            out.write(f"  {course_id}\n")
            processed = []
            for block in blocks:
                if block["already_migrated"]:
                    processed.append(dict(block, status="skipped_already_migrated"))
                else:
                    try:
                        processed.append(course_ops.migrate_course_block(block, user, env_cfg))
                    except Exception as exc:  # pylint: disable=broad-except
                        processed.append(dict(block, status="failed", error=f"{type(exc).__name__}: {exc}"))
                state["courses"][course_id] = processed + blocks[len(processed):]
                state_module.save_state(state_path, state)
                out.write(f"    {processed[-1]['status']:<26} {processed[-1]['block_location']}\n")
            state["courses"][course_id] = processed
            state_module.save_state(state_path, state)
