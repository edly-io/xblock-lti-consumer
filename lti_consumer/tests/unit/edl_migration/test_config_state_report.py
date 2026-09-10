"""Tests for environment detection, the run state file, and the reports."""
import io
import json
import os
import tempfile
from unittest import mock

from django.core.management.base import CommandError
from django.test import TestCase

from lti_consumer.edl_migration import config, report
from lti_consumer.edl_migration import state as state_module


def _cursor_returning(*slugs):
    """Build a `connection` double whose cursor yields `slugs` from fetchall()."""
    cursor = mock.MagicMock()
    cursor.fetchall.return_value = [(slug,) for slug in slugs]
    connection = mock.MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    return connection


class DetectEnvTest(TestCase):
    """Environment is detected from the database, never guessed."""

    def test_detects_prod_from_the_author13_slug(self):
        with mock.patch.object(config, "connection", _cursor_returning("author13")):
            env, cfg = config.detect_env()
        self.assertEqual(env, "prod")
        self.assertEqual(cfg["muzzylane_base"], "https://author.muzzylane.com")

    def test_detects_stage_from_the_muzzylane_slug(self):
        with mock.patch.object(config, "connection", _cursor_returning("muzzylane")):
            env, cfg = config.detect_env()
        self.assertEqual(env, "stage")
        self.assertEqual(cfg["muzzylane_base"], "https://forge-stage.muzzylane.com")

    def test_aborts_when_both_slugs_exist(self):
        with mock.patch.object(config, "connection", _cursor_returning("author13", "muzzylane")):
            with self.assertRaises(CommandError):
                config.detect_env()

    def test_aborts_when_neither_slug_exists(self):
        with mock.patch.object(config, "connection", _cursor_returning()):
            with self.assertRaises(CommandError):
                config.detect_env()


