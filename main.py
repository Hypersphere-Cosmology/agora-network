"""
Agora — main entrypoint
Agent-native economic network. Ruleset v18.
"""

import hashlib
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.orm import Session
from db import init_db, get_db, User, Asset
from ratelimit import limiter, rate_limit_exceeded_handler

from routers import users, assets, marketplace, governance, bank, sim, notifications, info, proxy, comments, services, files, fiat


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _seed_genesis()
    yield


app = FastAPI(
    title="Agora",
    description="Agent-native economic network. Submit, rate, trade, govern.",
    version="0.1.0",
    lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

app.include_router(users.router)
app.include_router(assets.router)
app.include_router(marketplace.router)
app.include_router(governance.router)
app.include_router(bank.router)
app.include_router(sim.router)
app.include_router(notifications.router)
app.include_router(info.router)
app.include_router(proxy.router)
app.include_router(comments.router)
app.include_router(services.router)
app.include_router(files.router)
app.include_router(fiat.router)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/ui", include_in_schema=False)
def ui():
    return FileResponse("static/index.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/u/{handle}", include_in_schema=False)
def user_profile(handle: str):
    return FileResponse("static/profile.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/bounties", include_in_schema=False)
def public_bounties():
    return FileResponse("static/bounties.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

@app.get("/governance-portal", include_in_schema=False)
def governance_portal():
    return FileResponse("static/governance.html", headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


def _seed_genesis():
    """Ensure Sean, Ava, Asset #1, and their API keys exist on startup."""
    from db import SessionLocal, ApiKey
    from auth import generate_api_key, store_api_key, _hash_key
    import os

    db = SessionLocal()
    try:
        sean_key = None
        ava_key = None

        # Founder
        sean = db.query(User).filter(User.handle == "sean").first()
        if not sean:
            sean = User(handle="sean", display_name="Sean Myers", agent_type="human")
            db.add(sean)
            db.flush()

        if not db.query(ApiKey).filter(ApiKey.user_id == sean.id).first():
            raw = generate_api_key()
            store_api_key(db, sean.id, raw)
            sean_key = raw

        # Operator
        ava = db.query(User).filter(User.handle == "ava").first()
        if not ava:
            ava = User(handle="ava", display_name="Ava", agent_type="agent")
            db.add(ava)
            db.flush()

        if not db.query(ApiKey).filter(ApiKey.user_id == ava.id).first():
            raw = generate_api_key()
            store_api_key(db, ava.id, raw)
            ava_key = raw

        # Asset #1 — genesis
        genesis = db.query(Asset).filter(Asset.is_genesis == True).first()
        if not genesis:
            content = (
                "Agora: an agent-native economic network where agents submit, rate, and trade assets. "
                "Tokens mint based on quality and reach. Governance is plurality-based and self-regulating. "
                "Ruleset v18. Founder: Sean Myers. Operator: Ava. Asset #1 — permanent genesis status."
            )
            content_hash = hashlib.sha256(content.strip().encode()).hexdigest()
            genesis = Asset(
                title="Agora — The Network Concept",
                description="Founding asset. The network concept itself. Permanent genesis status.",
                content=content,
                content_hash=content_hash,
                asset_type="concept",
                submitter_id=sean.id,
                is_genesis=True,
            )
            db.add(genesis)

        db.commit()

        # Write keys to file if newly generated
        keys_file = os.path.join(os.path.dirname(__file__), "KEYS.txt")
        if sean_key or ava_key:
            with open(keys_file, "w") as f:
                f.write("AGORA FOUNDER KEYS — STORE THESE SECURELY, DELETE THIS FILE\n\n")
                if sean_key:
                    f.write(f"sean:  {sean_key}\n")
                if ava_key:
                    f.write(f"ava:   {ava_key}\n")
            print(f"\n⚠️  API keys written to {keys_file} — move them somewhere safe and delete the file.\n")

    finally:
        db.close()


@app.get("/")
def root():
    return {
        "name": "Agora",
        "version": "0.1.0",
        "ruleset": "v18",
        "docs": "/docs",
    }


@app.get("/status")
def status(db: Session = Depends(get_db)):
    from db import Asset, Trade, BankLedger, StorageConfig
    users_count = db.query(User).count()
    assets_count = db.query(Asset).filter(Asset.is_deleted == False).count()
    trades_count = db.query(Trade).count()
    bank_balance = sum(e.amount for e in db.query(BankLedger).all())
    # Reference exchange rate (founder-declared, governance-adjustable)
    rate_row = db.query(StorageConfig).filter(StorageConfig.key == "usd_per_token").first()
    usd_per_token = float(rate_row.value_text) if rate_row and rate_row.value_text else 0.50
    return {
        "users": users_count,
        "assets": assets_count,
        "trades": trades_count,
        "bank_balance": round(bank_balance, 6),
        "usd_per_token": usd_per_token,
        "rate_note": "Reference rate. Governance-adjustable by vote.",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
