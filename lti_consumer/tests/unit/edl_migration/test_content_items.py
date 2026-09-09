"""
Tests for content item construction and the database write/read-back.

Runs against the real LtiConfiguration/LtiDlContentItem models -- no stubbing
-- because this module imports nothing from edx-platform.
"""
from django.test import TestCase
from opaque_keys.edx.keys import UsageKey

from lti_consumer.edl_migration import content_items
from lti_consumer.models import LtiConfiguration, LtiDlContentItem

PROD_CFG = {"muzzylane_base": "https://author.muzzylane.com", "lti_store_slug": "author13"}
STAGE_CFG = {"muzzylane_base": "https://forge-stage.muzzylane.com", "lti_store_slug": "muzzylane"}

COURSE_BLOCK = "block-v1:EDL+CAA+2026+type@lti_consumer+block@abc123"
LIBRARY_BLOCK = "lb:EDL:CAA2026:lti_consumer:assessment-identify-patterns-cb79be"
ACTIVITY = "67fc3b02-1111-2222-3333-444455556666"


class StripTitlePrefixTest(TestCase):
    """The 'ASSESSMENT: ' prefix is stripped for the deep-linking title."""

    def test_strips_prefix_case_insensitively(self):
        self.assertEqual(content_items.strip_title_prefix("ASSESSMENT: Identify Patterns"), "Identify Patterns")
        self.assertEqual(content_items.strip_title_prefix("assessment:Identify Patterns"), "Identify Patterns")
        self.assertEqual(content_items.strip_title_prefix("Assessment:   Spaced"), "Spaced")

    def test_leaves_other_titles_alone(self):
        self.assertEqual(content_items.strip_title_prefix("Initiative Capstone - IT"), "Initiative Capstone - IT")

    def test_handles_empty_and_none(self):
        self.assertEqual(content_items.strip_title_prefix(""), "")
        self.assertEqual(content_items.strip_title_prefix(None), "")


class BuildDlContentTest(TestCase):
    """The constructed content item matches the shape of real captured payloads."""

    def test_prod_shape(self):
        self.assertEqual(
            content_items.build_dl_content("ASSESSMENT: Identify Patterns", ACTIVITY, PROD_CFG),
            {
                "url": "https://author.muzzylane.com/authoring/oidc/launch",
                "title": "Identify Patterns",
                "custom": {"activityid": ACTIVITY, "post_category_scores": True},
                "lineItem": {"scoreMaximum": 100.0, "tag": "grade"},
            },
        )

    def test_stage_uses_the_stage_muzzylane_host(self):
        built = content_items.build_dl_content("Multiple Score Test", ACTIVITY, STAGE_CFG)
        self.assertEqual(built["url"], "https://forge-stage.muzzylane.com/authoring/oidc/launch")

    def test_carries_no_integration_launch_hash(self):
        # Proven unnecessary; writing one would tie the item to a stale session.
        built = content_items.build_dl_content("X", ACTIVITY, PROD_CFG)
        self.assertNotIn("integration_launch_hash", built["custom"])


class WriteContentItemTest(TestCase):
    """The write points the configuration at the external tool and replaces content."""

    def setUp(self):
        super().setUp()
        self.location = UsageKey.from_string(COURSE_BLOCK)
        self.dl_content = content_items.build_dl_content("ASSESSMENT: X", ACTIVITY, PROD_CFG)

    def test_sets_version_store_and_external_id_explicitly(self):
        # A bare get_or_create would leave these at lti_1p1 / CONFIG_ON_XBLOCK.
        config = content_items.write_content_item(self.location, self.dl_content, PROD_CFG)
        config.refresh_from_db()
        self.assertEqual(config.version, LtiConfiguration.LTI_1P3)
        self.assertEqual(config.config_store, LtiConfiguration.CONFIG_EXTERNAL)
        self.assertEqual(config.external_id, "lti_store:author13")

    def test_creates_a_single_lti_resource_link(self):
        config = content_items.write_content_item(self.location, self.dl_content, PROD_CFG)
        items = LtiDlContentItem.objects.filter(lti_configuration=config)
        self.assertEqual(items.count(), 1)
        self.assertEqual(items.get().content_type, LtiDlContentItem.LTI_RESOURCE_LINK)
        self.assertEqual(items.get().attributes, self.dl_content)

    def test_replaces_prior_content_rather_than_appending(self):
        content_items.write_content_item(self.location, self.dl_content, PROD_CFG)
        second = content_items.build_dl_content("ASSESSMENT: Y", "other-activity", PROD_CFG)
        config = content_items.write_content_item(self.location, second, PROD_CFG)
        items = LtiDlContentItem.objects.filter(lti_configuration=config)
        self.assertEqual(items.count(), 1)
        self.assertEqual(items.get().attributes["custom"]["activityid"], "other-activity")

    def test_reuses_an_existing_configuration_row(self):
        existing = LtiConfiguration.objects.create(location=self.location)
        config = content_items.write_content_item(self.location, self.dl_content, PROD_CFG)
        self.assertEqual(config.pk, existing.pk)
        self.assertEqual(LtiConfiguration.objects.filter(location=self.location).count(), 1)

    def test_works_for_a_library_component_location(self):
        # Library components own their own row, keyed by an lb: usage key.
        library_location = UsageKey.from_string(LIBRARY_BLOCK)
        content_items.write_content_item(library_location, self.dl_content, STAGE_CFG)
        self.assertEqual(content_items.read_activityid(library_location), ACTIVITY)


class ReadActivityidTest(TestCase):
    """read_activityid is what every verification step compares against."""

    def setUp(self):
        super().setUp()
        self.location = UsageKey.from_string(COURSE_BLOCK)

    def test_returns_none_with_no_configuration(self):
        self.assertIsNone(content_items.read_activityid(self.location))

    def test_returns_none_with_a_configuration_but_no_content(self):
        # This is the "Assignment is currently being set up" shape.
        LtiConfiguration.objects.create(location=self.location)
        self.assertIsNone(content_items.read_activityid(self.location))

    def test_returns_the_activityid_after_a_write(self):
        dl_content = content_items.build_dl_content("X", ACTIVITY, PROD_CFG)
        content_items.write_content_item(self.location, dl_content, PROD_CFG)
        self.assertEqual(content_items.read_activityid(self.location), ACTIVITY)

    def test_returns_none_when_more_than_one_item_exists(self):
        config = LtiConfiguration.objects.create(location=self.location)
        for _ in range(2):
            LtiDlContentItem.objects.create(
                lti_configuration=config,
                content_type=LtiDlContentItem.LTI_RESOURCE_LINK,
                attributes={"custom": {"activityid": ACTIVITY}},
            )
        self.assertIsNone(content_items.read_activityid(self.location))

    def test_survives_content_with_no_custom_block(self):
        config = LtiConfiguration.objects.create(location=self.location)
        LtiDlContentItem.objects.create(
            lti_configuration=config,
            content_type=LtiDlContentItem.LTI_RESOURCE_LINK,
            attributes={},
        )
        self.assertIsNone(content_items.read_activityid(self.location))
