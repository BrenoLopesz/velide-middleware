"""Real local cutover/receipt recovery; only HTTP and the runtime graph are doubles."""
from types import SimpleNamespace

import pytest
from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QMenu

import middleware_rollout
import test_middleware_rollout as rollout_fixtures
from services.rollout_controller import RolloutController


rollout = rollout_fixtures.rollout


@pytest.fixture
def controllers(qtbot, monkeypatch):
    created = []

    def make(coordinator, server, name, token):
        graph = rollout_fixtures.Graph()
        monkeypatch.setattr("services.rollout_quiescence.GraphQuiescence", lambda runtimes: graph)

        class Auth(QObject):
            access_token = pyqtSignal(str)

        runtime = QObject()
        auth = Auth(runtime)
        auth._bundle_access_token = token
        menu = QMenu()
        runtime.tray = SimpleNamespace(contextMenu=lambda: menu)
        runtime.runtimes = {name: SimpleNamespace(services=SimpleNamespace(auth=auth))}
        controller = RolloutController(
            runtime, coordinator.config_path, coordinator.bundle_dir, transport=server)
        qtbot.addWidget(controller.dialog)
        controller.timer.stop()
        created.append((runtime, controller))
        return controller, graph, auth

    yield make
    for _, controller in reversed(created):
        controller.shutdown()


@pytest.mark.parametrize("failure", ["offline", "receipt_write"])
def test_active_native_ack_failures_retry_without_rebuilding(rollout, controllers, monkeypatch, failure):
    coordinator, server, _, bundle, _ = rollout
    server.fail_ack = True
    coordinator.begin()
    coordinator.poll()
    receipt = coordinator.state["receipt_id"]
    original_config = coordinator.config_path.read_bytes()

    controller, graph, auth = controllers(coordinator, server, "native", bundle["access_token"])
    activated = []
    controller.activated.connect(activated.append)
    assert controller.coordinator.state["state"] == "ACK_PENDING"
    assert not controller.coordinator._paused
    with monkeypatch.context() as patch:
        if failure == "receipt_write":
            server.fail_ack = False
            atomic_json = middleware_rollout.atomic_json

            def fail_applied(path, state):
                if state["state"] == "APPLIED":
                    raise OSError("acknowledgement receipt disk failure")
                return atomic_json(path, state)

            patch.setattr(middleware_rollout, "atomic_json", fail_applied)

        # Exercise both automatic paths, including another token publication
        # after the startup timer's failed acknowledgement.
        for trigger in (controller.tick, lambda: auth.access_token.emit(bundle["access_token"])):
            trigger()
            assert activated == []
            assert not graph.paused and graph.resumed == graph.released == 0
            assert controller.coordinator.state["state"] == "ACK_PENDING"
            assert middleware_rollout.read_state(coordinator.config_path)["state"] == "ACK_PENDING"
            assert controller.timer.isActive() and controller.timer.interval() == 30000

    server.fail_ack = False
    controller.tick()
    assert controller.coordinator.state["state"] == "APPLIED"
    assert middleware_rollout.read_state(coordinator.config_path)["state"] == "APPLIED"
    assert not controller.timer.isActive()
    assert activated == []
    assert coordinator.config_path.read_bytes() == original_config
    assert {call[2] for call in server.calls if call[0] == "ack"} == {receipt}
    # A retained controller's queued work is inert after runtime shutdown too.
    controller.shutdown()
    calls = len(server.calls)
    auth.access_token.emit(bundle["access_token"])
    controller.tick()
    controller.schedule()
    assert len(server.calls) == calls and not controller.timer.isActive()


@pytest.mark.parametrize("failure", ["none", "lost_ack", "post_commit_state_write"])
def test_committed_legacy_graph_hands_off_once_and_recovers_ack(
        rollout, controllers, monkeypatch, failure):
    coordinator, server, _, bundle, _ = rollout
    controller, graph, auth = controllers(coordinator, server, "legacy", "legacy-access")
    activated = []

    def handoff(result):
        activated.append(result)
        # main.rebuild shuts down the controller synchronously from this signal.
        # Also cover a receiver that defers the rebuild until a later event turn.
        if failure != "lost_ack":
            controller.shutdown()

    controller.activated.connect(handoff)
    controller._perform(controller.coordinator.begin)
    assert graph.paused
    server.fail_ack = failure == "lost_ack"
    with monkeypatch.context() as patch:
        if failure == "post_commit_state_write":
            atomic_json = middleware_rollout.atomic_json

            def fail_ack_pending(path, state):
                if state["state"] == "ACK_PENDING":
                    raise OSError("post-commit state disk failure")
                return atomic_json(path, state)

            patch.setattr(middleware_rollout, "atomic_json", fail_ack_pending)
        controller.tick()

    assert len(activated) == 1
    if failure == "post_commit_state_write":
        assert activated[0] is None
    else:
        assert activated[0].native_applied
    assert middleware_rollout.committed_receipt(
        coordinator.config_path, coordinator.bundle_dir, controller.coordinator.state)
    assert graph.resumed == 0
    expected = {"none": "APPLIED", "lost_ack": "ACK_PENDING", "post_commit_state_write": "APPLYING"}
    assert middleware_rollout.read_state(coordinator.config_path)["state"] == expected[failure]
    assert not controller.timer.isActive()
    calls = len(server.calls)
    auth.access_token.emit("legacy-access")
    controller.tick()
    controller.schedule()
    controller._perform(lambda: pytest.fail("retired controller ran another operation"))
    assert len(activated) == 1 and len(server.calls) == calls
    assert not controller.timer.isActive()
    controller.shutdown()

    # Startup/rebuild reads the actual DB receipt, including a durable APPLYING
    # state left behind by a failed post-commit sidecar write.
    server.fail_ack = False
    native, native_graph, _ = controllers(coordinator, server, "native", bundle["access_token"])
    native.activated.connect(activated.append)
    native.tick()
    assert native.coordinator.state["state"] == "APPLIED"
    assert len(activated) == 1
    assert not native_graph.paused and native_graph.resumed == native_graph.released == 0
    receipt = controller.coordinator.state["receipt_id"]
    assert {call[2] for call in server.calls if call[0] == "ack"} == {receipt}


def test_rejected_package_resumes_legacy_without_handoff(rollout, controllers):
    coordinator, server, _, _, _ = rollout
    controller, graph, _ = controllers(coordinator, server, "legacy", "legacy-access")
    activated = []
    controller.activated.connect(activated.append)
    original_config = coordinator.config_path.read_bytes()
    controller._perform(controller.coordinator.begin)
    server.corruption = "package_json"
    controller.tick()
    assert coordinator.config_path.read_bytes() == original_config
    assert controller.coordinator.state["state"] == "INVALIDATED"
    assert activated == []
    assert graph.resumed == 1 and not graph.paused
