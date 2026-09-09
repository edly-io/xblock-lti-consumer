"""
Phase 2 -- the course side.

For each course block synced from a library component that Phase 1 migrated:
accept the library changes, write that component's deep-linking content item
onto this copy, verify, and publish the unit.

Two facts drive the whole shape of this, both confirmed against edx-platform
source and live blocks:

1. ``LtiConfiguration`` / ``LtiDlContentItem`` are keyed by exact block
   ``location``, so a library component and each course-side copy own
   *separate* rows. Studio's "Accept changes"
   (``sync_from_upstream_block`` + ``store.update_item``) only ever touches
   modulestore XBlock fields -- it never touches ``LtiDlContentItem``. So the
   content item has to be written here, once per course-side copy.

2. ``config_type``/``external_config``/``lti_version``/``has_score`` are all
   non-customizable ("Tier C") fields: the sync does an unconditional
   ``setattr`` from upstream. That means editing them in the course would be
   reverted by any later Accept Changes, which is exactly why Phase 1 fixes
   the library first -- and it means ``has_score`` is taken from the library
   whether or not that was intended (see the plan's open question Q3).
"""
from cms.lib.xblock.upstream_sync import UpstreamLinkException  # pylint: disable=import-error
from cms.lib.xblock.upstream_sync_block import sync_from_upstream_block  # pylint: disable=import-error
from opaque_keys.edx.keys import UsageKey
from xmodule.modulestore.django import modulestore  # pylint: disable=import-error

from lti_consumer.edl_migration import content_items
from lti_consumer.edl_migration.library_ops import MIGRATED_CONFIG_TYPE, MIGRATED_LTI_VERSION


def discover_course_blocks(library_entries):
    """
    Step 6: find every course-side block whose upstream is a component Phase 1
    handled, and decide per block whether it still needs work.

    The join key is ``block.upstream``, which holds the string form of the
    library component's usage key (``lb:ORG:SLUG:lti_consumer:...``). Verified
    on stage: 7 of 7 course-side blocks matched their library component exactly.

    Returns ``({course_id: [block_entry, ...]}, [skipped_course_note, ...])`` --
    courses whose blocks could not be listed are returned rather than silently
    dropped, so the pre-flight totals cannot read as full coverage when they
    are not.
    """
    upstream_map = {
        entry["usage_key"]: entry
        for entry in library_entries
        if entry.get("dl_content") and entry.get("status") in ("done", "pending", "skipped_already_migrated")
    }
    if not upstream_map:
        return {}, []

    store = modulestore()
    courses = {}
    unreadable = []
    for course_summary in store.get_course_summaries():
        course_key = course_summary.id
        try:
            blocks = store.get_items(course_key, qualifiers={"block_type": "lti_consumer"})
        except Exception as exc:  # pylint: disable=broad-except
            unreadable.append(f"{course_key}: {exc}")
            continue

        matched = []
        for block in blocks:
            upstream = getattr(block, "upstream", None)
            source = upstream_map.get(upstream) if upstream else None
            if source is None:
                continue

            fields_migrated = (
                getattr(block, "config_type", None) == MIGRATED_CONFIG_TYPE and
                getattr(block, "lti_version", None) == MIGRATED_LTI_VERSION
            )
            # A block only counts as done when its deep-linking content is
            # actually present and points at the right activity. Checking the
            # fields alone would skip blocks that are on 1.3 with no content
            # item -- learners see "Assignment is currently being set up" --
            # including any block a previous crashed run left half-written.
            existing_activityid = content_items.read_activityid(block.location)
            content_ready = existing_activityid == source["activityid"]
            # An item pointing somewhere else is not ours to overwrite: it may
            # be a real picker-produced selection. Report it, do not touch it.
            conflict = existing_activityid is not None and not content_ready

            matched.append({
                "block_location": str(block.location),
                "upstream": upstream,
                "upstream_short": upstream.rsplit(":", 1)[-1][:12],
                "activityid": source["activityid"],
                "existing_activityid": existing_activityid,
                "dl_content": source["dl_content"],
                "fields_migrated": fields_migrated,
                "conflict": conflict,
                "already_migrated": bool(fields_migrated and content_ready),
            })
        if matched:
            courses[str(course_key)] = matched
    return courses, unreadable


def migrate_course_block(block_entry, user, env_cfg):
    """
    Steps 7-9 (writes): accept changes, write the content item, verify both, then publish.

    Same gated shape as the library side: the unit is only published once a
    fresh read of both the synced fields and the content item matches intent.
    A mismatch leaves an unpublished draft, which learners never see.
    """
    result = dict(block_entry)

    if block_entry["conflict"]:
        result["status"] = "skipped_conflict"
        result["error"] = (
            f"existing DL content points at {block_entry['existing_activityid']}, "
            f"library source says {block_entry['activityid']} -- left untouched"
        )
        return result

    store = modulestore()
    block_location = UsageKey.from_string(block_entry["block_location"])
    course_block = store.get_item(block_location)

    # Step 7: Accept changes. Pulls the migrated config_type/external_config/
    # lti_version (and has_score) down from the published library component.
    # Permission and bad-link problems arrive as UpstreamLinkException
    # subclasses (BadUpstream wraps PermissionDenied/NotFound), so this catch
    # covers "the run's user cannot read that library" too.
    try:
        sync_from_upstream_block(downstream=course_block, user=user)
    except UpstreamLinkException as exc:
        result["status"] = "failed"
        result["error"] = f"accept changes failed: {exc}"
        return result
    store.update_item(course_block, user.id)

    # Step 8: write this copy's own deep-linking content item.
    content_items.write_content_item(block_location, block_entry["dl_content"], env_cfg)

    # Step 9: verify fresh from the store and the database, then publish.
    fresh_block = store.get_item(block_location)
    fields_ok = (
        getattr(fresh_block, "config_type", None) == MIGRATED_CONFIG_TYPE and
        getattr(fresh_block, "lti_version", None) == MIGRATED_LTI_VERSION
    )
    content_ok = content_items.read_activityid(block_location) == block_entry["activityid"]

    if not (fields_ok and content_ok):
        result["status"] = "failed_verification"
        result["error"] = f"read-back mismatch (fields_ok={fields_ok} content_ok={content_ok}) -- publish skipped"
        return result

    # Publishing is done at the parent unit, which is what Studio's own
    # Publish button does. Caveat worth knowing: this also publishes any other
    # draft edits sitting in that unit -- there is no way to publish a single
    # component on its own.
    parent = fresh_block.get_parent()
    if parent is None:
        result["status"] = "failed_verification"
        result["error"] = "could not resolve parent unit to publish -- publish skipped"
        return result

    store.publish(parent.location, user.id)
    result["status"] = "done"
    return result
