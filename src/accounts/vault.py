"""approach_03 - Vault ma hoa credential/cookie va vong doi token.

Envelope encryption: moi account 1 DEK rieng, DEK duoc wrap bang KEK.
Neu co ``cryptography``: AES-256-GCM that (DEK 32B, nonce 12B).
Neu khong: fallback stdlib (HMAC-SHA256 stream + tag) chi dung cho
dev/test, log warning ro rang - production BAT BUOC cai cryptography
hoac noi KMS/Vault that qua ``KekProvider``.

Tuan thu src/contracts.py: SecretVault Protocol (store/retrieve/rotate/
delete), TokenSet, AccountStatus, TOKEN_REFRESH_BEFORE_MINUTES.
Khong bao gio log/tra secret plaintext. Fail-closed: token revoked /
checkpoint -> NEEDS_REAUTH, khong retry vo han.

Fix ``fix_vault_kek_fallback_wipe`` (gop fix_vault_ephemeral_kek_and_
silent_fallb + fix_vault_ephemeral_kek_data_loss +
fix_vault_wipe_noop_and_aead_fallback) — FAIL-CLOSED o khoi dong:
- EnvKekProvider KHONG con tu sinh KEK ngau nhien khi thieu
  VAULT_KEK_B64: thieu KEK (va khong co kek truyen truc tiep) thi
  raise VaultError ngay. KEK tam thoi (ephemeral) chi duoc tao khi
  co co DEV_MODE ro rang (tham so ``dev_mode=True`` hoac env
  VAULT_DEV_MODE=1 / DEV_MODE=1) kem canh bao co cau truc — va phai
  hieu ro: ephemeral KEK chi song trong RAM, restart la MAT het
  secret da luu bang no.
- VAULT_KEK_B64 duoc validate nghiem: base64 hop le (validate=True)
  va giai ma dung 32 byte, sai la raise VaultError ngay, khong cat
  ngan/khong chap nhan KEK yeu.
- Thieu thu vien ``cryptography`` va khong o DEV_MODE: khoi tao
  EncryptedSecretVault raise VaultError; wrap/encrypt bang fallback
  HMAC cung bi chan o EnvKekProvider. Neu DEV_MODE cho phep fallback
  thi ban ghi mang dinh dang khac han: prefix wrap ``H1`` (thay
  ``G1`` cua AES-GCM) va truong ``alg`` =
  "hmac-sha256-stream-dev-fallback" (thay "aes-256-gcm"), kem log
  warning co cau truc moi lan khoi tao.
- ``_wipe``: DEK duoc giu dang bytearray tu dau va zero CHINH doi
  tuong bytearray do trong finally (truoc day code goi
  ``_wipe(bytearray(dek))`` — chi zero ban sao vua tao, DEK goc dang
  bytes van nguyen). Gioi han trung thuc: viec zero chi xoa duoc
  doi tuong bytearray ma code dang giu; cac ban sao da tao ra trong
  qua trinh tinh toan (bytes(dek) dua cho AESGCM, internals cua
  interpreter, swap/hibernation cua OS) nam ngoai kha nang kiem soat
  — day la giam thieu ton du, KHONG phai cam ket xoa tuyet doi. KEK/
  DEK dai han phai nam o KMS/HSM o production.

Fix ``fix_token_lock_distributed``: TokenManager truoc day chi dung
threading.Lock trong _RefreshState — chi khoa duoc 1 process, nhieu
worker refresh cung account dong thoi se dua nhau rotate
refresh_token (ben thua mat session), va nhanh cho lock khong co
timeout (acquire() chan vo han). Nay:
- Khoa la khoa PHAN TAN that qua backend dung chung: giao dien
  DistributedLock voi lease TTL, owner token ngau nhien, unlock chi
  xoa khi owner khop (chong unlock nham khoa cua nguoi khac), va
  acquire co timeout — het timeout la fail-closed, khong refresh.
  Backend san co: RedisDistributedLock (Redis SET NX PX + Lua unlock,
  production), PostgresAdvisoryLock (pg advisory lock giu theo
  session), InMemoryDistributedLock (semantics giong Redis nhung nam
  trong RAM — dung cho test va khi tat ca manager dung CHUNG mot
  instance; mac dinh TokenManager dung mot instance dung chung cap
  process, production nhieu process BAT BUOC inject backend
  Redis/Postgres dung chung, neu khong khoa chi co tac dung trong
  tung process).
- Sau khi lay duoc khoa, TokenManager DOC LAI credential_ref/version
  moi nhat tu store dung chung (latest_record) va chi refresh neu
  version chua doi; neu worker khac da rotate xong thi dung ngay
  token moi cua ho, khong goi refresher lan hai. Truoc khi rotate
  cung kiem tra lai lan nua (lease co the het han giua chung) — neu
  ref da doi thi huy rotate, dung ket qua cua ben thang.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Optional, Protocol

try:
    from src.contracts import (
        AccountStatus, PlatformAccount, TokenSet, utcnow,
        TOKEN_REFRESH_BEFORE_MINUTES,
    )
except ImportError:  # pragma: no cover
    from contracts import (  # type: ignore
        AccountStatus, PlatformAccount, TokenSet, utcnow,
        TOKEN_REFRESH_BEFORE_MINUTES,
    )

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_AESGCM = True
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore
    _HAS_AESGCM = False

__all__ = [
    "VaultError", "TokenRevokedError", "KekProvider", "EnvKekProvider",
    "EncryptedRecord", "InMemoryVaultStore", "EncryptedSecretVault",
    "TokenManager", "mask_secret", "validate_cookie_blob",
    "ALG_AES_GCM", "ALG_DEV_FALLBACK", "dev_mode_enabled",
    "DistributedLock", "InMemoryDistributedLock", "RedisDistributedLock",
    "PostgresAdvisoryLock",
]

_KINDS = {"password", "cookie", "access_token", "refresh_token",
          "token_set", "totp_seed", "proxy_password", "session"}

ALG_AES_GCM = "aes-256-gcm"
ALG_DEV_FALLBACK = "hmac-sha256-stream-dev-fallback"

_log = logging.getLogger("vault")


def _structured_warning(event: str, **fields: object) -> None:
    """Log warning dang JSON 1 dong (khong chua secret) de observability
    bat duoc bang event thay vi doc van ban tu do."""
    payload = {"level": "warning", "component": "vault", "event": event}
    payload.update(fields)
    _log.warning(json.dumps(payload, sort_keys=True))


def dev_mode_enabled(explicit: Optional[bool] = None) -> bool:
    """DEV_MODE chi bat khi duoc khai bao RO RANG: tham so explicit,
    hoac env VAULT_DEV_MODE / DEV_MODE thuoc {1, true, yes, on}."""
    if explicit is not None:
        return bool(explicit)
    for name in ("VAULT_DEV_MODE", "DEV_MODE"):
        if os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


class VaultError(RuntimeError):
    pass


class TokenRevokedError(VaultError):
    """Token bi thu hoi / checkpoint: fail-closed, khong retry."""
    pass


def mask_secret(secret: str | None) -> str:
    if not secret:
        return ""
    return "***" if len(secret) <= 8 else f"{secret[:2]}***{secret[-2:]}"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode())


def _decode_kek_b64(raw: str) -> bytes:
    """Giai ma VAULT_KEK_B64 nghiem ngat: base64 hop le + dung 32 byte."""
    try:
        kek = base64.b64decode(raw.strip(), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VaultError("VAULT_KEK_B64 is not valid base64") from exc
    if len(kek) != 32:
        raise VaultError(
            f"VAULT_KEK_B64 must decode to exactly 32 bytes, got {len(kek)}")
    return kek


# ---------------------------------------------------------------- KEK
class KekProvider(Protocol):
    def wrap(self, dek: bytes) -> bytes: ...
    def unwrap(self, wrapped: bytes) -> bytes: ...


class EnvKekProvider:
    """KEK tu env VAULT_KEK_B64 (32B) - dev/self-host. Prod dung KMS.

    Fail-closed: khong co KEK tu env va khong co ``kek`` truyen truc
    tiep thi raise VaultError, TRU KHI dev_mode bat — khi do moi sinh
    KEK tam thoi trong RAM (``self.ephemeral is True``) kem canh bao;
    secret da luu bang KEK tam thoi se khong giai ma duoc sau restart.
    """

    def __init__(self, kek: bytes | None = None, *,
                 dev_mode: Optional[bool] = None):
        self.dev_mode = dev_mode_enabled(dev_mode)
        self.ephemeral = False
        raw = os.environ.get("VAULT_KEK_B64", "").strip()
        if kek is not None:
            if not isinstance(kek, (bytes, bytearray)) or len(kek) != 32:
                raise VaultError("KEK must be exactly 32 bytes")
            self._kek = bytes(kek)
        elif raw:
            self._kek = _decode_kek_b64(raw)
        elif self.dev_mode:
            self._kek = os.urandom(32)
            self.ephemeral = True
            _structured_warning(
                "ephemeral_kek_generated",
                reason="VAULT_KEK_B64 missing in DEV_MODE",
                impact="secrets stored with this KEK are lost on restart")
        else:
            raise VaultError(
                "VAULT_KEK_B64 not configured: refusing to start with an "
                "ephemeral KEK (all secrets would be lost on restart). "
                "Set VAULT_KEK_B64 to base64 of exactly 32 bytes, or set "
                "VAULT_DEV_MODE=1 for throwaway dev/test use.")

    def _require_aead_or_dev(self, op: str) -> None:
        if not _HAS_AESGCM and not self.dev_mode:
            raise VaultError(
                f"cryptography/AESGCM missing: refusing to {op} with the "
                "insecure HMAC fallback outside DEV_MODE")

    def _keystream_xor(self, nonce: bytes, data: bytes) -> bytes:
        out, counter = bytearray(), 0
        while len(out) < len(data):
            out += hmac.new(self._kek, nonce + counter.to_bytes(4, "big"),
                            hashlib.sha256).digest()
            counter += 1
        return bytes(a ^ b for a, b in zip(data, bytes(out[:len(data)])))

    def wrap(self, dek: bytes) -> bytes:
        if _HAS_AESGCM:
            nonce = os.urandom(12)
            return b"G1" + nonce + AESGCM(self._kek).encrypt(nonce, bytes(dek), b"dek")
        self._require_aead_or_dev("wrap DEK")
        nonce = os.urandom(16)
        ct = self._keystream_xor(nonce, bytes(dek))
        tag = hmac.new(self._kek, b"wrap" + nonce + ct, hashlib.sha256).digest()
        return b"H1" + nonce + tag + ct

    def unwrap(self, wrapped: bytes) -> bytes:
        if wrapped[:2] == b"G1":
            if not _HAS_AESGCM:
                raise VaultError("AESGCM record but cryptography missing")
            try:
                return AESGCM(self._kek).decrypt(wrapped[2:14], wrapped[14:], b"dek")
            except Exception as exc:
                raise VaultError(
                    "KEK unwrap failed: wrong KEK or corrupted wrapped DEK") from exc
        if wrapped[:2] == b"H1":
            self._require_aead_or_dev("unwrap fallback DEK")
            nonce, tag, ct = wrapped[2:18], wrapped[18:50], wrapped[50:]
            exp = hmac.new(self._kek, b"wrap" + nonce + ct, hashlib.sha256).digest()
            if not hmac.compare_digest(tag, exp):
                raise VaultError("KEK unwrap tag mismatch")
            return self._keystream_xor(nonce, ct)
        raise VaultError("unknown wrapped DEK format")


# ---------------------------------------------------------------- AEAD data
def _aead_encrypt(dek: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    if _HAS_AESGCM:
        nonce = os.urandom(12)
        return nonce, AESGCM(bytes(dek)).encrypt(nonce, plaintext, aad)
    nonce = os.urandom(16)
    stream, counter = bytearray(), 0
    while len(stream) < len(plaintext):
        stream += hmac.new(dek, nonce + counter.to_bytes(4, "big"),
                           hashlib.sha256).digest()
        counter += 1
    ct = bytes(a ^ b for a, b in zip(plaintext, bytes(stream[:len(plaintext)])))
    tag = hmac.new(dek, b"data" + aad + nonce + ct, hashlib.sha256).digest()
    return nonce, tag + ct


def _aead_decrypt(dek: bytes, nonce: bytes, blob: bytes, aad: bytes,
                  alg: str = "") -> bytes:
    """Giai ma theo ``alg`` cua ban ghi. Ban ghi cu khong co alg thi
    suy ra tu do dai nonce (12 = AES-GCM, 16 = fallback) nhu truoc."""
    use_gcm = (alg == ALG_AES_GCM) if alg else (len(nonce) == 12)
    if use_gcm:
        if not _HAS_AESGCM:
            raise VaultError("AESGCM record but cryptography missing")
        try:
            return AESGCM(bytes(dek)).decrypt(nonce, blob, aad)
        except Exception as exc:
            raise VaultError(
                "decrypt failed: wrong DEK/KEK or data tampered") from exc
    tag, ct = blob[:32], blob[32:]
    exp = hmac.new(dek, b"data" + aad + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, exp):
        raise VaultError("decrypt tag mismatch - data tampered or wrong DEK")
    stream, counter = bytearray(), 0
    while len(stream) < len(ct):
        stream += hmac.new(dek, nonce + counter.to_bytes(4, "big"),
                           hashlib.sha256).digest()
        counter += 1
    return bytes(a ^ b for a, b in zip(ct, bytes(stream[:len(ct)])))


@dataclass
class EncryptedRecord:
    credential_ref: str
    account_id: str
    kind: str
    version: int
    wrapped_dek: str
    nonce: str
    ciphertext: str
    created_at: str
    rotated_from: str = ""
    # Dinh dang ma hoa cua ban ghi: ALG_AES_GCM hoac ALG_DEV_FALLBACK.
    # "" = ban ghi legacy (truoc fix nay) — giai ma suy tu nonce.
    alg: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class InMemoryVaultStore:
    """Store interface toi thieu; prod thay bang DB (chi luu ciphertext)."""

    def __init__(self) -> None:
        self._r: dict[str, EncryptedRecord] = {}
        self._lock = threading.Lock()

    def put(self, rec: EncryptedRecord) -> None:
        with self._lock:
            self._r[rec.credential_ref] = rec

    def get(self, ref: str) -> Optional[EncryptedRecord]:
        with self._lock:
            return self._r.get(ref)

    def remove(self, ref: str) -> None:
        with self._lock:
            self._r.pop(ref, None)

    def find_latest(self, account_id: str, kind: str) -> Optional[EncryptedRecord]:
        """Ban ghi version cao nhat cua (account_id, kind) trong store
        dung chung — nguon su that de cac worker thay ref moi nhat sau
        khi mot worker khac rotate."""
        with self._lock:
            best: Optional[EncryptedRecord] = None
            for rec in self._r.values():
                if rec.account_id == account_id and rec.kind == kind:
                    if best is None or rec.version > best.version:
                        best = rec
            return best


class EncryptedSecretVault:
    """Implements contracts.SecretVault.

    Fail-closed khi thieu ``cryptography``: neu AESGCM khong dung duoc
    va khong o DEV_MODE (tham so ``allow_insecure_fallback`` / env
    VAULT_DEV_MODE / DEV_MODE) thi __init__ raise VaultError — khong
    bao gio am tham ghi secret bang fallback yeu hon. O DEV_MODE,
    ``self.insecure_fallback`` = True va moi ban ghi moi mang
    ``alg = ALG_DEV_FALLBACK`` de phan biet khoi AES-GCM that.
    """

    def __init__(self, kek: KekProvider | None = None,
                 store: InMemoryVaultStore | None = None,
                 *, allow_insecure_fallback: Optional[bool] = None):
        self._kek = kek or EnvKekProvider()
        self._store = store or InMemoryVaultStore()
        self._versions: dict[str, int] = {}
        self._lock = threading.Lock()
        self.insecure_fallback = False
        if not _HAS_AESGCM:
            if not dev_mode_enabled(allow_insecure_fallback):
                raise VaultError(
                    "cryptography/AESGCM is required: refusing to run the "
                    "vault on the insecure HMAC fallback outside DEV_MODE. "
                    "Install cryptography, or set VAULT_DEV_MODE=1 for "
                    "throwaway dev/test use.")
            self.insecure_fallback = True
            _structured_warning(
                "insecure_fallback_enabled",
                alg=ALG_DEV_FALLBACK,
                impact="new records are NOT AES-256-GCM; dev/test only")

    # -- Protocol ----------------------------------------------------
    def store(self, *, account_id: str, kind: str, secret: str) -> str:
        if kind not in _KINDS:
            raise VaultError(f"unsupported secret kind: {kind}")
        if not account_id or not secret:
            raise VaultError("account_id and secret are required")
        if not _HAS_AESGCM and not self.insecure_fallback:
            # Phong thu lap 2 (vd _HAS_AESGCM bi thay doi sau khoi tao):
            # khong ghi secret moi bang fallback khi chua duoc cho phep.
            raise VaultError(
                "AESGCM unavailable and insecure fallback not enabled: "
                "refusing to store new secrets")
        # DEK song dang bytearray tu dau de _wipe zero dung doi tuong nay.
        dek = bytearray(os.urandom(32))
        try:
            aad = f"{account_id}:{kind}".encode()
            nonce, ct = _aead_encrypt(dek, secret.encode(), aad)
            wrapped = self._kek.wrap(dek)
        finally:
            _wipe(dek)
        with self._lock:
            # Version phai nhat quan giua cac vault instance dung CHUNG
            # mot store: lay max(bien dem local, version cao nhat da co
            # trong store) + 1, tranh hai instance cung danh so v{n}.
            ver = self._versions.get(f"{account_id}:{kind}", 0)
            latest = self.latest_record(account_id, kind)
            if latest is not None:
                ver = max(ver, latest.version)
            ver += 1
            self._versions[f"{account_id}:{kind}"] = ver
        ref = f"vault://{account_id}/{kind}/v{ver}/{secrets.token_hex(6)}"
        self._store.put(EncryptedRecord(
            credential_ref=ref, account_id=account_id, kind=kind, version=ver,
            wrapped_dek=_b64e(wrapped), nonce=_b64e(nonce),
            ciphertext=_b64e(ct), created_at=utcnow().isoformat(),
            alg=ALG_AES_GCM if _HAS_AESGCM else ALG_DEV_FALLBACK))
        return ref

    def retrieve(self, *, credential_ref: str) -> str:
        rec = self._store.get(credential_ref)
        if rec is None:
            raise VaultError("credential_ref not found")
        # Cung nguyen tac bytearray + wipe dung doi tuong nhu store().
        dek = bytearray(self._kek.unwrap(_b64d(rec.wrapped_dek)))
        try:
            pt = _aead_decrypt(dek, _b64d(rec.nonce), _b64d(rec.ciphertext),
                               f"{rec.account_id}:{rec.kind}".encode(),
                               alg=rec.alg)
            return pt.decode()
        finally:
            _wipe(dek)

    def rotate(self, *, credential_ref: str, new_secret: str) -> str:
        old = self._store.get(credential_ref)
        if old is None:
            raise VaultError("credential_ref not found")
        new_ref = self.store(account_id=old.account_id, kind=old.kind,
                             secret=new_secret)
        rec = self._store.get(new_ref)
        if rec is not None:
            rec.rotated_from = credential_ref
        self._store.remove(credential_ref)  # old version destroyed
        return new_ref

    def delete(self, *, credential_ref: str) -> None:
        self._store.remove(credential_ref)

    # -- Shared-state reads (token refresh phan tan) -------------------
    def record_for(self, credential_ref: str) -> Optional[EncryptedRecord]:
        return self._store.get(credential_ref)

    def latest_record(self, account_id: str, kind: str) -> Optional[EncryptedRecord]:
        """Record moi nhat cua (account_id, kind) trong store dung
        chung, hoac None neu store khong ho tro tra cuu (khi do caller
        chi dua vao credential_ref cua minh — che do cu)."""
        find = getattr(self._store, "find_latest", None)
        if not callable(find):
            return None
        return find(account_id, kind)

    def latest_ref(self, account_id: str, kind: str) -> Optional[str]:
        rec = self.latest_record(account_id, kind)
        return rec.credential_ref if rec is not None else None

    # -- TokenSet helpers ---------------------------------------------
    def store_token_set(self, *, account_id: str, tokens: TokenSet) -> str:
        payload = json.dumps({
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "token_type": tokens.token_type,
            "expires_at": tokens.expires_at.isoformat() if tokens.expires_at else None,
            "scopes": tokens.scopes,
        })
        return self.store(account_id=account_id, kind="token_set", secret=payload)

    def retrieve_token_set(self, *, credential_ref: str) -> TokenSet:
        from datetime import datetime
        d = json.loads(self.retrieve(credential_ref=credential_ref))
        return TokenSet(
            access_token=d.get("access_token"),
            refresh_token=d.get("refresh_token"),
            token_type=d.get("token_type", "Bearer"),
            expires_at=datetime.fromisoformat(d["expires_at"]) if d.get("expires_at") else None,
            scopes=list(d.get("scopes") or []))


def _wipe(buf: bytearray) -> None:
    """Zero CHINH doi tuong bytearray duoc truyen vao (khong phai ban sao).

    Gioi han (doc ky truoc khi dua vao production): ham nay chi xoa
    duoc vung nho cua dung doi tuong buf ma caller dang giu. Moi ban
    sao da tao ra truoc do — bytes(buf) dua cho AESGCM, chuoi noi bo
    cua trinh thong dich, ban ghi swap/hibernation cua he dieu hanh —
    deu nam ngoai tam voi. Day la giam thieu ton du trong RAM, khong
    phai cam ket xoa tuyet doi; khoa dai han phai o KMS/HSM.
    """
    for i in range(len(buf)):
        buf[i] = 0


def validate_cookie_blob(raw: str) -> list[dict]:
    """Parse + validate cookie nhap thu cong. Tra list cookie, raise neu sai.

    Chap nhan JSON list [{name,value,domain?}] hoac header 'a=b; c=d'.
    Khong log gia tri cookie.
    """
    raw = (raw or "").strip()
    if not raw:
        raise VaultError("empty cookie blob")
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise VaultError(f"invalid cookie JSON: {e.msg}") from e
        out = []
        for c in data:
            if not isinstance(c, dict) or not c.get("name") or "value" not in c:
                raise VaultError("cookie entry missing name/value")
            out.append({"name": str(c["name"]), "value": str(c["value"]),
                        "domain": str(c.get("domain", ""))})
        if not out:
            raise VaultError("no cookies")
        return out
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise VaultError("cookie header part missing '='")
        n, v = part.split("=", 1)
        out.append({"name": n.strip(), "value": v.strip(), "domain": ""})
    if not out:
        raise VaultError("no cookies")
    return out


# ---------------------------------------------------------------- Distributed lock
class DistributedLock(Protocol):
    """Khoa phan tan co lease: acquire tra owner token (None = timeout),
    release chi xoa khoa khi owner khop, khoa tu het han theo TTL."""

    def acquire(self, key: str, *, ttl_seconds: float,
                timeout_seconds: float) -> Optional[str]: ...

    def release(self, key: str, owner: str) -> bool: ...


class InMemoryDistributedLock:
    """Backend khoa trong RAM voi semantics cua Redis SET NX PX:
    lease TTL, owner token, unlock compare-and-delete, acquire cho co
    timeout. CHI khoa duoc cac ben dung CHUNG mot instance nay —
    TokenManager mac dinh dung mot instance dung chung cap process;
    nhieu process thi phai inject Redis/Postgres dung chung."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._locks: dict[str, tuple[str, float]] = {}  # key -> (owner, exp monotonic)

    def acquire(self, key: str, *, ttl_seconds: float,
                timeout_seconds: float) -> Optional[str]:
        owner = secrets.token_hex(16)
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        with self._cond:
            while True:
                now = time.monotonic()
                cur = self._locks.get(key)
                if cur is None or cur[1] <= now:  # trong, hoac lease da het han
                    self._locks[key] = (owner, now + ttl_seconds)
                    return owner
                remaining = deadline - now
                if remaining <= 0:
                    return None  # timeout: fail-closed o caller
                self._cond.wait(timeout=min(remaining, max(cur[1] - now, 0.001), 0.25))

    def release(self, key: str, owner: str) -> bool:
        with self._cond:
            cur = self._locks.get(key)
            if cur is not None and cur[0] == owner:
                del self._locks[key]
                self._cond.notify_all()
                return True
            return False  # khong phai chu khoa: tuyet doi khong xoa

    def extend(self, key: str, owner: str, *, ttl_seconds: float) -> bool:
        with self._cond:
            cur = self._locks.get(key)
            if cur is not None and cur[0] == owner and cur[1] > time.monotonic():
                self._locks[key] = (owner, time.monotonic() + ttl_seconds)
                return True
            return False


