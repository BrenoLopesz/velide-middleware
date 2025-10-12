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

# --- Basic Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_public_key(public_key_path: str):
    """
    Loads an existing public key from a PEM file.
    """
    logging.info(f"Loading public key from {public_key_path}...")
    try:
        with open(public_key_path, "rb") as key_file:
            public_key = serialization.load_pem_public_key(
                key_file.read()
            )
        return public_key
    except FileNotFoundError:
        logging.error(f"Public key file not found at {public_key_path}")
        return None
    except Exception as e:
        logging.error(f"Failed to load public key: {e}")
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

def verify_directory(directory: str, signature_file: str, public_key_path: str):
    """
    Verifies all files in a directory against a signature file.
    """
    # Step 1: Load public key
    public_key = load_public_key(public_key_path)
    if not public_key:
        logging.critical("Could not load public key. Aborting verification.")
        return

    # Step 2: Load signatures from file
    try:
        with open(signature_file, 'r') as f:
            signatures = json.load(f)
        logging.info(f"Loaded {len(signatures)} signatures from {signature_file}")
    except FileNotFoundError:
        logging.critical(f"Signature file not found: {signature_file}")
        return
    except json.JSONDecodeError:
        logging.critical(f"Error decoding JSON from {signature_file}. Is the file valid?")
        return
    except Exception as e:
        logging.critical(f"Failed to read signature file: {e}")
        return
        
    inconsistencies = []

    # Step 3: Verify each file listed in the signature file
    signed_files_on_disk = set()
    for relative_path in tqdm(signatures.keys(), desc="Verifying signatures"):
        file_path = os.path.join(directory, relative_path)
        signed_files_on_disk.add(relative_path)

        if not os.path.exists(file_path):
            inconsistencies.append(f"'{relative_path}': FAILED (File is missing)")
            continue
        
        try:
            # Calculate current hash
            current_hash = get_file_hash(file_path)
            
            # Decode the signature from Base64
            signature = base64.b64decode(signatures[relative_path])

            # Verify the signature
            public_key.verify(
                signature,
                current_hash,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH
                ),
                hashes.SHA256()
            )
        except InvalidSignature:
            inconsistencies.append(f"'{relative_path}': FAILED (Signature mismatch - file may be tampered)")
        except Exception as e:
            inconsistencies.append(f"'{relative_path}': FAILED (An error occurred: {e})")
    
    # Step 4: Check for any unsigned files in the directory
    all_files_on_disk = set()
    for root, _, files in os.walk(directory):
        for name in files:
            full_path = os.path.join(root, name)
            relative_path = os.path.relpath(full_path, directory)
            all_files_on_disk.add(relative_path)
    
    unsigned_files = all_files_on_disk - signed_files_on_disk
    for unsigned_file in unsigned_files:
        inconsistencies.append(f"'{unsigned_file}': FAILED (File is not signed)")

    # Step 5: Report the results
    logging.info("Verification complete.")
    if not inconsistencies:
        print("\n✅ Success: All files verified successfully. No inconsistencies found.")
    else:
        total = len(inconsistencies)
        print(f"\n❌ Verification Failed: Found {total} total inconsistencies.")
        
        # Print up to 3 examples
        print("Some of the inconsistencies found:")
        for issue in inconsistencies[:3]:
            print(f"  - {issue}")
        if total > 3:
            print(f"  ...and {total - 3} more.")

def main():
    """
    Main function to parse arguments and initiate the verification process.
    """
    parser = argparse.ArgumentParser(description="A CLI tool to verify file signatures in a directory.")
    parser.add_argument("folder", type=str, help="The path to the folder to verify.")
    parser.add_argument(
        "--key",
        type=str,
        required=True,
        help="Path to the public key file for verification."
    )
    parser.add_argument(
        "--signatures",
        type=str,
        default="signatures.json",
        help="The signature file to verify against. (Default: signatures.json)"
    )

    args = parser.parse_args()

    if not os.path.isdir(args.folder):
        logging.critical(f"Error: The specified folder '{args.folder}' does not exist.")
        return
        
    if not os.path.isfile(args.signatures):
        logging.critical(f"Error: The signatures file '{args.signatures}' does not exist.")
        return

    verify_directory(args.folder, args.signatures, args.key)


if __name__ == "__main__":
    main()
