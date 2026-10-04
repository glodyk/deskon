import re

import pytest

from deskon import constants as c


def test_active_and_reserved_event_types_are_disjoint():
    assert not (c.ACTIVE_EVENT_TYPES & c.RESERVED_EVENT_TYPES)


def test_active_event_types_match_contract():
    assert c.ACTIVE_EVENT_TYPES == {
        "REVIEW_OPENED",
        "REVIEW_CLOSED",
        "FINDING_CREATED",
        "RESELECTION_CREATED",
        "RESELECTION_RESOLVED",
    }


def test_reserved_event_types_match_contract():
    assert c.RESERVED_EVENT_TYPES == {
        "FINDING_PUBLISHED",
        "FINDING_CORRECTED",
        "FINDING_CANCELLED",
        "COMMENT_CREATED",
        "COMMENT_PUBLISHED",
        "COMMENT_CORRECTED",
        "RESELECTION_CORRECTED",
        "RESELECTION_CANCELLED",
    }


def test_entity_types_match_contract():
    assert c.ALL_ENTITY_TYPES == {
        "claim_review",
        "review_finding",
        "review_comment",
        "claim_reselection",
        "preliminary_note",
    }


def test_every_event_type_has_a_canonical_entity_type():
    assert set(c.EVENT_ENTITY_TYPE) == c.ALL_EVENT_TYPES
    assert set(c.EVENT_ENTITY_TYPE.values()) <= c.ALL_ENTITY_TYPES


def test_preliminary_note_has_no_events_yet():
    assert c.EntityType.PRELIMINARY_NOTE not in c.EVENT_ENTITY_TYPE.values()


def test_legacy_aliases_map_to_canonical_names():
    assert c.canonical_event_type("REVIEW_CYCLE_CREATED") == "REVIEW_OPENED"
    assert c.canonical_entity_type("REVIEW_FINDING") == "review_finding"
    # Nama kanonik dan nilai lain tidak berubah.
    assert c.canonical_event_type("REVIEW_CLOSED") == "REVIEW_CLOSED"
    assert c.canonical_entity_type("claim_review") == "claim_review"
    assert c.canonical_entity_type(None) is None


def test_legacy_aliases_target_canonical_values():
    assert set(c.LEGACY_EVENT_TYPE_ALIASES.values()) <= c.ACTIVE_EVENT_TYPES
    assert set(c.LEGACY_ENTITY_TYPE_ALIASES.values()) <= c.ALL_ENTITY_TYPES


# --- constants vs CHECK constraint di database (butuh PostgreSQL sementara) ---

CHECK_CONSTANTS = [
    ("core.claims", "ck_claims_status", c.CLAIM_STATUSES),
    ("review.claim_reviews", "ck_claim_reviews_review_type", c.REVIEW_TYPES),
    ("review.claim_reviews", "ck_claim_reviews_status", c.REVIEW_STATUSES),
    ("review.claim_reviews", "ck_claim_reviews_final_decision", c.FINAL_DECISIONS),
    ("review.review_findings", "ck_review_findings_status", c.FINDING_STATUSES),
    ("review.review_comments", "ck_review_comments_status", c.COMMENT_STATUSES),
    ("review.claim_reselections", "ck_reselections_status", c.RESELECTION_STATUSES),
    ("review.claim_reselections", "ck_reselections_action", c.RESELECTION_ACTIONS),
    ("review.claim_reselections", "ck_reselections_target_type", c.RESELECTION_TARGET_TYPES),
    ("core.users", "ck_users_role", c.USER_ROLES),
    ("core.claim_topups", "ck_claim_topups_type", c.TOPUP_TYPES),
    ("staging.import_batches", "ck_import_batches_status", c.IMPORT_BATCH_STATUSES),
    ("staging.import_batches", "ck_import_batches_source_type", c.IMPORT_SOURCE_TYPES),
    ("staging.staging_claims", "ck_staging_claims_validation_status", c.VALIDATION_STATUSES),
]


@pytest.mark.parametrize("table, constraint, expected", CHECK_CONSTANTS)
def test_constants_match_database_check_constraints(conn, table, constraint, expected):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass AND conname = %s",
            (table, constraint),
        )
        row = cur.fetchone()
    assert row is not None, f"{constraint} tidak ada di {table}"
    values = set(re.findall(r"'([^']*)'::character varying", row[0]))
    assert values == set(expected)
