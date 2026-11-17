import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from api.sync_journal import SyncJournal
from connectors.farmax.cancellation import cancellation_progress
from services.native_change_feed_service import NativeFeedWorker
from test_restored_write_fences import seeded


def page(after="0", end="1", head="1", revision="10", internal="123"):
    return dict(afterSequence=after, eventSequence=end, headSequence=head, resyncRequired=False,
                events=[dict(sequence=end, externalOrderId=internal, revision=revision)] if after != end else [])


class Client:
    def __init__(self, response, states):
        self.response, self.states = response, states
        self.calls = []
        self.after_read = lambda: None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def order_changes(self, after):
        self.calls.append(("changes", after))
        return self.response

    async def list_orders(self, after):
        self.calls.append(("inventory", after))
        return [state for state in self.states if state["externalOrderId"] > after][:100]

    async def order_states(self, refs):
        self.calls.append(("states", refs))
        self.after_read()
        return self.states


def worker(journal, client, current=lambda: True):
    gateway = SimpleNamespace(is_ready=lambda: True, get_client=lambda: client)
    return NativeFeedWorker(gateway, journal, current)


def journal_at(tmp_path):
    return SyncJournal(str(tmp_path / "feed.db"), "feed")


@pytest.mark.asyncio
async def test_worker_persists_cursor_before_fetch_and_restart_drains_dirty_work(tmp_path, remote_state):
    journal = journal_at(tmp_path)
    journal.accept_change_page(page())
    journal = journal_at(tmp_path)
    client = Client(page(after="1", end="1"), [remote_state(revision="10")])
    result = await worker(journal, client).reconcile()
    assert client.calls == [("changes", "1"), ("states", ["123"])]
    assert journal.dirty_orders() == [] and result["publications"]
    assert journal.next_effect()["operation"] == "DONE"


@pytest.mark.asyncio
async def test_new_notification_during_http_read_keeps_work_and_fences_older_state(tmp_path, remote_state):
    journal = journal_at(tmp_path)
    client = Client(page(), [remote_state(revision="10")])
    client.after_read = lambda: journal.accept_change_page(page(after="1", end="2", head="2", revision="11"))
    await worker(journal, client).reconcile()
    assert journal.feed_status()["cursor"] == "2"
    assert journal.dirty_orders() == [{"internal_id": "123"}]
    assert journal.next_effect() is None
    client.response, client.states = page(after="2", end="2", head="2"), [remote_state("PENDING", revision="11", execution=None)]
    client.after_read = lambda: None
    await worker(journal, client).reconcile()
    assert journal.next_effect()["operation"] == "WAITING"


