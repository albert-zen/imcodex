from __future__ import annotations

import asyncio

from imcodex.bridge.rehydration_retry import (
    NativeWriterRehydrationMixin,
    NativeWriterRehydrationRetry,
)


async def _wait_until_stopped(
    retry: NativeWriterRehydrationRetry,
    *,
    timeout_s: float = 1.0,
) -> None:
    async with asyncio.timeout(timeout_s):
        while retry.running:
            await asyncio.sleep(0)


async def test_native_writer_retry_converges_within_finite_budget() -> None:
    attempts: list[tuple[int, frozenset[tuple[str, str, str]]]] = []
    binding_a = ("qq", "a", "thr_a")
    binding_b = ("qq", "b", "thr_b")

    async def attempt(epoch: int, bindings: frozenset[tuple[str, str, str]]):
        attempts.append((epoch, bindings))
        if len(attempts) == 1:
            return frozenset({binding_b})
        return frozenset()

    retry = NativeWriterRehydrationRetry(
        attempt,
        delays_s=(0, 0, 0),
    )

    await retry.schedule(7, frozenset({binding_a, binding_b}))
    await _wait_until_stopped(retry)

    assert attempts == [
        (7, frozenset({binding_a, binding_b})),
        (7, frozenset({binding_b})),
    ]


async def test_native_writer_retry_exhausts_without_polling_forever() -> None:
    attempts: list[frozenset[tuple[str, str, str]]] = []
    binding = frozenset({("qq", "conflict", "thr_conflict")})

    async def attempt(_epoch: int, bindings: frozenset[tuple[str, str, str]]):
        attempts.append(bindings)
        return bindings

    retry = NativeWriterRehydrationRetry(
        attempt,
        delays_s=(0, 0, 0),
    )

    await retry.schedule(3, binding)
    await _wait_until_stopped(retry)

    assert attempts == [binding, binding, binding]
    assert retry.mark_reconciled(next(iter(binding))) == {
        next(iter(binding)): "failed"
    }


