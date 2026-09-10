"""
Environment detection and per-environment constants for the EDL LTI 1.3 migration.

Env is never taken as a CLI flag: the command already runs inside whichever
pod/container you connected to, so it detects prod vs stage from what's
actually in that database and aborts rather than silently guessing wrong.
"""
from django.core.management.base import CommandError
from django.db import connection

ENV_CONFIG = {
    "prod": {
        "muzzylane_base": "https://author.muzzylane.com",
        "lti_store_slug": "author13",
    },
    "stage": {
        "muzzylane_base": "https://forge-stage.muzzylane.com",
        "lti_store_slug": "muzzylane",
    },
}

MODE_SEARCH_TERMS = {
    "test": "test",
    "actual": "auto",
}

# Course keys in scope for the direct-course migration (lti13_migrate_direct)
# -- lti_consumer blocks configured with LTI 1.1 straight in the course, no
# content library involved. Unlike the library migration's MODE_SEARCH_TERMS,
# these can't be discovered by a text search: LSU created one "parent" course
# per auto-assessment family (9 total, confirmed by the user 2026-09-10, not
# the 4 originally assumed), so the "actual" set is this fixed list of prod
# course keys instead.
DIRECT_MIGRATION_COURSES = {
    "actual": [
        "course-v1:LSU+EAA+LSU_parent_2607",
        "course-v1:LSU+CAA+2607-LSU-CAA-Summer2026",
        "course-v1:LSU+OCAA+2607",
        "course-v1:LSU+SDLAA+LSU_parent_2607",
        "course-v1:LSU+RAA+LSU_parent_2607",
        "course-v1:LSU+IFAA+LSU_parent_2607",
        "course-v1:LSU+IAA+LSU_parent_2607",
        "course-v1:LSU+CPSAA+2607LSU",
        "course-v1:LSU+CTAA+2607-LSU-CTAA-Summer2026",
    ],
    # Test course provided by the user 2026-09-10: apps.visibleskills.org/authoring/
    # course/course-v1:EDL+Test02+2026
    "test": [
        "course-v1:EDL+Test02+2026",
    ],
}


def detect_env():
    """
    Detect prod vs stage by checking which environment's known lti_store slug
    actually exists in this database. Raises CommandError if zero or more
    than one match, rather than guessing.
    """
    slugs = {env: cfg["lti_store_slug"] for env, cfg in ENV_CONFIG.items()}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT slug FROM lti_store_externallticonfiguration WHERE slug IN (%s, %s)",
            list(slugs.values()),
        )
        found_slugs = {row[0] for row in cursor.fetchall()}

    matched_envs = [env for env, slug in slugs.items() if slug in found_slugs]

    if len(matched_envs) != 1:
        raise CommandError(
            f"Could not confidently detect environment: expected exactly one of "
            f"{slugs} to exist in lti_store_externallticonfiguration, found slugs {found_slugs}. "
            f"Aborting rather than guessing which Muzzy Lane URL to use."
        )

    env = matched_envs[0]
    return env, ENV_CONFIG[env]