@pytest.mark.asyncio
async def test_same_revision_notification_during_read_cannot_be_consumed(tmp_path, remote_state):
    journal = journal_at(tmp_path)
    client = Client(page(), [remote_state("PENDING", revision="10", execution=None)])
    client.after_read = lambda: journal.accept_change_page(page(after="1", end="2", head="2", revision="10"))
    result = await worker(journal, client).reconcile()
    assert result["more"] and journal.dirty_orders()
    assert journal.get_order("123") is None and journal.next_effect() is None
    # A concurrent periodic read has no claim on the new notification either.
    journal.native_states(client.states)
    assert journal.dirty_orders() and journal.get_order("123") is None
    client.after_read = lambda: None
    client.response = page(after="2", end="2", head="2")
    await worker(journal, client).reconcile()
    assert not journal.dirty_orders()
    assert journal.next_effect()["operation"] == "WAITING"


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_request", [False, True])
async def test_delayed_feed_read_cannot_reopen_acknowledged_cancellation(
        tmp_path, farmax_stack, remote_state, legacy_request):
    repo, engine = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("IN_TRANSIT", revision="1")])
    selected = journal.next_effect()
    journal.accept_change_page(page(after="0", end="0", head="0"))
    with engine.begin() as conn:
        conn.execute(text("UPDATE VENDAS SET STATUS='C'"))
    journal.record_farmax_progress("123", cancellation_progress(repo.fetch_sales_statuses_by_id((123,))))
    if legacy_request:
        # A recovered pre-request-ID intent must retain the reserved receipt.
        with sqlite3.connect(journal.path) as conn:
            conn.execute("UPDATE SyncOrders SET cancellation_request_id=NULL,cancellation_evidence=NULL")
    request = journal.claim(native=True)
    request_id = request["cancellation_request_id"]
    if legacy_request:
        assert request_id == "legacy"

    started, release = asyncio.Event(), asyncio.Event()
    class DelayedClient(Client):
        async def order_states(self, refs):
            self.calls.append(("states", refs))
            snapshot = self.states
            started.set()
            await release.wait()
            return snapshot
    client = DelayedClient(page(revision="2"), [remote_state("PENDING", revision="2", execution=None)])
    read = asyncio.create_task(worker(journal, client).reconcile())
    try:
        await asyncio.wait_for(started.wait(), 5)
        # The worker has persisted its cursor/claim and started the state read.
        # A cancellation ACK now wins the race with that older HTTP response.
        assert client.calls == [("changes", "0"), ("states", ["123"])]
        assert journal.feed_status()["cursor"] == "1" and journal.dirty_orders()
        assert journal.cancelled("123", request["attempts"], request_id)
        # Restore commercial eligibility independently. A wrongly authorized
        # stale WAITING target could now actually change the ERP's R fields.
        with engine.begin() as conn:
            conn.execute(text("UPDATE VENDAS SET STATUS='S'"))
    finally:
        release.set()
        await read

    writes = []
    def capture(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ")):
            writes.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert journal.apply_effect(selected["sequence"], selected["generation"], repo,
                                    "America/Sao_Paulo", lambda: True) is None
        stale = journal.next_effect()
        if stale:
            journal.apply_effect(stale["sequence"], stale["generation"], repo,
                                 "America/Sao_Paulo", lambda: True)
        assert writes == []
        assert stale is None
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    journal = SyncJournal(journal.path, journal.scope, source_timezone="America/Sao_Paulo")
    retained = journal.get_order("123")
    assert (retained["cancellation"], retained["submission"], retained["remote_revision"]) == (
        "ACKNOWLEDGED", "TERMINAL", "1")
    assert journal.dirty_orders() == [{"internal_id": "123"}]
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT disposition FROM SyncCancellationReceipts").fetchall() == [("ACKNOWLEDGED",)]

    # A later notification wakes the retained dirty order; the real cancellation
    # state remains acknowledged instead of being poisoned by a local receipt.
    client.response = page(after="1", end="2", head="2", revision="3")
    client.states = [remote_state("CANCELLED", revision="3", execution=None,
        cancellation="ACKNOWLEDGED", cancellationRequestId=request_id)]
    await worker(journal, client).reconcile()
    assert journal.get_order("123")["cancellation"] == "ACKNOWLEDGED"
    assert not journal.dirty_orders() and journal.next_effect() is None

    # Explicit same-request supersession still restores projection, including
    # the legacy receipt whose optional request ID may be absent in recovery.
    client.response = page(after="2", end="3", head="3", revision="4")
    client.states = [remote_state("PENDING", revision="4", execution=None,
        cancellation="SUPERSEDED", cancellationRequestId=None if legacy_request else request_id)]
    await worker(journal, client).reconcile()
    restored = journal.next_effect()
    assert restored["operation"] == "WAITING"
    observation = journal.apply_effect(restored["sequence"], restored["generation"], repo,
                                       "America/Sao_Paulo", lambda: True)
    assert journal.effect_result(restored["sequence"], generation=restored["generation"], observation=observation)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT STATUS,CD_ENTREGADOR,HORA_SAIDA,HORA_CHEGADA FROM ENTREGAS")).fetchone() == (
            "S", None, None, None)
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT disposition FROM SyncCancellationReceipts ORDER BY disposition").fetchall() == [
            ("ACKNOWLEDGED",), ("SUPERSEDED",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("cached_status", ["CANCELLED", "PENDING"])
async def test_post_ack_feed_read_recovers_migrated_remote_only_cancellation(
        tmp_path, farmax_stack, remote_state, cached_status):
    repo, _ = farmax_stack
    journal = seeded(tmp_path, repo)
    # V5 marked remote-only terminal states ACKNOWLEDGED too, and could then
    # cache an active snapshot without clearing that old cancellation latch.
    journal.native_states([remote_state(cached_status, revision="1", execution=None)])
    with sqlite3.connect(journal.path) as conn:
        conn.execute("""UPDATE SyncOrders SET cancellation='ACKNOWLEDGED',
            cancellation_request_id='legacy',cancellation_evidence='RECOVERED_UNKNOWN'""")
    restored = remote_state("PENDING", revision="2", execution=None)
    journal.native_states([restored])
    assert journal.get_order("123")["cancellation"] == "ACKNOWLEDGED"  # No read-start fence.
    client = Client(page(revision="2"), [restored])
    await worker(journal, client).reconcile()
    assert journal.get_order("123")["cancellation"] == "NONE"
    assert journal.next_effect()["operation"] == "WAITING"
    assert not journal.dirty_orders()


@pytest.mark.asyncio
async def test_post_ack_legacy_read_fence_rejects_intervening_local_ack(tmp_path, farmax_stack, remote_state):
    repo, _ = farmax_stack
    journal = seeded(tmp_path, repo)
    journal.native_states([remote_state("PENDING", execution=None)])
    journal.cancel("123")
    with sqlite3.connect(journal.path) as conn:
        conn.execute("UPDATE SyncOrders SET cancellation_request_id=NULL,cancellation_evidence=NULL")
    request = journal.claim(native=True)
    assert journal.cancelled("123", request["attempts"], "legacy")
    client = Client(page(revision="2"), [remote_state("PENDING", revision="2", execution=None)])
    # Legacy callback paths can repeat an ACK without changing its request ID.
    # The read must match the local generation as well as the ACK disposition.
    client.after_read = lambda: journal.cancelled("123")
    await worker(journal, client).reconcile()
    assert journal.get_order("123")["cancellation"] == "ACKNOWLEDGED"
    assert journal.dirty_orders() and journal.next_effect() is None
    with sqlite3.connect(journal.path) as conn:
        assert conn.execute("SELECT disposition FROM SyncCancellationReceipts").fetchall() == [("ACKNOWLEDGED",)]


@pytest.mark.asyncio
async def test_resync_inventory_cursor_survives_crash_and_replays_boundary(tmp_path, remote_state):
    journal = journal_at(tmp_path)
    journal.accept_change_page(dict(afterSequence="0", eventSequence="0", headSequence="500",
                                   resyncRequired=True, events=[]))
    states = [remote_state("PENDING", revision="20", execution=None, externalOrderId="{:03d}".format(i),
                           deliveryId="remote-{}".format(i)) for i in range(101)]
    client = Client(None, states)
    await worker(journal, client).reconcile()
    assert journal.feed_status()["cursor"] == "0"
    assert journal.feed_status()["resync_after"] == "099"
    journal = journal_at(tmp_path)
    await worker(journal, client).reconcile()
    assert journal.feed_status()["cursor"] == "500"
    assert journal.feed_status()["resync_boundary"] is None
    client.response = page(after="500", end="501", head="501", revision="21", internal="000")
    client.states = [remote_state("IN_TRANSIT", revision="21", externalOrderId="000", deliveryId="remote-0")]
    await worker(journal, client).reconcile()
    assert journal.get_order("000")["remote_revision"] == "21"
    assert journal.feed_status()["cursor"] == "501"


def test_out_of_order_transport_page_cannot_regress_cursor_or_drop_work(tmp_path):
    journal = journal_at(tmp_path)
    assert journal.accept_change_page(page())
    assert journal.accept_change_page(page(after="1", end="2", head="2", internal="456"))
    assert journal.accept_change_page(page()) is False
    assert journal.feed_status()["cursor"] == "2"
    assert journal.dirty_orders() == [{"internal_id": "123"}, {"internal_id": "456"}]


def test_invalid_page_cannot_checkpoint_partial_work(tmp_path):
    journal = journal_at(tmp_path)
    value = page()
    value["events"].append(dict(sequence="2", externalOrderId="456", revision="10"))
    with pytest.raises(ValueError):
        journal.accept_change_page(value)
    assert journal.feed_status()["cursor"] == "0" and journal.dirty_orders() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["missing", "older", "empty", "failure"])
