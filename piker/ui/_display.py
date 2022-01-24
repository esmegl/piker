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

'''
real-time display tasks for charting graphics update.

this module ties together quote and computational (fsp) streams with
graphics update methods via our custom ``pyqtgraph`` charting api.

'''
from contextlib import asynccontextmanager
from functools import partial
import time
from types import ModuleType
from typing import Optional

import numpy as np
from pydantic import create_model
import tractor
import trio

from .. import brokers
from ..data.feed import (
    open_feed,
    # Feed,
)
from ._chart import (
    ChartPlotWidget,
    LinkedSplits,
    GodWidget,
)
from .. import fsp
from ._l1 import L1Labels
from ..data._sharedmem import ShmArray, maybe_open_shm_array
from ._forms import (
    FieldsForm,
    mk_form,
    mk_order_pane_layout,
    open_form_input_handling,
)
from .order_mode import open_order_mode
from ..log import get_logger

log = get_logger(__name__)

# TODO: load this from a config.toml!
_quote_throttle_rate: int = 58  # Hz


def update_fsp_chart(
    chart: ChartPlotWidget,
    shm: ShmArray,
    graphics_name: str,
    array_key: Optional[str],

) -> None:

    array = shm.array

    # update graphics
    # NOTE: this does a length check internally which allows it
    # staying above the last row check below..
    chart.update_curve_from_array(
        graphics_name,
        array,
        array_key=array_key or graphics_name,
    )

    try:
        last_row = array[-1]
    except IndexError:
        # XXX: race condition with backfilling shm.
        #
        # the underlying issue is that a backfill (aka prepend) and subsequent
        # shm array first/last index update could result in an empty array
        # read here since the indices may be updated in such a way that
        # a read delivers an empty array (though it seems like we
        # *should* be able to prevent that?). also, as and alt and
        # something we need anyway, maybe there should be some kind of
        # signal that a prepend is taking place and this consumer can
        # respond (eg. redrawing graphics) accordingly.
        log.warning(f'Read-race on shm array: {graphics_name}@{shm.token}')
        return

    # TODO: provide a read sync mechanism to avoid this polling. the
    # underlying issue is that a backfill (aka prepend) and subsequent
    # shm array first/last index update could result in an empty array
    # read here since the stream is never torn down on the re-compute
    # steps.
    # read_tries = 2
    # while read_tries > 0:
    #     try:
    #         # read last
    #         array = shm.array
    #         value = array[-1][array_key]
    #         break

    #     except IndexError:
    #         read_tries -= 1
    #         continue

    # XXX: re: ``array_key``: fsp func names must be unique meaning we
    # can't have duplicates of the underlying data even if multiple
    # sub-charts reference it under different 'named charts'.

    # read from last calculated value and update any label
    last_val_sticky = chart._ysticks.get(graphics_name)
    if last_val_sticky:
        # array = shm.array[array_key]
        # if len(array):
        #     value = array[-1]
        last = last_row[array_key]
        last_val_sticky.update_from_data(-1, last)


# _clses = {
#     'clears': {'trade', 'utrade', 'last'},
#     'last': {'last'},
#     'bids': {'bid', 'bsize'},
#     'asks': {'ask', 'asize'},
# }

# XXX: idea for frame type data structure we could use on the
# wire instead of doing it here?
# frames = {
#     'index': ['type_a', 'type_c', 'type_n', 'type_n', 'type_c'],

#     'type_a': [tick0, tick1, tick2, .., tickn],
#     'type_b': [tick0, tick1, tick2, .., tickn],
#     'type_c': [tick0, tick1, tick2, .., tickn],
#     ...
#     'type_n': [tick0, tick1, tick2, .., tickn],
# }


