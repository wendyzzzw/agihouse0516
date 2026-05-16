"""Persona templates. 4 profiles, mapped 1:1 to demo.html PROFILES sequence
(6 budget + 4 family + 3 investor + 2 flexible = 15 buyers, A..O)."""

PERSONAS = {
    "budget": {
        "description": "Frugal first-time flyer who hates overpaying. "
                       "Patient. Will wait many ticks for a clearly good deal.",
        "traits": {"patience": 0.85, "risk_aversion": 0.7, "social": 0.5, "honesty": 0.85},
    },
    "family": {
        "description": "Family of four needing tickets for a fixed date. "
                       "Time-pressured. Will pay close to budget to secure seats.",
        "traits": {"patience": 0.25, "risk_aversion": 0.4, "social": 0.2, "honesty": 0.9},
    },
    "investor": {
        "description": "Sophisticated trader hunting arbitrage. "
                       "Calculating, info-hungry, only buys clear deals. May bluff.",
        "traits": {"patience": 0.6, "risk_aversion": 0.5, "social": 0.7, "honesty": 0.6},
    },
    "flexible": {
        "description": "Digital nomad with no fixed schedule. "
                       "Extreme patience, opportunistic, will wait for fire-sale prices.",
        "traits": {"patience": 0.95, "risk_aversion": 0.3, "social": 0.5, "honesty": 0.8},
    },
}

# matches PROFILES array in demo.html (15 entries, agents A..O)
PROFILE_SEQUENCE = (
    ["budget"] * 6
    + ["family"] * 4
    + ["investor"] * 3
    + ["flexible"] * 2
)

DEFAULT_TOOLS = [
    {"name": "ask_price", "description": "Observe a seller's currently posted price."},
    {"name": "send_message", "description": "Send a message to one neighbor (if comm matrix permits)."},
    {"name": "buy", "description": "Purchase a seat from a seller at their current price."},
]


def make_persona(profile: str) -> dict:
    p = PERSONAS[profile]
    return {
        "profile": profile,
        "description": p["description"],
        "traits": dict(p["traits"]),
    }
