"""Tests for fix_vault_kek_fallback_wipe (backend-sec).

KEK fail-closed (no silent ephemeral KEK), strict VAULT_KEK_B64
validation, DEV_MODE-only ephemeral/fallback with a distinct record
format, and _wipe zeroing the live bytearray DEK.
Run from repo root: python -m pytest tests/test_fix_vault_kek_fallback_wipe.py -q
"""
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.accounts import vault as vault_mod
from src.accounts.vault import (
    ALG_AES_GCM, ALG_DEV_FALLBACK, EncryptedSecretVault, EnvKekProvider,
    InMemoryVaultStore, VaultError, _wipe,
)

KEK1 = bytes(range(32))
KEK2 = bytes(range(32, 64))


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("VAULT_KEK_B64", "VAULT_DEV_MODE", "DEV_MODE"):
        monkeypatch.delenv(name, raising=False)
    yield


def _set_kek_env(monkeypatch, kek: bytes):
    monkeypatch.setenv("VAULT_KEK_B64", base64.b64encode(kek).decode())


# ---------------------------------------------------------------- KEK fail-closed
def test_missing_kek_raises_fail_closed():
    with pytest.raises(VaultError):
        EnvKekProvider()
    with pytest.raises(VaultError):
        EncryptedSecretVault()


def test_invalid_kek_b64_and_wrong_length_raise(monkeypatch):
    monkeypatch.setenv("VAULT_KEK_B64", "!!!not-base64!!!")
    with pytest.raises(VaultError):
        EnvKekProvider()
    monkeypatch.setenv("VAULT_KEK_B64", base64.b64encode(b"short-16-bytes!!").decode())
    with pytest.raises(VaultError):
        EnvKekProvider()
    with pytest.raises(VaultError):
        EnvKekProvider(kek=b"too short")


def test_explicit_kek_arg_works_without_env():
    p = EnvKekProvider(kek=KEK1)
    assert p.ephemeral is False
    v = EncryptedSecretVault(kek=p, store=InMemoryVaultStore())
    ref = v.store(account_id="acc1", kind="password", secret="s3cret-value")
    assert v.retrieve(credential_ref=ref) == "s3cret-value"


def test_ephemeral_kek_only_in_dev_mode(monkeypatch):
    monkeypatch.setenv("VAULT_DEV_MODE", "1")
    p = EnvKekProvider()
    assert p.ephemeral is True
    # A second ephemeral provider has a DIFFERENT KEK: data is not shared
    p2 = EnvKekProvider()
    store = InMemoryVaultStore()
    v1 = EncryptedSecretVault(kek=p, store=store)
    ref = v1.store(account_id="acc1", kind="cookie", secret="cookie-value")
    v2 = EncryptedSecretVault(kek=p2, store=store)
    with pytest.raises(VaultError):
        v2.retrieve(credential_ref=ref)


# ---------------------------------------------------------------- restart semantics
def test_restart_same_kek_retrieves_different_kek_fails(monkeypatch):
    _set_kek_env(monkeypatch, KEK1)
    store = InMemoryVaultStore()  # shared store = persistent DB across restarts
    v1 = EncryptedSecretVault(store=store)
    ref = v1.store(account_id="acc9", kind="access_token", secret="token-abc")
    rec = store.get(ref)
    assert rec.alg == ALG_AES_GCM  # real AES-GCM format marker

    # "restart": brand-new provider + vault instances, same env KEK
    v2 = EncryptedSecretVault(store=store)
    assert v2.retrieve(credential_ref=ref) == "token-abc"

    # restart with a DIFFERENT KEK must fail loudly as VaultError
    _set_kek_env(monkeypatch, KEK2)
    v3 = EncryptedSecretVault(store=store)
    with pytest.raises(VaultError):
        v3.retrieve(credential_ref=ref)


def test_rotate_and_delete_still_work(monkeypatch):
    _set_kek_env(monkeypatch, KEK1)
    v = EncryptedSecretVault(store=InMemoryVaultStore())
    ref = v.store(account_id="acc2", kind="password", secret="old-pass")
    ref2 = v.rotate(credential_ref=ref, new_secret="new-pass")
    assert ref2 != ref
    assert v.retrieve(credential_ref=ref2) == "new-pass"
    with pytest.raises(VaultError):
        v.retrieve(credential_ref=ref)
    v.delete(credential_ref=ref2)
    with pytest.raises(VaultError):
        v.retrieve(credential_ref=ref2)


# ---------------------------------------------------------------- cryptography missing
def test_missing_cryptography_fail_closed_outside_dev(monkeypatch):
    monkeypatch.setattr(vault_mod, "_HAS_AESGCM", False)
    monkeypatch.setattr(vault_mod, "AESGCM", None)
    with pytest.raises(VaultError):
        EncryptedSecretVault(kek=EnvKekProvider(kek=KEK1))


def test_missing_cryptography_dev_mode_uses_distinct_fallback_format(monkeypatch):
    monkeypatch.setattr(vault_mod, "_HAS_AESGCM", False)
    monkeypatch.setattr(vault_mod, "AESGCM", None)
    store = InMemoryVaultStore()
    v = EncryptedSecretVault(kek=EnvKekProvider(kek=KEK1, dev_mode=True),
                             store=store, allow_insecure_fallback=True)
    assert v.insecure_fallback is True
    ref = v.store(account_id="acc3", kind="session", secret="sess-123")
    rec = store.get(ref)
    assert rec.alg == ALG_DEV_FALLBACK and rec.alg != ALG_AES_GCM
    import base64 as _b64
    wrapped = _b64.urlsafe_b64decode(rec.wrapped_dek.encode())
    assert wrapped[:2] == b"H1"  # fallback wrap marker, not G1 (AES-GCM)
    assert v.retrieve(credential_ref=ref) == "sess-123"
    # wrong KEK still fails on fallback records
    v_bad = EncryptedSecretVault(kek=EnvKekProvider(kek=KEK2, dev_mode=True),
                                 store=store, allow_insecure_fallback=True)
    with pytest.raises(VaultError):
        v_bad.retrieve(credential_ref=ref)


# ---------------------------------------------------------------- _wipe
def test_wipe_zeroes_the_same_object():
    buf = bytearray(b"\xAB" * 32)
    _wipe(buf)
    assert bytes(buf) == b"\x00" * 32


def test_store_and_retrieve_wipe_the_live_dek(monkeypatch):
    _set_kek_env(monkeypatch, KEK1)
    seen = []
    orig_enc, orig_dec = vault_mod._aead_encrypt, vault_mod._aead_decrypt

    def spy_enc(dek, pt, aad):
        seen.append(dek)
        return orig_enc(dek, pt, aad)

    def spy_dec(dek, nonce, blob, aad, alg=""):
        seen.append(dek)
        return orig_dec(dek, nonce, blob, aad, alg=alg)

    monkeypatch.setattr(vault_mod, "_aead_encrypt", spy_enc)
    monkeypatch.setattr(vault_mod, "_aead_decrypt", spy_dec)
    v = EncryptedSecretVault(store=InMemoryVaultStore())
    ref = v.store(account_id="acc4", kind="password", secret="pw")
    assert v.retrieve(credential_ref=ref) == "pw"
    assert seen, "spies must have captured the DEKs"
    for dek in seen:
        assert isinstance(dek, bytearray), "DEK must be a bytearray from the start"
        assert bytes(dek) == b"\x00" * len(dek), "the SAME object must be zeroed, not a copy"
