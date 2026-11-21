"""Kafka topics this service produces and consumes (docs/PHASES.md Contracts 3 and 4)."""

from app.core.config import settings

# produced (through the outbox)
ORG_MEMBER_JOINED = "org.member.joined"
ORG_MEMBER_LEFT = "org.member.left"
ORG_POLICY_CHANGED = "org.policy.changed"
ORG_INVITE_CREATED = "org.invite.created"
ORG_PILOT_ENDING = "org.pilot.ending"
PRODUCED_TOPICS = (ORG_MEMBER_JOINED, ORG_MEMBER_LEFT, ORG_POLICY_CHANGED, ORG_INVITE_CREATED, ORG_PILOT_ENDING)

# consumed
USER_EVENTS = "user-events"
USER_DELETED = "user.deleted"
SUBSCRIPTION_CHANGED = "subscription.changed"   # Phase 10 only
CONSUMED_TOPICS = (USER_EVENTS, USER_DELETED)

# dead letters of the consumed topics
DLQ_TOPIC = settings.KAFKA_DLQ_TOPIC
