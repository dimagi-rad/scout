"""Every procrastinate job runs inside a Sentry ``queue.task`` transaction (#918)."""

import datetime

import pytest
import sentry_sdk
from procrastinate.exceptions import JobAborted
from procrastinate.testing import InMemoryConnector
from sentry_sdk.transport import Transport

from config.procrastinate import app
from config.task_tracing import _fetch_queued_at, queue_wait_seconds, trace_job

QUEUE = "tests_task_tracing"
UTC = datetime.UTC


@app.task(name="tests.traced_ok", queue=QUEUE)
async def traced_ok() -> None:
    with sentry_sdk.start_span(op="test.child", name="inside the task"):
        pass


@app.task(name="tests.traced_sync", queue=QUEUE)
def traced_sync() -> None:
    with sentry_sdk.start_span(op="test.child", name="inside the sync task"):
        pass


@app.task(name="tests.traced_fail", queue=QUEUE)
async def traced_fail() -> None:
    raise RuntimeError("boom")


class _CapturingTransport(Transport):
    def __init__(self):
        super().__init__()
        self.events = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.type == "transaction":
                self.events.append(item.payload.json)


class _StubConnector:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.calls = []

    async def execute_query_one_async(self, query, **arguments):
        self.calls.append(arguments)
        if self.error:
            raise self.error
        return self.row


@pytest.fixture
def sentry_transactions():
    transport = _CapturingTransport()
    client = sentry_sdk.Client(
        dsn="https://key@o0.ingest.sentry.io/1",
        transport=transport,
        traces_sample_rate=1.0,
        default_integrations=False,
    )
    scope = sentry_sdk.get_global_scope()
    previous = scope.client
    scope.set_client(client)
    yield transport.events
    scope.set_client(previous)


async def _run(task) -> str:
    connector = InMemoryConnector()
    with app.replace_connector(connector):
        async with app.open_async():
            job_id = await task.defer_async()
            await app.run_worker_async(
                queues=[QUEUE], wait=False, install_signal_handlers=False, listen_notify=False
            )
    return connector.jobs[job_id]["status"]


def test_worker_runs_every_job_through_the_tracing_middleware():
    assert trace_job in app.worker_defaults.get("worker_middleware", [])


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("task", "name"), [(traced_ok, "tests.traced_ok"), (traced_sync, "tests.traced_sync")]
)
async def test_job_is_a_queue_task_transaction(sentry_transactions, task, name):
    assert await _run(task) == "succeeded"

    [event] = sentry_transactions
    assert event["transaction"] == name
    assert event["contexts"]["trace"]["op"] == "queue.task"
    assert event["contexts"]["trace"]["status"] == "ok"
    assert event["tags"]["procrastinate.queue"] == QUEUE
    ops = {span["op"] for span in event["spans"]}
    # The task's own spans nest under the job, including a sync task's in its thread.
    assert {"queue.process", "test.child"} <= ops


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_failed_job_marks_the_transaction(sentry_transactions):
    assert await _run(traced_fail) == "failed"

    [event] = sentry_transactions
    assert event["contexts"]["trace"]["status"] == "internal_error"


@pytest.mark.asyncio
async def test_aborted_job_is_cancelled_not_failed(sentry_transactions):
    job = traced_ok.configure().job.evolve(id=9)
    context = type("Ctx", (), {"job": job, "start_timestamp": 0.0})()
    worker = type("W", (), {"app": type("A", (), {"connector": _StubConnector()})()})()

    async def call_next():
        raise JobAborted

    with pytest.raises(JobAborted):
        await trace_job(call_next, context, worker)
    [event] = sentry_transactions
    assert event["contexts"]["trace"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_queue_wait_is_recorded_from_the_last_enqueue(sentry_transactions):
    queued_at = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    connector = _StubConnector(row={"queued_at": queued_at})
    job = traced_ok.configure().job.evolve(id=7)
    context = type("Ctx", (), {"job": job, "start_timestamp": queued_at.timestamp() + 2.5})()
    worker = type("W", (), {"app": type("A", (), {"connector": connector})()})()

    async def call_next():
        return "done"

    assert await trace_job(call_next, context, worker) == "done"

    assert connector.calls == [{"job_id": 7}]
    [event] = sentry_transactions
    assert event["contexts"]["trace"]["data"]["queue.wait_ms"] == 2500
    [process] = [s for s in event["spans"] if s["op"] == "queue.process"]
    assert process["data"]["messaging.message.receive.latency"] == 2500


@pytest.mark.asyncio
async def test_unreadable_queue_time_does_not_fail_the_job(sentry_transactions):
    connector = _StubConnector(error=RuntimeError("db down"))
    job = traced_ok.configure().job.evolve(id=8)
    context = type("Ctx", (), {"job": job, "start_timestamp": 0.0})()
    worker = type("W", (), {"app": type("A", (), {"connector": connector})()})()

    async def call_next():
        return "done"

    assert await trace_job(call_next, context, worker) == "done"
    [event] = sentry_transactions
    assert "queue.wait_ms" not in event["contexts"]["trace"].get("data", {})


@pytest.mark.asyncio
@pytest.mark.django_db(transaction=True)
async def test_queue_time_query_runs_against_the_real_event_table():
    """Every event type the query names must exist in procrastinate's enum, or
    Postgres rejects the query and the wait is silently never recorded."""
    before = datetime.datetime.now(UTC)
    job_id = await traced_ok.defer_async()
    worker = type("W", (), {"app": app})()

    queued_at = await _fetch_queued_at(worker, job_id)

    assert queued_at is not None
    assert queued_at >= before - datetime.timedelta(seconds=5)


def test_queue_wait_starts_at_the_later_of_enqueue_and_schedule():
    queued = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    scheduled = queued + datetime.timedelta(minutes=5)
    start = (scheduled + datetime.timedelta(seconds=3)).timestamp()
    assert queue_wait_seconds(queued, scheduled, start) == pytest.approx(3.0)
    assert queue_wait_seconds(queued, None, queued.timestamp() + 1) == pytest.approx(1.0)
    assert queue_wait_seconds(None, None, 0.0) is None
    # Clock skew between DB and worker never reports a negative wait.
    assert queue_wait_seconds(queued, None, queued.timestamp() - 1) == 0.0
