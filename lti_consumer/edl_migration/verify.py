"""
Independent post-run verification.

Deliberately re-derives everything from scratch -- fresh OLX reads, fresh
database queries, and for course blocks a read of the **published** branch --
rather than reporting back what the run believed it did. A run's own
bookkeeping cannot catch the failure mode that matters most here: a write that
appeared to succeed but is not what the LMS will actually serve to a learner.
"""
from xmodule.modulestore import ModuleStoreEnum  # pylint: disable=import-error
from xmodule.modulestore.django import modulestore  # pylint: disable=import-error
from opaque_keys.edx.keys import UsageKey

from lti_consumer.edl_migration import content_items
from lti_consumer.edl_migration.library_ops import (
    MIGRATED_CONFIG_TYPE,
    MIGRATED_LTI_VERSION,
    read_olx_node,
)


def verify_library_entries(entries):
    """
    Re-check each migrated library component straight from the OLX and the
    database. Returns a list of ``{key, passed, detail}``.
    """
    results = []
    for entry in entries:
        if entry.get("status") != "done":
            continue
        usage_key = UsageKey.from_string(entry["usage_key"])
        problems = []
        try:
            attrib = read_olx_node(usage_key).attrib
        except Exception as exc:  # pylint: disable=broad-except
            results.append({"key": entry["usage_key"], "passed": False, "detail": f"OLX unreadable: {exc}"})
            continue

        if attrib.get("config_type") != MIGRATED_CONFIG_TYPE:
            problems.append(f"config_type={attrib.get('config_type')!r}")
        if attrib.get("lti_version") != MIGRATED_LTI_VERSION:
            problems.append(f"lti_version={attrib.get('lti_version')!r}")

        found = content_items.read_activityid(usage_key)
        if found != entry["activityid"]:
            problems.append(f"DL activityid={found!r}, expected {entry['activityid']!r}")

        results.append({
            "key": entry["usage_key"],
            "passed": not problems,
            "detail": "; ".join(problems) or "OLX on 1.3, DL content present and correct",
        })
    return results


def verify_course_blocks(courses):
    """
    Re-check each migrated course block, reading the **published** branch --
    the branch the LMS serves -- so a block left as an unpublished draft is
    reported as a failure rather than a pass.
    """
    store = modulestore()
    results = []
    for course_id, blocks in courses.items():
        for block in blocks:
            if block.get("status") != "done":
                continue
            location = UsageKey.from_string(block["block_location"])
            key = f"{course_id} / {block['block_location'].rsplit(':', 1)[-1][:14]}"
            problems = []

            try:
                with store.branch_setting(ModuleStoreEnum.Branch.published_only, location.course_key):
                    published = store.get_item(location)
            except Exception as exc:  # pylint: disable=broad-except
                results.append({"key": key, "passed": False,
                                "detail": f"not readable on the published branch: {exc}"})
                continue

            if getattr(published, "config_type", None) != MIGRATED_CONFIG_TYPE:
                problems.append(f"published config_type={getattr(published, 'config_type', None)!r}")
            if getattr(published, "lti_version", None) != MIGRATED_LTI_VERSION:
                problems.append(f"published lti_version={getattr(published, 'lti_version', None)!r}")

            found = content_items.read_activityid(location)
            if found != block["activityid"]:
                problems.append(f"DL activityid={found!r}, expected {block['activityid']!r}")

            results.append({
                "key": key,
                "passed": not problems,
                "detail": "; ".join(problems) or "published on 1.3, DL content present and correct",
            })
    return results
