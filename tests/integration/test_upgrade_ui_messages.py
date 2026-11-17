import logging

import pytest

from local_upgrade import upgrade_installation
from models.log_table_model import LogTableModel
from test_automatic_upgrade import installed, package_for, receipt, supply
from utils.log_handler import QLogHandler


def dashboard_messages(instructions):
    model = LogTableModel()
    handler = QLogHandler()
    handler.new_log.connect(model.add_log_entry)
    logger = logging.Logger("test.upgrade.dashboard", level=logging.INFO)
    logger.addHandler(handler)
    try:
        # Match main's existing INFO loop, without a special renderer/extra flag.
        for instruction in instructions:
            logger.info(instruction)
        return [model.data(model.index(row, 2)) for row in range(model.rowCount())]
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_deferred_upgrade_info_reaches_real_qloghandler_in_portuguese_and_keeps_diagnostic(tmp_path, qtbot):
    config, database, _, _ = installed(tmp_path)
    adopted = upgrade_installation(config, tmp_path)
    package = package_for(config, database, adopted)
    package["binding"]["installation_id"] = "wrong-installation"
    supply(config, package)
    original = config.read_bytes()
    result = upgrade_installation(config, tmp_path)
    assert result.package_error == "Migration package does not match this installation/configuration"
    assert config.read_bytes() == original and result.settings.api.native is None
    messages = dashboard_messages(result.instructions)
    assert messages == result.instructions and len(messages) == 1
    assert "Não foi possível concluir a migração" in messages[0]
    assert "configuração atual continua ativa" in messages[0]
    assert "dados foram preservados" in messages[0] and "suporte" in messages[0]
    assert result.package_error not in messages[0] and "Native cutover" not in messages[0]


@pytest.mark.parametrize("count", [1, 2])
def test_cds_history_info_reaches_real_qloghandler_as_confirmation_not_processing_or_loss(tmp_path, qtbot, count):
    config, _, folder, _ = installed(tmp_path, "CDS", mappings=False)
    for index in range(count):
        receipt(folder, "ent{}.json".format(index))
    result = upgrade_installation(config, tmp_path)
    messages = dashboard_messages(result.instructions)
    assert result.unresolved_cds == count and len(messages) == 1
    assert messages == result.instructions
    assert "Histórico do CDS: {} arquivo".format(count) in messages[0]
    assert "confirmação de envio" in messages[0]
    assert "não será reenviado automaticamente" in messages[0]
    assert "evitar duplicidade" in messages[0] and "Confira os pedidos no Velide" in messages[0]
    assert "pre-upgrade" not in messages[0] and "authoritative" not in messages[0]
    assert len(list(folder.glob("ent*.json"))) == count
