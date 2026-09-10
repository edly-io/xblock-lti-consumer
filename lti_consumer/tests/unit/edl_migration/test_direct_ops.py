"""
Tests for the direct-course migration (lti13_migrate_direct's single phase).

Mirrors test_course_ops.py's structure, since the two flows share the same
discover -> gate -> migrate -> verify shape. The differences that matter here:
there is no upstream library component (so no "accept changes" step, and
custom_parameters comes back as an already-parsed list, not an OLX string),
and discovery is driven by a fixed course key list rather than a scan of
every course in the modulestore.
"""
from unittest import mock

from django.test import TestCase
from opaque_keys.edx.keys import UsageKey

from lti_consumer.edl_migration import content_items, direct_ops
from lti_consumer.models import LtiConfiguration, LtiDlContentItem

PROD_CFG = {"muzzylane_base": "https://author.muzzylane.com", "lti_store_slug": "author13"}

ACTIVITY = "67fc3b02-1111-2222-3333-444455556666"
COURSE_ID = "course-v1:LSU+EAA+LSU_parent_2607"
BLOCK = "block-v1:LSU+EAA+LSU_parent_2607+type@lti_consumer+block@abc123"
VERTICAL = "block-v1:LSU+EAA+LSU_parent_2607+type@vertical+block@unit1"


def _course_block(config_type="new", lti_version="lti_1p1", custom_parameters=None, display_name="ASSESSMENT: X",
                   external_config="lti_store:author13"):
    """Build a course-side lti_consumer block double, configured directly (no upstream)."""
    block = mock.Mock(spec=["location", "config_type", "lti_version", "custom_parameters",
                             "display_name", "get_parent", "external_config"])
    block.location = UsageKey.from_string(BLOCK)
    block.config_type = config_type
    block.lti_version = lti_version
    block.custom_parameters = custom_parameters if custom_parameters is not None else [f"activityid={ACTIVITY}"]
    block.display_name = display_name
    block.external_config = external_config
    return block


class DiscoverCourseBlocksTest(TestCase):
    """Discovery: what needs work, straight off a fixed course key list."""

    def _discover(self, blocks, get_items_raises=None):
        store = mock.MagicMock()
        if get_items_raises:
            store.get_items.side_effect = get_items_raises
        else:
            store.get_items.return_value = blocks
        with mock.patch.object(direct_ops, "modulestore", return_value=store):
            return direct_ops.discover_course_blocks([COURSE_ID], PROD_CFG)

    def test_finds_a_pending_block_and_builds_its_dl_content(self):
        courses, unreadable = self._discover([_course_block()])
        self.assertEqual(unreadable, [])
        self.assertEqual(list(courses), [COURSE_ID])
        entry = courses[COURSE_ID][0]
        self.assertEqual(entry["status"], "pending")
        self.assertEqual(entry["activityid"], ACTIVITY)
        self.assertIsNotNone(entry["dl_content"])

    def test_a_block_with_no_activityid_anywhere_fails(self):
        courses, _ = self._discover([_course_block(custom_parameters=[])])
        entry = courses[COURSE_ID][0]
        self.assertEqual(entry["status"], "failed")
        self.assertIsNone(entry["activityid"])

    def test_a_block_on_1p3_with_matching_content_is_already_migrated(self):
        dl_content = content_items.build_dl_content("ASSESSMENT: X", ACTIVITY, PROD_CFG)
        content_items.write_content_item(UsageKey.from_string(BLOCK), dl_content, PROD_CFG)
        courses, _ = self._discover(
            [_course_block(config_type="external", lti_version="lti_1p3", custom_parameters=[])],
        )
        entry = courses[COURSE_ID][0]
        self.assertTrue(entry["already_migrated"])
        self.assertEqual(entry["status"], "skipped_already_migrated")
        self.assertFalse(entry["conflict"])

    def test_a_stale_external_config_is_not_already_migrated(self):
        # config_type/lti_version alone are not enough: external_config is
        # written directly here (unlike course_ops.py, where it's inherited
        # via Accept Changes), so a block migrated once against a different
        # lti_store_slug must be repaired, not skipped as already done.
        dl_content = content_items.build_dl_content("ASSESSMENT: X", ACTIVITY, PROD_CFG)
        content_items.write_content_item(UsageKey.from_string(BLOCK), dl_content, PROD_CFG)
        courses, _ = self._discover([_course_block(
            config_type="external", lti_version="lti_1p3", custom_parameters=[],
            external_config="lti_store:muzzylane",
        )])
        entry = courses[COURSE_ID][0]
        self.assertFalse(entry["fields_migrated"])
        self.assertFalse(entry["already_migrated"])
        self.assertEqual(entry["status"], "pending")

    def test_a_block_on_1p3_with_no_content_is_not_skipped(self):
        # Fields alone are not "done" -- a block left in this state by a
        # crashed run must be repaired, not skipped forever.
        courses, _ = self._discover([_course_block(config_type="external", lti_version="lti_1p3")])
        entry = courses[COURSE_ID][0]
        self.assertFalse(entry["already_migrated"])
        self.assertEqual(entry["status"], "pending")
        self.assertTrue(entry["fields_migrated"])

    def test_content_pointing_at_a_different_activity_is_a_conflict(self):
        other = content_items.build_dl_content("Other", "some-other-activity", PROD_CFG)
        content_items.write_content_item(UsageKey.from_string(BLOCK), other, PROD_CFG)
        courses, _ = self._discover([_course_block()])
        entry = courses[COURSE_ID][0]
        self.assertTrue(entry["conflict"])
        self.assertEqual(entry["status"], "skipped_conflict")
        self.assertEqual(entry["existing_activityid"], "some-other-activity")

    def test_a_course_that_cannot_be_listed_is_reported_not_dropped(self):
        courses, unreadable = self._discover([], get_items_raises=RuntimeError("mongo said no"))
        self.assertEqual(courses, {})
        self.assertEqual(len(unreadable), 1)
        self.assertIn("mongo said no", unreadable[0])

    def test_an_unparseable_course_key_is_reported_not_dropped(self):
        store = mock.MagicMock()
        with mock.patch.object(direct_ops, "modulestore", return_value=store):
            courses, unreadable = direct_ops.discover_course_blocks(["not-a-course-key"], PROD_CFG)
        self.assertEqual(courses, {})
        self.assertEqual(len(unreadable), 1)
        self.assertIn("not-a-course-key", unreadable[0])


