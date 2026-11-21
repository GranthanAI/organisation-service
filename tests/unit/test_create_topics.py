"""scripts/create_topics.py with a fake admin client: creates what is missing, leaves the rest, idempotent."""

import pytest

from scripts import create_topics

ALL = {"org.member.joined", "org.member.left", "org.policy.changed", "org.invite.created", "org.pilot.ending",
       "organisation.dlq"}


class FakeAdmin:
    existing: set[str] = set()
    created: list = []

    def __init__(self, bootstrap_servers):
        self.bootstrap_servers = bootstrap_servers

    async def start(self): ...

    async def close(self): ...

    async def list_topics(self):
        return sorted(FakeAdmin.existing)

    async def create_topics(self, new_topics):
        for t in new_topics:
            FakeAdmin.created.append((t.name, t.num_partitions, t.replication_factor))
            FakeAdmin.existing.add(t.name)


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    FakeAdmin.existing, FakeAdmin.created = {"some.other.topic"}, []
    monkeypatch.setattr(create_topics, "AIOKafkaAdminClient", FakeAdmin)


def test_the_topic_list_is_what_this_service_produces_plus_its_dlq():
    assert set(create_topics.TOPICS) == ALL


async def test_creates_all_six_with_three_partitions_and_replication_one_by_default(capsys):
    assert await create_topics.main(3, 1) == 0
    assert {c[0] for c in FakeAdmin.created} == ALL
    assert {(c[1], c[2]) for c in FakeAdmin.created} == {(3, 1)}
    out = capsys.readouterr().out
    assert out.count("created") >= 6 and "6 created, 0 already existed" in out
    assert "some.other.topic" in FakeAdmin.existing                      # other topics are left alone


async def test_running_twice_creates_nothing_the_second_time(capsys):
    await create_topics.main(3, 1)
    FakeAdmin.created.clear()
    capsys.readouterr()
    assert await create_topics.main(3, 1) == 0
    assert FakeAdmin.created == []
    out = capsys.readouterr().out
    assert out.count("found") == 6 and "0 created, 6 already existed" in out


async def test_only_the_missing_topics_are_created_and_existing_ones_keep_their_partitions(capsys):
    FakeAdmin.existing |= {"org.member.joined", "org.member.left"}
    await create_topics.main(6, 1)
    assert {c[0] for c in FakeAdmin.created} == ALL - {"org.member.joined", "org.member.left"}
    out = capsys.readouterr().out
    assert "found    org.member.joined" in out and "created  organisation.dlq" in out


async def test_the_flags_change_partitions_and_replication(capsys):
    await create_topics.main(6, 2)
    assert {(c[1], c[2]) for c in FakeAdmin.created} == {(6, 2)}


async def test_a_topic_that_is_still_missing_afterwards_is_an_error(monkeypatch, capsys):
    async def create_nothing(self, new_topics):
        return None

    monkeypatch.setattr(FakeAdmin, "create_topics", create_nothing)
    assert await create_topics.main(3, 1) == 1