def chart_maxmin(
    chart: ChartPlotWidget,
    vlm_chart: Optional[ChartPlotWidget] = None,

) -> tuple[
    tuple[int, int, int, int],
    float,
    float,
    float,
]:
    # TODO: implement this
    # https://arxiv.org/abs/cs/0610046
    # https://github.com/lemire/pythonmaxmin

    array = chart._arrays['ohlc']
    ifirst = array[0]['index']

    last_bars_range = chart.bars_range()
    l, lbar, rbar, r = last_bars_range
    in_view = array[lbar - ifirst:rbar - ifirst + 1]

    assert in_view.size

    mx, mn = np.nanmax(in_view['high']), np.nanmin(in_view['low'])

    # TODO: when we start using line charts, probably want to make
    # this an overloaded call on our `DataView
    # sym = chart.name
    # mx, mn = np.nanmax(in_view[sym]), np.nanmin(in_view[sym])

    mx_vlm_in_view = 0
    if vlm_chart:
        mx_vlm_in_view = np.max(in_view['volume'])

    return last_bars_range, mx, max(mn, 0), mx_vlm_in_view


async def update_chart_from_quotes(

    linked: LinkedSplits,
    stream: tractor.MsgStream,
    ohlcv: np.ndarray,

    wap_in_history: bool = False,
    vlm_chart: Optional[ChartPlotWidget] = None,

) -> None:
    '''The 'main' (price) chart real-time update loop.

    Receive from the quote stream and update the OHLC chart.

    '''
    # TODO: bunch of stuff:
    # - I'm starting to think all this logic should be
    #   done in one place and "graphics update routines"
    #   should not be doing any length checking and array diffing.
    # - handle odd lot orders
    # - update last open price correctly instead
    #   of copying it from last bar's close
    # - 1-5 sec bar lookback-autocorrection like tws does?
    #   (would require a background history checker task)

    chart = linked.chart

    # update last price sticky
    last_price_sticky = chart._ysticks[chart.name]
    last_price_sticky.update_from_data(
        *ohlcv.array[-1][['index', 'close']]
    )

    if vlm_chart:
        vlm_sticky = vlm_chart._ysticks['volume']

    maxmin = partial(chart_maxmin, chart, vlm_chart)

    chart.default_view()

    (
        last_bars_range,
        last_mx,
        last_mn,
        last_mx_vlm,
    ) = maxmin()

    last, volume = ohlcv.array[-1][['close', 'volume']]

    symbol = chart.linked.symbol

    l1 = L1Labels(
        chart,
        # determine precision/decimal lengths
        digits=symbol.tick_size_digits,
        size_digits=symbol.lot_size_digits,
    )
    chart._l1_labels = l1

    # TODO:
    # - in theory we should be able to read buffer data faster
    #   then msgs arrive.. needs some tinkering and testing

    # - if trade volume jumps above / below prior L1 price
    #   levels this might be dark volume we need to
    #   present differently -> likely dark vlm

    tick_size = chart.linked.symbol.tick_size
    tick_margin = 3 * tick_size

    chart.show()
    last_quote = time.time()

    # NOTE: all code below this loop is expected to be synchronous
    # and thus draw instructions are not picked up jntil the next
    # wait / iteration.
    async for quotes in stream:

        now = time.time()
        quote_period = now - last_quote
        if quote_period <= 1/_quote_throttle_rate:
            log.warning(f'TOO FAST: {1/quote_period}')
        last_quote = now

        # chart isn't active/shown so skip render cycle and pause feed(s)
        if chart.linked.isHidden():
            await chart.pause_all_feeds()
            continue

        for sym, quote in quotes.items():

            (
                brange,
                mx_in_view,
                mn_in_view,
                mx_vlm_in_view,
            ) = maxmin()
            l, lbar, rbar, r = brange
            mx = mx_in_view + tick_margin
            mn = mn_in_view - tick_margin

            # NOTE: vlm may be written by the ``brokerd`` backend
            # event though a tick sample is not emitted.
            # TODO: show dark trades differently
            # https://github.com/pikers/piker/issues/116
            array = ohlcv.array

            if vlm_chart:
                # print(f"volume: {end['volume']}")
                vlm_chart.update_curve_from_array('volume', array)

                # built-in tina $vlm FSP using chl3 typical price for ohlc step
                # last = array[-1]
                # chl3 = (last['close'] + last['high'] + last['low']) / 3
                # v = last['volume']
                # dv = last['volume'] * chl3

                vlm_sticky.update_from_data(*array[-1][['index', 'volume']])

                if (
                    mx_vlm_in_view != last_mx_vlm or
                    mx_vlm_in_view > last_mx_vlm
                ):
                    # print(f'mx vlm: {last_mx_vlm} -> {mx_vlm_in_view}')
                    vlm_chart._set_yrange(yrange=(0, mx_vlm_in_view * 1.375))
                    last_mx_vlm = mx_vlm_in_view

            ticks_frame = quote.get('ticks', ())

            frames_by_type: dict[str, dict] = {}
            lasts = {}

            # build tick-type "frames" of tick sequences since
            # likely the tick arrival rate is higher then our
            # (throttled) quote stream rate.
            for tick in ticks_frame:
                price = tick.get('price')
                ticktype = tick.get('type')

                if ticktype == 'n/a' or price == -1:
                    # okkk..
                    continue

                # keys are entered in olded-event-inserted-first order
                # since we iterate ``ticks_frame`` in standard order
                # above. in other words the order of the keys is the order
                # of tick events by type from the provider feed.
                frames_by_type.setdefault(ticktype, []).append(tick)

                # overwrites so the last tick per type is the entry
                lasts[ticktype] = tick

            # from pprint import pformat
            # frame_counts = {
            #     typ: len(frame) for typ, frame in frames_by_type.items()
            # }
            # print(f'{pformat(frame_counts)}')
            # print(f'framed: {pformat(frames_by_type)}')
            # print(f'lasts: {pformat(lasts)}')

            # TODO: eventually we want to separate out the utrade (aka
            # dark vlm prices) here and show them as an additional
            # graphic.
            clear_types = {'trade', 'utrade', 'last'}

            # XXX: if we wanted to iterate in "latest" (i.e. most
            # current) tick first order as an optimization where we only
            # update from the last tick from each type class.
            # last_clear_updated: bool = False
            # for typ, tick in reversed(lasts.items()):

            # iterate in FIFO order per frame
            for typ, tick in lasts.items():

                price = tick.get('price')
                size = tick.get('size')

                # compute max and min prices (including bid/ask) from
                # tick frames to determine the y-range for chart
                # auto-scaling.
                # TODO: we need a streaming minmax algo here, see def above.
                mx = max(price + tick_margin, mx)
                mn = min(price - tick_margin, mn)

                if typ in clear_types:

                    # XXX: if we only wanted to update graphics from the
                    # "current"/"latest received" clearing price tick
                    # once (see alt iteration order above).
                    # if last_clear_updated:
                    #     continue

                    # last_clear_updated = True
                    # we only want to update grahpics from the *last*
                    # tick event that falls under the "clearing price"
                    # set.

                    # update price sticky(s)
                    end = array[-1]
                    last_price_sticky.update_from_data(
                        *end[['index', 'close']]
                    )

                    # update ohlc sampled price bars
                    chart.update_ohlc_from_array(
                        chart.name,
                        array,
                    )

                    if wap_in_history:
                        # update vwap overlay line
                        chart.update_curve_from_array('bar_wap', ohlcv.array)

                # L1 book label-line updates
                # XXX: is this correct for ib?
                # if ticktype in ('trade', 'last'):
                # if ticktype in ('last',):  # 'size'):
                if typ in ('last',):  # 'size'):

                    label = {
                        l1.ask_label.fields['level']: l1.ask_label,
                        l1.bid_label.fields['level']: l1.bid_label,
                    }.get(price)

                    if label is not None:
                        label.update_fields({'level': price, 'size': size})

                        # TODO: on trades should we be knocking down
                        # the relevant L1 queue?
                        # label.size -= size

                # elif ticktype in ('ask', 'asize'):
                elif typ in ('ask', 'asize'):
                    l1.ask_label.update_fields({'level': price, 'size': size})

                # elif ticktype in ('bid', 'bsize'):
                elif typ in ('bid', 'bsize'):
                    l1.bid_label.update_fields({'level': price, 'size': size})

            # check for y-range re-size
            if (mx > last_mx) or (mn < last_mn):
                # print(f'new y range: {(mn, mx)}')
                chart._set_yrange(
                    yrange=(mn, mx),
                    # TODO: we should probably scale
                    # the view margin based on the size
                    # of the true range? This way you can
                    # slap in orders outside the current
                    # L1 (only) book range.
                    # range_margin=0.1,
                )

            last_mx, last_mn = mx, mn

            # run synchronous update on all derived fsp subplots
            for name, subchart in linked.subplots.items():
                update_fsp_chart(
                    subchart,
                    subchart._shm,

                    # XXX: do we really needs seperate names here?
                    name,
                    array_key=name,
                )

                # TODO: all overlays on all subplots..

            # run synchronous update on all derived overlays
            for curve_name, shm in chart._overlays.items():
                update_fsp_chart(
                    chart,
                    shm,
                    curve_name,
                    array_key=curve_name,
                )


