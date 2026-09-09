"""
Tests for phase 1 (the library side).

The edx-platform functions these exercise are the stubs installed by this test
package's __init__; each test supplies the return values it needs. The
assertions that matter most are the negative ones -- that nothing is written or
published when it should not be.
"""
from unittest import mock

from django.test import TestCase
from opaque_keys.edx.keys import UsageKey

from lti_consumer.edl_migration import content_items, library_ops

PROD_CFG = {"muzzylane_base": "https://author.muzzylane.com", "lti_store_slug": "author13"}

ACTIVITY = "67fc3b02-1111-2222-3333-444455556666"
LIBRARY_BLOCK = "lb:EDL:CAA2026:lti_consumer:assessment-identify-patterns-cb79be"

# Real OLX shape pulled from lib:EDL:Test001 -- custom_parameters is a JSON
# array of strings and there are sibling attributes that must survive a write.
OLX_LTI_1P1 = (
    '<lti_consumer xblock-family="xblock.v1" '
    'custom_parameters="[&quot;activityid=67fc3b02-1111-2222-3333-444455556666&quot;, '
    '&quot;activityversion=1.1.27&quot;]" '
    'display_name="ASSESSMENT: Identify Patterns" has_score="true" '
    'launch_target="new_window" lti_id="muzzy"/>'
)
OLX_MIGRATED = (
    '<lti_consumer xblock-family="xblock.v1" custom_parameters="[]" '
    'display_name="ASSESSMENT: Identify Patterns" has_score="true" '
    'launch_target="new_window" lti_id="muzzy" config_type="external" '
    'external_config="lti_store:author13" lti_version="lti_1p3"/>'
)


class ExtractActivityidTest(TestCase):
    """Anything unreadable returns None rather than raising."""

    def test_extracts_from_a_real_custom_parameters_array(self):
        params = '["activityid=abc-123", "activityversion=1.1.27"]'
        self.assertEqual(library_ops.extract_activityid(params), "abc-123")

    def test_returns_none_when_absent(self):
        self.assertIsNone(library_ops.extract_activityid('["activityversion=1.1.27"]'))

    def test_returns_none_for_empty_missing_and_malformed_input(self):
        for value in ("[]", "", None, "not json", '"a string"', "42"):
            self.assertIsNone(library_ops.extract_activityid(value))

    def test_returns_none_for_an_empty_activityid_value(self):
        self.assertIsNone(library_ops.extract_activityid('["activityid="]'))

    def test_survives_non_string_entries(self):
        self.assertEqual(library_ops.extract_activityid('[1, null, "activityid=abc"]'), "abc")


