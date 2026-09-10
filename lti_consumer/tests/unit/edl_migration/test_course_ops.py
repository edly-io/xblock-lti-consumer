"""
Tests for phase 2 (the course side).

The most important cases here are the ones that decide whether a block gets
repaired or skipped, and the ones that prove nothing is published unless a
fresh read of both the fields and the content item matches intent.
"""
from unittest import mock

from django.test import TestCase
from opaque_keys.edx.keys import UsageKey

from cms.lib.xblock.upstream_sync import UpstreamLinkException  # pylint: disable=import-error
from lti_consumer.edl_migration import content_items, course_ops
from lti_consumer.models import LtiConfiguration, LtiDlContentItem

PROD_CFG = {"muzzylane_base": "https://author.muzzylane.com", "lti_store_slug": "author13"}

ACTIVITY = "67fc3b02-1111-2222-3333-444455556666"
UPSTREAM = "lb:EDL:CAA2026:lti_consumer:assessment-identify-patterns-cb79be"
COURSE_ID = "course-v1:EDL+CAA+2026"
BLOCK = "block-v1:EDL+CAA+2026+type@lti_consumer+block@abc123"
VERTICAL = "block-v1:EDL+CAA+2026+type@vertical+block@unit1"


def _library_entry(status="done"):
    """Build a phase 1 state entry for the library component the course blocks link to."""
    return {
        "usage_key": UPSTREAM,
        "activityid": ACTIVITY,
        "status": status,
        "dl_content": content_items.build_dl_content("ASSESSMENT: X", ACTIVITY, PROD_CFG),
    }


def _course_block(config_type="new", lti_version="lti_1p1", upstream=UPSTREAM):
    """Build a course-side lti_consumer block double."""
    block = mock.Mock()
    block.location = UsageKey.from_string(BLOCK)
    block.upstream = upstream
    block.config_type = config_type
    block.lti_version = lti_version
    return block


class DiscoverCourseBlocksTest(TestCase):
    """Step 5: the join key, and the decision of what still needs work."""

    def _discover(self, entries, blocks, get_items_raises=None):
        """Run discovery against a modulestore double holding `blocks` in one course."""
        store = mock.MagicMock()
        summary = mock.Mock()
        summary.id = UsageKey.from_string(BLOCK).course_key
        store.get_course_summaries.return_value = [summary]
        if get_items_raises:
            store.get_items.side_effect = get_items_raises
        else:
            store.get_items.return_value = blocks
        with mock.patch.object(course_ops, "modulestore", return_value=store):
            return course_ops.discover_course_blocks(entries)

    def test_matches_a_block_by_its_upstream_key(self):
        courses, unreadable = self._discover([_library_entry()], [_course_block()])
        self.assertEqual(unreadable, [])
        self.assertEqual(list(courses), [COURSE_ID])
        self.assertEqual(courses[COURSE_ID][0]["activityid"], ACTIVITY)

    def test_ignores_blocks_with_no_or_unrelated_upstream(self):
        courses, _ = self._discover(
            [_library_entry()],
            [_course_block(upstream=None), _course_block(upstream="lb:EDL:OTHER:lti_consumer:z")],
        )
        self.assertEqual(courses, {})

    def test_ignores_library_entries_with_no_content_to_propagate(self):
        entry = dict(_library_entry(), dl_content=None)
        courses, _ = self._discover([entry], [_course_block()])
        self.assertEqual(courses, {})

    def test_ignores_library_entries_that_failed(self):
        courses, _ = self._discover([_library_entry(status="failed_verification")], [_course_block()])
        self.assertEqual(courses, {})

    def test_a_block_on_1p3_with_matching_content_is_already_migrated(self):
        content_items.write_content_item(
            UsageKey.from_string(BLOCK), _library_entry()["dl_content"], PROD_CFG,
        )
        courses, _ = self._discover(
            [_library_entry()], [_course_block(config_type="external", lti_version="lti_1p3")],
        )
        self.assertTrue(courses[COURSE_ID][0]["already_migrated"])
        self.assertFalse(courses[COURSE_ID][0]["conflict"])

    def test_a_block_on_1p3_with_no_content_is_not_skipped(self):
        # This is the whole point: fields alone are not "done". A block left in
        # this state by a crashed run, or by someone clicking Accept Changes in
        # Studio, must be repaired rather than skipped forever.
        courses, _ = self._discover(
            [_library_entry()], [_course_block(config_type="external", lti_version="lti_1p3")],
        )
        entry = courses[COURSE_ID][0]
        self.assertFalse(entry["already_migrated"])
        self.assertFalse(entry["conflict"])
        self.assertTrue(entry["fields_migrated"])

    def test_content_pointing_at_a_different_activity_is_a_conflict(self):
        # Possibly a real picker-produced selection: report, never overwrite.
        other = content_items.build_dl_content("Other", "some-other-activity", PROD_CFG)
        content_items.write_content_item(UsageKey.from_string(BLOCK), other, PROD_CFG)
        courses, _ = self._discover([_library_entry()], [_course_block()])
        entry = courses[COURSE_ID][0]
        self.assertTrue(entry["conflict"])
        self.assertFalse(entry["already_migrated"])
        self.assertEqual(entry["existing_activityid"], "some-other-activity")

    def test_a_course_that_cannot_be_listed_is_reported_not_dropped(self):
        courses, unreadable = self._discover(
            [_library_entry()], [], get_items_raises=RuntimeError("mongo said no"),
        )
        self.assertEqual(courses, {})
        self.assertEqual(len(unreadable), 1)
        self.assertIn("mongo said no", unreadable[0])