class RedisDistributedLock:
    """Khoa phan tan that tren Redis: SET key owner NX PX (lease),
    unlock bang Lua compare-and-delete (chi chu khoa moi xoa duoc).
    ``client`` la redis-py client (hoac tuong thich: set(nx=, px=), eval)."""

    _UNLOCK_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
                   "return redis.call('del', KEYS[1]) else return 0 end")

    def __init__(self, client, *, prefix: str = "lock:"):
        self._client = client
        self._prefix = prefix

    def acquire(self, key: str, *, ttl_seconds: float,
                timeout_seconds: float) -> Optional[str]:
        owner = secrets.token_hex(16)
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while True:
            ok = self._client.set(self._prefix + key, owner, nx=True,
                                  px=max(int(ttl_seconds * 1000), 1))
            if ok:
                return owner
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.05, remaining))

    def release(self, key: str, owner: str) -> bool:
        try:
            return bool(self._client.eval(
                self._UNLOCK_LUA, 1, self._prefix + key, owner))
        except Exception:
            return False  # lease se tu het han; khong xoa mu


class PostgresAdvisoryLock:
    """Khoa phan tan bang Postgres advisory lock (giao dien thay the
    Redis): moi lan acquire giu mot connection rieng, lock song theo
    session, release = pg_advisory_unlock + tra connection.
    ``connection_factory`` tra ve psycopg connection (autocommit)."""

    def __init__(self, connection_factory: Callable[[], object]):
        self._factory = connection_factory
        self._held: dict[str, object] = {}  # owner -> connection
        self._guard = threading.Lock()

    @staticmethod
    def _lock_id(key: str) -> int:
        raw = hashlib.sha256(key.encode()).digest()[:8]
        return int.from_bytes(raw, "big", signed=True)

    def _query_bool(self, conn, sql: str, lock_id: int) -> bool:
        cur = conn.cursor()
        try:
            cur.execute(sql, (lock_id,))
            row = cur.fetchone()
            return bool(row and row[0])
        finally:
            cur.close()

    def acquire(self, key: str, *, ttl_seconds: float,
                timeout_seconds: float) -> Optional[str]:
        # ttl_seconds khong ap dung: advisory lock song theo session,
        # nhung acquire van co timeout nhu cac backend khac.
        del ttl_seconds
        owner = secrets.token_hex(16)
        conn = self._factory()
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        try:
            while True:
                if self._query_bool(conn, "SELECT pg_try_advisory_lock(%s)",
                                    self._lock_id(key)):
                    with self._guard:
                        self._held[owner] = conn
                    return owner
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    conn.close()
                    return None
                time.sleep(min(0.05, remaining))
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

    def release(self, key: str, owner: str) -> bool:
        with self._guard:
            conn = self._held.pop(owner, None)
        if conn is None:
            return False  # khong phai chu khoa
        try:
            return self._query_bool(conn, "SELECT pg_advisory_unlock(%s)",
                                    self._lock_id(key))
        finally:
            try:
                conn.close()
            except Exception:
                pass


