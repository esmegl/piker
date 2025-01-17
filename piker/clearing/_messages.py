# piker: trading gear for hackers
# Copyright (C) Tyler Goodlet (in stewardship for pikers)

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
Clearing sub-system message and protocols.

"""
# from collections import (
#     ChainMap,
#     deque,
# )
from typing import (
    Optional,
    Literal,
)

from ..data._source import Symbol
from ..data.types import Struct


# TODO: a composite for tracking msg flow on 2-legged
# dialogs.
# class Dialog(ChainMap):
#     '''
#     Msg collection abstraction to easily track the state changes of
#     a msg flow in one high level, query-able and immutable construct.

#     The main use case is to query data from a (long-running)
#     msg-transaction-sequence


#     '''
#     def update(
#         self,
#         msg,
#     ) -> None:
#         self.maps.insert(0, msg.to_dict())

#     def flatten(self) -> dict:
#         return dict(self)


# TODO: ``msgspec`` stuff worth paying attention to:
# - schema evolution:
# https://jcristharif.com/msgspec/usage.html#schema-evolution
# - for eg. ``BrokerdStatus``, instead just have separate messages?
# - use literals for a common msg determined by diff keys?
#   - https://jcristharif.com/msgspec/usage.html#literal

# --------------
# Client -> emsd
# --------------

class Order(Struct):

    # TODO: ideally we can combine these 2 fields into
    # 1 and just use the size polarity to determine a buy/sell.
    # i would like to see this become more like
    # https://jcristharif.com/msgspec/usage.html#literal
    # action: Literal[
    #     'live',
    #     'dark',
    #     'alert',
    # ]

    action: Literal[
        'buy',
        'sell',
        'alert',
    ]
    # determines whether the create execution
    # will be submitted to the ems or directly to
    # the backend broker
    exec_mode: Literal[
        'dark',
        'live',
        # 'paper',  no right?
    ]

    # internal ``emdsd`` unique "order id"
    oid: str  # uuid4
    symbol: str | Symbol
    account: str  # should we set a default as '' ?

    price: float
    size: float  # -ve is "sell", +ve is "buy"

    brokers: Optional[list[str]] = []


class Cancel(Struct):
    '''
    Cancel msg for removing a dark (ems triggered) or
    broker-submitted (live) trigger/order.

    '''
    action: str = 'cancel'
    oid: str  # uuid4
    symbol: str


# --------------
# Client <- emsd
# --------------
# update msgs from ems which relay state change info
# from the active clearing engine.

class Status(Struct):

    name: str = 'status'
    time_ns: int
    oid: str  # uuid4 ems-order dialog id

    resp: Literal[
      'pending',  # acked by broker but not yet open
      'open',
      'dark_open',  # dark/algo triggered order is open in ems clearing loop
      'triggered',  # above triggered order sent to brokerd, or an alert closed
      'closed',  # fully cleared all size/units
      'fill',  # partial execution
      'canceled',
      'error',
    ]

    # this maps normally to the ``BrokerdOrder.reqid`` below, an id
    # normally allocated internally by the backend broker routing system
    reqid: Optional[int | str] = None

    # the (last) source order/request msg if provided
    # (eg. the Order/Cancel which causes this msg) and
    # acts as a back-reference to the corresponding
    # request message which was the source of this msg.
    req: Optional[Order | Cancel] = None

    # XXX: better design/name here?
    # flag that can be set to indicate a message for an order
    # event that wasn't originated by piker's emsd (eg. some external
    # trading system which does it's own order control but that you
    # might want to "track" using piker UIs/systems).
    src: Optional[str] = None

    # for relaying a boxed brokerd-dialog-side msg data "through" the
    # ems layer to clients.
    brokerd_msg: dict = {}


# ---------------
# emsd -> brokerd
# ---------------
# requests *sent* from ems to respective backend broker daemon

class BrokerdCancel(Struct):

    action: str = 'cancel'
    oid: str  # piker emsd order id
    time_ns: int

    account: str
    # "broker request id": broker specific/internal order id if this is
    # None, creates a new order otherwise if the id is valid the backend
    # api must modify the existing matching order. If the broker allows
    # for setting a unique order id then this value will be relayed back
    # on the emsd order request stream as the ``BrokerdOrderAck.reqid``
    # field
    reqid: Optional[int | str] = None


class BrokerdOrder(Struct):

    oid: str
    account: str
    time_ns: int

    # TODO: if we instead rely on a +ve/-ve size to determine
    # the action we more or less don't need this field right?
    action: str = ''  # {buy, sell}

    # "broker request id": broker specific/internal order id if this is
    # None, creates a new order otherwise if the id is valid the backend
    # api must modify the existing matching order. If the broker allows
    # for setting a unique order id then this value will be relayed back
    # on the emsd order request stream as the ``BrokerdOrderAck.reqid``
    # field
    reqid: Optional[int | str] = None

    symbol: str  # fqsn
    price: float
    size: float


# ---------------
# emsd <- brokerd
# ---------------
# requests *received* to ems from broker backend

class BrokerdOrderAck(Struct):
    '''
    Immediate reponse to a brokerd order request providing the broker
    specific unique order id so that the EMS can associate this
    (presumably differently formatted broker side ID) with our own
    ``.oid`` (which is a uuid4).

    '''
    name: str = 'ack'

    # defined and provided by backend
    reqid: int | str

    # emsd id originally sent in matching request msg
    oid: str
    account: str = ''


class BrokerdStatus(Struct):

    name: str = 'status'
    reqid: int | str
    time_ns: int
    status: Literal[
        'open',
        'canceled',
        'fill',
        'pending',
        'error',
    ]

    account: str
    filled: float = 0.0
    reason: str = ''
    remaining: float = 0.0

    # external: bool = False

    # XXX: not required schema as of yet
    broker_details: dict = {
        'name': '',
    }


class BrokerdFill(Struct):
    '''
    A single message indicating a "fill-details" event from the broker
    if avaiable.

    '''
    name: str = 'fill'
    reqid: int | str
    time_ns: int

    # order exeuction related
    size: float
    price: float

    action: Optional[str] = None
    broker_details: dict = {}  # meta-data (eg. commisions etc.)

    # brokerd timestamp required for order mode arrow placement on x-axis

    # TODO: maybe int if we force ns?
    # we need to normalize this somehow since backends will use their
    # own format and likely across many disparate epoch clocks...
    broker_time: float


class BrokerdError(Struct):
    '''
    Optional error type that can be relayed to emsd for error handling.

    This is still a TODO thing since we're not sure how to employ it yet.

    '''
    name: str = 'error'
    oid: str

    # if no brokerd order request was actually submitted (eg. we errored
    # at the ``pikerd`` layer) then there will be ``reqid`` allocated.
    reqid: Optional[int | str] = None

    symbol: str
    reason: str
    broker_details: dict = {}


class BrokerdPosition(Struct):
    '''Position update event from brokerd.

    '''
    name: str = 'position'

    broker: str
    account: str
    symbol: str
    size: float
    avg_price: float
    currency: str = ''