def maybe_mk_fsp_shm(
    sym: str,
    field_name: str,
    display_name: Optional[str] = None,
    readonly: bool = True,

) -> (ShmArray, bool):
    '''Allocate a single row shm array for an symbol-fsp pair if none
    exists, otherwise load the shm already existing for that token.

    '''
    uid = tractor.current_actor().uid
    if not display_name:
        display_name = field_name

    # TODO: load function here and introspect
    # return stream type(s)

    # TODO: should `index` be a required internal field?
    fsp_dtype = np.dtype([('index', int), (field_name, float)])

    key = f'{sym}.fsp.{display_name}.{".".join(uid)}'

    shm, opened = maybe_open_shm_array(
        key,
        # TODO: create entry for each time frame
        dtype=fsp_dtype,
        readonly=True,
    )
    return shm, opened


@asynccontextmanager
async def open_fsp_sidepane(

    linked: LinkedSplits,
    conf: dict[str, dict[str, str]],

) -> FieldsForm:

    schema = {}

    assert len(conf) == 1  # for now

    # add (single) selection widget
    for display_name, config in conf.items():
        schema[display_name] = {
                'label': '**fsp**:',
                'type': 'select',
                'default_value': [display_name],
            }

        # add parameters for selection "options"
        params = config.get('params', {})
        for name, config in params.items():

            default = config['default_value']
            kwargs = config.get('widget_kwargs', {})

            # add to ORM schema
            schema.update({
                name: {
                    'label': f'**{name}**:',
                    'type': 'edit',
                    'default_value': default,
                    'kwargs': kwargs,
                },
            })

    sidepane: FieldsForm = mk_form(
        parent=linked.godwidget,
        fields_schema=schema,
    )

    # https://pydantic-docs.helpmanual.io/usage/models/#dynamic-model-creation
    FspConfig = create_model(
        'FspConfig',
        name=display_name,
        **params,
    )
    sidepane.model = FspConfig()

    # just a logger for now until we get fsp configs up and running.
    async def settings_change(key: str, value: str) -> bool:
        print(f'{key}: {value}')
        return True

    # TODO:
    async with (
        open_form_input_handling(
            sidepane,
            focus_next=linked.godwidget,
            on_value_change=settings_change,
        )
    ):
        yield sidepane


