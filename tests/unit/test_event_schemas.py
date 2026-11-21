import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.clock import FixedClock
from app.core.enums import EndReason, JoinSource, OrgType, Role
from app.events import topics
from app.events.schemas import (
    PRODUCED,
    EmailVerifiedPayload,
    OrgInviteCreated,
    OrgMemberJoined,
    OrgMemberLeft,
    OrgPilotEnding,
    OrgPolicyChanged,
    ProducedEvent,
    UserDeletedEvent,
    UserEventEnvelope,
    UserRegisteredPayload,
    iso_z,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CLOCK = FixedClock(datetime(2026, 11, 21, 12, 0, 0, 654321, tzinfo=timezone.utc))
ORG, USER = str(uuid.uuid4()), str(uuid.uuid4())


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def joined(**kw):
    base = dict(org_id=ORG, user_id=USER, role=Role.MEMBER, org_type=OrgType.COLLEGE, source=JoinSource.INVITE)
    return OrgMemberJoined.create(CLOCK, **{**base, **kw})


def left(**kw):
    base = dict(org_id=ORG, user_id=USER, role=Role.MEMBER, org_type=OrgType.COLLEGE, reason=EndReason.LEFT)
    return OrgMemberLeft.create(CLOCK, **{**base, **kw})


# --- produced events ---
def test_the_envelope_fields():
    body = joined().model_dump(mode="json")
    assert set(body) == {"event_id", "schema_version", "occurred_at", "org_id", "user_id", "role", "org_type", "source"}
    assert body["schema_version"] == "1.0"
    assert uuid.UUID(body["event_id"]).version == 7
    assert body["role"] == "member" and body["org_type"] == "college" and body["source"] == "invite"


def test_occurred_at_is_iso_utc_with_z():
    body = joined().model_dump(mode="json")
    assert body["occurred_at"] == "2026-11-21T12:00:00Z" == iso_z(CLOCK.now())
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", body["occurred_at"])


@pytest.mark.parametrize("bad", ["2026-11-21T12:00:00", "2026-11-21T12:00:00+00:00", "2026-11-21 12:00:00Z", "yesterday", ""])
def test_occurred_at_must_end_in_z(bad):
    with pytest.raises(ValidationError):
        OrgMemberJoined(occurred_at=bad, org_id=ORG, user_id=USER, role="member", org_type="college", source="staff")


def test_every_event_gets_its_own_uuid7():
    assert len({joined().event_id for _ in range(50)}) == 50


@pytest.mark.parametrize("make", [joined, left])
def test_produced_models_forbid_unknown_fields(make):
    body = make().model_dump(mode="json")
    with pytest.raises(ValidationError):
        type(make()).model_validate({**body, "surprise": 1})


def test_org_type_is_required_on_member_events():
    for cls, extra in ((OrgMemberJoined, {"source": "staff"}), (OrgMemberLeft, {"reason": "left"})):
        with pytest.raises(ValidationError):
            cls(occurred_at="2026-11-21T12:00:00Z", org_id=ORG, user_id=USER, role="member", **extra)


def test_enum_fields_reject_other_values():
    with pytest.raises(ValidationError):
        joined(org_type="school")
    with pytest.raises(ValidationError):
        left(reason="fired")
    with pytest.raises(ValidationError):
        joined(source="magic")


def test_event_id_must_be_a_uuid():
    with pytest.raises(ValidationError):
        OrgMemberJoined(event_id="not-a-uuid", occurred_at="2026-11-21T12:00:00Z", org_id=ORG, user_id=USER,
                        role="member", org_type="college", source="staff")


def test_member_left_carries_all_four_reasons():
    for reason in EndReason:
        assert left(reason=reason).model_dump(mode="json")["reason"] == reason.value


def test_the_other_produced_models():
    policy = OrgPolicyChanged.create(CLOCK, org_id=ORG, version=2, lock_modes=True, approved_mode_ids=[ORG],
                                     member_spend_cap_paise=None)
    invite = OrgInviteCreated.create(CLOCK, org_id=ORG, invite_id=USER, email="a@b.co", org_name="Amity",
                                     inviter_name=None, accept_url="https://app/join/x", expires_at="2026-12-05T12:00:00Z")
    pilot = OrgPilotEnding.create(CLOCK, org_id=ORG, ends_at="2026-12-21T12:00:00Z", days_left=7)
    assert policy.model_dump(mode="json")["member_spend_cap_paise"] is None
    assert invite.model_dump(mode="json")["accept_url"].startswith("https://")
    assert pilot.days_left == 7


def test_the_topic_table_covers_every_produced_topic_and_keys():
    assert set(PRODUCED) == set(topics.PRODUCED_TOPICS)
    assert PRODUCED[topics.ORG_MEMBER_JOINED][1] == PRODUCED[topics.ORG_MEMBER_LEFT][1] == "user_id"   # not org_id
    assert PRODUCED[topics.ORG_POLICY_CHANGED][1] == PRODUCED[topics.ORG_INVITE_CREATED][1] == "org_id"
    assert issubclass(PRODUCED[topics.ORG_PILOT_ENDING][0], ProducedEvent)


# --- billing's consumed models ---
@pytest.mark.parametrize("model, name", [(OrgMemberJoined, "OrgMemberJoined"), (OrgMemberLeft, "OrgMemberLeft")])
def test_member_events_are_a_superset_of_what_billing_reads(model, name):
    billing_fields = set(fixture("billing_org_member_fields.json")[name])
    assert billing_fields <= set(model.model_fields), billing_fields - set(model.model_fields)


def test_a_member_event_has_a_value_for_every_billing_field():
    body = joined().model_dump(mode="json")
    for field in fixture("billing_org_member_fields.json")["OrgMemberJoined"]:
        assert body[field] not in (None, "")


# --- consumed fixtures (copied from auth-service code) ---
def test_user_registered_fixture_parses():
    envelope = UserEventEnvelope.model_validate(fixture("user_event_registered.json"))
    assert envelope.event_type == "UserRegistered"
    payload = UserRegisteredPayload.model_validate(envelope.payload)
    assert payload.user_id == uuid.UUID("0194b7a2-5f3e-7c11-8a41-2d3c4b5a6e70")
    assert payload.email == "Ada.Lovelace@Amity.edu" and payload.full_name == "Ada Lovelace"
    assert payload.timestamp == datetime(2026, 11, 21, 12, 0, 0, 123456, tzinfo=timezone.utc)


def test_email_verified_fixture_parses():
    envelope = UserEventEnvelope.model_validate(fixture("user_event_email_verified.json"))
    payload = EmailVerifiedPayload.model_validate(envelope.payload)
    assert envelope.event_type == "EmailVerified" and payload.email == "ada.lovelace@amity.edu"


def test_an_unhandled_user_event_type_still_parses_as_an_envelope():
    envelope = UserEventEnvelope.model_validate(fixture("user_event_password_reset.json"))
    assert envelope.event_type == "PasswordReset" and envelope.payload["event_id"]


def test_user_deleted_fixture_parses():
    event = UserDeletedEvent.model_validate(fixture("user_deleted.json"))
    assert event.event_id == "7b8c9d0e-3333-4f44-a555-6677889900aa" and event.schema_version == "1.0"
    assert event.user_id == uuid.UUID("0194b7a2-5f3e-7c11-8a41-2d3c4b5a6e70")


def test_consumed_models_ignore_unknown_fields():
    body = {**fixture("user_deleted.json"), "added_later": {"x": 1}}
    assert UserDeletedEvent.model_validate(body).user_id
    env = {**fixture("user_event_registered.json"), "extra": 1}
    env["payload"] = {**env["payload"], "locale": "en-IN"}
    assert UserRegisteredPayload.model_validate(UserEventEnvelope.model_validate(env).payload).email


@pytest.mark.parametrize("breakage", [
    lambda d: d.pop("user_id"), lambda d: d.update(user_id="not-a-uuid"), lambda d: d.update(event_type="user.created"),
    lambda d: d.pop("event_id"),
])
def test_a_broken_user_deleted_is_a_schema_error(breakage):
    body = fixture("user_deleted.json")
    breakage(body)
    with pytest.raises(ValidationError):
        UserDeletedEvent.model_validate(body)


def test_a_payload_missing_its_user_is_a_schema_error():
    payload = dict(fixture("user_event_registered.json")["payload"])
    payload.pop("user_id")
    with pytest.raises(ValidationError):
        UserRegisteredPayload.model_validate(payload)