class StateFileTest(TestCase):
    """The state file is stamped with env and mode, and refuses cross-run reuse."""

    def setUp(self):
        super().setUp()
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "state.json")

    def test_default_path_includes_env_and_mode(self):
        self.assertEqual(state_module.default_state_path("prod", "actual"), "lti13_state_prod_actual.json")
        self.assertEqual(state_module.default_state_path("stage", "test"), "lti13_state_stage_test.json")

    def test_default_path_honours_a_custom_prefix(self):
        # lti13_migrate_direct uses its own prefix so it cannot collide with
        # lti13_migrate's state file for the same env/mode.
        self.assertEqual(
            state_module.default_state_path("prod", "actual", prefix="lti13_direct_state"),
            "lti13_direct_state_prod_actual.json",
        )

    def test_missing_file_yields_a_fresh_stamped_state(self):
        state = state_module.load_state(self.path, "prod", "actual")
        self.assertEqual(state, {"env": "prod", "mode": "actual",
                                 "libraries": [], "courses": {}, "unreadable_courses": []})

    def test_round_trips_and_leaves_no_temp_file(self):
        state = state_module.new_state("stage", "test")
        state["libraries"] = [{"usage_key": "lb:EDL:X:lti_consumer:y", "status": "done"}]
        state_module.save_state(self.path, state)
        self.assertEqual(state_module.load_state(self.path, "stage", "test"), state)
        self.assertFalse(os.path.exists(f"{self.path}.tmp"))

    def test_refuses_a_state_file_from_a_different_env(self):
        # A stage run's Muzzy Lane URLs must not be replayed against prod.
        state_module.save_state(self.path, state_module.new_state("stage", "actual"))
        with self.assertRaises(CommandError):
            state_module.load_state(self.path, "prod", "actual")

    def test_refuses_a_state_file_from_a_different_mode(self):
        state_module.save_state(self.path, state_module.new_state("prod", "test"))
        with self.assertRaises(CommandError):
            state_module.load_state(self.path, "prod", "actual")

    def test_rejects_unrelated_json(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump({"something": "else"}, handle)
        with self.assertRaises(CommandError):
            state_module.load_state(self.path, "prod", "actual")

    def test_rejects_invalid_json(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with self.assertRaises(CommandError):
            state_module.load_state(self.path, "prod", "actual")


class ConfirmTest(TestCase):
    """Confirmation requires an explicit yes; anything else means no."""

    def test_accepts_y_and_yes_in_any_case(self):
        for answer in ("y", "Y", "yes", "YES", " yes "):
            with mock.patch("builtins.input", return_value=answer):
                self.assertTrue(report.confirm(io.StringIO()))

    def test_rejects_everything_else(self):
        for answer in ("", "n", "no", "maybe", "yeah"):
            with mock.patch("builtins.input", return_value=answer):
                self.assertFalse(report.confirm(io.StringIO()))

    def test_returns_false_without_stdin_instead_of_raising(self):
        # kubectl exec without -it closes stdin; "could not ask" must not mean yes.
        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=EOFError):
            self.assertFalse(report.confirm(out))
        self.assertIn("No stdin available", out.getvalue())


class LibrariesPreflightTest(TestCase):
    """The phase 1 pre-flight surfaces gaps and duplicates rather than hiding them."""

    def _print(self, libraries, apply_mode=False):
        out = io.StringIO()
        report.print_libraries_preflight(out, "prod", "actual", "auto", "EDL", libraries, apply_mode)
        return out.getvalue()

    def test_reports_name_matched_and_content_bearing_counts_separately(self):
        libraries = [
            {"slug": "A", "title": "A lib", "components": [
                {"usage_key": "lb:EDL:A:lti_consumer:one", "display_name": "One",
                 "activityid": "act-1", "status": "pending", "error": None},
            ]},
            {"slug": "B", "title": "B lib", "components": []},
        ]
        output = self._print(libraries)
        self.assertIn("matched by slug/org/title/description: 2", output)
        self.assertIn("containing lti_consumer components:    1", output)

    def test_flags_duplicate_activityids(self):
        # IAA2026 really does hold two components per activity for two activities.
        libraries = [{"slug": "IAA2026", "title": "I-Auto", "components": [
            {"usage_key": "lb:EDL:IAA2026:lti_consumer:a", "display_name": "Capstone",
             "activityid": "dup", "status": "pending", "error": None},
            {"usage_key": "lb:EDL:IAA2026:lti_consumer:b", "display_name": "Capstone",
             "activityid": "dup", "status": "pending", "error": None},
        ]}]
        output = self._print(libraries)
        self.assertIn("DUPLICATE", output)
        self.assertIn("activityid=dup", output)

    def test_breaks_the_total_down_by_status(self):
        libraries = [{"slug": "A", "title": "A", "components": [
            {"usage_key": "lb:EDL:A:lti_consumer:1", "display_name": "One",
             "activityid": "a1", "status": "pending", "error": None},
            {"usage_key": "lb:EDL:A:lti_consumer:2", "display_name": "Two",
             "activityid": "a2", "status": "skipped_already_migrated", "error": None},
            {"usage_key": "lb:EDL:A:lti_consumer:3", "display_name": "Three",
             "activityid": None, "status": "failed", "error": "no activityid"},
        ]}]
        output = self._print(libraries)
        self.assertIn("3 lti_consumer components", output)
        self.assertIn("1  pending", output)
        self.assertIn("1  skipped_already_migrated", output)
        self.assertIn("1  failed", output)

    def test_states_which_mode_it_is_running_in(self):
        self.assertIn("DRY-RUN", self._print([], apply_mode=False))
        self.assertIn("APPLY", self._print([], apply_mode=True))


class ActivityIdTableTest(TestCase):
    """The activityid -> LTI 1.1 library block table read alongside Phase 2's matches."""

    def _print(self, entries):
        out = io.StringIO()
        report.print_activityid_table(out, entries)
        return out.getvalue()

    def test_lists_only_pending_components(self):
        entries = [
            {"usage_key": "lb:EDL:A:lti_consumer:one", "display_name": "One",
             "activityid": "act-1", "status": "pending"},
            {"usage_key": "lb:EDL:A:lti_consumer:two", "display_name": "Two",
             "activityid": "act-2", "status": "skipped_already_migrated"},
        ]
        output = self._print(entries)
        self.assertIn("act-1", output)
        self.assertIn("lb:EDL:A:lti_consumer:one", output)
        self.assertNotIn("act-2", output)

    def test_reports_when_nothing_is_pending(self):
        output = self._print([])
        self.assertIn("none", output)


class CoursesPreflightTest(TestCase):
    """The phase 2 pre-flight names conflicts, skips and unreadable courses."""

    def _print(self, courses, unreadable=(), apply_mode=True):
        out = io.StringIO()
        report.print_courses_preflight(out, "prod", "actual", courses, list(unreadable), apply_mode)
        return out.getvalue()

    def test_describes_a_pending_block(self):
        courses = {"course-v1:EDL+CAA+2026": [{
            "block_location": "block-v1:EDL+CAA+2026+type@lti_consumer+block@abc",
            "upstream_short": "assessment-i", "activityid": "act-1",
            "existing_activityid": None, "conflict": False, "already_migrated": False,
        }]}
        output = self._print(courses)
        self.assertIn("accept changes -> DL content (activityid=act-1) -> publish unit", output)
        self.assertIn("1  pending", output)

    def test_flags_a_conflicting_existing_content_item(self):
        courses = {"course-v1:EDL+CAA+2026": [{
            "block_location": "block-v1:EDL+CAA+2026+type@lti_consumer+block@abc",
            "upstream_short": "assessment-i", "activityid": "act-1",
            "existing_activityid": "other", "conflict": True, "already_migrated": False,
        }]}
        output = self._print(courses)
        self.assertIn("CONFLICT", output)
        self.assertIn("1  skipped_conflict", output)

    def test_reports_unreadable_courses_outside_the_totals(self):
        output = self._print({}, unreadable=["course-v1:X+Y+Z: boom"])
        self.assertIn("NOT covered by the totals", output)
        self.assertIn("course-v1:X+Y+Z: boom", output)

    def test_warns_about_unit_level_publishing_and_has_score(self):
        output = self._print({})
        self.assertIn("unpublished draft edit", output)
        self.assertIn("has_score", output)


class DirectCoursesPreflightTest(TestCase):
    """The direct-course pre-flight names conflicts, zero-block courses, and unreadable ones."""

    def _print(self, course_keys, courses, unreadable=(), apply_mode=True):
        out = io.StringIO()
        report.print_direct_courses_preflight(out, "prod", "actual", course_keys, courses, list(unreadable),
                                               apply_mode)
        return out.getvalue()

    def test_describes_a_pending_block(self):
        courses = {"course-v1:LSU+EAA+LSU_parent_2607": [{
            "block_location": "block-v1:LSU+EAA+LSU_parent_2607+type@lti_consumer+block@abc",
            "activityid": "act-1", "existing_activityid": None,
            "conflict": False, "already_migrated": False, "status": "pending", "error": None,
        }]}
        output = self._print(["course-v1:LSU+EAA+LSU_parent_2607"], courses)
        self.assertIn("activityid=act-1  flip fields -> DL content -> publish unit", output)
        self.assertIn("1  pending", output)

    def test_flags_a_conflicting_existing_content_item(self):
        courses = {"course-v1:LSU+EAA+LSU_parent_2607": [{
            "block_location": "block-v1:LSU+EAA+LSU_parent_2607+type@lti_consumer+block@abc",
            "activityid": "act-1", "existing_activityid": "other",
            "conflict": True, "already_migrated": False, "status": "skipped_conflict",
            "error": "custom_parameters declares act-1, existing DL content points at other -- left untouched",
        }]}
        output = self._print(["course-v1:LSU+EAA+LSU_parent_2607"], courses)
        self.assertIn("CONFLICT", output)
        self.assertIn("1  skipped_conflict", output)

    def test_flags_a_course_with_zero_lti_consumer_blocks(self):
        # A course in scope that simply has no lti_consumer blocks must not
        # silently vanish from the report -- it is a real, checkable gap.
        output = self._print(["course-v1:LSU+EAA+LSU_parent_2607", "course-v1:LSU+CAA+2607-LSU-CAA-Summer2026"], {})
        self.assertIn("2 course(s) in scope had zero lti_consumer blocks", output)
        self.assertIn("course-v1:LSU+EAA+LSU_parent_2607", output)

    def test_a_course_with_blocks_is_not_also_reported_as_zero_blocks(self):
        courses = {"course-v1:LSU+EAA+LSU_parent_2607": [{
            "block_location": "block-v1:LSU+EAA+LSU_parent_2607+type@lti_consumer+block@abc",
            "activityid": "act-1", "existing_activityid": None,
            "conflict": False, "already_migrated": False, "status": "pending", "error": None,
        }]}
        output = self._print(["course-v1:LSU+EAA+LSU_parent_2607"], courses)
        self.assertNotIn("zero lti_consumer blocks", output)

    def test_reports_unreadable_courses_outside_the_totals_and_not_as_zero_blocks(self):
        # A course key containing its own ":" must not be mistaken for one
        # with zero blocks just because a naive prefix split gets confused.
        output = self._print(
            ["course-v1:LSU+EAA+LSU_parent_2607"], {},
            unreadable=["course-v1:LSU+EAA+LSU_parent_2607: mongo said no"],
        )
        self.assertIn("NOT covered by the totals", output)
        self.assertIn("mongo said no", output)
        self.assertNotIn("zero lti_consumer blocks", output)

    def test_states_which_mode_it_is_running_in(self):
        self.assertIn("DRY-RUN", self._print([], {}, apply_mode=False))
        self.assertIn("APPLY", self._print([], {}, apply_mode=True))


class VerificationReportTest(TestCase):
    """Failures are always listed individually, never just counted."""

    def test_counts_and_lists_failures(self):
        out = io.StringIO()
        report.print_verification_report(out, "Courses", [
            {"key": "a", "passed": True, "detail": "ok"},
            {"key": "b", "passed": False, "detail": "published lti_version='lti_1p1'"},
        ])
        output = out.getvalue()
        self.assertIn("Checked: 2", output)
        self.assertIn("PASS  1", output)
        self.assertIn("FAIL  1", output)
        self.assertIn("published lti_version='lti_1p1'", output)


class PrintStateTest(TestCase):
    """State is echoed to stdout because the state file is on ephemeral storage."""

    def test_dumps_recoverable_json(self):
        out = io.StringIO()
        state = state_module.new_state("prod", "actual")
        state["libraries"] = [{"usage_key": "lb:EDL:A:lti_consumer:1", "activityid": "act-1"}]
        report.print_state(out, state)
        body = out.getvalue()
        self.assertIn("act-1", body)
        # The dumped block must be valid JSON on its own, or it is not a backup.
        payload = body[body.index("{"):]
        self.assertEqual(json.loads(payload)["libraries"][0]["activityid"], "act-1")
