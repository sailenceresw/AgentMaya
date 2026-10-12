"""What a round trip actually costs, per venue.

Every trade pays twice — once to get in, once to get out — and on the
MetaMask/Hyperliquid path it pays two different parties each time:

* **Hyperliquid's exchange fee.** Tier 0 (under $5M of 14-day volume) is
  1.5bps for a maker order (one that rests on the book) and 4.5bps for a
  taker (one that crosses it).
* **MetaMask's builder fee.** The Agent Wallet attaches a Hyperliquid
  "builder fee" to every open, close and TP/SL order. Its size is set per
  wallet by MetaMask's fee service and Hyperliquid caps it at 10bps for
  perps. ``mm perps quote`` reports the real figure as ``builderFeeBips``;
  until that has been read, the cap is assumed.

Which side of the book a leg lands on matters as much as the rates. A limit
entry rests, so it pays maker. Hyperliquid TP/SL orders are *trigger market*
orders, so every exit through them pays taker. On Binance the take-profit is
a resting reduce-only limit (maker) and the stop is a stop-market (taker).

The figure that decides whether a strategy can make money is the round trip
as a fraction of notional, compared with the price move the trade is aiming
for. Fees scale with position size exactly as profit does, so trading
smaller never fixes a fee problem; only a larger move per trade does.
"""
from __future__ import annotations

from dataclasses import dataclass

from maya.config import Settings, Venue

# Basis points -> fraction.
_BP = 1e-4

# Hyperliquid tier 0, perps.
HL_MAKER_BPS = 1.5
HL_TAKER_BPS = 4.5
# Hyperliquid's cap on a perps builder fee — MetaMask's actual figure is read
# from `mm perps quote` and can only be lower.
HL_BUILDER_CAP_BPS = 10.0
# Binance USD-M VIP 0.
BINANCE_MAKER_BPS = 2.0
BINANCE_TAKER_BPS = 5.0


@dataclass(frozen=True)
class FeeModel:
    """Per-side rates, as fractions of notional."""

    maker: float
    taker: float
    builder: float = 0.0          # charged on every leg, maker or taker
    tp_is_maker: bool = False     # True where the take-profit rests as a limit

    @classmethod
    def for_settings(cls, settings: Settings) -> "FeeModel":
        """The model for the configured venue, with any explicit overrides.

        ``MAYA_FEE_RATE`` (the legacy single per-side rate) still wins when
        set, applied to both sides with no builder fee — that is what it
        always meant.
        """
        if settings.fee_rate is not None:
            return cls(maker=settings.fee_rate, taker=settings.fee_rate)
        if settings.venue is Venue.HYPERLIQUID:
            maker = settings.fee_maker_bps if settings.fee_maker_bps is not None else HL_MAKER_BPS
            taker = settings.fee_taker_bps if settings.fee_taker_bps is not None else HL_TAKER_BPS
            builder = (settings.builder_fee_bps if settings.builder_fee_bps is not None
                       else HL_BUILDER_CAP_BPS)
            return cls(maker * _BP, taker * _BP, builder * _BP, tp_is_maker=False)
        maker = settings.fee_maker_bps if settings.fee_maker_bps is not None else BINANCE_MAKER_BPS
        taker = settings.fee_taker_bps if settings.fee_taker_bps is not None else BINANCE_TAKER_BPS
        builder = settings.builder_fee_bps or 0.0
        return cls(maker * _BP, taker * _BP, builder * _BP, tp_is_maker=True)

    # -- per leg ----------------------------------------------------------- #
    def entry_rate(self, order_type: str = "limit") -> float:
        return (self.maker if order_type == "limit" else self.taker) + self.builder

    def exit_rate(self, reason: str = "stop_loss") -> float:
        """Exit cost by how the position closed. Only a resting take-profit
        on a venue that supports one pays maker; stops, forced and
        max-duration closes are market orders."""
        maker_exit = self.tp_is_maker and reason == "take_profit"
        return (self.maker if maker_exit else self.taker) + self.builder

    # -- round trips --------------------------------------------------------- #
    def round_trip(self, order_type: str = "limit", reason: str = "stop_loss") -> float:
        return self.entry_rate(order_type) + self.exit_rate(reason)

    def worst_round_trip(self, order_type: str = "limit") -> float:
        """The dearer of the two planned exits. Used for gating, so a trade
        is never approved on the assumption that it will exit cheaply."""
        return max(self.round_trip(order_type, "take_profit"),
                   self.round_trip(order_type, "stop_loss"))

    def cost_usd(self, notional: float, order_type: str = "limit",
                 reason: str = "stop_loss") -> float:
        return notional * self.round_trip(order_type, reason)


@dataclass(frozen=True)
class TradeEconomics:
    """A planned trade's outcome *after* fees, as fractions of entry price."""

    target_move: float        # gross distance to TP1
    stop_move: float          # gross distance to the stop
    fees: float               # round trip charged on the trade

    @property
    def net_win(self) -> float:
        return self.target_move - self.fees

    @property
    def net_loss(self) -> float:
        return self.stop_move + self.fees

    @property
    def net_rr(self) -> float:
        return self.net_win / self.net_loss if self.net_loss > 0 else 0.0

    @property
    def fee_share_of_target(self) -> float:
        """How much of the hoped-for profit the fees take."""
        return self.fees / self.target_move if self.target_move > 0 else float("inf")

    @property
    def breakeven_win_rate(self) -> float:
        """Win rate needed just to pay for the losers and the fees."""
        total = self.net_win + self.net_loss
        return self.net_loss / total if self.net_win > 0 and total > 0 else 1.0


def economics(entry: float, stop: float, target: float, model: FeeModel,
              order_type: str = "limit") -> TradeEconomics:
    return TradeEconomics(
        target_move=abs(target - entry) / entry,
        stop_move=abs(entry - stop) / entry,
        fees=model.worst_round_trip(order_type),
    )
