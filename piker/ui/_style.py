"""
Qt UI styling.
"""
import pyqtgraph as pg
from PyQt5 import QtGui


# chart-wide font
_font = QtGui.QFont("Hack", 4)
_i3_rgba = QtGui.QColor.fromRgbF(*[0.14]*3 + [1])


# splitter widget config
_xaxis_at = 'bottom'


# charting config
_min_points_to_show = 3


_tina_mode = False


def enable_tina_mode() -> None:
    """Enable "tina mode" to make everything look "conventional"
    like your pet hedgehog always wanted.
    """
    # white background (for tinas like our pal xb)
    pg.setConfigOption('background', 'w')
