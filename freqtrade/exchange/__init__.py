# pragma pylint: disable=W0603
""" Cryptocurrency Exchanges support """
import enum
import logging
import ccxt
from random import randint
from typing import List, Dict, Any, Optional

import arrow
import requests
from cachetools import cached, TTLCache

from freqtrade import OperationalException
from freqtrade.exchange.interface import Exchange

logger = logging.getLogger(__name__)

# Current selected exchange
_API: Exchange = None
_CONF: dict = {}

# Holds all open sell orders for dry_run
_DRY_RUN_OPEN_ORDERS: Dict[str, Any] = {}


def init(config: dict) -> None:
    """
    Initializes this module with the given config,
    it does basic validation whether the specified
    exchange and pairs are valid.
    :param config: config to use
    :return: None
    """
    global _CONF, _API

    _CONF.update(config)

    if config['dry_run']:
        logger.info('Instance is running with dry_run enabled')

    exchange_config = config['exchange']

    # Find matching class for the given exchange name
    name = exchange_config['name']

    # TODO add check for a list of supported exchanges

    try:
        # exchange_class = Exchanges[name.upper()].value
        _API = getattr(ccxt, name.lower())({
            'apiKey': exchange_config.get('key'),
            'secret': exchange_config.get('secret'),
        })
    except KeyError:
        raise OperationalException('Exchange {} is not supported'.format(name))

    # we need load api markets
    _API.load_markets()

    # Check if all pairs are available
    validate_pairs(config['exchange']['pair_whitelist'])


def validate_pairs(pairs: List[str]) -> None:
    """
    Checks if all given pairs are tradable on the current exchange.
    Raises OperationalException if one pair is not available.
    :param pairs: list of pairs
    :return: None
    """

    if not _API.markets:
        _API.load_markets()

    try:
        markets = _API.markets
    except requests.exceptions.RequestException as e:
        logger.warning('Unable to validate pairs (assuming they are correct). Reason: %s', e)
        return

    stake_cur = _CONF['stake_currency']
    for pair in pairs:
        # Note: ccxt has BaseCurrency/QuoteCurrency format for pairs
        pair = pair.replace('_', '/')

        # TODO: add a support for having coins in BTC/USDT format
        if not pair.endswith(stake_cur):
            raise OperationalException(
                'Pair {} not compatible with stake_currency: {}'.format(pair, stake_cur)
            )
        if pair not in markets:
            raise OperationalException(
                'Pair {} is not available at {}'.format(pair, _API.name.lower()))


def buy(pair: str, rate: float, amount: float) -> str:
    if _CONF['dry_run']:
        global _DRY_RUN_OPEN_ORDERS
        order_id = 'dry_run_buy_{}'.format(randint(0, 10**6))
        _DRY_RUN_OPEN_ORDERS[order_id] = {
            'pair': pair,
            'rate': rate,
            'amount': amount,
            'type': 'LIMIT_BUY',
            'remaining': 0.0,
            'opened': arrow.utcnow().datetime,
            'closed': arrow.utcnow().datetime,
        }
        return order_id

    return _API.buy(pair, rate, amount)


def sell(pair: str, rate: float, amount: float) -> str:
    if _CONF['dry_run']:
        global _DRY_RUN_OPEN_ORDERS
        order_id = 'dry_run_sell_{}'.format(randint(0, 10**6))
        _DRY_RUN_OPEN_ORDERS[order_id] = {
            'pair': pair,
            'rate': rate,
            'amount': amount,
            'type': 'LIMIT_SELL',
            'remaining': 0.0,
            'opened': arrow.utcnow().datetime,
            'closed': arrow.utcnow().datetime,
        }
        return order_id

    return _API.sell(pair, rate, amount)


def get_balance(currency: str) -> float:
    if _CONF['dry_run']:
        return 999.9

    return _API.fetch_balance()[currency]


def get_balances():
    if _CONF['dry_run']:
        return []

    return _API.fetch_balance()


def get_ticker(pair: str, refresh: Optional[bool] = True) -> dict:
    return _API.get_ticker(pair, refresh)


@cached(TTLCache(maxsize=100, ttl=30))
def get_ticker_history(pair: str, tick_interval) -> List[Dict]:
    ## implement https://github.com/ccxt/ccxt/blob/master/python/ccxt/bittrex.py#L394-L400
    return _API.get_ticker_history(pair, tick_interval)


def cancel_order(order_id: str) -> None:
    if _CONF['dry_run']:
        return

    return _API.cancel_order(order_id)


def get_order(order_id: str) -> Dict:
    if _CONF['dry_run']:
        order = _DRY_RUN_OPEN_ORDERS[order_id]
        order.update({
            'id': order_id
        })
        return order

    return _API.get_order(order_id)


def get_pair_detail_url(pair: str) -> str:
    return _API.get_pair_detail_url(pair)


def get_markets() -> List[str]:
    return _API.get_markets()


def get_market_summaries() -> List[Dict]:
    # TODO: check other exchanges how they implement market summaries
    summaries = _API.public_get_marketsummaries()['result']
    for market in summaries:
        name = market['MarketName'].split('-')
        market['MarketName'] = name[-1] + '/' + name[0]
    return summaries
        


def get_name() -> str:
    return _API.name


def get_fee() -> float:
    return _API.fee


def get_wallet_health() -> List[Dict]:
    data =  _API.request('Currencies/GetWalletHealth', api='v2')
    if not data['success']:
        raise OperationalException('{}'.format(data['message']))
    return [{
        'Currency': entry['Health']['Currency'],
        'IsActive': entry['Health']['IsActive'],
        'LastChecked': entry['Health']['LastChecked'],
        'Notice': entry['Currency'].get('Notice'),
    } for entry in data['result']]