async def open_fspd_cluster(

    linkedsplits: LinkedSplits,
    fsps: dict[str, str],
    sym: str,
    src_shm: list,
    brokermod: ModuleType,
    group_status_key: str,
    loglevel: str,

    # this con
    display_in_own_task: bool = False,

) -> None:
    '''Create sub-actors (under flat tree)
    for each entry in config and attach to local graphics update tasks.

    Pass target entrypoint and historical data.

    '''
    linkedsplits.focus()

    # spawns sub-processes which execute cpu bound fsp work
    # which is streamed back to this parent.
    async with (
        tractor.open_nursery() as n,
        trio.open_nursery() as ln,
    ):
        # Currently we spawn an actor per fsp chain but
        # likely we'll want to pool them eventually to
        # scale horizonatlly once cores are used up.
        for display_name, conf in fsps.items():

            func_name = conf['func_name']

            shm, opened = maybe_mk_fsp_shm(
                sym,
                field_name=func_name,
                display_name=display_name,
                readonly=True,
            )

            # XXX: fsp may have been opened by a duplicate chart.
            # Error for now until we figure out how to wrap fsps as
            # "feeds".  assert opened, f"A chart for {key} likely
            # already exists?"

            portal = await n.start_actor(
                enable_modules=['piker.fsp._engine'],
                name='fsp.' + display_name,
            )

            # init async
            ln.start_soon(
                partial(
                    update_chart_from_fsp,

                    portal,
                    linkedsplits,
                    brokermod,
                    sym,
                    src_shm,
                    func_name,
                    display_name,
                    conf=conf,
                    shm=shm,
                    is_overlay=conf.get('overlay', False),
                    group_status_key=group_status_key,
                    loglevel=loglevel,
                )
            )

    # blocks here until all fsp actors complete


