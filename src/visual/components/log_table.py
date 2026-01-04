from PyQt5.QtWidgets import QTableView, QHeaderView, QAbstractItemView
from models.log_table_model import LogTableModel
from visual.fonts import get_fonts


class LogTable(QTableView):
    def __init__(self):
        super().__init__()
        # self.row_count is no longer needed; the model is the source of truth.
        self.fonts = get_fonts()

        # Use the new custom model instead of QStandardItemModel
        self._model = LogTableModel()
        self.setModel(self._model)

        # The model now provides its own headers, 
        # so setHorizontalHeaderLabels is not needed.
        self._configure_table()

    def _configure_table(self):
        """Sets up the visual properties and behavior of the table."""

        # Disables editting
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)

        self.setFont(self.fonts["regular_small"])
        # --- Column Sizing ---
        h_header = self.horizontalHeader()
        h_header.setFont(self.fonts["regular_small"])

        # Column 0 ("Horário"): Fixed width
        h_header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.setColumnWidth(0, 110)

        # Column 1 ("Tipo"): Fixed width
        h_header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.setColumnWidth(1, 65)  # Adjusted width slightly

        # Column 2 ("Mensagem"): Takes all remaining space
        h_header.setSectionResizeMode(2, QHeaderView.Stretch)

        # --- Word Wrapping and Row Height ---
        self.setWordWrap(True)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.verticalHeader().setVisible(False)

    def add_row(self, formatted_time: str, level: str, message: str):
        """
        Adds a new log entry to the table by calling the model's method.
        """
        # --- VALIDATION ---
        safe_formatted_time = str(formatted_time)
        safe_level = str(level)
        safe_message = str(message)

        # --- DELEGATE TO MODEL ---
        # The view's job is to pass the data to the model.
        # The model handles the insertion and notifies the view.
        self._model.add_log_entry(
            timestamp=safe_formatted_time, level=safe_level, message=safe_message
        )

        self.scrollToBottom()
