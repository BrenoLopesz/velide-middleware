import hashlib
import logging
from cryptography.hazmat.primitives import serialization


def load_public_key(public_key_path: str):
    """
    Loads an existing public key from a PEM file.
    """
    logging.info(f"Loading public key from {public_key_path}...")
    try:
        with open(public_key_path, "rb") as key_file:
            public_key = serialization.load_pem_public_key(key_file.read())
        return public_key
    except FileNotFoundError:
        logging.error(f"Public key file not found at {public_key_path}")
        return None
    except Exception as e:
        logging.error(f"Failed to load public key: {e}")
        return None


def get_file_hash(file_path: str) -> str:
    """
    Calculates the SHA-256 hash of a file and returns it as a hex string.
    Reads the file in chunks to handle large files efficiently.
    """
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read and update hash in chunks of 4K
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()