async def test_native_writer_retry_replacement_cancels_prior_epoch_without_overlap() -> None:
    sleep_started = asyncio.Event()
    second_sleep_release = asyncio.Event()
    sleep_calls = 0
    attempts: list[int] = []

    async def sleep(_delay_s: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        sleep_started.set()
        if sleep_calls == 1:
            await asyncio.Event().wait()
        await second_sleep_release.wait()

    async def attempt(epoch: int, _bindings: frozenset[tuple[str, str, str]]):
        attempts.append(epoch)
        return frozenset()

    retry = NativeWriterRehydrationRetry(
        attempt,
        delays_s=(0,),
        sleep=sleep,
    )
    binding = frozenset({("qq", "conflict", "thr_conflict")})

    await retry.schedule(1, binding)
    await sleep_started.wait()
    await retry.schedule(2, binding)
    second_sleep_release.set()
    await _wait_until_stopped(retry)

    assert sleep_calls == 2
    assert attempts == [2]


async def test_native_writer_retry_drops_binding_reconciled_during_delay() -> None:
    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    attempts: list[frozenset[tuple[str, str, str]]] = []
    binding = ("qq", "conflict", "thr_conflict")

    async def sleep(_delay_s: float) -> None:
        sleep_started.set()
        await release_sleep.wait()

    async def attempt(_epoch: int, bindings: frozenset[tuple[str, str, str]]):
        attempts.append(bindings)
        return bindings

    retry = NativeWriterRehydrationRetry(attempt, delays_s=(0,), sleep=sleep)

    await retry.schedule(4, frozenset({binding}))
    await sleep_started.wait()
    retry.mark_reconciled(binding)
    release_sleep.set()
    await _wait_until_stopped(retry)

    assert attempts == []


async def test_native_writer_retry_does_not_readd_binding_reconciled_during_attempt() -> None:
    attempt_started = asyncio.Event()
    release_attempt = asyncio.Event()
    binding = ("qq", "conflict", "thr_conflict")
    attempts = 0

    async def attempt(_epoch: int, bindings: frozenset[tuple[str, str, str]]):
        nonlocal attempts
        attempts += 1
        assert bindings == frozenset({binding})
        attempt_started.set()
        await release_attempt.wait()
        return bindings

    retry = NativeWriterRehydrationRetry(attempt, delays_s=(0, 0))

    await retry.schedule(4, frozenset({binding}))
    await attempt_started.wait()
    retry.mark_reconciled(binding)
    release_attempt.set()
    await _wait_until_stopped(retry)

    assert attempts == 1


def test_writer_retry_summary_removes_binding_that_was_rebound_before_retry() -> None:
    summary = NativeWriterRehydrationMixin._merge_writer_retry_summary(
        previous={
            "total": 2,
            "succeeded": 1,
            "failed": 1,
            "unverified": 0,
            "activeWriterConflicts": 1,
        },
        attempted_count=1,
        attempt={"total": 0, "succeeded": 0, "failed": 0, "unverified": 0},
        remaining_count=0,
    )

    assert summary == {
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "unverified": 0,
    }


async def test_multi_binding_retry_does_not_double_count_ordinary_reconciliation() -> None:
    binding_a = ("qq", "a", "thr_a")
    binding_b = ("qq", "b", "thr_b")
    b_started = asyncio.Event()
    release_b = asyncio.Event()

    class HealthClient:
        def __init__(self) -> None:
            self.facts = {"connection_epoch": 9, "ready": True}

        def connection_facts(self) -> dict:
            return dict(self.facts)

        def update_ready_health(self, *, status: str, rehydration: dict) -> None:
            self.facts.update(status=status, rehydration=dict(rehydration))

    class Harness(NativeWriterRehydrationMixin):
        def __init__(self) -> None:
            client = HealthClient()
            self.backend = type("Backend", (), {"client": client})()
            self._native_writer_rehydration_summary = {
                "total": 2,
                "succeeded": 0,
                "failed": 2,
                "unverified": 0,
                "activeWriterConflicts": 2,
            }
            self._native_writer_rehydration_retry = NativeWriterRehydrationRetry(
                self._retry_native_writer_rehydration,
                delays_s=(0,),
            )

        async def _rehydrate_current_connection(self, *, binding_keys: set):
            binding_key = next(iter(binding_keys))
            if binding_key == binding_a:
                return (
                    {
                        "rehydration": {
                            "total": 1,
                            "succeeded": 0,
                            "failed": 0,
                            "unverified": 1,
                        }
                    },
                    {"retryableBindings": []},
                )
            b_started.set()
            await release_b.wait()
            return (
                {
                    "rehydration": {
                        "total": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "unverified": 0,
                    }
                },
                {"retryableBindings": []},
            )

    harness = Harness()
    await harness._native_writer_rehydration_retry.schedule(
        9,
        frozenset({binding_a, binding_b}),
    )
    await b_started.wait()

    harness._handle_binding_reconciled(*binding_a)
    release_b.set()
    await _wait_until_stopped(harness._native_writer_rehydration_retry)

    assert harness._native_writer_rehydration_summary == {
        "total": 2,
        "succeeded": 2,
        "failed": 0,
        "unverified": 0,
    }
    assert harness.backend.client.facts["status"] == "connected"


async def test_superseded_retry_is_not_decremented_again_when_new_thread_finishes() -> None:
    old_binding = ("qq", "conflict", "thr_old")
    new_binding = ("qq", "conflict", "thr_new")

    class HealthClient:
        def __init__(self) -> None:
            self.facts = {"connection_epoch": 10, "ready": True}

        def connection_facts(self) -> dict:
            return dict(self.facts)

        def update_ready_health(self, *, status: str, rehydration: dict) -> None:
            self.facts.update(status=status, rehydration=dict(rehydration))

    class Harness(NativeWriterRehydrationMixin):
        def __init__(self) -> None:
            client = HealthClient()
            self.backend = type("Backend", (), {"client": client})()
            self._native_writer_rehydration_summary = {
                "total": 2,
                "succeeded": 1,
                "failed": 1,
                "unverified": 0,
                "activeWriterConflicts": 1,
            }
            self._native_writer_rehydration_retry = NativeWriterRehydrationRetry(
                self._retry_native_writer_rehydration,
                delays_s=(0,),
            )

        async def _rehydrate_current_connection(self, *, binding_keys: set):
            assert binding_keys == {old_binding}
            return (
                {
                    "rehydration": {
                        "total": 0,
                        "succeeded": 0,
                        "failed": 0,
                        "unverified": 0,
                    }
                },
                {"retryableBindings": []},
            )

    harness = Harness()
    await harness._native_writer_rehydration_retry.schedule(
        10,
        frozenset({old_binding}),
    )
    await _wait_until_stopped(harness._native_writer_rehydration_retry)

    assert harness._native_writer_rehydration_summary == {
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "unverified": 0,
    }
    harness._handle_binding_reconciled(*new_binding)
    assert harness._native_writer_rehydration_summary == {
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "unverified": 0,
    }
