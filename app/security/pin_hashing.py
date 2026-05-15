"""
PIN Hashing Security Fix

Replaces insecure unsalted SHA-256 with bcrypt for PIN storage.
Fixes issue #20: Insecure PIN Hashing (Unsalted SHA-256)
"""

import bcrypt
import hashlib
import secrets
from typing import Optional

# DEPRECATED: Old insecure PIN hashing (DO NOT USE)
def hash_pin_insecure(pin: str) -> str:
    """Insecure: Uses unsalted SHA-256. Vulnerable to rainbow table attacks."""
    return hashlib.sha256(pin.encode()).hexdigest()

def verify_pin_insecure(pin: str, pin_hash: str) -> bool:
    """Insecure verification matching old behavior."""
    return hashlib.sha256(pin.encode()).hexdigest() == pin_hash


# NEW: Secure PIN hashing with bcrypt
def hash_pin(pin: str, rounds: int = 12) -> str:
    """
    Securely hash a PIN using bcrypt with automatic salt generation.
    
    Args:
        pin: The plaintext PIN to hash
        rounds: bcrypt cost factor (default: 12, range: 4-31)
    
    Returns:
        bcrypt hash string
    """
    if not pin or len(pin) < 4:
        raise ValueError("PIN must be at least 4 characters")
    if len(pin) > 72:
        raise ValueError("PIN must be at most 72 characters (bcrypt limit)")
    
    # Generate bcrypt hash with automatic salt
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(pin.encode('utf-8'), salt).decode('utf-8')


def verify_pin(pin: str, pin_hash: str) -> bool:
    """
    Securely verify a PIN against its bcrypt hash.
    Uses constant-time comparison to prevent timing attacks.
    
    Args:
        pin: The plaintext PIN to verify
        pin_hash: The stored bcrypt hash
    
    Returns:
        True if PIN matches, False otherwise
    """
    if not pin or not pin_hash:
        return False
    
    try:
        return bcrypt.checkpw(pin.encode('utf-8'), pin_hash.encode('utf-8'))
    except (ValueError, TypeError):
        return False


def migrate_pin_hash(old_hash: str, pin: str) -> Optional[str]:
    """
    Migrate from insecure SHA-256 hash to secure bcrypt hash.
    Call this during login when the old hash is detected.
    
    Args:
        old_hash: The old insecure SHA-256 hash
        pin: The plaintext PIN (available during login)
    
    Returns:
        New bcrypt hash if migration successful, None otherwise
    """
    # Verify against old hash first
    if not verify_pin_insecure(pin, old_hash):
        return None
    
    # Generate new secure hash
    return hash_pin(pin)


def generate_pin(length: int = 6) -> str:
    """Generate a cryptographically secure random PIN."""
    digits = '0123456789'
    return ''.join(secrets.choice(digits) for _ in range(length))


# Migration script
async def migrate_all_pins(db):
    """
    Migrate all existing PIN hashes from SHA-256 to bcrypt.
    Should be run as a background task.
    """
    users = await db.query("SELECT id, pin_hash FROM users WHERE pin_hash NOT LIKE '$2b$%'")
    
    migrated = 0
    for user in users:
        # Cannot migrate without knowing the PIN - mark for migration on next login
        await db.execute(
            "UPDATE users SET needs_pin_migration = TRUE WHERE id = $1",
            user['id']
        )
        migrated += 1
    
    return migrated
