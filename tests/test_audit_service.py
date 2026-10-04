import json
from datetime import date
from decimal import Decimal

import psycopg2
import pytest

from deskon.constants import EVENT_ENTITY_TYPE, RESERVED_EVENT_TYPES, EntityType, EventType
from deskon.errors import AuditEventError, NotFoundError
from deskon.services import audit_service


def _record(conn, fx, **overrides):
    kwargs = dict(
        review_id=fx["review_id"],
        user_id=fx["user_id"],
        event_type=EventType.REVIEW_OPENED,
        entity_type=EntityType.CLAIM_REVIEW,
        entity_id=fx["review_id"],
        payload={"cycle_no": 1, "review_type": "VERIFIKASI_PASCA_KLAIM"},
    )
    kwargs.update(overrides)
    return audit_service.record_event(conn, **kwargs)


def test_record_event_writes_base_fields_and_payload(conn, review_fixture):
    fx = review_fixture
    event = _record(conn, fx)

    assert event.review_id == fx["review_id"]
    assert event.user_id == fx["user_id"]
    assert event.event_type == "REVIEW_OPENED"
    assert event.entity_type == "claim_review"
    assert event.entity_id == fx["review_id"]
    assert event.event_data == {
        "claim_id": fx["claim_id"],
        "nosjp": fx["nosjp"],
        "review_id": fx["review_id"],
        "cycle_no": 1,
        "review_type": "VERIFIKASI_PASCA_KLAIM",
    }


def test_event_data_is_real_json_not_string(conn, review_fixture):
    _record(conn, review_fixture, payload={"note": 'kutip " dan \\ dan ünïcode'})
    with conn.cursor() as cur:
        cur.execute("SELECT jsonb_typeof(event_data), event_data->>'note' FROM review.review_events")
        assert cur.fetchone() == ("object", 'kutip " dan \\ dan ünïcode')


def test_payload_serializes_dates_and_decimals(conn, review_fixture):
    event = _record(
        conn, review_fixture,
        payload={"service_month": date(2025, 10, 1), "biayars": Decimal("1250000.50")},
    )
    assert event.event_data["service_month"] == "2025-10-01"
    assert event.event_data["biayars"] == "1250000.50"


def test_claim_id_and_nosjp_come_from_the_review(conn, review_fixture):
    event = _record(conn, review_fixture, payload=None)
    assert event.event_data == {
        "claim_id": review_fixture["claim_id"],
        "nosjp": review_fixture["nosjp"],
        "review_id": review_fixture["review_id"],
    }


@pytest.mark.parametrize("key", ["claim_id", "nosjp", "review_id"])
def test_payload_cannot_override_base_fields(conn, review_fixture, key):
    with pytest.raises(AuditEventError):
        _record(conn, review_fixture, payload={key: "lain"})


@pytest.mark.parametrize("event_type", sorted(RESERVED_EVENT_TYPES))
def test_reserved_event_types_are_rejected(conn, review_fixture, event_type):
    entity = EVENT_ENTITY_TYPE[event_type]
    with pytest.raises(AuditEventError, match="cadangan"):
        _record(conn, review_fixture, event_type=event_type, entity_type=entity)


def test_unknown_and_legacy_event_types_are_rejected(conn, review_fixture):
    for event_type in ("REVIEW_CYCLE_CREATED", "NOTE_IMPORTED", "whatever"):
        with pytest.raises(AuditEventError, match="tidak dikenal"):
            _record(conn, review_fixture, event_type=event_type)


def test_entity_type_must_match_event_type(conn, review_fixture):
    with pytest.raises(AuditEventError):
        _record(conn, review_fixture, entity_type=EntityType.REVIEW_FINDING)
    with pytest.raises(AuditEventError):
        _record(
            conn, review_fixture,
            event_type=EventType.FINDING_CREATED, entity_type="REVIEW_FINDING",
        )


def test_ids_must_be_integers(conn, review_fixture):
    with pytest.raises(AuditEventError):
        _record(conn, review_fixture, entity_id=None)
    with pytest.raises(AuditEventError):
        _record(conn, review_fixture, user_id="1")


def test_unknown_review_is_not_found(conn, review_fixture):
    with pytest.raises(NotFoundError):
        _record(conn, review_fixture, review_id=review_fixture["review_id"] + 999)


def test_record_event_does_not_commit(conn, pg_dsn, review_fixture):
    _record(conn, review_fixture)
    other = psycopg2.connect(**pg_dsn)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT count(*) FROM review.review_events")
            assert cur.fetchone()[0] == 0
    finally:
        other.close()


def test_event_is_rolled_back_with_the_callers_transaction(conn, review_fixture):
    _record(conn, review_fixture)
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM review.review_events")
        assert cur.fetchone()[0] == 0


def test_list_review_events_maps_legacy_names_without_rewriting(conn, review_fixture):
    fx = review_fixture
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO review.review_events (review_id, user_id, event_type, entity_type, entity_id, event_data) "
            "VALUES (%s, %s, 'REVIEW_CYCLE_CREATED', 'claim_review', %s, %s::jsonb)",
            (fx["review_id"], fx["user_id"], fx["review_id"], json.dumps({"new_review_id": fx["review_id"]})),
        )
    _record(conn, fx)

    events = audit_service.list_review_events(conn, fx["review_id"])
    assert [e.event_type for e in events] == ["REVIEW_CYCLE_CREATED", "REVIEW_OPENED"]
    assert [e.canonical_event_type for e in events] == ["REVIEW_OPENED", "REVIEW_OPENED"]


def test_audit_service_adds_only_identity_metadata(conn, review_fixture):
    payload = {"final_decision": "LAYAK", "resolution_note": "ok"}
    event = _record(
        conn, review_fixture,
        event_type=EventType.REVIEW_CLOSED, payload=payload,
    )
    assert set(event.event_data) == {"claim_id", "nosjp", "review_id"} | set(payload)