class MigrateCourseBlockTest(TestCase):
    """Migrate in place: flip fields, write, verify, publish -- no accept-changes step."""

    def setUp(self):
        super().setUp()
        self.location = UsageKey.from_string(BLOCK)
        self.user = mock.Mock(id=7)
        self.entry = {
            "block_location": BLOCK,
            "display_name": "ASSESSMENT: X",
            "activityid": ACTIVITY,
            "existing_activityid": None,
            "dl_content": content_items.build_dl_content("ASSESSMENT: X", ACTIVITY, PROD_CFG),
            "fields_migrated": False,
            "conflict": False,
            "already_migrated": False,
            "status": "pending",
            "error": None,
        }

    def _store(self, fresh_config_type="external", fresh_lti_version="lti_1p3",
               fresh_external_config="lti_store:author13", parent=True):
        """Build a modulestore double: first get_item is the draft, second the post-write read."""
        store = mock.MagicMock()
        draft = _course_block()
        fresh = _course_block(config_type=fresh_config_type, lti_version=fresh_lti_version,
                               external_config=fresh_external_config)
        if parent:
            parent_block = mock.Mock()
            parent_block.location = UsageKey.from_string(VERTICAL)
            fresh.get_parent.return_value = parent_block
        else:
            fresh.get_parent.return_value = None
        store.get_item.side_effect = [draft, fresh]
        # Kept off the mock's own call-tracking attributes (side_effect is
        # consumed into an internal iterator once read) so tests can still
        # inspect the exact draft object migrate_course_block mutated.
        store.draft_block = draft
        return store

    def _migrate(self, store):
        with mock.patch.object(direct_ops, "modulestore", return_value=store):
            result = direct_ops.migrate_course_block(self.entry, self.user, PROD_CFG)
        return result, store

    def test_a_conflict_is_skipped_without_touching_anything(self):
        self.entry["conflict"] = True
        self.entry["existing_activityid"] = "some-other-activity"
        store = mock.MagicMock()
        with mock.patch.object(direct_ops, "modulestore", return_value=store):
            result = direct_ops.migrate_course_block(self.entry, self.user, PROD_CFG)
        self.assertEqual(result["status"], "skipped_conflict")
        self.assertIn("some-other-activity", result["error"])
        store.get_item.assert_not_called()
        store.update_item.assert_not_called()
        store.publish.assert_not_called()

    def test_happy_path_writes_verifies_then_publishes(self):
        result, store = self._migrate(self._store())
        self.assertEqual(result["status"], "done")
        store.update_item.assert_called_once()
        self.assertEqual(content_items.read_activityid(self.location), ACTIVITY)
        store.publish.assert_called_once_with(UsageKey.from_string(VERTICAL), 7)

    def test_publishes_the_parent_unit_not_the_component(self):
        _, store = self._migrate(self._store())
        published = store.publish.call_args[0][0]
        self.assertEqual(published.block_type, "vertical")

    def test_the_four_fields_are_set_on_the_draft_before_saving(self):
        store = self._store()
        self._migrate(store)
        draft = store.draft_block
        self.assertEqual(draft.config_type, "external")
        self.assertEqual(draft.lti_version, "lti_1p3")
        self.assertEqual(draft.external_config, "lti_store:author13")
        self.assertEqual(draft.custom_parameters, [])

    def test_does_not_publish_when_the_fields_did_not_actually_save(self):
        result, store = self._migrate(self._store(fresh_lti_version="lti_1p1"))
        self.assertEqual(result["status"], "failed_verification")
        self.assertIn("publish skipped", result["error"])
        store.publish.assert_not_called()

    def test_does_not_publish_when_external_config_did_not_actually_save(self):
        result, store = self._migrate(self._store(fresh_external_config="lti_store:muzzylane"))
        self.assertEqual(result["status"], "failed_verification")
        self.assertIn("publish skipped", result["error"])
        store.publish.assert_not_called()

    def test_does_not_publish_when_the_content_item_does_not_match(self):
        self.entry["activityid"] = "expected-something-else"
        result, store = self._migrate(self._store())
        self.assertEqual(result["status"], "failed_verification")
        store.publish.assert_not_called()

    def test_does_not_publish_when_the_parent_unit_cannot_be_resolved(self):
        result, store = self._migrate(self._store(parent=False))
        self.assertEqual(result["status"], "failed_verification")
        self.assertIn("parent unit", result["error"])
        store.publish.assert_not_called()

    def test_configuration_is_left_pointing_at_the_external_tool(self):
        self._migrate(self._store())
        config = LtiConfiguration.objects.get(location=self.location)
        self.assertEqual(config.version, LtiConfiguration.LTI_1P3)
        self.assertEqual(config.config_store, LtiConfiguration.CONFIG_EXTERNAL)
        self.assertEqual(config.external_id, "lti_store:author13")
        self.assertTrue(LtiDlContentItem.objects.filter(lti_configuration=config).exists())
