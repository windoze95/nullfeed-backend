"""Tests for secure PIN hashing."""
import pytest
from app.security.pin_hashing import hash_pin, verify_pin, generate_pin

def test_hash_pin_returns_bcrypt():
    result = hash_pin("1234")
    assert result.startswith("$2b$")

def test_verify_correct_pin():
    h = hash_pin("1234")
    assert verify_pin("1234", h) is True

def test_verify_wrong_pin():
    h = hash_pin("1234")
    assert verify_pin("0000", h) is False

def test_different_hashes_for_same_pin():
    h1 = hash_pin("1234")
    h2 = hash_pin("1234")
    assert h1 != h2  # Different salts

def test_short_pin_rejected():
    with pytest.raises(ValueError):
        hash_pin("12")

def test_empty_pin_rejected():
    with pytest.raises(ValueError):
        hash_pin("")

def test_verify_empty_returns_false():
    assert verify_pin("", "somehash") is False

def test_generate_pin_default_length():
    pin = generate_pin()
    assert len(pin) == 6
    assert pin.isdigit()

def test_generate_pin_custom_length():
    pin = generate_pin(4)
    assert len(pin) == 4