class InspectComponentTest(TestCase):
    """Step 2 is strictly read-only and decides each component's status."""

    def setUp(self):
        super().setUp()
        self.usage_key = UsageKey.from_string(LIBRARY_BLOCK)
        patcher = mock.patch.object(library_ops, "library_component_usage_key", return_value=self.usage_key)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _inspect(self, olx):
        with mock.patch.object(library_ops, "get_block_draft_olx", return_value=olx):
            return library_ops.inspect_component("lib_key", "component", PROD_CFG)

    def test_an_lti_1p1_component_is_pending_with_content_built(self):
        result = self._inspect(OLX_LTI_1P1)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["activityid"], ACTIVITY)
        self.assertEqual(
            result["dl_content"],
            content_items.build_dl_content("ASSESSMENT: Identify Patterns", ACTIVITY, PROD_CFG),
        )

    def test_writes_nothing(self):
        with mock.patch.object(library_ops, "set_library_block_olx") as write, \
             mock.patch.object(library_ops, "publish_component_changes") as publish:
            self._inspect(OLX_LTI_1P1)
        write.assert_not_called()
        publish.assert_not_called()

    def test_a_component_with_no_activityid_anywhere_fails(self):
        result = self._inspect('<lti_consumer display_name="X" custom_parameters="[]"/>')
        self.assertEqual(result["status"], "failed")
        self.assertIn("no activityid", result["error"])
        self.assertIsNone(result["dl_content"])

    def test_migrated_fields_with_matching_content_is_skipped(self):
        content_items.write_content_item(
            self.usage_key, content_items.build_dl_content("X", ACTIVITY, PROD_CFG), PROD_CFG,
        )
        result = self._inspect(OLX_MIGRATED)
        self.assertEqual(result["status"], "skipped_already_migrated")

    def test_migrated_fields_with_no_content_is_never_reported_as_done(self):
        # Fields say 1.3 but there is no deep-linking content: this is the
        # "Assignment is currently being set up" state. It must not be treated
        # as already migrated. With custom_parameters already cleared there is
        # nothing left to rebuild from, so it fails loudly rather than
        # silently doing the wrong thing.
        result = self._inspect(OLX_MIGRATED)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["already_migrated"])
        self.assertTrue(result["fields_migrated"])

    def test_migrated_fields_with_wrong_content_is_repaired_not_skipped(self):
        # Same broken shape, but here the LTI 1.1 custom_parameters are still
        # present, so the right activityid is known and the component can be
        # repaired.
        olx = OLX_LTI_1P1.replace(
            'lti_id="muzzy"',
            'lti_id="muzzy" config_type="external" lti_version="lti_1p3"',
        )
        result = self._inspect(olx)
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["already_migrated"])
        self.assertEqual(result["activityid"], ACTIVITY)

    def test_content_pointing_at_a_different_activity_is_a_conflict(self):
        # Possibly a deliberate picker-produced selection: report, never
        # overwrite it with whatever custom_parameters happens to say.
        content_items.write_content_item(
            self.usage_key,
            content_items.build_dl_content("Other", "some-other-activity", PROD_CFG),
            PROD_CFG,
        )
        result = self._inspect(OLX_LTI_1P1)
        self.assertEqual(result["status"], "skipped_conflict")
        self.assertTrue(result["conflict"])
        self.assertIsNone(result["dl_content"])
        self.assertEqual(result["existing_activityid"], "some-other-activity")

    def test_a_conflicting_component_is_never_written(self):
        content_items.write_content_item(
            self.usage_key,
            content_items.build_dl_content("Other", "some-other-activity", PROD_CFG),
            PROD_CFG,
        )
        with mock.patch.object(library_ops, "set_library_block_olx") as write, \
             mock.patch.object(library_ops, "publish_component_changes") as publish:
            self._inspect(OLX_LTI_1P1)
        write.assert_not_called()
        publish.assert_not_called()

    def test_recovers_the_activityid_from_existing_content_after_migration(self):
        # custom_parameters is cleared by migration, so a re-run has to read the
        # activityid back out of the content item. This is what makes the
        # command safe to run twice.
        content_items.write_content_item(
            self.usage_key, content_items.build_dl_content("X", ACTIVITY, PROD_CFG), PROD_CFG,
        )
        result = self._inspect(OLX_MIGRATED)
        self.assertEqual(result["activityid"], ACTIVITY)


