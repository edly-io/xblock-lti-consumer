"""
Direct-course migration -- for courses whose ``lti_consumer`` blocks are
configured with LTI 1.1 straight in the course, with no content library in
between (the LSU parent courses).

This is the same field-level migration as the library side's Phase 1
(``config_type`` / ``external_config`` / ``lti_version`` / ``custom_parameters``
-> DL content -> verify -> publish), collapsed into a single phase: there is
no upstream library component to sync from, so course_ops.py's "accept
changes" step never applies here. A course-side block is a plain modulestore
XBlock with typed fields -- reading ``block.custom_parameters`` already
returns a Python list, not the JSON-encoded OLX attribute string
``library_ops.py`` has to parse -- so this edits fields directly via
``store.update_item`` rather than an lxml OLX round trip.

Both "is this already migrated" (``fields_migrated``) and "did the write
actually stick" (``fields_ok``) check ``external_config`` in addition to
``config_type``/``lti_version``. ``course_ops.py`` doesn't need to -- there,
``external_config`` arrives via ``sync_from_upstream_block`` from the library,
a field this module never touches directly. Here there is no upstream to sync
from: ``migrate_course_block`` sets ``external_config`` itself, so a block
with the right ``config_type``/``lti_version`` but a stale or wrong
``external_config`` (e.g. migrated once against the wrong environment) must
not read as "done".
"""
from opaque_keys.edx.keys import CourseKey, UsageKey
from xmodule.modulestore.django import modulestore  # pylint: disable=import-error

from lti_consumer.edl_migration import content_items

# Same four fields as library_ops.MIGRATED_CONFIG_TYPE / MIGRATED_LTI_VERSION --
# duplicated rather than imported, since these two flows are edited through
# entirely different mechanisms (OLX text vs. field assignment) and should
# stay independently readable.
MIGRATED_CONFIG_TYPE = "external"
MIGRATED_LTI_VERSION = "lti_1p3"


def extract_activityid(custom_parameters):
    """
    Pull ``activityid=<uuid>`` out of a block's ``custom_parameters`` field.

    Unlike ``library_ops.extract_activityid``, this takes an already-parsed
    list (the XBlock ``List`` field value), not a JSON-encoded OLX string --
    a course-side block's fields come back typed from the modulestore.
    """
    for param in custom_parameters or []:
        if isinstance(param, str) and param.startswith("activityid="):
            return param.split("=", 1)[1] or None
    return None


def discover_course_blocks(course_keys, env_cfg):
    """
    Step 1 (read-only): find every ``lti_consumer`` block in the given
    courses, and decide per block whether it still needs work.

    Returns ``({course_id: [block_entry, ...]}, [skipped_course_note, ...])``
    -- the same shape ``course_ops.discover_course_blocks`` returns, so
    ``verify.verify_course_blocks`` (which only reads ``block_location``,
    ``activityid`` and ``status``) can be reused as-is for post-run
    verification.
    """
    store = modulestore()
    courses = {}
    unreadable = []

    for course_key_str in course_keys:
        try:
            course_key = CourseKey.from_string(course_key_str)
        except Exception as exc:  # pylint: disable=broad-except
            unreadable.append(f"{course_key_str}: {exc}")
            continue

        try:
            blocks = store.get_items(course_key, qualifiers={"block_type": "lti_consumer"})
        except Exception as exc:  # pylint: disable=broad-except
            unreadable.append(f"{course_key_str}: {exc}")
            continue

        matched = []
        for block in blocks:
            fields_migrated = (
                getattr(block, "config_type", None) == MIGRATED_CONFIG_TYPE and
                getattr(block, "lti_version", None) == MIGRATED_LTI_VERSION and
                getattr(block, "external_config", None) == f"lti_store:{env_cfg['lti_store_slug']}"
            )
            # Two possible sources for the activityid, same priority order as
            # library_ops.inspect_component: custom_parameters (the LTI 1.1
            # ground truth, present until migration clears it), falling back
            # to the block's own DL content item (where it survives
            # afterwards -- what makes a re-run safe).
            declared = extract_activityid(getattr(block, "custom_parameters", None))
            existing = content_items.read_activityid(block.location)
            activityid = declared or existing

            entry = {
                "block_location": str(block.location),
                "display_name": getattr(block, "display_name", "") or "",
                "activityid": activityid,
                "existing_activityid": existing,
                "dl_content": None,
                "fields_migrated": fields_migrated,
                "conflict": bool(declared and existing and declared != existing),
                "already_migrated": False,
                "status": None,
                "error": None,
            }

            if not activityid:
                entry["status"] = "failed"
                entry["error"] = (
                    "no activityid in custom_parameters or existing DL content -- cannot construct content item"
                )
                matched.append(entry)
                continue

            # Existing content pointing somewhere other than what
            # custom_parameters declares is not ours to resolve: it may be a
            # real picker-produced selection made deliberately. Report it,
            # leave it alone.
            if entry["conflict"]:
                entry["status"] = "skipped_conflict"
                entry["error"] = (
                    f"custom_parameters declares {declared}, existing DL content points at {existing} "
                    "-- left untouched"
                )
                matched.append(entry)
                continue

            entry["dl_content"] = content_items.build_dl_content(entry["display_name"], activityid, env_cfg)
            entry["already_migrated"] = bool(fields_migrated and existing == activityid)
            entry["status"] = "skipped_already_migrated" if entry["already_migrated"] else "pending"
            matched.append(entry)

        if matched:
            # Keyed by the original config string, not str(course_key): the
            # pre-flight's "zero blocks found" check compares this dict's keys
            # against that same config list, and a re-serialized CourseKey is
            # only guaranteed to match it byte-for-byte, not required to.
            courses[course_key_str] = matched

    return courses, unreadable


