import pytest
from unittest.mock import MagicMock

from connectors.farmax.cancellation import classify_cancellation, cancellation_progress, CancellationDecision
from connectors.farmax.farmax_status_tracker import FarmaxStatusTracker
from models.farmax_models import FarmaxSale


@pytest.mark.parametrize("statuses,exists,deleted,concluded,expected", [
    (["C", "C"], True, False, "S", "CANCEL"),
    (["C", "V"], True, False, "S", "ACTIVE"),
    (["D", "V"], True, False, "S", "ACTIVE"),
    (["D"], True, False, "S", "UNRESOLVED"),
    (["V", "V"], True, False, "S", "ACTIVE"),
    (["I", "I"], False, True, "S", "CANCEL"),
    (["V", "V"], False, True, "S", "CANCEL"),
    (["C", "V"], False, True, "S", "CANCEL"),
    (["S", "C"], False, True, "S", "CANCEL"),
    (["S", "V", "C"], False, True, "S", "CANCEL"),
    (["C", None], True, False, "S", "UNRESOLVED"),
    (["I", "I"], True, False, "S", "UNRESOLVED"),
    (["I"], False, False, "S", "UNRESOLVED"),
    (["V"], False, True, "N", "REMOVAL_CANDIDATE"),
    ([], False, True, "S", "UNRESOLVED"),
])
def test_sale_level_evidence(statuses, exists, deleted, concluded, expected):
    items = [FarmaxSale(cd_venda=123, cd_filial=1, id_produto=100, ordem=index,
                        evidence_complete=True, snapshot_item_count=len(statuses), status=status, concluido=concluded,
                        delivery_exists=exists, delivery_deleted=deleted)
             for index, status in enumerate(statuses)]
    assert classify_cancellation(items) == CancellationDecision(expected)


def test_tracker_groups_items_and_only_requests_cancellation(qtbot):
    persistence = MagicMock()
    persistence.get_external_id.return_value = "remote"
    tracker = FarmaxStatusTracker(MagicMock(), persistence)
    tracker._is_running = True
    received = []
    tracker.order_cancelled.connect(lambda *args: received.append(args))
    tracker._on_statuses_retrieved([
        item(123, 1, "C"), item(123, 2, "V"),
        item(124, 1, "C"), item(124, 2, "C"),
    ])
    assert received == [("124", "remote")]
    persistence.mark_as_cancelled.assert_not_called()


def item(sale, position, status, **changes):
    values = dict(cd_venda=sale, cd_filial=1, id_produto=100, ordem=position,
                  status=status, evidence_complete=True, snapshot_item_count=2, delivery_exists=True)
    values.update(changes)
    return FarmaxSale(**values)


def test_progress_counts_null_and_repeated_product_rows_without_false_100_percent():
    progress = cancellation_progress([item(123, 1, "C"), item(123, 2, None)])
    assert progress["total_items"] == 2
    assert progress["cancelled_items"] == 1
    assert progress["unknown_items"] == 1
    assert progress["percentage"] == 50
    assert progress["decision"] == "UNRESOLVED"


@pytest.mark.parametrize("rows", [
    [item(123, 1, "C"), item(123, 1, "C")],
    [item(123, 1, "C"), item(123, 2, "C", cd_filial=2)],
    [item(123, 1, "C"), item(123, 2, "C", id_produto=None)],
    [item(123, 1, "C")],  # truncated snapshot
    [item(123, 1, "C"), item(124, 2, "C")],
])
def test_ambiguous_or_incomplete_snapshot_never_authorizes_cancellation(rows):
    progress = cancellation_progress(rows)
    assert progress["percentage"] is None
    assert progress["decision"] == "UNRESOLVED"


def test_partial_then_all_cancelled_survives_tracker_restart(qtbot, caplog):
    import logging
    persistence = MagicMock()
    persistence.journal = None
    persistence.get_external_id.return_value = "remote"
    requests, reviews, active, progress = [], [], [], []
    for statuses in (("S", "S"), ("C", "S"), ("C", "C")):
        # No in-memory accumulation is needed: repository supplies all rows.
        tracker = FarmaxStatusTracker(MagicMock(), persistence)
        tracker._is_running = True
        tracker.order_cancelled.connect(lambda *args: requests.append(args))
        tracker.review_required.connect(lambda *args: reviews.append(args))
        tracker.source_active.connect(active.append)
        tracker.cancellation_progress.connect(lambda key, value: progress.append(value))
        with caplog.at_level(logging.INFO):
            tracker._on_statuses_retrieved([item(123, i + 1, status) for i, status in enumerate(statuses)])
    assert [row["percentage"] for row in progress] == [0, 50, 100]
    assert requests == [("123", "remote")]
    assert active == ["123", "123"] and reviews == []
    assert "1/2 itens cancelados (50.0%); entrega permanece ativa" in caplog.text
    persistence.mark_as_cancelled.assert_not_called()


@pytest.mark.parametrize("has_journal", [False, True])
def test_progress_emits_every_poll_without_repeated_logs(qtbot, has_journal):
    persistence = MagicMock()
    persistence.journal = MagicMock() if has_journal else None
    tracker = FarmaxStatusTracker(MagicMock(), persistence)
    tracker._is_running = True
    tracker._logger = MagicMock()
    progress, active = [], []
    tracker.cancellation_progress.connect(lambda key, value: progress.append(value))
    tracker.source_active.connect(active.append)
    partial = [item(123, 1, "C"), item(123, 2, "S")]
    for _ in range(3):
        tracker._on_statuses_retrieved(partial)
    assert len(progress) == 3 and active == ["123"] * 3
    assert tracker._logger.info.call_count == (0 if has_journal else 1)
    tracker._on_statuses_retrieved([item(123, 1, "S"), item(123, 2, "S")])
    tracker._on_statuses_retrieved(partial)
    assert tracker._logger.info.call_count == (0 if has_journal else 2)
    tracker._logger.warning.assert_not_called()


def test_batch_lock_waits_for_all_workers(qtbot):
    tracker = FarmaxStatusTracker(MagicMock(), MagicMock())
    tracker._pending_workers = 2
    tracker._is_processing = True
    tracker._set_processing_finished()
    assert tracker._is_processing
    tracker._set_processing_finished()
    assert not tracker._is_processing