async def update_chart_from_fsp(

    portal: tractor.Portal,

    linkedsplits: LinkedSplits,
    brokermod: ModuleType,
    sym: str,
    src_shm: ShmArray,
    func_name: str,
    display_name: str,
    conf: dict[str, dict],

    shm: ShmArray,
    is_overlay: bool,

    group_status_key: str,
    loglevel: str,

) -> None:
    '''FSP stream chart update loop.

    This is called once for each entry in the fsp
    config map.

    '''
    done = linkedsplits.window().status_bar.open_status(
        f'loading fsp, {display_name}..',
        group_key=group_status_key,
    )

    async with (
        portal.open_context(

            # chaining entrypoint
            fsp.cascade,

            # name as title of sub-chart
            brokername=brokermod.name,
            src_shm_token=src_shm.token,
            dst_shm_token=shm.token,
            symbol=sym,
            func_name=func_name,
            loglevel=loglevel,

        ) as (ctx, last_index),
        ctx.open_stream() as stream,

        open_fsp_sidepane(linkedsplits, {display_name: conf},) as sidepane,
    ):
        if is_overlay:
            chart = linkedsplits.chart
            chart.draw_curve(
                name=display_name,
                data=shm.array,
                overlay=True,
                color='default_light',
            )
            # specially store ref to shm for lookup in display loop
            chart._overlays[display_name] = shm

        else:
            chart = linkedsplits.add_plot(
                name=display_name,
                array=shm.array,

                array_key=conf['func_name'],
                sidepane=sidepane,

                # curve by default
                ohlc=False,

                # settings passed down to ``ChartPlotWidget``
                **conf.get('chart_kwargs', {})
                # static_yrange=(0, 100),
            )

            # XXX: ONLY for sub-chart fsps, overlays have their
            # data looked up from the chart's internal array set.
            # TODO: we must get a data view api going STAT!!
            chart._shm = shm

            # should **not** be the same sub-chart widget
            assert chart.name != linkedsplits.chart.name

        array_key = func_name

        # first UI update, usually from shm pushed history
        update_fsp_chart(
            chart,
            shm,
            display_name,
            array_key=array_key,
        )

        chart.linked.focus()

        # TODO: figure out if we can roll our own `FillToThreshold` to
        # get brush filled polygons for OS/OB conditions.
        # ``pg.FillBetweenItems`` seems to be one technique using
        # generic fills between curve types while ``PlotCurveItem`` has
        # logic inside ``.paint()`` for ``self.opts['fillLevel']`` which
        # might be the best solution?
        # graphics = chart.update_from_array(chart.name, array[func_name])
        # graphics.curve.setBrush(50, 50, 200, 100)
        # graphics.curve.setFillLevel(50)

        if func_name == 'rsi':
            from ._lines import level_line
            # add moveable over-[sold/bought] lines
            # and labels only for the 70/30 lines
            level_line(chart, 20)
            level_line(chart, 30, orient_v='top')
            level_line(chart, 70, orient_v='bottom')
            level_line(chart, 80, orient_v='top')

        chart._set_yrange()

        done()
        chart.linked.resize_sidepanes()

        # update chart graphics
        i = 0
        last = time.time()
        async for value in stream:

            # chart isn't actively shown so just skip render cycle
            if chart.linked.isHidden():
                print(f'{i} unseen fsp cyclce')
                i += 1
                continue

            else:
                now = time.time()
                period = now - last

                if period <= 1/_quote_throttle_rate:
                    # faster then display refresh rate
                    print(f'fsp too fast: {1/period}')
                    continue

                # run synchronous update
                update_fsp_chart(
                    chart,
                    shm,
                    display_name,
                    array_key=func_name,
                )

                # set time of last graphics update
                last = time.time()


