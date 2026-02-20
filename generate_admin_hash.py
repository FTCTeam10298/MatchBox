#!/usr/bin/env python3
"""Generate a new admin password hash for MatchBox.

Prompts for a password, generates a random salt, and prints the values
to paste into matchbox.py replacing ADMIN_SALT and ADMIN_HASH.
"""

import getpass
import hashlib
import os

password = getpass.getpass("Enter new admin password: ")
confirm = getpass.getpass("Confirm password: ")

if password != confirm:
    print("Passwords do not match.")
    raise SystemExit(1)

if not password:
    print("Password cannot be empty.")
    raise SystemExit(1)

salt = os.urandom(16)
hash_hex = hashlib.sha256(salt + password.encode()).hexdigest()

print()
print("Replace these lines in matchbox.py:")
print()
print(f"ADMIN_SALT = {salt!r}")
print(f"ADMIN_HASH = '{hash_hex}'")
