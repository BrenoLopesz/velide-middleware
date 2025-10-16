import argparse
import logging
import os
import hashlib
import json
from tqdm import tqdm
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import serialization, hashes
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
        key_size=4096,  # Increased key size for better security
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

def sign_directory(directory: str, private_key_path: str, manifest_path: str):
    """
    Walks a directory, hashes each file, creates a manifest file,
    and then signs the manifest file itself.
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

    # Step 2: Find all files to include in the manifest
    files_to_hash = []
    # Exclude manifest and signature files themselves from the list
    exclude_files = [os.path.basename(manifest_path), os.path.basename(manifest_path).replace('.json', '.sig')]
    for root, _, files in os.walk(directory):
        for name in files:
            if name not in exclude_files:
                files_to_hash.append(os.path.join(root, name))

    if not files_to_hash:
        logging.warning(f"No files found in directory '{directory}'. Nothing to do.")
        return

    logging.info(f"Found {len(files_to_hash)} files to include in the manifest from '{directory}'.")

    # Step 3: Hash all files and build the manifest data structure
    manifest_files = []
    for file_path in tqdm(files_to_hash, desc="Hashing files"):
        try:
            file_hash = get_file_hash(file_path)
            relative_path = os.path.relpath(file_path, directory).replace('\\', '/') # Normalize path separators
            manifest_files.append({"filename": relative_path, "hash": file_hash})
        except Exception as e:
            logging.error(f"Could not hash file {file_path}: {e}")

    manifest_data = {
        "manifest_version": "1.0",
        "hash_algorithm": "sha256",
        "files": manifest_files
    }

    # Step 4: Write the manifest JSON file to disk
    try:
        with open(manifest_path, 'w') as f:
            json.dump(manifest_data, f, indent=4, sort_keys=True)
        logging.info(f"Manifest for {len(manifest_files)} files successfully created at {manifest_path}")
    except Exception as e:
        logging.error(f"Could not write manifest to {manifest_path}: {e}")
        return

    # Step 5: Sign the manifest file itself
    logging.info(f"Signing the manifest file: {manifest_path}")
    try:
        with open(manifest_path, "rb") as f:
            manifest_bytes = f.read()

        manifest_hash = hashlib.sha256(manifest_bytes).digest()

        signature = private_key.sign(
            manifest_hash,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        
        # Step 6: Save the signature to a .sig file
        signature_path = manifest_path.rsplit('.', 1)[0] + ".sig"
        with open(signature_path, 'w') as f:
            f.write(base64.b64encode(signature).decode('utf-8'))
        
        logging.info(f"Manifest signature saved to {signature_path}")

    except Exception as e:
        logging.error(f"Failed to sign the manifest file: {e}")


def main():
    """
    Main function to parse arguments and initiate the manifest creation and signing process.
    """
    parser = argparse.ArgumentParser(description="A CLI tool to create and sign a manifest of all files in a directory.")
    parser.add_argument("folder", type=str, help="The path to the folder to be scanned.")
    parser.add_argument(
        "--key",
        type=str,
        default="private.pem",
        help="Path to the private key file. If it doesn't exist, it will be created. (Default: private.pem)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="manifest.json",
        help="The file where the manifest will be stored. A corresponding .sig file will be created. (Default: manifest.json)"
    )

    args = parser.parse_args()

    if not os.path.isdir(args.folder):
        logging.critical(f"Error: The specified folder '{args.folder}' does not exist.")
        return

    sign_directory(args.folder, args.key, args.output)


if __name__ == "__main__":
    main()