async def check_for_new_bars(feed, ohlcv, linkedsplits):
    """Task which updates from new bars in the shared ohlcv buffer every
    ``delay_s`` seconds.

    """
    # TODO: right now we'll spin printing bars if the last time
    # stamp is before a large period of no market activity.
    # Likely the best way to solve this is to make this task
    # aware of the instrument's tradable hours?

    price_chart = linkedsplits.chart
    price_chart.default_view()

    async with feed.index_stream() as stream:
        async for index in stream:
            # update chart historical bars graphics by incrementing
            # a time step and drawing the history and new bar

            # When appending a new bar, in the time between the insert
            # from the writing process and the Qt render call, here,
            # the index of the shm buffer may be incremented and the
            # (render) call here might read the new flat bar appended
            # to the buffer (since -1 index read). In that case H==L and the
            # body will be set as None (not drawn) on what this render call
            # *thinks* is the curent bar (even though it's reading data from
            # the newly inserted flat bar.
            #
            # HACK: We need to therefore write only the history (not the
            # current bar) and then either write the current bar manually
            # or place a cursor for visual cue of the current time step.

            # XXX: this puts a flat bar on the current time step
            # TODO: if we eventually have an x-axis time-step "cursor"
            # we can get rid of this since it is extra overhead.
            price_chart.update_ohlc_from_array(
                price_chart.name,
                ohlcv.array,
                just_history=False,
            )

            # main chart overlays
            for name in price_chart._overlays:

                price_chart.update_curve_from_array(
                    name,
                    price_chart._arrays[name]
                )

            # each subplot
            for name, chart in linkedsplits.subplots.items():
                chart.update_curve_from_array(
                    chart.name,
                    chart._shm.array,
                    array_key=chart.data_key
                )

            # shift the view if in follow mode
            price_chart.increment_view()


def has_vlm(ohlcv: ShmArray) -> bool:
    # make sure that the instrument supports volume history
    # (sometimes this is not the case for some commodities and
    # derivatives)
    volm = ohlcv.array['volume']
    return not bool(np.all(np.isin(volm, -1)) or np.all(np.isnan(volm)))


@asynccontextmanager
async def maybe_open_vlm_display(

    linked: LinkedSplits,
    ohlcv: ShmArray,

) -> ChartPlotWidget:

    if not has_vlm(ohlcv):
        log.warning(f"{linked.symbol.key} does not seem to have volume info")
        yield
        return
    else:

        shm, opened = maybe_mk_fsp_shm(
            linked.symbol.key,
            '$_vlm',
            readonly=True,
        )

        async with open_fsp_sidepane(
            linked, {
                'volume': {

                    'params': {

                        'price_func': {
                            'default_value': 'ohl3',
                            # tell target ``Edit`` widget to not allow
                            # edits for now.
                            'widget_kwargs': {'readonly': True},
                        },
                    },
                }
            },
        ) as sidepane:

            # built-in $vlm
            shm = ohlcv
            chart = linked.add_plot(
                name='volume',
                array=shm.array,

                array_key='volume',
                sidepane=sidepane,

                # curve by default
                ohlc=False,

                # Draw vertical bars from zero.
                # we do this internally ourselves since
                # the curve item internals are pretty convoluted.
                style='step',

                # original pyqtgraph flag for reference
                # stepMode=True,
            )

            # XXX: ONLY for sub-chart fsps, overlays have their
            # data looked up from the chart's internal array set.
            # TODO: we must get a data view api going STAT!!
            chart._shm = shm

            # should **not** be the same sub-chart widget
            assert chart.name != linked.chart.name

            # sticky only on sub-charts atm
            last_val_sticky = chart._ysticks[chart.name]

            # read from last calculated value
            value = shm.array['volume'][-1]

            last_val_sticky.update_from_data(-1, value)

            chart.update_curve_from_array(
                'volume',
                shm.array,
            )

            # size view to data once at outset
            chart._set_yrange()

            # size pain to parent chart
            # TODO: this appears to nearly fix a bug where the vlm sidepane
            # could be sized correctly nearly immediately (since the
            # order pane is already sized), right now it doesn't seem to
            # fully align until the VWAP fsp-actor comes up...
            await trio.sleep(0)
            chart.linked.resize_sidepanes()
            await trio.sleep(0)

            yield chart


