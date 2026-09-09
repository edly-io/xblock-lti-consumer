"""
Phase 1 -- the library side.

For each ``lti_consumer`` component in a matched content library: capture the
Muzzy Lane ``activityid`` out of its LTI 1.1 ``custom_parameters``, switch the
component to LTI 1.3 Reusable Configuration, write its deep-linking content
item, verify, and publish.

Library components are Learning-Core-backed (``LibraryUsageLocatorV2``), so
they are edited as **OLX text** rather than by XBlock field assignment -- hence
the lxml round trip below instead of ``store.update_item``. Real OLX from
``lib:EDL:Test001`` looks like this (``custom_parameters`` is a JSON array of
strings, XML-entity-escaped; lxml hands it back already unescaped)::

    <lti_consumer xblock-family="xblock.v1"
                  custom_parameters="[&quot;activityid=67fc3b02-...&quot;,
                                      &quot;activityversion=1.1.27&quot;]"
                  display_name="ASSESSMENT: Strengthen Relationships"
                  has_score="true" launch_target="new_window" lti_id="muzzy"/>

Phase 1 must fully complete -- including the publish -- before Phase 2 runs,
because Studio's sync loads the upstream at
``LatestVersion.PUBLISHED``: an unpublished library edit is invisible to the
course side.
"""
import json

from lxml import etree
from opaque_keys.edx.keys import UsageKey
from opaque_keys.edx.locator import LibraryLocatorV2

from openedx.core.djangoapps.content_libraries.api import (  # pylint: disable=import-error
    get_libraries_for_user,
    get_library_components,
    library_component_usage_key,
    publish_component_changes,
    set_library_block_olx,
)
from openedx.core.djangoapps.xblock.api import get_block_draft_olx  # pylint: disable=import-error

from lti_consumer.edl_migration import content_items
from lti_consumer.edl_migration.config import MODE_SEARCH_TERMS

# The four XBlock fields that constitute the LTI 1.1 -> 1.3 migration.
MIGRATED_CONFIG_TYPE = "external"
MIGRATED_LTI_VERSION = "lti_1p3"
CLEARED_CUSTOM_PARAMETERS = "[]"


def extract_activityid(custom_parameters_attr):
    """
    Pull ``activityid=<uuid>`` out of an OLX ``custom_parameters`` attribute.

    Returns None rather than raising for anything unexpected -- a component we
    cannot read an activityid from is reported as a failure by the caller, not
    silently migrated to a broken state.
    """
    try:
        params = json.loads(custom_parameters_attr or "[]")
    except (ValueError, TypeError):
        return None
    if not isinstance(params, list):
        return None
    for param in params:
        if isinstance(param, str) and param.startswith("activityid="):
            return param.split("=", 1)[1] or None
    return None


def read_olx_node(usage_key):
    """Read a library component's current draft OLX and parse it into an lxml node."""
    olx = get_block_draft_olx(usage_key)
    olx_bytes = olx.encode() if isinstance(olx, str) else olx
    # strip_cdata=False mirrors set_library_block_olx's own parser, so a round
    # trip cannot quietly drop CDATA if an lti_consumer block ever carries any.
    return etree.fromstring(olx_bytes, parser=etree.XMLParser(strip_cdata=False))


def discover_libraries(user, mode, org=None, library_scope=None):
    """
    Step 1: find the libraries in scope, and their lti_consumer components.

    NOTE on the search: `text_search` matches the library's **slug, org short
    name, title or description** -- not just its title. That is why searching
    "test" on stage returns 7 libraries when only 1 holds any LTI content, and
    it is why the pre-flight reports name-matched and content-bearing counts
    separately instead of quietly narrowing to the latter.

    Libraries with zero lti_consumer components are returned too, so the
    pre-flight can show that gap rather than hide it.
    """
    search_term = MODE_SEARCH_TERMS[mode]
    discovered = []
    for lib in get_libraries_for_user(user, org=org, text_search=search_term):
        if library_scope and lib.slug != library_scope:
            continue
        lib_key = LibraryLocatorV2(org=lib.org.short_name, slug=lib.slug)
        discovered.append({
            "lib_key": lib_key,
            "slug": lib.slug,
            "title": lib.learning_package.title if lib.learning_package else "?",
            "components": list(get_library_components(lib_key, block_types=["lti_consumer"])),
        })
    return discovered


