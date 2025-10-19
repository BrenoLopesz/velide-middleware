from urllib.parse import quote_plus
from config import FarmaxConfig

def get_farmax_engine_string(config: FarmaxConfig) -> str:
    safe_password = quote_plus(config.password)
    return (
        f"firebird+fdb://{config.user}:{safe_password}"
        f"@{config.host}/{config.file}"
    )