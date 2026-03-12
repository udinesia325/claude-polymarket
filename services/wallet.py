"""
services/wallet.py
──────────────────
Wallet management via web3.py.
Handles signing, balance checks, and USDC allowance on Polygon / Mumbai.
"""

import logging
from decimal import Decimal

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

from config.settings import Settings

logger = logging.getLogger(__name__)

# Minimal ERC-20 ABI (balanceOf + approve + allowance)
ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# USDC contract addresses
USDC_ADDRESS = {
    137: "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",   # Polygon mainnet
    80001: "0x0FA8781a83E46826621b3BC094Ea2A0212e71B23",  # Mumbai testnet
}

# Polymarket CLOB exchange address (spender for USDC approval)
CLOB_EXCHANGE = {
    137: "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    80001: "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
}


class WalletService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        # POA middleware required for Polygon / Mumbai
        self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

        self.account = self.w3.eth.account.from_key(settings.wallet_private_key)
        assert self.account.address.lower() == settings.wallet_address.lower(), (
            "WALLET_ADDRESS does not match the address derived from WALLET_PRIVATE_KEY"
        )

        usdc_addr = USDC_ADDRESS.get(settings.chain_id)
        if not usdc_addr:
            raise ValueError(f"No USDC address configured for chain_id={settings.chain_id}")
        self.usdc = self.w3.eth.contract(
            address=Web3.to_checksum_address(usdc_addr), abi=ERC20_ABI
        )

        logger.info(
            "WalletService ready | chain=%s | address=%s | mode=%s",
            settings.chain_id,
            self.account.address,
            settings.trading_mode,
        )

    # ── Balance helpers ───────────────────────────────────────────────────────

    def get_matic_balance(self) -> Decimal:
        """Return MATIC balance in ether units."""
        raw = self.w3.eth.get_balance(self.account.address)
        return Decimal(self.w3.from_wei(raw, "ether"))

    def get_usdc_balance(self) -> Decimal:
        """Return USDC balance (6 decimals → human-readable)."""
        raw = self.usdc.functions.balanceOf(self.account.address).call()
        return Decimal(raw) / Decimal(10**6)

    # ── Allowance ─────────────────────────────────────────────────────────────

    def get_usdc_allowance(self) -> Decimal:
        """Return USDC allowance granted to the CLOB exchange."""
        spender = CLOB_EXCHANGE.get(self.settings.chain_id, "")
        if not spender:
            return Decimal(0)
        raw = self.usdc.functions.allowance(
            self.account.address, Web3.to_checksum_address(spender)
        ).call()
        return Decimal(raw) / Decimal(10**6)

    def ensure_usdc_approval(self, amount_usdc: float = 1_000_000) -> str | None:
        """
        Approve the CLOB exchange to spend USDC if current allowance is insufficient.
        Returns tx hash string or None if no tx was needed.
        """
        if self.settings.dry_run:
            logger.info("[DRY_RUN] Skipping USDC approve tx")
            return None

        spender = CLOB_EXCHANGE.get(self.settings.chain_id)
        if not spender:
            raise ValueError("CLOB_EXCHANGE address unknown for this chain")

        amount_wei = int(Decimal(str(amount_usdc)) * Decimal(10**6))
        current = self.get_usdc_allowance()
        if current >= Decimal(str(amount_usdc)):
            logger.debug("USDC allowance sufficient (%.2f)", float(current))
            return None

        nonce = self.w3.eth.get_transaction_count(self.account.address)
        tx = self.usdc.functions.approve(
            Web3.to_checksum_address(spender), amount_wei
        ).build_transaction(
            {
                "from": self.account.address,
                "nonce": nonce,
                "gas": 100_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.settings.chain_id,
            }
        )
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        logger.info("USDC approved | tx=%s | status=%s", tx_hash.hex(), receipt.status)
        return tx_hash.hex()

    # ── Summary ───────────────────────────────────────────────────────────────

    def get_summary(self) -> dict:
        return {
            "address": self.account.address,
            "chain_id": self.settings.chain_id,
            "matic_balance": float(self.get_matic_balance()),
            "usdc_balance": float(self.get_usdc_balance()),
            "usdc_allowance": float(self.get_usdc_allowance()),
        }
