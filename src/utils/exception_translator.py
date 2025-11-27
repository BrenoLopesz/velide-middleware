import httpx

def get_friendly_error_msg(e: Exception) -> str:
    """
    Translates technical exceptions into user-friendly Portuguese messages.
    """
    # Network / Connection Errors
    if isinstance(e, httpx.ConnectTimeout):
        return "Tempo de conexão esgotado (Timeout)"
    
    if isinstance(e, httpx.ReadTimeout):
        return "O servidor Velide demorou muito para responder"
        
    if isinstance(e, httpx.ConnectError):
        # Often happens when internet is down or DNS fails
        return "Falha na conexão. Verifique sua internet"
        
    if isinstance(e, httpx.NetworkError):
        return "Erro de rede ou conexão instável"

    if isinstance(e, httpx.HTTPStatusError):
        return f"Erro no servidor (Código {e.response.status_code})"

    # Generic Fallback
    return f"Erro inesperado ({type(e).__name__})"