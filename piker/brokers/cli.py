"""
Console interface to broker client/daemons.
"""
from functools import partial
from importlib import import_module

import click
import trio
import pandas as pd

from ..log import get_console_log, colorize_json


def run(main, loglevel='info'):
    log = get_console_log(loglevel)

    # main sandwich
    try:
        return trio.run(main)
    except Exception as err:
        log.exception(err)
    finally:
        log.debug("Exiting piker")


@click.group()
def cli():
    pass


@cli.command()
@click.option('--broker', default='questrade', help='Broker backend to use')
@click.option('--loglevel', '-l', default='warning', help='Logging level')
@click.option('--keys', '-k', multiple=True,
              help='Return results only for these keys')
@click.argument('meth', nargs=1)
@click.argument('kwargs', nargs=-1)
def api(meth, kwargs, loglevel, broker, keys):
    """client for testing broker API methods with pretty printing of output.
    """
    log = get_console_log(loglevel)
    brokermod = import_module('.' + broker, 'piker.brokers')

    _kwargs = {}
    for kwarg in kwargs:
        if '=' not in kwarg:
            log.error(f"kwarg `{kwarg}` must be of form <key>=<value>")
        else:
            key, _, value = kwarg.partition('=')
            _kwargs[key] = value

    data = run(partial(brokermod.api, meth, **_kwargs), loglevel=loglevel)

    if keys:
        # filter to requested keys
        filtered = []
        if meth in data:  # often a list of dicts
            for item in data[meth]:
                filtered.append({key: item[key] for key in keys})

        else:  # likely just a dict
            filtered.append({key: data[key] for key in keys})
        data = filtered

    click.echo(colorize_json(data))


@cli.command()
@click.option('--broker', default='questrade', help='Broker backend to use')
@click.option('--loglevel', '-l', default='warning', help='Logging level')
@click.option('--df-output', '-df', flag_value=True,
              help='Ouput in `pandas.DataFrame` format')
@click.argument('tickers', nargs=-1)
def quote(loglevel, broker, tickers, df_output):
    """client for testing broker API methods with pretty printing of output.
    """
    brokermod = import_module('.' + broker, 'piker.brokers')
    data = run(partial(brokermod.quote, tickers), loglevel=loglevel)
    quotes = data['quotes']
    cols = quotes[0].copy()
    cols.pop('symbol')
    if df_output:
        df = pd.DataFrame(
            data['quotes'],
            index=[item['symbol'] for item in quotes],
            columns=cols,
        )
        click.echo(df)
    else:
        click.echo(colorize_json(data))


@cli.command()
@click.option('--broker', default='questrade', help='Broker backend to use')
@click.option('--loglevel', '-l', default='info', help='Logging level')
@click.argument('tickers', nargs=-1)
def stream(broker, loglevel, tickers, keys):
    # import broker module daemon entry point
    bm = import_module('.' + broker, 'piker.brokers')
    run(
        partial(bm.serve_forever, [
            partial(bm.poll_tickers, tickers=tickers)
        ]),
        loglevel
    )
