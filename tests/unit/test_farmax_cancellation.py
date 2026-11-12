import pytest
from unittest.mock import MagicMock

from connectors.farmax.cancellation import classify_cancellation, CancellationDecision
from connectors.farmax.farmax_status_tracker import FarmaxStatusTracker
from models.farmax_models import FarmaxSale


@pytest.mark.parametrize("statuses,exists,deleted,concluded,expected", [
    (["C", "C"], True, False, "S", "UNRESOLVED"),
    (["C", "V"], True, False, "S", "UNRESOLVED"),
    (["D", "V"], True, False, "S", "UNRESOLVED"),
    (["D"], True, False, "S", "UNRESOLVED"),
    (["V", "V"], True, False, "S", "ACTIVE"),
    (["I", "I"], False, True, "S", "REMOVAL_CANDIDATE"),
    (["V", "V"], False, True, "S", "REMOVAL_CANDIDATE"),
    (["C", "V"], False, True, "S", "REMOVAL_CANDIDATE"),
    (["S", "C"], False, True, "S", "REMOVAL_CANDIDATE"),
    (["S", "V", "C"], False, True, "S", "REMOVAL_CANDIDATE"),
    (["I"], False, False, "S", "UNRESOLVED"),
    (["V"], False, True, "N", "REMOVAL_CANDIDATE"),
    ([], False, True, "S", "UNRESOLVED"),
])
def test_sale_level_evidence(statuses, exists, deleted, concluded, expected):
    items = [FarmaxSale(cd_venda=123, status=status, concluido=concluded,
                        delivery_exists=exists, delivery_deleted=deleted)
             for status in statuses]
    assert classify_cancellation(items) == CancellationDecision(expected)


def test_tracker_groups_items_and_only_requests_cancellation(qtbot):
    persistence = MagicMock()
    persistence.get_external_id.return_value = "remote"
    tracker = FarmaxStatusTracker(MagicMock(), persistence)
    tracker._is_running = True
    received = []
    tracker.order_cancelled.connect(lambda *args: received.append(args))
    tracker._on_statuses_retrieved([
        FarmaxSale(cd_venda=123, status="C"),
        FarmaxSale(cd_venda=123, status="V"),
        FarmaxSale(cd_venda=124, status="C"),
        FarmaxSale(cd_venda=124, status="C"),
    ])
    assert received == []  # All-C is not authorization to remove a delivery.
    persistence.mark_as_cancelled.assert_not_called()


def test_batch_lock_waits_for_all_workers(qtbot):
    tracker = FarmaxStatusTracker(MagicMock(), MagicMock())
    tracker._pending_workers = 2
    tracker._is_processing = True
    tracker._set_processing_finished()
    assert tracker._is_processing
    tracker._set_processing_finished()
    assert not tracker._is_processing