class MigrateComponentTest(TestCase):
    """Step 3-5: writes happen in order, and the publish is gated on read-back."""

    def setUp(self):
        super().setUp()
        self.usage_key = UsageKey.from_string(LIBRARY_BLOCK)
        self.entry = {
            "usage_key": LIBRARY_BLOCK,
            "display_name": "ASSESSMENT: Identify Patterns",
            "activityid": ACTIVITY,
            "already_migrated": False,
            "status": "pending",
            "dl_content": content_items.build_dl_content("ASSESSMENT: Identify Patterns", ACTIVITY, PROD_CFG),
            "error": None,
        }
        self.user = mock.Mock(id=7)

    def _migrate(self, olx_sequence):
        """Run migrate_component with get_block_draft_olx returning each OLX in turn."""
        with mock.patch.object(library_ops, "get_block_draft_olx", side_effect=olx_sequence), \
             mock.patch.object(library_ops, "set_library_block_olx") as write, \
             mock.patch.object(library_ops, "publish_component_changes") as publish:
            result = library_ops.migrate_component(self.entry, self.user, PROD_CFG)
        return result, write, publish

    def test_happy_path_writes_verifies_then_publishes(self):
        result, write, publish = self._migrate([OLX_LTI_1P1, OLX_MIGRATED])
        self.assertEqual(result["status"], "done")
        write.assert_called_once()
        publish.assert_called_once_with(self.usage_key, 7)

    def test_written_olx_sets_all_four_migration_attributes(self):
        _, write, _ = self._migrate([OLX_LTI_1P1, OLX_MIGRATED])
        written = write.call_args[0][1]
        self.assertIn('config_type="external"', written)
        self.assertIn('external_config="lti_store:author13"', written)
        self.assertIn('lti_version="lti_1p3"', written)
        self.assertIn('custom_parameters="[]"', written)

    def test_written_olx_preserves_unrelated_attributes(self):
        # An OLX round trip must not quietly drop sibling attributes.
        _, write, _ = self._migrate([OLX_LTI_1P1, OLX_MIGRATED])
        written = write.call_args[0][1]
        self.assertIn('display_name="ASSESSMENT: Identify Patterns"', written)
        self.assertIn('has_score="true"', written)
        self.assertIn('launch_target="new_window"', written)
        self.assertIn('lti_id="muzzy"', written)

    def test_writes_the_deep_linking_content_item(self):
        self._migrate([OLX_LTI_1P1, OLX_MIGRATED])
        self.assertEqual(content_items.read_activityid(self.usage_key), ACTIVITY)

    def test_does_not_publish_when_the_olx_read_back_does_not_match(self):
        # The write silently did not take: leave it as an unpublished draft.
        result, write, publish = self._migrate([OLX_LTI_1P1, OLX_LTI_1P1])
        self.assertEqual(result["status"], "failed_verification")
        self.assertIn("publish skipped", result["error"])
        write.assert_called_once()
        publish.assert_not_called()

    def test_does_not_publish_when_the_content_item_does_not_match(self):
        self.entry["dl_content"] = content_items.build_dl_content("X", "a-different-activity", PROD_CFG)
        result, _, publish = self._migrate([OLX_LTI_1P1, OLX_MIGRATED])
        self.assertEqual(result["status"], "failed_verification")
        publish.assert_not_called()


class DiscoverLibrariesTest(TestCase):
    """Step 1 keeps content-free libraries visible instead of narrowing them away."""

    def _library(self, slug, title="A library"):
        lib = mock.Mock(slug=slug)
        lib.org.short_name = "EDL"
        lib.learning_package.title = title
        return lib

    def test_returns_libraries_with_no_lti_components_too(self):
        # Searching "test" on stage matches 7 libraries but only 1 holds LTI
        # content; the pre-flight needs both numbers, so discovery must not
        # filter the empty ones out.
        with mock.patch.object(library_ops, "get_libraries_for_user",
                               return_value=[self._library("A"), self._library("B")]), \
             mock.patch.object(library_ops, "get_library_components", side_effect=[["c1"], []]):
            found = library_ops.discover_libraries(mock.Mock(), "test")
        self.assertEqual([lib["slug"] for lib in found], ["A", "B"])
        self.assertEqual([len(lib["components"]) for lib in found], [1, 0])

    def test_library_scope_narrows_to_one_slug(self):
        with mock.patch.object(library_ops, "get_libraries_for_user",
                               return_value=[self._library("A"), self._library("B")]), \
             mock.patch.object(library_ops, "get_library_components", return_value=[]):
            found = library_ops.discover_libraries(mock.Mock(), "test", library_scope="B")
        self.assertEqual([lib["slug"] for lib in found], ["B"])

    def test_passes_org_and_the_mode_search_term_through(self):
        with mock.patch.object(library_ops, "get_libraries_for_user", return_value=[]) as search, \
             mock.patch.object(library_ops, "get_library_components", return_value=[]):
            library_ops.discover_libraries(mock.Mock(), "actual", org="EDL")
        self.assertEqual(search.call_args.kwargs, {"org": "EDL", "text_search": "auto"})
