import os
import json
import base64
import binascii
from typing import Optional
from utils.bundle_dir import BUNDLE_DIR
from models.exceptions import TokenStorageError

# --- HELPER FUNCTIONS ---

def _encode_string_base64(data_string: str) -> str:
    """Helper to Base64 encode a string."""
    data_bytes = data_string.encode('utf-8')
    encoded_bytes = base64.b64encode(data_bytes)
    return encoded_bytes.decode('utf-8')

def _decode_string_base64(encoded_string: str) -> str:
    """
    Helper to Base64 decode a string.
    Raises TokenStorageError on decoding failure.
    """
    try:
        encoded_bytes = encoded_string.encode('utf-8')
        decoded_bytes = base64.b64decode(encoded_bytes)
        return decoded_bytes.decode('utf-8')
    except (binascii.Error, UnicodeDecodeError) as e:
        # Catches errors if the string is not valid Base64 or can't be decoded to UTF-8
        raise TokenStorageError(message="Falha ao decodificar o token.", original_exception=e) from e

# --- TOKEN STORAGE FUNCTIONS ---

def store_token_at_file(token: dict):
    """
    Encodes and saves the token dictionary to a file.
    Raises TokenStorageError on failure.
    """
    try:
        token_json_string = json.dumps(token)
        encoded_token = _encode_string_base64(token_json_string)

        file_path = os.path.join(BUNDLE_DIR, "resources", "token.txt")
        file_dir = os.path.dirname(file_path)

        # Ensure the directory exists
        os.makedirs(file_dir, exist_ok=True)

        with open(file_path, 'w') as file:
            file.write(encoded_token)
            
    except (IOError, OSError) as e:
        # Catch file system errors (e.g., permissions, disk full)
        raise TokenStorageError(original_exception=e) from e
    except TypeError as e:
        # Catch cases where the token dictionary isn't serializable to JSON
        raise TokenStorageError(message="Token não é um JSON válido.", original_exception=e) from e

def read_token_from_file() -> Optional[dict]:
    """
    Reads, decodes, and returns the token dictionary from a file.
    
    Returns:
        A dictionary containing the token if found and successfully decoded.
        None if the token file does not exist.
        
    Raises:
        TokenStorageError on failure to read, decode, or parse the file content.
    """
    file_path = os.path.join(BUNDLE_DIR, "resources", "token.txt")
    
    try:
        with open(file_path, 'r') as file:
            encoded_token = file.read().strip()

        # Handle case where file exists but is empty
        if not encoded_token:
            return None

        token_json_string = _decode_string_base64(encoded_token)
        token: dict = json.loads(token_json_string)
        return token

    except FileNotFoundError:
        # This is an expected case if no token has been stored yet.
        return None
    except (IOError, OSError) as e:
        # Catch other file system errors during read
        raise TokenStorageError(original_exception=e) from e
    except json.JSONDecodeError as e:
        # Catch errors if the decoded string is not valid JSON
        raise TokenStorageError(message="Falha ao decodificar JSON do arquivo de token.", original_exception=e) from e