async def test_unresolved_dirty_prefix_is_fair_and_retained_across_restart(tmp_path, remote_state, response):
    journal = journal_at(tmp_path)
    for i in range(101):
        journal.accept_change_page(page(after=str(i), end=str(i+1), head=str(i+1),
                                       internal="{:03d}".format(i)))
    class Unresolved(Client):
        async def order_states(self, refs):
            self.calls.append(("states", refs))
            if refs == ["100"]:
                return [remote_state("PENDING", execution=None, externalOrderId="100", revision="10")]
            if response == "failure":
                raise OSError("Offline")
            if response == "empty":
                return []
            return [remote_state("PENDING", execution=None, externalOrderId=ref,
                                 found=response != "missing", revision="9" if response == "older" else "10")
                    for ref in refs]
    client = Unresolved(page(after="101", end="101", head="101"), [])
    if response == "failure":
        with pytest.raises(OSError):
            await worker(journal, client).reconcile()
    else:
        await worker(journal, client).reconcile()
    journal = journal_at(tmp_path)
    await worker(journal, client).reconcile()
    assert client.calls[-1] == ("states", ["100"])
    assert journal.get_order("100")["remote_revision"] == "10"
    assert len(journal.dirty_orders()) == 100
    assert journal.dirty_orders(due_only=True) == []
    # A new notification interrupts the old retry lease even at the same revision.
    journal.accept_change_page(page(after="101", end="102", head="102", internal="000"))
    assert journal.dirty_orders(due_only=True) == [{"internal_id": "000"}]


