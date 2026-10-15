"""Currency metadata for multi-currency support.

Phase 1 (data model foundation): every money document carries an ISO 4217
currency code, defaulting to USD. No exchange rates, no forex postings, no
UI pickers yet — those are later phases. Behavior for existing USD flows
is unchanged.

To add a currency, add one line to CURRENCIES below.
"""

# ISO code -> metadata. Ordered by usage (most-used first).
# "decimals": minor-unit places (0 = zero-decimal, e.g. JPY has no cents).
# "stripe": True where Stripe supports presentment (charging) in that currency.
CURRENCIES = {
    "USD": {"symbol": "$", "name": "US Dollar", "decimals": 2, "stripe": True},
    "EUR": {"symbol": "\u20ac", "name": "Euro", "decimals": 2, "stripe": True},
    "JPY": {"symbol": "\u00a5", "name": "Japanese Yen", "decimals": 0, "stripe": True},
    "GBP": {"symbol": "\u00a3", "name": "British Pound", "decimals": 2, "stripe": True},
    "CNY": {"symbol": "\u00a5", "name": "Chinese Yuan", "decimals": 2, "stripe": True},
    "AUD": {"symbol": "A$", "name": "Australian Dollar", "decimals": 2, "stripe": True},
    "CAD": {"symbol": "C$", "name": "Canadian Dollar", "decimals": 2, "stripe": True},
    "CHF": {"symbol": "CHF", "name": "Swiss Franc", "decimals": 2, "stripe": True},
    "HKD": {"symbol": "HK$", "name": "Hong Kong Dollar", "decimals": 2, "stripe": True},
    "SGD": {"symbol": "S$", "name": "Singapore Dollar", "decimals": 2, "stripe": True},
    "SEK": {"symbol": "kr", "name": "Swedish Krona", "decimals": 2, "stripe": True},
    "NZD": {"symbol": "NZ$", "name": "New Zealand Dollar", "decimals": 2, "stripe": True},
    "MXN": {"symbol": "MX$", "name": "Mexican Peso", "decimals": 2, "stripe": True},
    "INR": {"symbol": "\u20b9", "name": "Indian Rupee", "decimals": 2, "stripe": True},
    "BRL": {"symbol": "R$", "name": "Brazilian Real", "decimals": 2, "stripe": True},
    "ZAR": {"symbol": "R", "name": "South African Rand", "decimals": 2, "stripe": True},
    "KRW": {"symbol": "\u20a9", "name": "South Korean Won", "decimals": 0, "stripe": True},
    "TWD": {"symbol": "NT$", "name": "Taiwan Dollar", "decimals": 2, "stripe": True},
    "THB": {"symbol": "\u0e3f", "name": "Thai Baht", "decimals": 2, "stripe": True},
    "MYR": {"symbol": "RM", "name": "Malaysian Ringgit", "decimals": 2, "stripe": True},
}

DEFAULT_CURRENCY = "USD"


def is_supported(code) -> bool:
    """True if code is a known ISO currency in our metadata."""
    return str(code or "").upper().strip() in CURRENCIES


def normalize(code) -> str:
    """Uppercase and validate a currency code. Raises ValueError if unknown."""
    c = str(code or DEFAULT_CURRENCY).upper().strip()
    if c not in CURRENCIES:
        raise ValueError(f"Unsupported currency: {code!r}")
    return c


def decimals_for(code) -> int:
    """Minor-unit decimal places for a currency (0 for zero-decimal)."""
    return CURRENCIES[normalize(code)]["decimals"]


def is_zero_decimal(code) -> bool:
    """True for currencies with no minor unit (JPY, KRW, ...)."""
    return decimals_for(code) == 0


def symbol_for(code) -> str:
    return CURRENCIES[normalize(code)]["symbol"]


def stripe_supported(code) -> bool:
    """True where Stripe supports presentment (charging) in this currency."""
    return bool(CURRENCIES[normalize(code)].get("stripe"))


def get_base_currency(settings) -> str:
    """Workspace base currency. Prefers the canonical `base_currency` key,
    falls back to the legacy `currency` key, then USD. Never raises."""
    s = settings or {}
    for key in ("base_currency", "currency"):
        raw = s.get(key)
        if raw and is_supported(raw):
            return str(raw).upper().strip()
    return DEFAULT_CURRENCY


def to_minor_units(amount, currency=DEFAULT_CURRENCY) -> int:
    """Convert a major-unit amount to minor units for Stripe/API use.

    Handles zero-decimal currencies: to_minor_units(500, "JPY") == 500,
    while to_minor_units(5.00, "USD") == 500.
    """
    factor = 10 ** decimals_for(currency)
    return int(round(float(amount) * factor))


def from_minor_units(minor, currency=DEFAULT_CURRENCY) -> float:
    """Convert minor units back to a major-unit amount."""
    factor = 10 ** decimals_for(currency)
    return int(minor) / factor