async def display_symbol_data(

    godwidget: GodWidget,
    provider: str,
    sym: str,
    loglevel: str,

    order_mode_started: trio.Event,

) -> None:
    '''Spawn a real-time updated chart for ``symbol``.

    Spawned ``LinkedSplits`` chart widgets can remain up but hidden so
    that multiple symbols can be viewed and switched between extremely
    fast from a cached watch-list.

    '''
    sbar = godwidget.window.status_bar
    loading_sym_key = sbar.open_status(
        f'loading {sym}.{provider} ->',
        group_key=True
    )

    # historical data fetch
    brokermod = brokers.get_brokermod(provider)

    # ohlc_status_done = sbar.open_status(
    #     'retreiving OHLC history.. ',
    #     clear_on_next=True,
    #     group_key=loading_sym_key,
    # )

    async with(
        open_feed(
            provider,
            [sym],
            loglevel=loglevel,

            # 60 FPS to limit context switches
            tick_throttle=_quote_throttle_rate,

        ) as feed,
    ):

        ohlcv: ShmArray = feed.shm
        bars = ohlcv.array
        symbol = feed.symbols[sym]

        # load in symbol's ohlc data
        godwidget.window.setWindowTitle(
            f'{symbol.key}@{symbol.brokers} '
            f'tick:{symbol.tick_size}'
        )

        linkedsplits = godwidget.linkedsplits
        linkedsplits._symbol = symbol

        # generate order mode side-pane UI
        # A ``FieldsForm`` form to configure order entry
        pp_pane: FieldsForm = mk_order_pane_layout(godwidget)

        # add as next-to-y-axis singleton pane
        godwidget.pp_pane = pp_pane

        # create main OHLC chart
        chart = linkedsplits.plot_ohlc_main(
            symbol,
            bars,
            sidepane=pp_pane,
        )
        chart._feeds[symbol.key] = feed
        chart.setFocus()

        # plot historical vwap if available
        wap_in_history = False

        if brokermod._show_wap_in_history:

            if 'bar_wap' in bars.dtype.fields:
                wap_in_history = True
                chart.draw_curve(
                    name='bar_wap',
                    data=bars,
                    add_label=False,
                )

        # size view to data once at outset
        chart._set_yrange()

        # TODO: a data view api that makes this less shit
        chart._shm = ohlcv

        # TODO: eventually we'll support some kind of n-compose syntax
        fsp_conf = {

            'rsi': {

                # literal python func ref lookup name
                'func_name': 'rsi',

                # map of parameters to place on the fsp sidepane widget
                # which should map to dynamic inputs available to the
                # fsp function at runtime.
                'params': {
                    'period': {
                        'default_value': 14,
                        'widget_kwargs': {'readonly': True},
                    },
                },

                # ``ChartPlotWidget`` options passthrough
                'chart_kwargs': {
                    'static_yrange': (0, 100),
                },
            },

        }

        if has_vlm(ohlcv):  # and provider != 'binance':
            # binance is too fast atm for FSPs until we wrap
            # the fsp streams as throttled ``Feeds``, see
            #

            # add VWAP to fsp config for downstream loading
            fsp_conf.update({
                'vwap': {
                    'func_name': 'vwap',
                    'overlay': True,
                    'anchor': 'session',
                },
            })

        # NOTE: we must immediately tell Qt to show the OHLC chart
        # to avoid a race where the subplots get added/shown to
        # the linked set *before* the main price chart!
        linkedsplits.show()
        linkedsplits.focus()
        await trio.sleep(0)

        async with (
            trio.open_nursery() as ln,
            maybe_open_vlm_display(linkedsplits, ohlcv) as vlm_chart,
        ):
            # load initial fsp chain (otherwise known as "indicators")
            ln.start_soon(
                open_fspd_cluster,
                linkedsplits,
                fsp_conf,
                sym,
                ohlcv,
                brokermod,
                loading_sym_key,
                loglevel,
            )

            # start graphics update loop(s)after receiving first live quote
            ln.start_soon(
                update_chart_from_quotes,
                linkedsplits,
                feed.stream,
                ohlcv,
                wap_in_history,
                vlm_chart,
            )

            ln.start_soon(
                check_for_new_bars,
                feed,
                ohlcv,
                linkedsplits
            )

            async with (
                open_order_mode(
                    feed,
                    chart,
                    symbol,
                    provider,
                    order_mode_started
                )
            ):
                await trio.sleep_forever()