EDL Muzzy Lane LTI 1.1 → 1.3 migration
======================================

EDL-specific migration tooling, isolated from the rest of ``lti_consumer``
because it encodes EDL's own library naming conventions and Muzzy Lane
environment URLs. Nothing here is part of the plugin's public API.

What it does
------------

One management command, ``lti13_migrate``, in two phases:

**Phase 1 — libraries.** For each ``lti_consumer`` component in a matched
content library: capture the Muzzy Lane ``activityid`` from its LTI 1.1
``custom_parameters``, switch four OLX attributes to LTI 1.3 Reusable
Configuration (``config_type``, ``external_config``, ``lti_version``,
``custom_parameters``), write the deep-linking content item, verify both
writes by reading them back, and only then publish the component.

**Phase 2 — courses.** For each course block synced from one of those
components: accept the library changes, write that component's deep-linking
content item onto this copy, verify fresh from the store and the database, and
only then publish the parent unit.

Both phases are read-only unless ``--apply`` is passed, and each is preceded by
a pre-flight summary and an explicit ``y``/``yes`` confirmation.

Why there is no browser involved
--------------------------------

The original plan called for Selenium driving Studio's deep-linking picker with
UUID matching against Muzzy Lane's activity list. That turned out to be
unnecessary. Verified live against real prod and stage blocks:

* ``LtiDlContentItem.attributes`` is an opaque, unvalidated JSON blob
  (``LtiDlLtiResourceLinkSerializer.custom`` is a plain ``DictField``), so the
  selection a picker would produce can simply be written directly.
* Muzzy Lane's ``integration_launch_hash`` is **not** required. A real
  deep-link round trip against a brand-new block and a never-used activity
  behaved identically with and without it — once the unit was published.
* Every "Assignment is currently being set up" failure during investigation
  was a **missing publish**, not a missing hash.
* Every currently-LTI-1.1 block already carries ``activityid=<uuid>``, which is
  the identity Muzzy Lane routes on.

Module layout
-------------

===================== ==========================================================
``config.py``         Per-environment constants; detects prod vs stage from the
                      database rather than taking it as a flag.
``content_items.py``  Builds, writes and reads back the deep-linking content
                      item. Touches only this plugin's own models — no
                      edx-platform imports — so it is identical for a library
                      component and each course-side copy.
``library_ops.py``    Phase 1. Library components are Learning-Core-backed, so
                      they are edited as OLX text, not by field assignment.
``course_ops.py``     Phase 2. Course blocks are plain modulestore XBlocks.
``verify.py``         Independent post-run verification, re-derived from
                      scratch — including a read of the **published** branch.
``report.py``         Pre-flight summaries, confirmation prompt, reports.
``state.py``          Per-run audit/resume log, stamped with env and mode.
===================== ==========================================================

Things worth knowing before you run it on production
----------------------------------------------------

* **Publishing is not surgical.** ``store.publish`` operates on the parent
  unit, which is what Studio's own Publish button does — so any *other*
  unpublished draft edit sitting in that unit goes live too. There is no way to
  publish a single component on its own. The Phase 2 pre-flight says so.
* **``has_score`` comes from the library.** It is a non-customizable field, so
  Accept Changes overwrites whatever the course had. 48 blocks on prod are
  currently ``has_score=False``; whether that is intentional is still an open
  content question.
* **Migrating clears ``custom_parameters``**, which drops the
  ``activityversion`` pin. Real picker round trips drop it too, so this matches
  Muzzy Lane's own behaviour — but migrated assessments will launch whatever
  version Muzzy Lane has published latest.
* **The activityid stays recoverable.** After migration it lives in the
  component's own deep-linking content item, which is where the command reads
  it from on a re-run. That is what makes the command safe to run twice.
* **A block is only "done" when its deep-linking content is actually present
  and correct** — not merely when its fields say ``lti_1p3``. Fields-only is
  exactly the broken state this migration removes, so the command will repair
  such a block rather than skip it.
* **An existing content item pointing at a different activity is never
  overwritten.** It is reported as a conflict and left alone, in case it is a
  real picker-produced selection.
* **Stage is not a full replica.** It covers 2 of the 9 real library families,
  so a clean stage run proves the mechanism, not full coverage.

Running it
----------

Confirmation prompts read from stdin, so use ``-it`` — or pass ``--yes`` for a
plan you have already reviewed via a dry run. Without either, the command
declines to proceed rather than treating "could not ask" as "yes".

Production (Kubernetes)::

    kubectl exec -it deployment/cms -- python manage.py cms lti13_migrate --mode test
    kubectl exec -it deployment/cms -- python manage.py cms lti13_migrate --mode test --apply

Stage / dev (Tutor)::

    docker exec -it tutor_local-cms-1 ./manage.py cms lti13_migrate --mode test
    docker exec -it tutor_local-cms-1 ./manage.py cms lti13_migrate --mode test --apply

Recommended rollout::

    # 1. dry run, both modes, on stage; review the pre-flight output in full
    # 2. apply --mode test on stage, verify a real launch and grade passback
    # 3. apply --mode actual on stage (covers CPSAA2026 and CTAA2026 only)
    # 4. dry run on prod; review
    # 5. apply on prod one library at a time with --library, starting non-LSU

The full state is printed to stdout at the end of each phase. Keep it: the
state *file* lives on the pod's ephemeral working directory and will not
survive a restart.
