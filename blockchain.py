"""
Agora — blockchain integration
SOL wallet generation, on-chain payment verification, auto-confirm purchases.
"""

import json
import time
import asyncio
from pathlib import Path
from typing import Optional

# Wallet file — stored locally, never committed
WALLET_FILE = Path(__file__).parent / "wallet.json"
SECRETS_FILE = Path(__file__).parent / ".secrets" / "treasury.json"

# Solana mainnet RPC endpoints (public, no API key needed)
SOL_RPC_MAINNET = "https://api.mainnet-beta.solana.com"
SOL_RPC_DEVNET  = "https://api.devnet.solana.com"


def generate_sol_wallet() -> dict:
    """Generate a new Solana keypair. Returns {pubkey, privkey_base58}."""
    from solders.keypair import Keypair
    kp = Keypair()
    return {
        "pubkey": str(kp.pubkey()),
        "privkey_base58": str(kp),   # base58 encoded secret key
        "network": "mainnet-beta",
    }


def load_treasury() -> Optional[dict]:
    """Load treasury wallet from secrets file. Returns None if not configured."""
    if SECRETS_FILE.exists():
        return json.loads(SECRETS_FILE.read_text())
    return None


def save_treasury(wallet: dict):
    """Save treasury wallet to secrets file (never commit this)."""
    SECRETS_FILE.parent.mkdir(exist_ok=True)
    SECRETS_FILE.write_text(json.dumps(wallet, indent=2))
    SECRETS_FILE.chmod(0o600)  # owner read-only


async def get_sol_balance(pubkey: str, rpc: str = SOL_RPC_MAINNET) -> float:
    """Get SOL balance for a pubkey. Returns balance in SOL."""
    import httpx
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getBalance",
        "params": [pubkey, {"commitment": "confirmed"}]
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(rpc, json=payload)
        data = r.json()
        lamports = data.get("result", {}).get("value", 0)
        return lamports / 1e9  # lamports → SOL


