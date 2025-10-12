class AuthorizationError(Exception):
    """Base class for exceptions in this module."""
    pass

class NetworkError(AuthorizationError):
    """Raised for network-related errors like timeouts or connection issues."""
    def __init__(self, original_exception):
        self.original_exception = original_exception
        super().__init__(f"Ocorreu um erro na rede: {original_exception}")

class ApiError(AuthorizationError):
    """Raised when the API returns a non-successful HTTP status code."""
    def __init__(self, status_code: int, response_text: str):
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(f"API retornou um erro {status_code}: {response_text}")

class TokenPollingError(AuthorizationError):
    """
    Raised for specific, non-successful OAuth polling responses.
    These are expected errors from the token endpoint.
    """
    def __init__(self, error_code: str, error_description: str = ""):
        self.error_code = error_code
        self.error_description = error_description
        super().__init__(f"Erro no polling do OAuth: {error_code} - {error_description}")

class TokenStorageError(AuthorizationError):
    """Raised when the token cannot be written to the file system."""
    def __init__(self, original_exception):
        self.original_exception = original_exception
        super().__init__(f"Falha em armazenar token no disco: {original_exception}")