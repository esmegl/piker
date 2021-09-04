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
Text entry "forms" widgets (mostly for configuration and UI user input).

'''
from __future__ import annotations
from contextlib import asynccontextmanager
from functools import partial
from textwrap import dedent
from typing import (
    Optional, Any, Callable, Awaitable
)

import trio
from PyQt5 import QtGui
from PyQt5.QtCore import QSize, QModelIndex, Qt, QEvent
from PyQt5.QtWidgets import (
    QWidget,
    QLabel,
    QComboBox,
    QLineEdit,
    QHBoxLayout,
    QVBoxLayout,
    QFormLayout,
    QProgressBar,
    QSizePolicy,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)
# import pydantic

from ._event import open_handlers
from ._style import hcolor, _font, _font_small, DpiAwareFont
from ._label import FormatLabel
from .. import brokers


class FontAndChartAwareLineEdit(QLineEdit):

    def __init__(

        self,
        parent: QWidget,
        # parent_chart: QWidget,  # noqa
        font: DpiAwareFont = _font,
        width_in_chars: int = None,

    ) -> None:

        # self.setContextMenuPolicy(Qt.CustomContextMenu)
        # self.customContextMenuRequested.connect(self.show_menu)
        # self.setStyleSheet(f"font: 18px")

        self.dpi_font = font
        # self.godwidget = parent_chart

        if width_in_chars:
            self._chars = int(width_in_chars)

        else:
            # chart count which will be used to calculate
            # width of input field.
            self._chars: int = 9

        super().__init__(parent)
        # size it as we specify
        # https://doc.qt.io/qt-5/qsizepolicy.html#Policy-enum
        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Fixed,
        )
        self.setFont(font.font)

        # witty bit of margin
        self.setTextMargins(2, 2, 2, 2)

    def sizeHint(self) -> QSize:
        """
        Scale edit box to size of dpi aware font.

        """
        psh = super().sizeHint()

        dpi_font = self.dpi_font
        psh.setHeight(dpi_font.px_size)

        # space for ``._chars: int``
        char_w_pxs = dpi_font.boundingRect(self.text()).width()
        chars_w = char_w_pxs + 6  # * dpi_font.scale() * self._chars
        psh.setWidth(chars_w)

        return psh

    def set_width_in_chars(
        self,
        chars: int,

    ) -> None:
        self._chars = chars
        self.sizeHint()
        self.update()

    def focus(self) -> None:
        self.selectAll()
        self.show()
        self.setFocus()


class FontScaledDelegate(QStyledItemDelegate):
    '''
    Super simple view delegate to render text in the same
    font size as the search widget.

    '''
    def __init__(
        self,

        parent=None,
        font: DpiAwareFont = _font,

    ) -> None:

        super().__init__(parent)
        self.dpi_font = font

    def sizeHint(
        self,

        option: QStyleOptionViewItem,
        index: QModelIndex,

    ) -> QSize:

        # value = index.data()
        # br = self.dpi_font.boundingRect(value)
        # w, h = br.width(), br.height()
        parent = self.parent()

        if getattr(parent, '_max_item_size', None):
            return QSize(*self.parent()._max_item_size)

        else:
            return super().sizeHint(option, index)


# slew of resources which helped get this where it is:
# https://stackoverflow.com/questions/20648210/qcombobox-adjusttocontents-changing-height
# https://stackoverflow.com/questions/3151798/how-do-i-set-the-qcombobox-width-to-fit-the-largest-item
# https://stackoverflow.com/questions/6337589/qlistwidget-adjust-size-to-content#6370892
# https://stackoverflow.com/questions/25304267/qt-resize-of-qlistview
# https://stackoverflow.com/questions/28227406/how-to-set-qlistview-rows-height-permanently

class FieldsForm(QWidget):

    vbox: QVBoxLayout
    form: QFormLayout

    def __init__(
        self,
        parent=None,

    ) -> None:

        super().__init__(parent)

        # size it as we specify
        self.setSizePolicy(
            QSizePolicy.Expanding,
            QSizePolicy.Expanding,
        )

        # XXX: not sure why we have to create this here exactly
        # (instead of in the pane creation routine) but it's
        # here and is managed by downstream layout routines.
        # best guess is that you have to create layouts in order
        # of hierarchy in order for things to display correctly?
        # TODO: we may want to hand this *down* from some "pane manager"
        # thing eventually?
        self.vbox = QVBoxLayout(self)
        # self.vbox.setAlignment(Qt.AlignVCenter)
        self.vbox.setAlignment(Qt.AlignBottom)
        self.vbox.setContentsMargins(0, 4, 3, 6)
        self.vbox.setSpacing(0)

        # split layout for the (<label>: |<widget>|) parameters entry
        self.form = QFormLayout()
        self.form.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(3)
        self.form.setHorizontalSpacing(0)

        self.vbox.addLayout(self.form, stretch=1/3)

        self.labels: dict[str, QLabel] = {}
        self.fields: dict[str, QWidget] = {}

        self._font_size = _font_small.px_size - 2
        self._max_item_width: (float, float) = 0, 0

    def add_field_label(
        self,

        name: str,

        font_size: Optional[int] = None,
        font_color: str = 'default_lightest',

    ) -> QtGui.QLabel:

        # add label to left of search bar
        # self.label = label = QtGui.QLabel()
        font_size = font_size or self._font_size - 1

        self.label = label = FormatLabel(
            fmt_str=name,
            font=_font.font,
            font_size=font_size,
            font_color=font_color,
        )

        # for later lookup
        self.labels[name] = label

        return label

    def add_edit_field(
        self,

        key: str,
        label_name: str,
        value: str,

    ) -> FontAndChartAwareLineEdit:

        # TODO: maybe a distint layout per "field" item?
        label = self.add_field_label(label_name)

        edit = FontAndChartAwareLineEdit(
            parent=self,
        )
        edit.setStyleSheet(
            f"""QLineEdit {{
                color : {hcolor('gunmetal')};
                font-size : {self._font_size}px;
            }}
            """
        )
        edit.setText(str(value))
        self.form.addRow(label, edit)

        self.fields[key] = edit

        return edit

    def add_select_field(
        self,

        key: str,
        label_name: str,
        values: list[str],

    ) -> QComboBox:

        # TODO: maybe a distint layout per "field" item?
        label = self.add_field_label(label_name)

        select = QComboBox(self)
        select._key = key

        for i, value in enumerate(values):
            select.insertItem(i, str(value))

        select.setStyleSheet(
            f"""QComboBox {{
                color : {hcolor('gunmetal')};
                font-size : {self._font_size}px;
            }}
            """
        )
        select.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        select.setIconSize(QSize(0, 0))
        self.setSizePolicy(
            QSizePolicy.Fixed,
            QSizePolicy.Fixed,
        )
        view = select.view()
        view.setUniformItemSizes(True)
        view.setItemDelegate(FontScaledDelegate(view))

        # compute maximum item size so that the weird
        # "style item delegate" thing can then specify
        # that size on each item...
        values.sort()
        br = _font.boundingRect(str(values[-1]))
        w, h = br.width(), br.height()

        # TODO: something better then this monkey patch
        # view._max_item_size = w, h

        # limit to 6 items?
        view.setMaximumHeight(6*h)

        # one entry in view
        select.setMinimumHeight(h)

        select.show()

        self.form.addRow(label, select)

        self.fields[key] = select

        return select


async def handle_field_input(

    widget: QWidget,
    recv_chan: trio.abc.ReceiveChannel,
    form: FieldsForm,
    on_value_change: Callable[[str, Any], Awaitable[bool]],
    focus_next: QWidget,

) -> None:

    async for kbmsg in recv_chan:

        if kbmsg.etype in {QEvent.KeyPress, QEvent.KeyRelease}:
            event, etype, key, mods, txt = kbmsg.to_tuple()
            print(f'key: {kbmsg.key}, mods: {kbmsg.mods}, txt: {kbmsg.txt}')

            # default controls set
            ctl = False
            if kbmsg.mods == Qt.ControlModifier:
                ctl = True

            if ctl and key in {  # cancel and refocus

                Qt.Key_C,
                Qt.Key_Space,  # i feel like this is the "native" one
                Qt.Key_Alt,
            }:

                widget.clearFocus()
                # normally the godwidget
                focus_next.focus()
                continue

            # process field input
            if key in (Qt.Key_Enter, Qt.Key_Return):

                key = widget._key
                value = widget.text()
                on_value_change(key, value)


def mk_form(

    parent: QWidget,
    fields_schema: dict,
    font_size: Optional[int] = None,

) -> FieldsForm:

    # TODO: generate components from model
    # instead of schema dict (aka use an ORM)
    form = FieldsForm(parent=parent)
    form._font_size = font_size or _font_small.px_size

    # generate sub-components from schema dict
    for key, config in fields_schema.items():
        wtype = config['type']
        label = str(config.get('label', key))

        # plain (line) edit field
        if wtype == 'edit':
            w = form.add_edit_field(
                key,
                label,
                config['default_value']
            )

        # drop-down selection
        elif wtype == 'select':
            values = list(config['default_value'])
            w = form.add_select_field(
                key,
                label,
                values
            )

        w._key = key

    return form


@asynccontextmanager
async def open_form_input_handling(

    form: FieldsForm,
    focus_next: QWidget,
    on_value_change: Callable[[str, Any], Awaitable[bool]],

) -> FieldsForm:

    # assert form.model, f'{form} must define a `.model`'

    async with open_handlers(

        list(form.fields.values()),
        event_types={
            QEvent.KeyPress,
        },

        async_handler=partial(
            handle_field_input,
            form=form,
            focus_next=focus_next,
            on_value_change=on_value_change,
        ),

        # block key repeats?
        filter_auto_repeats=True,
    ):
        yield form


class FillStatusBar(QProgressBar):
    '''A status bar for fills up to a position limit.

    '''
    border_px: int = 2
    slot_margin_px: int = 2

    def __init__(
        self,
        approx_height_px: float,
        width_px: float,
        font_size: int,
        parent=None

    ) -> None:
        super().__init__(parent=parent)
        self.approx_h = approx_height_px
        self.font_size = font_size

        self.setFormat('')  # label format
        self.setMinimumWidth(width_px)
        self.setMaximumWidth(width_px)

    def set_slots(
        self,
        slots: int,
        value: float,

    ) -> None:

        approx_h = self.approx_h
        # TODO: compute "used height" thus far and mostly fill the rest
        tot_slot_h, r = divmod(
            approx_h,
            slots,
        )
        clipped = slots * tot_slot_h + 2*self.border_px
        self.setMaximumHeight(clipped)
        slot_height_px = tot_slot_h - 2*self.slot_margin_px

        self.setOrientation(Qt.Vertical)
        self.setStyleSheet(
            f"""
            QProgressBar {{

                text-align: center;

                font-size : {self.font_size - 2}px;

                background-color: {hcolor('papas_special')};
                color : {hcolor('papas_special')};

                border: {self.border_px}px solid {hcolor('default_light')};
                border-radius: 2px;
            }}

            QProgressBar::chunk {{

                background-color: {hcolor('default_spotlight')};
                color: {hcolor('bracket')};

                border-radius: 2px;

                margin: {self.slot_margin_px}px;
                height: {slot_height_px}px;

            }}
            """
        )

        # margin-bottom: {slot_margin_px*2}px;
        # margin-top: {slot_margin_px*2}px;
        # color: #19232D;
        # width: 10px;

        self.setRange(0, slots)
        self.setValue(value)


def mk_fill_status_bar(

    parent_pane: QWidget,
    form: FieldsForm,
    pane_vbox: QVBoxLayout,
    label_font_size: Optional[int] = None,

) -> (
    # TODO: turn this into a composite?
    QHBoxLayout,
    QProgressBar,
    QLabel,
    QLabel,
    QLabel,
):
    # indent = 18
    # bracket_val = 0.375 * 0.666 * w
    # indent = bracket_val / (1 + 5/8)

    chart_h = round(parent_pane.height() * 5/8)
    bar_h = chart_h * 0.375

    # TODO: once things are sized to screen
    bar_label_font_size = label_font_size or _font.px_size - 2

    label_font = DpiAwareFont()
    label_font._set_qfont_px_size(bar_label_font_size)
    br = label_font.boundingRect(f'{3.32:.1f}% port')
    w, h = br.width(), br.height()
    text_w = 8/3 * w
    # text_w = 8/3 * w

    # PnL on lhs
    bar_labels_lhs = QVBoxLayout()
    left_label = form.add_field_label(
        dedent("""
        {pnl:>+.2%} pnl
        """),
        font_size=bar_label_font_size,
        font_color='gunmetal',
    )

    bar_labels_lhs.addSpacing(5/8 * text_w)
    bar_labels_lhs.addWidget(
        left_label,
        alignment=Qt.AlignLeft | Qt.AlignTop,
    )

    # this hbox is added as a layout by the paner maker/caller
    hbox = QHBoxLayout()

    hbox.addLayout(bar_labels_lhs)
    # hbox.addSpacing(indent)  # push to right a bit

    # config
    # hbox.setSpacing(indent * 0.375)
    hbox.setSpacing(0)
    hbox.setAlignment(Qt.AlignTop | Qt.AlignLeft)
    hbox.setContentsMargins(0, 0, 0, 0)

    # TODO: use percentage str formatter:
    # https://docs.python.org/3/library/string.html#grammar-token-precision

    top_label = form.add_field_label(
        # {limit:.1f} limit
        dedent("""
        {limit}
        """),
        font_size=bar_label_font_size,
        font_color='gunmetal',
    )

    bottom_label = form.add_field_label(
        dedent("""
        x: {step_size}\n
        """),
        font_size=bar_label_font_size,
        font_color='gunmetal',
    )

    bar = FillStatusBar(
        approx_height_px=bar_h,
        # approx_height_px=8/3 * w,
        width_px=h * (1 + 1/6),
        font_size=form._font_size,
        parent=form
    )

    hbox.addWidget(bar, alignment=Qt.AlignLeft | Qt.AlignTop)

    bar_labels_rhs_vbox = QVBoxLayout()
    bar_labels_rhs_vbox.addWidget(
        top_label,
        alignment=Qt.AlignLeft | Qt.AlignTop
    )
    bar_labels_rhs_vbox.addWidget(
        bottom_label,
        alignment=Qt.AlignLeft | Qt.AlignBottom
    )

    hbox.addLayout(bar_labels_rhs_vbox)

    # compute "chunk" sizes for fill-status-bar based on some static height
    slots = 4
    bar.set_slots(slots, value=0)

    return hbox, bar, left_label, top_label, bottom_label


def mk_order_pane_layout(

    parent: QWidget,
    # accounts: dict[str, Optional[str]],

) -> FieldsForm:

    # font_size: int = _font_small.px_size - 2
    font_size: int = _font.px_size - 2
    accounts = brokers.config.load_accounts()

    # TODO: maybe just allocate the whole fields form here
    # and expect an async ctx entry?
    form = mk_form(
        parent=parent,
        fields_schema={
            'account': {
                'type': 'select',
                'default_value': accounts.keys(),
            },
            'size_unit': {
                'label': '**allocate**:',
                'type': 'select',
                'default_value': [
                    '$ size',
                    '# units',
                    # '% of port',
                ],
            },
            # 'disti_weight': {
            #     'label': '**weighting**:',
            #     'type': 'select',
            #     'default_value': ['uniform'],
            # },
            'limit': {
                'label': '**limit**:',
                'type': 'edit',
                'default_value': 5000,
            },
            'slots': {
                'label': '**slots**:',
                'type': 'edit',
                'default_value': 4,
            },
        },
    )

    # top level pane layout
    # XXX: see ``FieldsForm.__init__()`` for why we can't do basic
    # config of the vbox here
    vbox = form.vbox

    # _, h = form.width(), form.height()
    # print(f'w, h: {w, h}')

    hbox, fill_bar, left_label, top_label, bottom_label = mk_fill_status_bar(
        parent,
        form,
        pane_vbox=vbox,
        label_font_size=font_size,

    )
    # TODO: would be nice to have some better way of reffing these over
    # monkey patching...
    form.fill_bar = fill_bar
    form.left_label = left_label
    form.bottom_label = bottom_label
    form.top_label = top_label

    # add pp fill bar + spacing
    vbox.addLayout(hbox, stretch=1/3)

    # TODO: status labels for brokerd real-time info
    # feed_label = form.add_field_label(
    #     dedent("""
    #     brokerd.ib\n
    #     |_@{host}:{port}\n
    #     |_consumers: {cons}\n
    #     |_streams: {streams}\n
    #     |_shms: {streams}\n
    #     """),
    #     font_size=_font_small.px_size,
    # )

    # # add feed info label
    # vbox.addWidget(
    #     feed_label,
    #     alignment=Qt.AlignBottom,
    #     # stretch=1/3,
    # )

    # TODO: handle resize events and appropriately scale this
    # to the sidepane height?
    # https://doc.qt.io/qt-5/layout.html#adding-widgets-to-a-layout
    vbox.setSpacing(_font.px_size * 1.375)

    form.show()
    return form
