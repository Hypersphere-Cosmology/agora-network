"""
Agora — live network config
Values here can be changed by governance vote.
"""

# Fee rate on all token transfers and bounty claims
TRADE_FEE_RATE: float = 0.01   # 1% — subject to governance vote


def get_fee_rate() -> float:
    return TRADE_FEE_RATE


def set_fee_rate(rate: float):
    global TRADE_FEE_RATE
    TRADE_FEE_RATE = rate