@pytest.mark.asyncio
async def test_continuously_invalidated_prefix_cannot_jump_ahead_of_unvisited_order(tmp_path, remote_state):
    journal = journal_at(tmp_path)
    for i in range(101):
        journal.accept_change_page(page(after=str(i), end=str(i+1), head=str(i+1),
                                       internal="{:03d}".format(i)))
    class Busy(Client):
        async def order_changes(self, after):
            return page(after=after, end=after, head=after)
        async def order_states(self, refs):
            self.calls.append(refs)
            after = int(journal.feed_status()["cursor"])
            for i in range(100):
                journal.accept_change_page(page(after=str(after+i), end=str(after+i+1),
                                               head=str(after+i+1), internal="{:03d}".format(i)))
            return [remote_state("PENDING", execution=None, externalOrderId=ref,
                                 deliveryId="remote-"+ref, revision="10") for ref in refs]
    client = Busy(None, [])
    await worker(journal, client).reconcile()
    journal = journal_at(tmp_path)
    await worker(journal, client).reconcile()
    assert client.calls[1][0] == "100"
    assert journal.get_order("100")["remote_revision"] == "10"


@pytest.mark.asyncio
async def test_session_invalidation_during_response_does_not_commit(tmp_path, remote_state):
    journal = journal_at(tmp_path)
    valid = [True]
    client = Client(page(), [remote_state(revision="10")])
    client.after_read = lambda: valid.__setitem__(0, False)
    assert await worker(journal, client, lambda: valid[0]).reconcile() is None
    # Notification is durable, the untrusted read is not.
    assert journal.feed_status()["cursor"] == "1" and journal.dirty_orders()
    assert journal.get_order("123") is None


@pytest.mark.asyncio
async def test_http_bootstrap_contract_uses_nullable_cursor_and_source_binding():
    import json
    import httpx
    from test_native_sync import client as native_client, MERCHANT, SOURCE
    api = native_client()
    requests = []
    response = dict(afterSequence=None, eventSequence="0", headSequence="8", snapshotSequence="8",
                    resyncRequired=True, events=[])
    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"integrationOrderChanges": response}})
    async with api:
        await api._client.aclose()
        api._client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        assert await api.order_changes(None) == response
    assert requests[0]["variables"] == dict(merchantId=MERCHANT, connectionId=SOURCE, after=None)
    assert "limit:100" in requests[0]["query"]


@pytest.mark.asyncio
async def test_socket_reconnect_uses_durable_cursor_and_commits_before_wakeup(tmp_path, monkeypatch):
    from unittest.mock import MagicMock
    from test_native_sync import client as native_client
    from workers.velide_websockets_worker import VelideWebsocketsWorker
    journal = journal_at(tmp_path)
    journal.accept_change_page(page())
    variables, wakeups = [], []
    class Transport:
        def __init__(self, **kwargs):
            pass
    class Socket:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def subscribe(self, query, variable_values):
            variables.append(variable_values)
            yield {"integrationOrderChanges": page(after="1", end="2", head="2", internal="456")}
    monkeypatch.setattr("workers.velide_websockets_worker.WebsocketsTransport", Transport)
    monkeypatch.setattr("workers.velide_websockets_worker.Client", Socket)
    socket = VelideWebsocketsWorker(native_client().config, MagicMock(), journal)
    socket.signals.refresh_requested.connect(lambda: wakeups.append((journal.feed_status()["cursor"], journal.dirty_orders())))
    await socket._subscribe_and_listen("test-token")
    assert variables[0]["after"] == "1"
    assert wakeups == [("2", [{"internal_id": "123"}, {"internal_id": "456"}])]
