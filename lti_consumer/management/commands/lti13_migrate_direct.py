"""
EDL Muzzy Lane LTI 1.1 -> 1.3 migration for courses with directly-configured
blocks (no content library involved) -- the LSU parent courses.

    manage.py cms lti13_migrate_direct --mode {test,actual} --as-user <user> [--apply]

Unlike ``lti13_migrate`` (library-backed courses, two phases with an "accept
changes" sync in between), these LSU courses have their ``lti_consumer``
blocks configured with LTI 1.1 straight in the course -- there is no upstream
library component to sync from. So this is a single phase:

    Step 0  resolve the run's user, auto-detect prod vs stage
    Step 1  discover lti_consumer blocks in the courses fixed for --mode
    Step 2  PRE-FLIGHT  ->  confirm
    Step 3  per block: flip fields -> write DL content -> verify -> publish unit
    Step 4  independent verification, re-derived from scratch

Read-only unless ``--apply`` is passed, same as ``lti13_migrate``.

``--mode actual`` is fixed to the 9 LSU parent courses (see
``edl_migration.config.DIRECT_MIGRATION_COURSES``); ``--mode test`` currently
covers one course, ``course-v1:EDL+Test02+2026``. ``--course`` restricts a
run to one course id within that fixed set, for a staged rollout.

See lti_consumer/edl_migration/README.rst for the full runbook.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from lti_consumer.edl_migration import config, direct_ops, report, verify
from lti_consumer.edl_migration import state as state_module


class Command(BaseCommand):
    """Migrate LSU's directly-configured lti_consumer course blocks from LTI 1.1 to LTI 1.3."""

    help = (
        "Migrate lti_consumer blocks configured directly in a course (no content library) "
        "from LTI 1.1 to LTI 1.3 with deep-linking content."
    )

    def add_arguments(self, parser):
        """Declare the command line. --mode and --as-user are the only required arguments."""
        parser.add_argument(
            "--mode", required=True, choices=["test", "actual"],
            help="test = rehearse against placeholder test courses; actual = the 9 LSU parent courses",
        )
        parser.add_argument(
            "--as-user", required=True,
            help="username or email to attribute this run to. Not auto-picked: whether these courses "
                 "can even be read depends on this user's permissions.",
        )
        parser.add_argument(
            "--course", dest="course_scope", default=None,
            help="restrict to one course id, out of the fixed set for --mode",
        )
        parser.add_argument("--apply", action="store_true", help="write and publish changes (default: dry run)")
        parser.add_argument("--state-file", dest="state_file", default=None, help="override the state file path")
        parser.add_argument(
            "--yes", action="store_true",
            help="skip the confirmation prompt. Only for a plan already reviewed via a dry run.",
        )

    def handle(self, *args, **options):
        """Discover, pre-flight, confirm, migrate, verify."""
        out = self.stdout
        apply_mode = options["apply"]
        mode = options["mode"]

        # --- Step 0: who is this running as, and which environment are we in? -------------
        user = self._resolve_user(options["as_user"])
        env, env_cfg = config.detect_env()
        state_path = options["state_file"] or state_module.default_state_path(
            env, mode, prefix="lti13_direct_state",
        )
        state = state_module.load_state(state_path, env, mode)

        course_keys = config.DIRECT_MIGRATION_COURSES[mode]
        if not course_keys:
            raise CommandError(
                f"No course keys configured for --mode {mode!r} yet -- "
                f"edl_migration.config.DIRECT_MIGRATION_COURSES[{mode!r}] is empty."
            )
        if options["course_scope"]:
            if options["course_scope"] not in course_keys:
                raise CommandError(
                    f"--course {options['course_scope']!r} is not one of the courses configured "
                    f"for --mode {mode!r}."
                )
            course_keys = [options["course_scope"]]

        # --- Step 1: discover the blocks in scope (read-only) ------------------------------
        courses, unreadable = direct_ops.discover_course_blocks(course_keys, env_cfg)
        state["courses"] = courses
        state["unreadable_courses"] = unreadable
        state_module.save_state(state_path, state)
        report.print_state(out, state)

        # --- Step 2: pre-flight, then confirm -----------------------------------------------
        report.print_direct_courses_preflight(out, env, mode, course_keys, courses, unreadable, apply_mode)
        if not options["yes"] and not report.confirm(out):
            out.write(self.style.WARNING("Aborted. No changes made.\n"))
            return

        if not apply_mode:
            out.write(self.style.SUCCESS(
                f"\nDry run complete. Nothing was written. State file: {state_path}\n"
            ))
            return

        # --- Step 3: writes -------------------------------------------------------------------
        self._apply(out, courses, user, env_cfg, state, state_path)

        # --- Step 4: independent verification, re-derived from scratch -----------------------
        report.print_state(out, state)
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

    def _apply(self, out, courses, user, env_cfg, state, state_path):
        """
        Migrate every pending block, saving state after each one.

        Each block is wrapped individually: an unexpected failure on one must
        not abandon the run and lose the record of everything already
        written -- especially because a migrated block's cleared
        custom_parameters no longer hold its activityid.
        """
        out.write("\nMigrating course blocks...\n")
        for course_id, blocks in courses.items():
            out.write(f"  {course_id}\n")
            processed = []
            for block in blocks:
                if block["status"] in ("skipped_already_migrated", "skipped_conflict", "failed"):
                    processed.append(block)
                else:
                    try:
                        processed.append(direct_ops.migrate_course_block(block, user, env_cfg))
                    except Exception as exc:  # pylint: disable=broad-except
                        processed.append(dict(block, status="failed", error=f"{type(exc).__name__}: {exc}"))
                state["courses"][course_id] = processed + blocks[len(processed):]
                state_module.save_state(state_path, state)
                out.write(f"    {processed[-1]['status']:<26} {processed[-1]['block_location']}\n")
            state["courses"][course_id] = processed
            state_module.save_state(state_path, state)
