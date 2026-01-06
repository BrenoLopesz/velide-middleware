from enum import Enum

class ConnectionState(Enum):
    DISCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    ERROR = 3

class ConnectionColors:
    # Tailwind CSS Hex codes
    RED_500 = "#ef4444"
    YELLOW_500 = "#eab308"
    GREEN_500 = "#22c55e"
    GRAY_400 = "#9ca3af"

    @staticmethod
    def get_color(state: ConnectionState) -> str:
        mapping = {
            ConnectionState.DISCONNECTED: ConnectionColors.RED_500,
            ConnectionState.CONNECTING: ConnectionColors.YELLOW_500,
            ConnectionState.CONNECTED: ConnectionColors.GREEN_500,
            ConnectionState.ERROR: ConnectionColors.GRAY_400,
        }
        return mapping.get(state, ConnectionColors.GRAY_400)

    @staticmethod
    def get_label(state: ConnectionState) -> str:
        mapping = {
            ConnectionState.DISCONNECTED: "Desconectado",
            ConnectionState.CONNECTING: "Conectando...",
            ConnectionState.CONNECTED: "Conectado",
            ConnectionState.ERROR: "Erro na Conexão",
        }
        return mapping.get(state, "Desconhecido")