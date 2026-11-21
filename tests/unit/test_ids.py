import threading
import time
import uuid

from app.utils.ids import Uuid7Generator, uuid7, uuid7_timestamp_ms


class Ticker:
    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


def test_version_and_variant_bits():
    u = uuid7()
    assert isinstance(u, uuid.UUID)
    assert u.version == 7
    assert u.variant == uuid.RFC_4122


def test_embeds_the_current_time_in_milliseconds():
    before = time.time_ns() // 1_000_000
    u = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= uuid7_timestamp_ms(u) <= after + 1


def test_time_ordering_across_milliseconds():
    first = uuid7()
    time.sleep(0.01)
    second = uuid7()
    assert first < second
    assert str(first) < str(second)  # the text form sorts the same way


def test_ids_in_one_burst_are_unique_and_strictly_increasing():
    ids = [uuid7() for _ in range(20_000)]
    assert len(set(ids)) == len(ids)
    assert all(a < b for a, b in zip(ids, ids[1:]))


def test_later_timestamps_sort_after_earlier_ones():
    clock = Ticker(1_700_000_000_000)
    gen = Uuid7Generator(clock)
    older = gen.new()
    clock.ms = 1_800_000_000_000
    newer = gen.new()
    assert older < newer
    assert uuid7_timestamp_ms(older) == 1_700_000_000_000
    assert uuid7_timestamp_ms(newer) == 1_800_000_000_000


def test_counter_overflow_borrows_forward_and_stays_ordered():
    gen = Uuid7Generator(Ticker(2_000_000_000_000))
    ids = [gen.new() for _ in range(10_000)]  # far more than 4096 in one millisecond
    assert all(a < b for a, b in zip(ids, ids[1:]))
    assert len(set(ids)) == len(ids)


def test_a_clock_that_goes_backwards_never_breaks_ordering():
    clock = Ticker(3_000_000_000_000)
    gen = Uuid7Generator(clock)
    a = gen.new()
    clock.ms -= 1_000
    b = gen.new()
    assert a < b


def test_unique_across_threads():
    out: list[uuid.UUID] = []

    def work():
        out.extend(uuid7() for _ in range(2_000))

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(set(out)) == len(out) == 16_000
