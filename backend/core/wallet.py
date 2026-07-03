"""Wallet generation, key encryption, and CLOB client construction.

Custodial model: the server generates an Ethereum signer keypair per user, stores
the private key encrypted at rest, and signs orders on the user's behalf.

Encryption: AES-256-GCM with a key derived from the server ``ENCRYPTION_SECRET``
via HKDF. The copy engine must decrypt autonomously to sign orders, so this is
not passphrase-gated — onboarding is create-only and ``/export-key`` is gated
only by wallet auth (see BUILD_PLAN.md for that tradeoff). ``encrypt_for_export``/
``decrypt_export`` (scrypt-based, passphrase-keyed) below are unused by the
current API but kept — harmless, generically useful if a passphrase layer is
reintroduced later.

Signing model: uses ``polymarket-client``'s ``AsyncSecureClient`` — NOT
py-clob-client or py-clob-client-v2, both of which have real, currently-open,
upstream-unfixed bugs for anything beyond plain EOA (see BUILD_PLAN.md
§wallet model: py-clob-client is archived and signs a rejected order format;
py-clob-client-v2's L1 auth always binds the API key to the EOA regardless of
signature_type/funder, breaking POLY_1271/deposit-wallet order placement).

With a Builder API key configured (``POLYMARKET_BUILDER_API_KEY`` etc., from
polymarket.com/settings?tab=builder), ``make_clob_client(funder=None)`` derives
and deploys a gasless Deposit Wallet automatically (``AsyncSecureClient.create``
calls this internally). Without one, it falls back to plain EOA mode (the
signer's own address holds funds; needs a little MATIC for the one-time
allowance — and real order placement is unverified in that mode).

``ensure_allowances`` approves the exchange operators individually rather than
calling the SDK's bundled ``setup_trading_approvals()`` — that bundle also
tries to approve the auto-redeem operator, which our Builder key's relayer
currently rejects ("operator is not in the allowed list"; confirmed with
Polymarket that this needs no config change on our end — auto-redeem isn't
required for placing/filling orders, only for automatic claim-on-resolution).
The three operators we do approve are exactly the ones ``get_balance_allowance``
tracks, i.e. the ones that actually gate order placement.

A freshly created/deployed wallet takes a few seconds to be indexed by
Polymarket's backend — ``wait_wallet_ready`` retries the balance read past that
window; every code path that just created a wallet must call it before relying
on balance/allowance reads or submitting approvals.
"""
from __future__ import annotations

import asyncio
import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from eth_account import Account

from backend.config import (
    POLYMARKET_BUILDER_API_KEY,
    POLYMARKET_BUILDER_PASSPHRASE,
    POLYMARKET_BUILDER_SECRET,
)

_NONCE = 12
_SALT = 16
_SCHEME_AT_REST = b"\x01"
_SCHEME_EXPORT = b"\x02"


# ---------------------------------------------------------------------------
# Keypair generation
# ---------------------------------------------------------------------------

def create_signer() -> dict:
    """Generate a fresh Ethereum keypair (uses os.urandom entropy).

    Returns {'address': '0x...'(checksummed), 'private_key': '0x...'}.
    The caller encrypts the private key before any DB write — it is never
    persisted in plaintext.
    """
    acct = Account.create()
    return {"address": acct.address, "private_key": "0x" + acct.key.hex()}


def address_for_key(private_key_hex: str) -> str:
    """Checksummed EOA address for a private key."""
    return Account.from_key(private_key_hex).address


# ---------------------------------------------------------------------------
# Encryption — at rest (HKDF over high-entropy server secret)
# ---------------------------------------------------------------------------

def _aesgcm_encrypt(plaintext: str, key: bytes, scheme: bytes, salt: bytes = b"") -> str:
    nonce = os.urandom(_NONCE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode(), None)
    return base64.b64encode(scheme + salt + nonce + ct).decode()


def encrypt_private_key(private_key_hex: str, secret: str) -> str:
    """AES-256-GCM encrypt for storage. ``secret`` = ENCRYPTION_SECRET."""
    if not secret:
        raise ValueError("ENCRYPTION_SECRET is not set")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"copybot-at-rest").derive(secret.encode())
    return _aesgcm_encrypt(private_key_hex, key, _SCHEME_AT_REST)


def decrypt_private_key(blob: str, secret: str) -> str:
    raw = base64.b64decode(blob)
    if raw[:1] != _SCHEME_AT_REST:
        raise ValueError("not an at-rest ciphertext")
    nonce, ct = raw[1:1 + _NONCE], raw[1 + _NONCE:]
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=b"copybot-at-rest").derive(secret.encode())
    return AESGCM(key).decrypt(nonce, ct, None).decode()


