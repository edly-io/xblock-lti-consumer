"""
Deep-linking content item construction, write and read-back.

Kept in its own module because this is the one part of the migration that
touches nothing but this plugin's own models -- no edx-platform imports at
all. That means it is identical for a library component (an ``lb:`` location)
and for each course-side copy (a ``block-v1:`` location), and it can be unit
tested without edx-platform installed.

Why the content item can be constructed rather than picked in a browser (all
verified live against real prod/stage blocks):

* ``LtiDlContentItem.attributes`` is an opaque, unvalidated JSON blob --
  ``LtiDlLtiResourceLinkSerializer.custom`` is a plain ``DictField`` -- so the
  selection a Muzzy Lane picker round trip would have produced can simply be
  written directly.
* A real deep-link round trip against a brand-new block and a never-used
  activity behaved identically whether or not Muzzy Lane's
  ``integration_launch_hash`` was present, once the unit was actually
  published. Every "Assignment is currently being set up" failure seen during
  investigation was a missing publish, not a missing hash.
* Every currently-LTI-1.1 block already carries ``activityid=<uuid>`` in its
  ``custom_parameters``, and that UUID is the identity Muzzy Lane routes on.
"""
import re

from lti_consumer.models import LtiConfiguration, LtiDlContentItem

# Content titles in the libraries are prefixed "ASSESSMENT: " for authors'
# benefit; the deep-linking title should not carry that. Cosmetic only --
# Muzzy Lane routes on activityid, never on title.
ASSESSMENT_PREFIX_RE = re.compile(r"^assessment:\s*", re.IGNORECASE)


def strip_title_prefix(display_name):
    """Return `display_name` without a leading, case-insensitive "ASSESSMENT: "."""
    return ASSESSMENT_PREFIX_RE.sub("", display_name or "").strip()


def build_dl_content(display_name, activityid, env_cfg):
    """
    Build the deep-linking content item body for one Muzzy Lane activity.

    Mirrors the shape of every real captured deep-link payload, minus
    `integration_launch_hash` (proven unnecessary -- see module docstring).
    """
    return {
        "url": f"{env_cfg['muzzylane_base']}/authoring/oidc/launch",
        "title": strip_title_prefix(display_name),
        "custom": {"activityid": activityid, "post_category_scores": True},
        "lineItem": {"scoreMaximum": 100.0, "tag": "grade"},
    }


def write_content_item(location, dl_content, env_cfg):
    """
    Point `location`'s LtiConfiguration at the external tool and replace its
    deep-linking content with `dl_content`.

    `version`/`config_store`/`external_id` are set explicitly rather than left
    to model defaults: a bare `get_or_create` would create the row as
    `lti_1p1` / `CONFIG_ON_XBLOCK`, which only self-heals the next time the
    block is rendered. The values written here match what a real deep-link
    round trip leaves behind.

    Replacing rather than appending matches `deep_linking_response_endpoint`,
    which also clears prior items for the configuration before inserting.
    """
    config, _ = LtiConfiguration.objects.get_or_create(location=location)
    config.version = LtiConfiguration.LTI_1P3
    config.config_store = LtiConfiguration.CONFIG_EXTERNAL
    config.external_id = f"lti_store:{env_cfg['lti_store_slug']}"
    config.save()

    LtiDlContentItem.objects.filter(lti_configuration=config).delete()
    LtiDlContentItem.objects.create(
        lti_configuration=config,
        content_type=LtiDlContentItem.LTI_RESOURCE_LINK,
        attributes=dl_content,
    )
    return config


def read_activityid(location):
    """
    Return the activityid currently stored in `location`'s deep-linking
    content, or None if there is no configuration, no content item, or more
    than one (which a single ltiResourceLink launch would not produce).

    Read fresh from the database on purpose: this is what the verification
    steps compare against, so it must not trust anything the run holds in
    memory.
    """
    config = LtiConfiguration.objects.filter(location=location).first()
    if config is None:
        return None
    items = list(LtiDlContentItem.objects.filter(lti_configuration=config))
    if len(items) != 1:
        return None
    return (items[0].attributes or {}).get("custom", {}).get("activityid")
