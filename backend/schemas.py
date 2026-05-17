"""Pydantic schemas — the contract between agents, engine, and the frontend."""
from __future__ import annotations
from typing import Optional, Literal, Dict, List, Any
from enum import Enum
from pydantic import BaseModel, Field


class ActionType(str, Enum):
    BUY = "BUY"
    BID = "BID"
    ACCEPT_OFFER = "ACCEPT_OFFER"
    COUNTER_OFFER = "COUNTER_OFFER"
    REJECT_OFFER = "REJECT_OFFER"
    COMMUNICATE = "COMMUNICATE"
    PROBE = "PROBE"
    SHARE_INFO = "SHARE_INFO"
    COORDINATE = "COORDINATE"
    LIE = "LIE"
    BROADCAST = "BROADCAST"
    SET_PRICE = "SET_PRICE"
    UNDERCUT = "UNDERCUT"
    BUILD_TOOL = "BUILD_TOOL"
    FORM_CONNECTION = "FORM_CONNECTION"
    WAIT = "WAIT"
    EXIT = "EXIT"


class Action(BaseModel):
    """One turn output by an agent. The engine executes the subset it supports."""
    action: ActionType
    target: Optional[str] = None      # seller_id or agent_id, depending on action
    content: Optional[str] = None     # message, offer terms, or price/update content
    reasoning: Optional[str] = None   # one-sentence why (helps debugging)


class Persona(BaseModel):
    profile: str                       # budget | family | investor | flexible
    description: str
    traits: Dict[str, float] = Field(default_factory=dict)
    # traits keys: patience, risk_aversion, social, honesty (each 0-1)


class Tool(BaseModel):
    name: str
    description: str


class AgentState(BaseModel):
    """Single-agent JSON schema. Serialised to agents/{id}.json each tick if you want
    folder-mode message passing — also the exact shape passed into the claude -p prompt."""
    id: str
    type: Literal["buyer", "seller"] = "buyer"
    archetype: Optional[str] = None
    persona: Persona
    budget: int
    goal: Optional[Dict[str, Any]] = None
    constraints: Dict[str, Any] = Field(default_factory=dict)
    actions: List[str] = Field(default_factory=list)
    bought: bool = False
    exited: bool = False
    purchase_price: Optional[int] = None
    purchase_seller: Optional[str] = None
    satisfaction: Optional[int] = None
    ticks_waited: int = 0
    beliefs: Dict[str, Any] = Field(default_factory=dict)
    tools: List[Tool] = Field(default_factory=list)
    inbox: List[Dict[str, Any]] = Field(default_factory=list)
    outbox: List[Dict[str, Any]] = Field(default_factory=list)


class SellerState(BaseModel):
    id: str
    name: str
    inventory: int
    initial_inventory: int
    base_price: int
    current_price: int


class CommMatrix(BaseModel):
    """Who-can-talk-to-whom. Replaces the more abstract "topology graph" concept
    with an explicit boolean matrix the frontend can render as-is."""
    topology: str
    nodes: List[str]
    matrix: Dict[str, Dict[str, bool]]

    def can_communicate(self, a: str, b: str) -> bool:
        return self.matrix.get(a, {}).get(b, False)


class Message(BaseModel):
    id: str
    turn: int
    sender: str
    recipient: str
    content: str


class Event(BaseModel):
    """Frontend-shaped event matching demo.html ACTIVITIES schema."""
    turn: int
    from_: str = Field(alias="from")
    to: Optional[str] = None
    msg: str
    cls: str        # log-probe | log-trade | log-buy | log-tool | log-lie | log-collude
    toolIdx: Optional[int] = None
    letter: Optional[str] = None
    price: Optional[int] = None
    sat: Optional[int] = None

    model_config = {"populate_by_name": True}


class RunSummary(BaseModel):
    avg_price: float
    by_profile: Dict[str, Optional[float]]
    n_bought: int
    n_missed: int
    avg_satisfaction: float
    total_messages: int


class RunResult(BaseModel):
    topology: str
    seed: int
    ticks: int
    summary: RunSummary
    prices_over_time: List[Dict[str, Any]]
    events: List[Dict[str, Any]]
    agents: Dict[str, Dict[str, Any]]
    comm_matrix: Dict[str, Dict[str, bool]]
