# piker: trading gear for hackers
# Copyright (C) Tyler Goodlet (in stewardship for piker0)

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
Notifications utils.

"""
import os
import platform
import subprocess
from typing import Optional

import trio

from ..log import get_logger
from ..clearing._messages import (
    Status,
)

log = get_logger(__name__)


_dbus_uid: Optional[str] = ''


async def notify_from_ems_status_msg(
    msg: Status,
    duration: int = 3000,
    is_subproc: bool = False,

) -> None:
    '''
    Send a linux desktop notification.

    Handle subprocesses by discovering the dbus user id
    on first call.

    '''
    if platform.system() != "Linux":
        return

    # TODO: this in another task?
    # not sure if this will ever be a bottleneck,
    # we probably could do graphics stuff first tho?

    if is_subproc:
        global _dbus_uid
        if not _dbus_uid:
            su = os.environ['SUDO_USER']

            # TODO: use `trio` but we need to use nursery.start()
            # to use pipes?
            # result = await trio.run_process(
            result = subprocess.run(
                [
                    'id',
                    '-u',
                    su,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # check=True
            )
            _dbus_uid = result.stdout.decode("utf-8").replace('\n', '')

            os.environ['DBUS_SESSION_BUS_ADDRESS'] = (
                f'unix:path=/run/user/{_dbus_uid}/bus'
            )

    result = await trio.run_process(
        [
            'notify-send',
            '-u', 'normal',
            '-t', f'{duration}',
            'piker',

            # TODO: add in standard fill/exec info that maybe we
            # pack in a broker independent way?
            f"'{msg.pformat()}'",
        ],
    )
    log.runtime(result)
