import argparse
import logging
import os
import hashlib
import json
from tqdm import tqdm
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.exceptions import InvalidSignature
import base64

# --- Basic Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def generate_keys(private_key_path: str, public_key_path: str):
    """
    Generates a new RSA private and public key pair and saves them to disk.
    """
    logging.info("Generating new RSA key pair...")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Serialize and save the private key
    with open(private_key_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        ))
    logging.info(f"Private key saved to {private_key_path}")

    # Serialize and save the public key
    public_key = private_key.public_key()
    with open(public_key_path, "wb") as f:
        f.write(public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        ))
    logging.info(f"Public key saved to {public_key_path}")

    return private_key

def load_private_key(private_key_path: str):
    """
    Loads an existing private key from a PEM file.
    """
    logging.info(f"Loading private key from {private_key_path}...")
    try:
        with open(private_key_path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,
            )
        return private_key
    except FileNotFoundError:
        logging.error(f"Private key file not found at {private_key_path}")
        return None
    except Exception as e:
        logging.error(f"Failed to load private key: {e}")
        return None

def get_file_hash(file_path: str) -> bytes:
    """
    Calculates the SHA-256 hash of a file.
    Reads the file in chunks to handle large files efficiently.
    """
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        # Read and update hash in chunks of 4K
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.digest()

def sign_directory(directory: str, private_key_path: str, output_file: str):
    """
    Walks a directory, hashes each file, signs the hash, and saves the results.
    """
    public_key_path = private_key_path.replace(".pem", "_public.pem")

    # Step 1: Load or generate keys
    if not os.path.exists(private_key_path):
        logging.warning(f"No private key found at {private_key_path}. A new one will be generated.")
        private_key = generate_keys(private_key_path, public_key_path)
    else:
        private_key = load_private_key(private_key_path)

    if not private_key:
        logging.critical("Could not obtain a private key. Aborting.")
        return

    # Step 2: Find all files to sign
    files_to_sign = []
    for root, _, files in os.walk(directory):
        for name in files:
            files_to_sign.append(os.path.join(root, name))

    if not files_to_sign:
        logging.warning(f"No files found in directory '{directory}'. Nothing to do.")
        return

    logging.info(f"Found {len(files_to_sign)} files to sign in '{directory}'.")

    # Step 3: Hash and sign each file
    signatures = {}
    for file_path in tqdm(files_to_sign, desc="Signing files"):
        try:
            # Calculate hash
            file_hash = get_file_hash(file_path)

            # Sign the hash
            signature = private_key.sign(
                file_hash,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
            
            # Store the signature as a base64 encoded string
            relative_path = os.path.relpath(file_path, directory)
            signatures[relative_path] = base64.b64encode(signature).decode('utf-8')

        except Exception as e:
            logging.error(f"Could not sign file {file_path}: {e}")

    # Step 4: Save signatures to the output file
    try:
        with open(output_file, 'w') as f:
            json.dump(signatures, f, indent=4)
        logging.info(f"Successfully signed {len(signatures)} files. Signatures saved to {output_file}")
    except Exception as e:
        logging.error(f"Could not write signatures to {output_file}: {e}")


def main():
    """
    Main function to parse arguments and initiate the signing process.
    """
    parser = argparse.ArgumentParser(description="A CLI tool to sign all files in a directory recursively.")
    parser.add_argument("folder", type=str, help="The path to the folder whose contents will be signed.")
    parser.add_argument(
        "--key",
        type=str,
        default="private.pem",
        help="Path to the private key file. If it doesn't exist, it will be created. (Default: private.pem)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="signatures.json",
        help="The file where the signatures will be stored. (Default: signatures.json)"
    )

    args = parser.parse_args()

    if not os.path.isdir(args.folder):
        logging.critical(f"Error: The specified folder '{args.folder}' does not exist.")
        return

    sign_directory(args.folder, args.key, args.output)


if __name__ == "__main__":
    main()