class MigrateCourseBlockTest(TestCase):
    """Steps 7-9: accept changes, write, verify, publish -- in that order."""

    def setUp(self):
        super().setUp()
        self.location = UsageKey.from_string(BLOCK)
        self.user = mock.Mock(id=7)
        self.entry = {
            "block_location": BLOCK,
            "upstream": UPSTREAM,
            "upstream_short": "assessment-i",
            "activityid": ACTIVITY,
            "existing_activityid": None,
            "dl_content": _library_entry()["dl_content"],
            "fields_migrated": False,
            "conflict": False,
            "already_migrated": False,
        }

    def _store(self, fresh_config_type="external", fresh_lti_version="lti_1p3", parent=True):
        """Build a modulestore double: first get_item is the draft, second the post-sync read."""
        store = mock.MagicMock()
        draft = _course_block()
        fresh = _course_block(config_type=fresh_config_type, lti_version=fresh_lti_version)
        if parent:
            parent_block = mock.Mock()
            parent_block.location = UsageKey.from_string(VERTICAL)
            fresh.get_parent.return_value = parent_block
        else:
            fresh.get_parent.return_value = None
        store.get_item.side_effect = [draft, fresh]
        return store

    def _migrate(self, store, sync_side_effect=None):
        """Run migrate_course_block against `store`, optionally failing the sync."""
        with mock.patch.object(course_ops, "modulestore", return_value=store), \
             mock.patch.object(course_ops, "sync_from_upstream_block",
                               side_effect=sync_side_effect) as sync:
            result = course_ops.migrate_course_block(self.entry, self.user, PROD_CFG)
        return result, sync, store

    def test_happy_path_syncs_writes_verifies_then_publishes(self):
        result, sync, store = self._migrate(self._store())
        self.assertEqual(result["status"], "done")
        self.assertEqual(sync.call_args.kwargs["user"], self.user)
        store.update_item.assert_called_once()
        self.assertEqual(content_items.read_activityid(self.location), ACTIVITY)
        store.publish.assert_called_once_with(UsageKey.from_string(VERTICAL), 7)

    def test_publishes_the_parent_unit_not_the_component(self):
        _, _, store = self._migrate(self._store())
        published = store.publish.call_args[0][0]
        self.assertEqual(published.block_type, "vertical")

    def test_a_conflict_is_skipped_without_touching_anything(self):
        self.entry["conflict"] = True
        self.entry["existing_activityid"] = "some-other-activity"
        store = mock.MagicMock()
        with mock.patch.object(course_ops, "modulestore", return_value=store), \
             mock.patch.object(course_ops, "sync_from_upstream_block") as sync:
            result = course_ops.migrate_course_block(self.entry, self.user, PROD_CFG)
        self.assertEqual(result["status"], "skipped_conflict")
        sync.assert_not_called()
        store.update_item.assert_not_called()
        store.publish.assert_not_called()

    def test_a_failed_accept_changes_writes_nothing_and_does_not_publish(self):
        store = self._store()
        result, _, store = self._migrate(store, sync_side_effect=UpstreamLinkException("no permission"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("accept changes failed", result["error"])
        store.update_item.assert_not_called()
        store.publish.assert_not_called()
        self.assertFalse(LtiDlContentItem.objects.exists())

    def test_does_not_publish_when_the_synced_fields_are_wrong(self):
        # e.g. phase 2 run against a library that was never actually migrated.
        result, _, store = self._migrate(self._store(fresh_lti_version="lti_1p1"))
        self.assertEqual(result["status"], "failed_verification")
        self.assertIn("publish skipped", result["error"])
        store.publish.assert_not_called()

    def test_does_not_publish_when_the_content_item_does_not_match(self):
        self.entry["activityid"] = "expected-something-else"
        result, _, store = self._migrate(self._store())
        self.assertEqual(result["status"], "failed_verification")
        store.publish.assert_not_called()

    def test_does_not_publish_when_the_parent_unit_cannot_be_resolved(self):
        result, _, store = self._migrate(self._store(parent=False))
        self.assertEqual(result["status"], "failed_verification")
        self.assertIn("parent unit", result["error"])
        store.publish.assert_not_called()

    def test_configuration_is_left_pointing_at_the_external_tool(self):
        self._migrate(self._store())
        config = LtiConfiguration.objects.get(location=self.location)
        self.assertEqual(config.version, LtiConfiguration.LTI_1P3)
        self.assertEqual(config.config_store, LtiConfiguration.CONFIG_EXTERNAL)
        self.assertEqual(config.external_id, "lti_store:author13")
