import argparse
import logging
import os
import hashlib
import json
from tqdm import tqdm
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.exceptions import InvalidSignature
import base64

from src.utils.cryptography import get_file_hash, load_public_key

# --- Basic Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def verify_directory(directory: str, manifest_path: str, public_key_path: str):
    """
    Verifies files in a directory against a signed manifest.
    """
    # Step 1: Load public key
    public_key = load_public_key(public_key_path)
    if not public_key:
        logging.critical("Could not load public key. Aborting verification.")
        return False, []

    # Step 2: Verify the manifest file itself
    signature_path = manifest_path.rsplit('.', 1)[0] + ".sig"
    logging.info(f"Attempting to verify manifest '{manifest_path}' with signature '{signature_path}'...")
    
    try:
        with open(manifest_path, "rb") as f:
            manifest_bytes = f.read()
        
        with open(signature_path, 'r') as f:
            signature = base64.b64decode(f.read())

        manifest_hash = hashlib.sha256(manifest_bytes).digest()

        public_key.verify(
            signature,
            manifest_hash,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        logging.info("✅ Manifest signature is valid. The manifest is trusted.")
    except FileNotFoundError as e:
        logging.critical(f"Required file not found: {e.filename}. Cannot trust the manifest.")
        return False, []
    except InvalidSignature:
        logging.critical("❌ MANIFEST TAMPERED: Signature mismatch. The manifest file cannot be trusted.")
        return False, []
    except Exception as e:
        logging.critical(f"An unexpected error occurred during manifest verification: {e}")
        return False, []

    # Step 3: Load the (now trusted) manifest data
    try:
        manifest_data = json.loads(manifest_bytes)
        # Create a lookup dictionary of hash -> filename for efficient checking
        manifest_hashes = {item['hash']: item['filename'] for item in manifest_data['files']}
        all_manifest_files = set(manifest_hashes.values())
    except (json.JSONDecodeError, KeyError) as e:
        logging.critical(f"Manifest file is corrupted or has an invalid format: {e}")
        return False, []
        
    inconsistencies = []
    found_and_valid_files = set()

    # Step 4: Walk the local directory and verify files against the manifest hashes
    logging.info("Verifying local files against the trusted manifest...")
    
    # Files to ignore during verification walk
    ignore_files = {os.path.basename(manifest_path), os.path.basename(signature_path)}
    
    local_files_to_check = []
    for root, _, files in os.walk(directory):
        for name in files:
            if name not in ignore_files:
                local_files_to_check.append(os.path.join(root, name))

    for file_path in tqdm(local_files_to_check, desc="Verifying files"):
        relative_path = os.path.relpath(file_path, directory).replace('\\', '/')
        try:
            current_hash = get_file_hash(file_path)
            if current_hash in manifest_hashes:
                # The hash is valid, so this file is trusted.
                original_filename = manifest_hashes[current_hash]
                found_and_valid_files.add(original_filename)
            else:
                # This file exists locally but its hash is not in the manifest.
                inconsistencies.append(f"'{relative_path}': UNTRUSTED (File is not listed in the manifest)")
        except Exception as e:
            inconsistencies.append(f"'{relative_path}': FAILED (Could not process file: {e})")

    # Step 5: Identify any files that were in the manifest but not found locally
    missing_files = all_manifest_files - found_and_valid_files
    
    # --- Reporting ---
    logging.info("Verification complete.")
    if not inconsistencies:
        print("\n✅ Success: All local files have been verified successfully against the manifest.")
        if missing_files:
            print("\nℹ️ The following files from the manifest were not found locally:")
            for missing in sorted(list(missing_files)):
                print(f"  - {missing}")
        return True, []
    else:
        total = len(inconsistencies)
        print(f"\n❌ Verification Failed: Found {total} critical inconsistencies.")
        print("Details:")
        for issue in inconsistencies:
            print(f"  - {issue}")
        return False, inconsistencies


def main():
    """
    Main function to parse arguments and initiate the verification process.
    """
    parser = argparse.ArgumentParser(description="A CLI tool to verify files in a directory against a signed manifest.")
    parser.add_argument("folder", type=str, help="The path to the folder to verify.")
    parser.add_argument(
        "--key",
        type=str,
        required=True,
        help="Path to the public key file for verification."
    )
    parser.add_argument(
        "--manifest",
        type=str,
        default="manifest.json",
        help="The manifest file to verify against. (Default: manifest.json)"
    )

    args = parser.parse_args()

    if not os.path.isdir(args.folder):
        logging.critical(f"Error: The specified folder '{args.folder}' does not exist.")
        return
        
    if not os.path.isfile(args.manifest):
        logging.critical(f"Error: The manifest file '{args.manifest}' does not exist.")
        return

    verify_directory(args.folder, args.manifest, args.key)


if __name__ == "__main__":
    main()