def migrate_course_block(block_entry, user, env_cfg):
    """
    Migrate one course-side block in place: flip the four LTI fields, write
    the deep-linking content item, verify both fresh, then publish the unit.

    Gated the same way as both flows in the library migration: the unit is
    only published once a fresh read of the fields and the content item
    matches intent, so a mismatch leaves an unpublished draft (invisible to
    learners) rather than going live half-migrated.
    """
    result = dict(block_entry)

    # Defense in depth: the command's caller already routes conflicts away
    # from this function, but a direct call (e.g. a re-run script) must not
    # overwrite a possibly real, picker-produced selection either.
    if block_entry["conflict"]:
        result["status"] = "skipped_conflict"
        result["error"] = (
            f"existing DL content points at {block_entry['existing_activityid']}, "
            f"declared activityid is {block_entry['activityid']} -- left untouched"
        )
        return result

    store = modulestore()
    location = UsageKey.from_string(block_entry["block_location"])
    block = store.get_item(location)

    # Step: flip the four fields that constitute the migration, then save.
    block.config_type = MIGRATED_CONFIG_TYPE
    block.external_config = f"lti_store:{env_cfg['lti_store_slug']}"
    block.lti_version = MIGRATED_LTI_VERSION
    block.custom_parameters = []
    store.update_item(block, user.id)

    # Write this block's own deep-linking content item.
    content_items.write_content_item(location, block_entry["dl_content"], env_cfg)

    # Verify fresh from the store and the database, then publish.
    fresh_block = store.get_item(location)
    fields_ok = (
        getattr(fresh_block, "config_type", None) == MIGRATED_CONFIG_TYPE and
        getattr(fresh_block, "lti_version", None) == MIGRATED_LTI_VERSION and
        getattr(fresh_block, "external_config", None) == f"lti_store:{env_cfg['lti_store_slug']}"
    )
    content_ok = content_items.read_activityid(location) == block_entry["activityid"]

    if not (fields_ok and content_ok):
        result["status"] = "failed_verification"
        result["error"] = f"read-back mismatch (fields_ok={fields_ok} content_ok={content_ok}) -- publish skipped"
        return result

    # Publishing is done at the parent unit, same caveat as course_ops.py:
    # this also publishes any other draft edits sitting in that unit -- there
    # is no way to publish a single component on its own.
    parent = fresh_block.get_parent()
    if parent is None:
        result["status"] = "failed_verification"
        result["error"] = "could not resolve parent unit to publish -- publish skipped"
        return result

    store.publish(parent.location, user.id)
    result["status"] = "done"
    return result