async def verify_sol_payment(
    txid: str,
    expected_recipient: str,
    expected_usd: float,
    sol_price_usd: float,
    rpc: str = SOL_RPC_MAINNET,
    tolerance_pct: float = 2.0,  # allow 2% price slippage
) -> dict:
    """
    Verify a Solana transaction.
    Returns: {verified: bool, amount_sol: float, amount_usd: float, error: str|None}
    """
    import httpx

    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getTransaction",
        "params": [
            txid,
            {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(rpc, json=payload)
            data = r.json()

        tx = data.get("result")
        if not tx:
            return {"verified": False, "error": "Transaction not found. May not be confirmed yet."}

        # Check transaction succeeded
        if tx.get("meta", {}).get("err") is not None:
            return {"verified": False, "error": "Transaction failed on-chain."}

        # Parse account keys
        account_keys = tx.get("transaction", {}).get("message", {}).get("accountKeys", [])
        pre_balances = tx.get("meta", {}).get("preBalances", [])
        post_balances = tx.get("meta", {}).get("postBalances", [])

        # Find recipient index
        recipient_idx = None
        for i, key in enumerate(account_keys):
            if str(key) == expected_recipient:
                recipient_idx = i
                break

        if recipient_idx is None:
            return {"verified": False, "error": f"Recipient {expected_recipient} not found in transaction."}

        # Calculate amount received (in lamports → SOL)
        received_lamports = post_balances[recipient_idx] - pre_balances[recipient_idx]
        received_sol = received_lamports / 1e9

        if received_sol <= 0:
            return {"verified": False, "error": f"No SOL received by {expected_recipient}."}

        # Check amount matches expected (within tolerance)
        expected_sol = expected_usd / sol_price_usd
        received_usd = received_sol * sol_price_usd
        diff_pct = abs(received_usd - expected_usd) / expected_usd * 100

        if diff_pct > tolerance_pct:
            return {
                "verified": False,
                "error": f"Amount mismatch: expected ${expected_usd:.2f}, received ${received_usd:.2f} ({diff_pct:.1f}% off)",
                "amount_sol": received_sol,
                "amount_usd": received_usd,
            }

        return {
            "verified": True,
            "amount_sol": received_sol,
            "amount_usd": received_usd,
            "error": None,
        }

    except Exception as e:
        return {"verified": False, "error": str(e)}


async def get_sol_price_usd() -> float:
    """Fetch current SOL price from CoinGecko (free, no API key)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"}
            )
            return float(r.json()["solana"]["usd"])
    except Exception:
        return 150.0  # fallback if API is down


async def verify_eth_payment(
    txid: str,
    expected_recipient: str,
    expected_usd: float,
    eth_price_usd: float,
    rpc: str = "https://cloudflare-eth.com",
    tolerance_pct: float = 2.0,
) -> dict:
    """Verify an Ethereum transaction."""
    import httpx

    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "eth_getTransactionByHash",
        "params": [txid if txid.startswith("0x") else f"0x{txid}"]
    }

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(rpc, json=payload)
            tx = r.json().get("result")

        if not tx:
            return {"verified": False, "error": "Transaction not found."}

        # Check recipient
        to_addr = (tx.get("to") or "").lower()
        if to_addr != expected_recipient.lower():
            return {"verified": False, "error": f"Recipient mismatch: got {to_addr}"}

        # Check receipt (confirms transaction succeeded)
        receipt_payload = {
            "jsonrpc": "2.0", "id": 2,
            "method": "eth_getTransactionReceipt",
            "params": [txid if txid.startswith("0x") else f"0x{txid}"]
        }
        async with httpx.AsyncClient(timeout=20) as client:
            rr = await client.post(rpc, json=receipt_payload)
            receipt = rr.json().get("result")

        if not receipt:
            return {"verified": False, "error": "Transaction not confirmed yet."}
        if receipt.get("status") != "0x1":
            return {"verified": False, "error": "Transaction reverted on-chain."}

        # Parse value (wei → ETH)
        value_wei = int(tx.get("value", "0x0"), 16)
        value_eth = value_wei / 1e18
        value_usd = value_eth * eth_price_usd

        diff_pct = abs(value_usd - expected_usd) / expected_usd * 100
        if diff_pct > tolerance_pct:
            return {
                "verified": False,
                "error": f"Amount mismatch: expected ${expected_usd:.2f}, got ${value_usd:.2f}",
                "amount_eth": value_eth,
                "amount_usd": value_usd,
            }

        return {"verified": True, "amount_eth": value_eth, "amount_usd": value_usd, "error": None}

    except Exception as e:
        return {"verified": False, "error": str(e)}


async def auto_verify_and_confirm(purchase_id: int, db_session_factory):
    """
    Background task: poll for transaction confirmation, auto-mint on success.
    Called after buyer submits txid.
    """
    import asyncio
    from db import TokenPurchase, User, TokenEvent, StorageConfig
    from notifications import notify

    await asyncio.sleep(10)  # brief delay before first check

    for attempt in range(12):  # try for ~10 minutes
        db = db_session_factory()
        try:
            purchase = db.query(TokenPurchase).filter(TokenPurchase.id == purchase_id).first()
            if not purchase or purchase.status not in ("confirming",):
                return  # already handled

            if not purchase.txid:
                await asyncio.sleep(30)
                continue

            # Get current crypto price
            if purchase.payment_method == "sol":
                sol_price = await get_sol_price_usd()
                treasury = load_treasury()
                recipient = treasury.get("pubkey") if treasury else None
                if not recipient:
                    return  # no wallet configured — fall back to manual

                result = await verify_sol_payment(
                    txid=purchase.txid,
                    expected_recipient=recipient,
                    expected_usd=purchase.amount_usd,
                    sol_price_usd=sol_price,
                )
            elif purchase.payment_method == "eth":
                import httpx
                try:
                    async with httpx.AsyncClient(timeout=10) as c:
                        r = await c.get("https://api.coingecko.com/api/v3/simple/price",
                                        params={"ids":"ethereum","vs_currencies":"usd"})
                        eth_price = float(r.json()["ethereum"]["usd"])
                except Exception:
                    eth_price = 3000.0

                from db import PaymentAddress
                addr_row = db.query(PaymentAddress).filter(
                    PaymentAddress.currency == "eth", PaymentAddress.is_active == True
                ).first()
                if not addr_row:
                    return

                result = await verify_eth_payment(
                    txid=purchase.txid,
                    expected_recipient=addr_row.address,
                    expected_usd=purchase.amount_usd,
                    eth_price_usd=eth_price,
                )
            else:
                return  # manual — needs human confirmation

            if result["verified"]:
                # Mint tokens
                buyer = db.query(User).filter(User.id == purchase.buyer_id).first()
                buyer.token_balance = round(buyer.token_balance + purchase.amount_tokens, 6)
                purchase.status = "complete"
                purchase.notes = (purchase.notes or "") + f"\nAuto-confirmed on-chain: {result}"
                from datetime import datetime, timezone
                purchase.updated_at = datetime.now(timezone.utc)

                db.add(TokenEvent(
                    event_type="purchase_mint",
                    user_id=buyer.id,
                    amount=purchase.amount_tokens,
                    note=f"auto-confirmed: {purchase.amount_tokens} A for ${purchase.amount_usd:.2f} via {purchase.payment_method}"
                ))
                db.commit()

                notify(db, buyer.id, "tokens_minted",
                       f"✅ {purchase.amount_tokens} A minted automatically! "
                       f"On-chain payment verified. New balance: {buyer.token_balance:.4f} A.")
                db.commit()
                return

            elif "not found" in (result.get("error") or "").lower() or "not confirmed" in (result.get("error") or "").lower():
                # Transaction pending — retry
                db.close()
                await asyncio.sleep(60)
                continue
            else:
                # Hard failure — flag for manual review
                purchase.notes = (purchase.notes or "") + f"\nAuto-verify failed: {result.get('error')}"
                purchase.status = "confirming"  # keep for manual review
                db.commit()

                from db import User as UserModel
                for handle in ("sean", "ava"):
                    founder = db.query(UserModel).filter(UserModel.handle == handle).first()
                    if founder:
                        notify(db, founder.id, "verify_failed",
                               f"Auto-verify FAILED for purchase #{purchase_id}: {result.get('error')}. Manual review needed.")
                db.commit()
                return

        except Exception as e:
            print(f"[blockchain] auto_verify error: {e}")
        finally:
            db.close()

        await asyncio.sleep(60)
