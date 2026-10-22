"""The pieces the three pulls share: the backoff, the wait budget and the window limiter, with no network at all."""

from __future__ import annotations

import random

import pytest

from relay.pulls.common import MAX_TOTAL_WAIT_S, MAX_WAIT_S, PullResult, PullThrottled, Waiter, WindowLimiter, backoff_s

from .pull_helpers import Clock, pull_env


def test_the_backoff_doubles_from_the_base_is_jittered_between_half_and_all_and_is_capped():
    for attempt, top in ((0, 5.0), (1, 10.0), (2, 20.0), (3, 40.0), (4, 80.0), (5, 120.0), (9, 120.0), (40, 120.0)):
        values = {backoff_s(attempt, random.Random(seed)) for seed in range(50)}
        assert all(top / 2 <= v <= top for v in values), (attempt, min(values), max(values))
        assert len(values) > 20 and min(values) < top * 0.75 < max(values)  # it is jitter, not a constant
    assert backoff_s(-3, random.Random(1)) <= 5.0  # a negative attempt does not shrink below the base
    assert backoff_s(2, random.Random(1), base=1.0, cap=3.0) <= 3.0


def test_the_waiter_sleeps_through_the_injected_sleeper_and_counts_what_it_slept():
    env, clock = pull_env()
    result = PullResult()
    waiter = Waiter(env, result)
    waiter.wait(10, "x")
    waiter.wait(20.5, "y")
    assert clock.sleeps == [10, 20.5] and result.waited_s == 30.5


def test_one_wait_over_the_cap_ends_the_run_without_sleeping_and_carries_the_asked_for_time():
    env, clock = pull_env()
    waiter = Waiter(env, PullResult())
    waiter.wait(MAX_WAIT_S, "exactly the cap")  # control: the cap itself is allowed
    with pytest.raises(PullThrottled, match="too long") as err:
        waiter.wait(MAX_WAIT_S + 1, "too long")
    assert err.value.after_s == MAX_WAIT_S + 1 and clock.sleeps == [MAX_WAIT_S]


def test_the_run_total_is_capped_too():
    env, clock = pull_env()
    waiter = Waiter(env, PullResult())
    for _ in range(int(MAX_TOTAL_WAIT_S // MAX_WAIT_S)):
        waiter.wait(MAX_WAIT_S, "w")
    assert sum(clock.sleeps) == MAX_TOTAL_WAIT_S
    with pytest.raises(PullThrottled):
        waiter.wait(1, "one more second")


def test_a_limiter_with_a_free_slot_never_sleeps_and_a_full_one_waits_for_the_oldest_call():
    clock = Clock(now=0.0)
    w = WindowLimiter(2, 10.0, clock.monotonic, clock.sleep)
    assert w.ready_in() == 0.0 and w.acquire() == 0.0 and clock.sleeps == []
    clock.now = 3.0
    assert w.acquire() == 0.0 and w.ready_in() == pytest.approx(7.0)  # full: the call at 0 leaves the window at 10
    assert w.acquire() == pytest.approx(7.0) and clock.now == pytest.approx(10.0)


def test_a_call_exactly_one_window_old_has_left_the_window():
    clock = Clock(now=0.0)
    w = WindowLimiter(1, 10.0, clock.monotonic, clock.sleep)
    w.acquire()
    clock.now = 10.0
    assert w.ready_in() == 0.0 and len(w.calls) == 1  # the next call may go: the one at 0 is a full window old
    w.acquire()
    assert len(w.calls) == 1 and w.calls[0] == 10.0  # and the old one was dropped from the record


@pytest.mark.parametrize("seed", range(40))
def test_property_a_window_limiter_never_lets_more_than_its_limit_into_any_window(seed):
    rng = random.Random(seed)
    limit, window = rng.randint(1, 8), rng.choice([5.0, 10.0, 60.0])
    clock = Clock(now=0.0)
    w = WindowLimiter(limit, window, clock.monotonic, clock.sleep)
    times = []
    for _ in range(rng.randint(30, 150)):
        clock.now += rng.choice([0.0, 0.0, 0.01, 0.5, 3.0, window + 1])
        w.acquire()
        times.append(clock.now)
    for i, t in enumerate(times):
        assert sum(1 for u in times[: i + 1] if t - u < window) <= limit
    assert sum(clock.sleeps) <= window * len(times)