# ---------------------------------------------------------------------------
# Encryption — export (scrypt over user passphrase, salt embedded)
# ---------------------------------------------------------------------------

def _scrypt_key(passphrase: str, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2 ** 14, r=8, p=1).derive(passphrase.encode())


def encrypt_for_export(private_key_hex: str, passphrase: str) -> str:
    """Re-encrypt under a user passphrase for /export-key (second factor)."""
    if not passphrase:
        raise ValueError("passphrase required")
    salt = os.urandom(_SALT)
    return _aesgcm_encrypt(private_key_hex, _scrypt_key(passphrase, salt),
                           _SCHEME_EXPORT, salt)


def decrypt_export(blob: str, passphrase: str) -> str:
    raw = base64.b64decode(blob)
    if raw[:1] != _SCHEME_EXPORT:
        raise ValueError("not an export ciphertext")
    salt = raw[1:1 + _SALT]
    nonce = raw[1 + _SALT:1 + _SALT + _NONCE]
    ct = raw[1 + _SALT + _NONCE:]
    return AESGCM(_scrypt_key(passphrase, salt)).decrypt(nonce, ct, None).decode()


# ---------------------------------------------------------------------------
# CLOB client (polymarket-client's AsyncSecureClient)
# ---------------------------------------------------------------------------

def _builder_api_key():
    if not (POLYMARKET_BUILDER_API_KEY and POLYMARKET_BUILDER_SECRET
            and POLYMARKET_BUILDER_PASSPHRASE):
        return None
    from polymarket.auth import BuilderApiKey
    return BuilderApiKey(
        key=POLYMARKET_BUILDER_API_KEY,
        secret=POLYMARKET_BUILDER_SECRET,
        passphrase=POLYMARKET_BUILDER_PASSPHRASE,
    )


async def make_clob_client(private_key_hex: str, *, funder: str | None = None):
    """Build an authenticated AsyncSecureClient.

    funder=None with a Builder key configured -> derive+deploy the gasless
    Deposit Wallet (first call for a new signer). Pass the known deposit/funder
    address on subsequent calls to skip re-derivation. Without a Builder key,
    funder=None falls back to plain EOA mode (signer's own address).
    """
    from polymarket import AsyncSecureClient
    from polymarket.errors import RateLimitError

    api_key = _builder_api_key()
    wallet_arg = funder
    if wallet_arg is None and api_key is None:
        wallet_arg = address_for_key(private_key_hex)  # gasless unavailable -> EOA

    # Deployment/registration goes through Polymarket's relayer, which does get
    # rate-limited under bursts of signups — retry a couple times with backoff
    # rather than failing the whole signup on a transient 429.
    delay = 2.0
    for attempt in range(3):
        try:
            return await AsyncSecureClient.create(
                private_key=private_key_hex, wallet=wallet_arg, api_key=api_key)
        except RateLimitError:
            if attempt == 2:
                raise
            await asyncio.sleep(delay)
            delay *= 2


async def wait_wallet_ready(client, *, attempts: int = 8, delay: float = 3.0):
    """Retry the balance read until the (just-created) wallet is indexed by
    Polymarket's backend — takes a few seconds after deployment. Returns the
    BalanceAllowance once it succeeds; re-raises the last error otherwise."""
    for i in range(attempts):
        try:
            return await client.get_balance_allowance(asset_type="COLLATERAL")
        except Exception:
            if i == attempts - 1:
                raise
            await asyncio.sleep(delay)


# Exchange operators that actually gate order placement (matches what
# get_balance_allowance tracks) — deliberately excludes auto_redeem_operator;
# see module docstring.
def _core_operators(env) -> tuple[str, str, str]:
    return (env.standard_exchange, env.neg_risk_exchange, env.neg_risk_adapter)


async def ensure_allowances(client) -> None:
    """Approve the exchange operators individually — see module docstring for
    why this doesn't use the SDK's bundled setup_trading_approvals(). Submits
    all approval calls first, then waits for confirmations concurrently."""
    env = client.environment
    collateral, conditional = env.collateral_token, env.conditional_tokens
    handles = []
    for operator in _core_operators(env):
        handles.append(await client.approve_erc20(
            token_address=collateral, spender_address=operator, amount="max"))
        handles.append(await client.approve_erc1155_for_all(
            token_address=conditional, operator_address=operator, approved=True))
    await asyncio.gather(*(h.wait() for h in handles))