_DEFAULT_LOCK = InMemoryDistributedLock()


def _default_lock_backend() -> InMemoryDistributedLock:
    """Instance khoa dung chung cap process cho cac TokenManager
    khong duoc inject backend rieng (van chi trong 1 process —
    production nhieu process phai inject backend dung chung that)."""
    return _DEFAULT_LOCK


# ---------------------------------------------------------------- Token manager
@dataclass
class _RefreshState:
    failures: int = 0
    last_error: str = ""


class TokenManager:
    """Refresh truoc han voi KHOA PHAN TAN theo account_id.

    ``lock_provider`` la DistributedLock dung chung giua cac worker/
    process (production: RedisDistributedLock hoac
    PostgresAdvisoryLock); mac dinh la InMemoryDistributedLock dung
    chung cap process. ``lock_ttl_seconds`` la lease cua khoa,
    ``lock_timeout_seconds`` la thoi gian cho toi da de lay khoa —
    het timeout thi fail-closed (khong refresh, khong rotate).
    ``refresher`` la ham cua adapter: TokenSet -> TokenSet, raise
    TokenRevokedError neu bi thu hoi/checkpoint.
    """

    def __init__(self, vault: EncryptedSecretVault,
                 refresher: Callable[[PlatformAccount, TokenSet], TokenSet],
                 *, max_failures: int = 3,
                 before_minutes: int = TOKEN_REFRESH_BEFORE_MINUTES,
                 lock_provider: Optional[DistributedLock] = None,
                 lock_ttl_seconds: float = 60.0,
                 lock_timeout_seconds: float = 10.0):
        self._vault, self._refresher = vault, refresher
        self._max_failures, self._before = max_failures, before_minutes
        self._lock: DistributedLock = lock_provider or _default_lock_backend()
        self._lock_ttl = float(lock_ttl_seconds)
        self._lock_timeout = float(lock_timeout_seconds)
        self._states: dict[str, _RefreshState] = {}
        self._guard = threading.Lock()

    def _state(self, account_id: str) -> _RefreshState:
        with self._guard:
            return self._states.setdefault(account_id, _RefreshState())

    def _read_current(self, account: PlatformAccount) -> Optional[TokenSet]:
        """Doc token set hien tai tu store dung chung. Neu
        account.credential_ref da bi worker khac rotate mat (ref cu
        khong con ton tai) thi tu dong nhay sang ref moi nhat cua
        account trong store; khong tim thay gi thi tra None."""
        try:
            return self._vault.retrieve_token_set(
                credential_ref=account.credential_ref)
        except VaultError:
            latest = self._vault.latest_ref(account.id, "token_set")
            if latest and latest != account.credential_ref:
                account.credential_ref = latest
                return self._vault.retrieve_token_set(credential_ref=latest)
            return None

    def ensure_fresh(self, account: PlatformAccount) -> tuple[TokenSet, AccountStatus]:
        """Tra (tokens, status_moi). Fail-closed khi revoked/het luot/
        khong lay duoc khoa phan tan trong timeout."""
        if not account.credential_ref:
            return TokenSet(), AccountStatus.NEEDS_REAUTH
        tokens = self._read_current(account)
        if tokens is None:
            return TokenSet(), AccountStatus.NEEDS_REAUTH
        if not tokens.needs_refresh(before_minutes=self._before):
            return tokens, account.status
        base_ref = account.credential_ref
        rec = self._vault.record_for(base_ref)
        base_version = rec.version if rec is not None else None
        st = self._state(account.id)
        key = f"token-refresh:{account.id}"
        owner = self._lock.acquire(key, ttl_seconds=self._lock_ttl,
                                   timeout_seconds=self._lock_timeout)
        if owner is None:
            # Het timeout lay khoa: FAIL-CLOSED — khong refresh, khong
            # rotate. Token con han thi tra lai kem TOKEN_EXPIRING de
            # caller lui lai sau; token da het han thi NEEDS_REAUTH.
            if tokens.is_expired():
                return tokens, AccountStatus.NEEDS_REAUTH
            return tokens, AccountStatus.TOKEN_EXPIRING
        try:
            # DOC LAI tu store dung chung SAU khi co khoa: neu ref/
            # version da doi thi worker khac da refresh xong — dung
            # ket qua cua ho, TUYET DOI khong refresh lan hai (rotate
            # refresh_token se lam ben thua mat session).
            latest = self._vault.latest_record(account.id, "token_set")
            if latest is not None and (
                    latest.credential_ref != base_ref
                    or (base_version is not None
                        and latest.version != base_version)):
                account.credential_ref = latest.credential_ref
                fresh = self._vault.retrieve_token_set(
                    credential_ref=latest.credential_ref)
                if not fresh.needs_refresh(before_minutes=self._before):
                    return fresh, AccountStatus.ACTIVE
                tokens = fresh
            try:
                new_tokens = self._refresher(account, tokens)
            except TokenRevokedError:
                return tokens, AccountStatus.NEEDS_REAUTH
            except Exception as exc:  # loi tam thoi: dem, khong retry vo han
                st.failures += 1
                st.last_error = type(exc).__name__
                if st.failures >= self._max_failures:
                    return tokens, AccountStatus.NEEDS_REAUTH
                return tokens, AccountStatus.TOKEN_EXPIRING
            if not new_tokens.access_token:
                return tokens, AccountStatus.NEEDS_REAUTH
            # Kiem tra lan cuoi truoc khi rotate: lease co the da het
            # han va mot worker khac co the da rotate trong luc ta
            # dang refresh — neu ref da doi thi huy rotate cua minh.
            winner = self._vault.latest_ref(account.id, "token_set")
            if winner and winner != account.credential_ref:
                account.credential_ref = winner
                won = self._vault.retrieve_token_set(credential_ref=winner)
                st.failures = 0
                return won, AccountStatus.ACTIVE
            new_ref = self._vault.rotate(
                credential_ref=account.credential_ref,
                new_secret=json.dumps({
                    "access_token": new_tokens.access_token,
                    "refresh_token": new_tokens.refresh_token or tokens.refresh_token,
                    "token_type": new_tokens.token_type,
                    "expires_at": new_tokens.expires_at.isoformat() if new_tokens.expires_at else None,
                    "scopes": new_tokens.scopes}))
            account.credential_ref = new_ref
            st.failures = 0
            return new_tokens, AccountStatus.ACTIVE
        finally:
            self._lock.release(key, owner)

    @staticmethod
    def expiry_within(tokens: TokenSet, *, hours: int = 24) -> bool:
        return bool(tokens.expires_at and
                    tokens.expires_at <= utcnow() + timedelta(hours=hours))