def inspect_component(lib_key, component, env_cfg):
    """
    Step 2 (read-only): work out what this component needs, without writing.

    Everything the pre-flight summary and the state file need is computed here,
    so an operator sees the full plan -- including the activityid that will be
    wired to each block -- before authorising a single write.
    """
    usage_key = library_component_usage_key(lib_key, component)
    attrib = read_olx_node(usage_key).attrib

    display_name = attrib.get("display_name", "")
    fields_migrated = (
        attrib.get("config_type") == MIGRATED_CONFIG_TYPE and
        attrib.get("lti_version") == MIGRATED_LTI_VERSION
    )
    # Two possible sources for the activityid, in priority order:
    #   custom_parameters -- the LTI 1.1 ground truth, present until migration
    #                        clears it;
    #   the component's own DL content item -- where it survives afterwards,
    #                        which is what makes a re-run safe.
    declared = extract_activityid(attrib.get("custom_parameters"))
    existing = content_items.read_activityid(usage_key)
    activityid = declared or existing

    result = {
        "usage_key": str(usage_key),
        "display_name": display_name,
        "activityid": activityid,
        "existing_activityid": existing,
        "fields_migrated": fields_migrated,
        "already_migrated": bool(fields_migrated and existing and existing == activityid),
        "conflict": bool(declared and existing and declared != existing),
        "status": None,
        "dl_content": None,
        "error": None,
    }

    if not activityid:
        result["status"] = "failed"
        result["error"] = "no activityid in custom_parameters or existing DL content -- cannot construct content item"
        return result

    # Existing content pointing somewhere other than what custom_parameters
    # declares is not ours to resolve: it may be a real picker-produced
    # selection that someone made deliberately. Report it and leave it alone.
    if result["conflict"]:
        result["status"] = "skipped_conflict"
        result["error"] = (
            f"custom_parameters declares {declared}, existing DL content points at {existing} -- left untouched"
        )
        return result

    result["dl_content"] = content_items.build_dl_content(display_name, activityid, env_cfg)

    # Migrated fields *and* correct deep-linking content is the only genuinely
    # done state. Fields alone are not enough -- that is the exact "Assignment
    # is currently being set up" shape this migration exists to remove -- so a
    # component in that state is repaired rather than skipped.
    result["status"] = "skipped_already_migrated" if result["already_migrated"] else "pending"
    return result


def migrate_component(entry, user, env_cfg):
    """
    Steps 3-5 (writes): edit the OLX, write the content item, verify both, then publish.

    The publish is deliberately last and gated: if either read-back check
    fails, the component is left as an unpublished draft -- invisible to
    learners and to Phase 2 -- and reported as failed_verification rather than
    going live in a half-migrated state.
    """
    result = dict(entry)
    usage_key = UsageKey.from_string(entry["usage_key"])
    external_config = f"lti_store:{env_cfg['lti_store_slug']}"

    # Step 3: switch the four fields that constitute the migration, in OLX.
    node = read_olx_node(usage_key)
    node.set("config_type", MIGRATED_CONFIG_TYPE)
    node.set("external_config", external_config)
    node.set("lti_version", MIGRATED_LTI_VERSION)
    node.set("custom_parameters", CLEARED_CUSTOM_PARAMETERS)
    set_library_block_olx(usage_key, etree.tostring(node, encoding="unicode"))

    # Step 4: write the deep-linking content item this component's course-side
    # copies will each get their own copy of in Phase 2.
    content_items.write_content_item(usage_key, entry["dl_content"], env_cfg)

    # Step 5: verify both writes by reading them back fresh, then publish.
    verify_attrib = read_olx_node(usage_key).attrib
    fields_ok = (
        verify_attrib.get("config_type") == MIGRATED_CONFIG_TYPE and
        verify_attrib.get("lti_version") == MIGRATED_LTI_VERSION and
        verify_attrib.get("external_config") == external_config and
        verify_attrib.get("custom_parameters", CLEARED_CUSTOM_PARAMETERS) == CLEARED_CUSTOM_PARAMETERS
    )
    content_ok = content_items.read_activityid(usage_key) == entry["activityid"]

    if not (fields_ok and content_ok):
        result["status"] = "failed_verification"
        result["error"] = f"read-back mismatch (fields_ok={fields_ok} content_ok={content_ok}) -- publish skipped"
        return result

    publish_component_changes(usage_key, user.id)
    result["status"] = "done"
    return result
