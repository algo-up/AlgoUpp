# pragma pylint: disable=too-many-instance-attributes, pointless-string-statement
"""
This module contains the hyperopt logic
"""

import io
import locale
import logging
import os
import random
import sys
import warnings
from collections import OrderedDict, deque
from math import factorial, log
from multiprocessing import Manager
from operator import itemgetter
from os import path
from pathlib import Path
from pprint import pformat
from queue import Queue
from typing import Any, Dict, List, Optional, Set, Tuple

import progressbar
import rapidjson
import tabulate
from colorama import Fore, Style
from colorama import init as colorama_init
from joblib import (Parallel, cpu_count, delayed, dump, load, parallel_backend,
                    wrap_non_picklable_objects)
from numpy import iinfo, int32
from pandas import DataFrame, isna, json_normalize

# Import IHyperOpt and IHyperOptLoss to allow unpickling classes from these modules
import freqtrade.optimize.hyperopt_backend as backend
from freqtrade.data.converter import trim_dataframe
from freqtrade.data.history import get_timerange
from freqtrade.exceptions import OperationalException
from freqtrade.misc import plural, round_dict
from freqtrade.optimize.backtesting import Backtesting
from freqtrade.optimize.hyperopt_interface import IHyperOpt  # noqa: F401
from freqtrade.optimize.hyperopt_loss_interface import \
    IHyperOptLoss  # noqa: F401
from freqtrade.resolvers.hyperopt_resolver import (HyperOptLossResolver,
                                                   HyperOptResolver)

# Suppress scikit-learn FutureWarnings from skopt
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=FutureWarning)
    from skopt import Optimizer
    from skopt.space import Dimension
# Additional regressors already pluggable into the optimizer
# from sklearn.linear_model import ARDRegression, BayesianRidge
# possibly interesting regressors that need predict method override
# from sklearn.ensemble import HistGradientBoostingRegressor
# from xgboost import XGBoostRegressor


progressbar.streams.wrap_stderr()
progressbar.streams.wrap_stdout()
logger = logging.getLogger(__name__)

# supported strategies when asking for multiple points to the optimizer
LIE_STRATS = ["cl_min", "cl_mean", "cl_max"]
LIE_STRATS_N = len(LIE_STRATS)

# supported estimators
ESTIMATORS = ["GBRT", "ET", "RF"]
ESTIMATORS_N = len(ESTIMATORS)

VOID_LOSS = iinfo(int32).max  # just a big enough number to be bad result in loss optimization


class Hyperopt:
    """
    Hyperopt class, this class contains all the logic to run a hyperopt simulation

    To run a backtest:
    hyperopt = Hyperopt(config)
    hyperopt.start()
    """
    def __init__(self, config: Dict[str, Any]) -> None:

        self.config = config

        self.backtesting = Backtesting(self.config)

        self.custom_hyperopt = HyperOptResolver.load_hyperopt(self.config)

        self.custom_hyperoptloss = HyperOptLossResolver.load_hyperoptloss(self.config)
        self.calculate_loss = self.custom_hyperoptloss.hyperopt_loss_function

        self.results_file = (self.config['user_data_dir'] / 'hyperopt_results' /
                             'hyperopt_results.pickle')
        self.opts_file = (self.config['user_data_dir'] / 'hyperopt_results' /
                          'hyperopt_optimizers.pickle')
        self.data_pickle_file = (self.config['user_data_dir'] / 'hyperopt_results' /
                                 'hyperopt_tickerdata.pkl')

        self.n_jobs = self.config.get('hyperopt_jobs', -1)
        if self.n_jobs < 0:
            self.n_jobs = cpu_count() // 2 or 1
        self.effort = max(0.01,
                          self.config['effort'] if 'effort' in self.config else 1
                          )
        self.total_epochs = self.config['epochs'] if 'epochs' in self.config else 0
        self.max_epoch = 0
        self.max_epoch_reached = False
        self.min_epochs = 0
        self.epochs_limit = lambda: self.total_epochs or self.max_epoch

        # a guessed number extracted by the space dimensions
        self.search_space_size = 0
        # total number of candles being backtested
        self.n_candles = 0

        self.current_best_loss = VOID_LOSS
        self.current_best_epoch = 0
        self.epochs_since_last_best: List = []
        self.avg_best_occurrence = 0

        if not self.config.get('hyperopt_continue'):
            self.clean_hyperopt()
        else:
            logger.info("Continuing on previous hyperopt results.")

        self.num_epochs_saved = 0

        # evaluations
        self.trials: List = []

        # configure multi mode
        self.setup_multi()

        # Populate functions here (hasattr is slow so should not be run during "regular" operations)
        if hasattr(self.custom_hyperopt, 'populate_indicators'):
            self.backtesting.strategy.advise_indicators = \
                self.custom_hyperopt.populate_indicators  # type: ignore
        if hasattr(self.custom_hyperopt, 'populate_buy_trend'):
            self.backtesting.strategy.advise_buy = \
                self.custom_hyperopt.populate_buy_trend  # type: ignore
        if hasattr(self.custom_hyperopt, 'populate_sell_trend'):
            self.backtesting.strategy.advise_sell = \
                self.custom_hyperopt.populate_sell_trend  # type: ignore

        # Use max_open_trades for hyperopt as well, except --disable-max-market-positions is set
        if self.config.get('use_max_market_positions', True):
            self.max_open_trades = self.config['max_open_trades']
        else:
            logger.debug('Ignoring max_open_trades (--disable-max-market-positions was used) ...')
            self.max_open_trades = 0
        self.position_stacking = self.config.get('position_stacking', False)

        if self.has_space('sell'):
            # Make sure use_sell_signal is enabled
            if 'ask_strategy' not in self.config:
                self.config['ask_strategy'] = {}
            self.config['ask_strategy']['use_sell_signal'] = True

        self.print_all = self.config.get('print_all', False)
        self.hyperopt_table_header = 0
        self.print_colorized = self.config.get('print_colorized', False)
        self.print_json = self.config.get('print_json', False)

    def setup_multi(self):
        # optimizers
        self.opts: List[Optimizer] = []
        self.opt: Optimizer = None
        self.Xi: Dict = {}
        self.yi: Dict = {}

        backend.manager = Manager()
        self.mode = self.config.get('mode', 'single')
        self.shared = False
        # in multi opt one model is enough
        self.n_models = 1
        if self.mode in ('multi', 'shared'):
            self.multi = True
            if self.mode == 'shared':
                self.shared = True
                self.opt_base_estimator = lambda: 'GBRT'
            else:
                self.opt_base_estimator = self.estimators
            self.opt_acq_optimizer = 'sampling'
            backend.optimizers = backend.manager.Queue()
            backend.results_batch = backend.manager.Queue()
        else:
            self.multi = False
            backend.results_list = backend.manager.list([])
            # this is where opt_ask_and_tell stores the results after points are
            # used for fit and predict, to avoid additional pickling
            self.batch_results = []
            # self.opt_base_estimator = lambda: BayesianRidge(n_iter=100, normalize=True)
            self.opt_acq_optimizer = 'sampling'
            self.opt_base_estimator = lambda: 'ET'
            # The GaussianProcessRegressor is heavy, which makes it not a good default
            # however longer backtests might make it a better tradeoff
            # self.opt_base_estimator = lambda: 'GP'
            # self.opt_acq_optimizer = 'lbfgs'

        # in single opt assume runs are expensive so default to 1 point per ask
        self.n_points = self.config.get('n_points', 1)
        # if 0 n_points are given, don't use any base estimator (akin to random search)
        if self.n_points < 1:
            self.n_points = 1
            self.opt_base_estimator = lambda: "DUMMY"
            self.opt_acq_optimizer = "sampling"
        if self.n_points < 2:
            # ask_points is what is used in the ask call
            # because when n_points is None, it doesn't
            # waste time generating new points
            self.ask_points = None
        else:
            self.ask_points = self.n_points
        # var used in epochs and batches calculation
        self.opt_points = self.n_jobs * (self.n_points or 1)
        # lie strategy
        lie_strat = self.config.get('lie_strat', 'default')
        if lie_strat == 'default':
            self.lie_strat = lambda: 'cl_min'
        elif lie_strat == 'random':
            self.lie_strat = self.lie_strategy
        else:
            self.lie_strat = lambda: lie_strat

    @staticmethod
    def get_lock_filename(config: Dict[str, Any]) -> str:

        return str(config['user_data_dir'] / 'hyperopt.lock')

    def clean_hyperopt(self) -> None:
        """
        Remove hyperopt pickle files to restart hyperopt.
        """
        for f in [self.data_pickle_file, self.results_file, self.opts_file]:
            p = Path(f)
            if p.is_file():
                logger.info(f"Removing `{p}`.")
                p.unlink()

    def _get_params_dict(self, raw_params: List[Any]) -> Dict:

        dimensions: List[Dimension] = self.dimensions

        # Ensure the number of dimensions match
        # the number of parameters in the list.
        if len(raw_params) != len(dimensions):
            raise ValueError('Mismatch in number of search-space dimensions.')

        # Return a dict where the keys are the names of the dimensions
        # and the values are taken from the list of parameters.
        return {d.name: v for d, v in zip(dimensions, raw_params)}

    def _save_results(self) -> None:
        """
        Save hyperopt results to file
        """
        num_epochs = len(self.epochs)
        if num_epochs > self.num_epochs_saved:
            logger.debug(f"Saving {num_epochs} {plural(num_epochs, 'epoch')}.")
            dump(self.epochs, self.results_file)
            self.num_epochs_saved = num_epochs
            self.save_opts()
            logger.debug(f"{self.num_epochs_saved} {plural(self.num_epochs_saved, 'epoch')} "
                         f"saved to '{self.results_file}'.")

    def save_opts(self) -> None:
        """
        Save optimizers state to disk. The minimum required state could also be constructed
        from the attributes [ models, space, rng ] with Xi, yi loaded from trials.
        All we really care about are [rng, Xi, yi] since models are never passed over queues
        and space is dependent on dimensions matching with hyperopt config
        """
        # synchronize with saved trials
        opts = []
        n_opts = 0
        if self.multi:
            while not backend.optimizers.empty():
                opt = backend.optimizers.get()
                opt = Hyperopt.opt_clear(opt)
                opts.append(opt)
            n_opts = len(opts)
            for opt in opts:
                backend.optimizers.put(opt)
        else:
            # when we clear the object for saving we have to make a copy to preserve state
            opt = Hyperopt.opt_rand(self.opt, seed=False)
            if self.opt:
                n_opts = 1
                opts = [Hyperopt.opt_clear(self.opt)]
            # (the optimizer copy function also fits a new model with the known points)
            self.opt = opt
        logger.debug(f"Saving {n_opts} {plural(n_opts, 'optimizer')}.")
        dump(opts, self.opts_file)

    @staticmethod
    def _read_results(results_file: Path) -> List:
        """
        Read hyperopt results from file
        """
        logger.info("Reading epochs from '%s'", results_file)
        data = load(results_file)
        return data

    def _get_params_details(self, params: Dict) -> Dict:
        """
        Return the params for each space
        """
        result: Dict = {}

        if self.has_space('buy'):
            result['buy'] = {p.name: params.get(p.name)
                             for p in self.hyperopt_space('buy')}
        if self.has_space('sell'):
            result['sell'] = {p.name: params.get(p.name)
                              for p in self.hyperopt_space('sell')}
        if self.has_space('roi'):
            result['roi'] = self.custom_hyperopt.generate_roi_table(params)
        if self.has_space('stoploss'):
            result['stoploss'] = {p.name: params.get(p.name)
                                  for p in self.hyperopt_space('stoploss')}
        if self.has_space('trailing'):
            result['trailing'] = self.custom_hyperopt.generate_trailing_params(params)

        return result

    @staticmethod
    def print_epoch_details(results, total_epochs: int, print_json: bool,
                            no_header: bool = False, header_str: str = None) -> None:
        """
        Display details of the hyperopt result
        """
        params = results.get('params_details', {})

        # Default header string
        if header_str is None:
            header_str = "Best result"

        if not no_header:
            explanation_str = Hyperopt._format_explanation_string(results, total_epochs)
            print(f"\n{header_str}:\n\n{explanation_str}\n")

        if print_json:
            result_dict: Dict = {}
            for s in ['buy', 'sell', 'roi', 'stoploss', 'trailing']:
                Hyperopt._params_update_for_json(result_dict, params, s)
            print(rapidjson.dumps(result_dict, default=str, number_mode=rapidjson.NM_NATIVE))

        else:
            Hyperopt._params_pretty_print(params, 'buy', "Buy hyperspace params:")
            Hyperopt._params_pretty_print(params, 'sell', "Sell hyperspace params:")
            Hyperopt._params_pretty_print(params, 'roi', "ROI table:")
            Hyperopt._params_pretty_print(params, 'stoploss', "Stoploss:")
            Hyperopt._params_pretty_print(params, 'trailing', "Trailing stop:")

    @staticmethod
    def _params_update_for_json(result_dict, params, space: str) -> None:
        if space in params:
            space_params = Hyperopt._space_params(params, space)
            if space in ['buy', 'sell']:
                result_dict.setdefault('params', {}).update(space_params)
            elif space == 'roi':
                # TODO: get rid of OrderedDict when support for python 3.6 will be
                # dropped (dicts keep the order as the language feature)

                # Convert keys in min_roi dict to strings because
                # rapidjson cannot dump dicts with integer keys...
                # OrderedDict is used to keep the numeric order of the items
                # in the dict.
                result_dict['minimal_roi'] = OrderedDict(
                    (str(k), v) for k, v in space_params.items()
                )
            else:  # 'stoploss', 'trailing'
                result_dict.update(space_params)

    @staticmethod
    def _params_pretty_print(params, space: str, header: str) -> None:
        if space in params:
            space_params = Hyperopt._space_params(params, space, 5)
            params_result = f"\n# {header}\n"
            if space == 'stoploss':
                params_result += f"stoploss = {space_params.get('stoploss')}"
            elif space == 'roi':
                # TODO: get rid of OrderedDict when support for python 3.6 will be
                # dropped (dicts keep the order as the language feature)
                minimal_roi_result = rapidjson.dumps(
                    OrderedDict(
                        (str(k), v) for k, v in space_params.items()
                    ),
                    default=str, indent=4, number_mode=rapidjson.NM_NATIVE)
                params_result += f"minimal_roi = {minimal_roi_result}"
            else:
                params_result += f"{space}_params = {pformat(space_params, indent=4)}"
                params_result = params_result.replace("}", "\n}").replace("{", "{\n ")

            params_result = params_result.replace("\n", "\n    ")
            print(params_result)

    @staticmethod
    def _space_params(params, space: str, r: int = None) -> Dict:
        d = params[space]
        # Round floats to `r` digits after the decimal point if requested
        return round_dict(d, r) if r else d

    @staticmethod
    def is_best_loss(results, current_best_loss: float) -> bool:
        return results['loss'] < current_best_loss

    def print_results(self, results) -> None:
        """
        Log results if it is better than any previous evaluation
        """
        is_best = results['is_best']

        if self.print_all or is_best:
            print(
                self.get_result_table(
                    self.config, results, self.total_epochs,
                    self.print_all, self.print_colorized,
                    self.hyperopt_table_header
                )
            )
            self.hyperopt_table_header = 2

    @staticmethod
    def _format_explanation_string(results, total_epochs) -> str:
        return (("*" if 'is_initial_point' in results and results['is_initial_point'] else " ") +
                f"{results['current_epoch']:5d}/{total_epochs}: " +
                f"{results['results_explanation']} " +
                f"Objective: {results['loss']:.5f}")

    @staticmethod
    def get_result_table(config: dict, results: list, total_epochs: int, highlight_best: bool,
                         print_colorized: bool, remove_header: int) -> str:
        """
        Log result table
        """
        if not results:
            return ''

        tabulate.PRESERVE_WHITESPACE = True

        trials = json_normalize(results, max_level=1)
        trials['Best'] = ''
        trials = trials[['Best', 'current_epoch', 'results_metrics.trade_count',
                         'results_metrics.avg_profit', 'results_metrics.total_profit',
                         'results_metrics.profit', 'results_metrics.duration',
                         'loss', 'is_initial_point', 'is_best']]
        trials.columns = ['Best', 'Epoch', 'Trades', 'Avg profit', 'Total profit',
                          'Profit', 'Avg duration', 'Objective', 'is_initial_point', 'is_best']
        trials['is_profit'] = False
        trials.loc[trials['is_initial_point'], 'Best'] = '*     '
        trials.loc[trials['is_best'], 'Best'] = 'Best'
        trials.loc[trials['is_initial_point'] & trials['is_best'], 'Best'] = '* Best'
        trials.loc[trials['Total profit'] > 0, 'is_profit'] = True
        trials['Trades'] = trials['Trades'].astype(str)

        trials['Epoch'] = trials['Epoch'].apply(
            lambda x: '{}/{}'.format(str(x).rjust(len(str(total_epochs)), ' '), total_epochs)
        )
        trials['Avg profit'] = trials['Avg profit'].apply(
            lambda x: '{:,.2f}%'.format(x).rjust(7, ' ') if not isna(x) else "--".rjust(7, ' ')
        )
        trials['Avg duration'] = trials['Avg duration'].apply(
            lambda x: '{:,.1f} m'.format(x).rjust(7, ' ') if not isna(x) else "--".rjust(7, ' ')
        )
        trials['Objective'] = trials['Objective'].apply(
            lambda x: '{:,.5f}'.format(x).rjust(8, ' ') if x != 100000 else "N/A".rjust(8, ' ')
        )

        trials['Profit'] = trials.apply(
            lambda x: '{:,.8f} {} {}'.format(
                x['Total profit'], config['stake_currency'],
                '({:,.2f}%)'.format(x['Profit']).rjust(10, ' ')
            ).rjust(25+len(config['stake_currency']))
            if x['Total profit'] != 0.0 else '--'.rjust(25+len(config['stake_currency'])),
            axis=1
        )
        trials = trials.drop(columns=['Total profit'])

        if print_colorized:
            for i in range(len(trials)):
                if trials.loc[i]['is_profit']:
                    for j in range(len(trials.loc[i])-3):
                        trials.iat[i, j] = "{}{}{}".format(Fore.GREEN,
                                                           str(trials.loc[i][j]), Fore.RESET)
                if trials.loc[i]['is_best'] and highlight_best:
                    for j in range(len(trials.loc[i])-3):
                        trials.iat[i, j] = "{}{}{}".format(Style.BRIGHT,
                                                           str(trials.loc[i][j]), Style.RESET_ALL)

        trials = trials.drop(columns=['is_initial_point', 'is_best', 'is_profit'])
        if remove_header > 0:
            table = tabulate.tabulate(
                trials.to_dict(orient='list'), tablefmt='orgtbl',
                headers='keys', stralign="right"
            )

            table = table.split("\n", remove_header)[remove_header]
        elif remove_header < 0:
            table = tabulate.tabulate(
                trials.to_dict(orient='list'), tablefmt='psql',
                headers='keys', stralign="right"
            )
            table = "\n".join(table.split("\n")[0:remove_header])
        else:
            table = tabulate.tabulate(
                trials.to_dict(orient='list'), tablefmt='psql',
                headers='keys', stralign="right"
            )
        return table

    @staticmethod
    def export_csv_file(config: dict, results: list, total_epochs: int, highlight_best: bool,
                        csv_file: str) -> None:
        """
        Log result to csv-file
        """
        if not results:
            return

        # Verification for overwrite
        if path.isfile(csv_file):
            logger.error(f"CSV file already exists: {csv_file}")
            return

        try:
            io.open(csv_file, 'w+').close()
        except IOError:
            logger.error(f"Failed to create CSV file: {csv_file}")
            return

        trials = json_normalize(results, max_level=1)
        trials['Best'] = ''
        trials['Stake currency'] = config['stake_currency']

        base_metrics = ['Best', 'current_epoch', 'results_metrics.trade_count',
                        'results_metrics.avg_profit', 'results_metrics.total_profit',
                        'Stake currency', 'results_metrics.profit', 'results_metrics.duration',
                        'loss', 'is_initial_point', 'is_best']
        param_metrics = [("params_dict."+param) for param in results[0]['params_dict'].keys()]
        trials = trials[base_metrics + param_metrics]

        base_columns = ['Best', 'Epoch', 'Trades', 'Avg profit', 'Total profit', 'Stake currency',
                        'Profit', 'Avg duration', 'Objective', 'is_initial_point', 'is_best']
        param_columns = list(results[0]['params_dict'].keys())
        trials.columns = base_columns + param_columns

        trials['is_profit'] = False
        trials.loc[trials['is_initial_point'], 'Best'] = '*'
        trials.loc[trials['is_best'], 'Best'] = 'Best'
        trials.loc[trials['is_initial_point'] & trials['is_best'], 'Best'] = '* Best'
        trials.loc[trials['Total profit'] > 0, 'is_profit'] = True
        trials['Epoch'] = trials['Epoch'].astype(str)
        trials['Trades'] = trials['Trades'].astype(str)

        trials['Total profit'] = trials['Total profit'].apply(
            lambda x: '{:,.8f}'.format(x) if x != 0.0 else ""
        )
        trials['Profit'] = trials['Profit'].apply(
            lambda x: '{:,.2f}'.format(x) if not isna(x) else ""
        )
        trials['Avg profit'] = trials['Avg profit'].apply(
            lambda x: '{:,.2f}%'.format(x) if not isna(x) else ""
        )
        trials['Avg duration'] = trials['Avg duration'].apply(
            lambda x: '{:,.1f} m'.format(x) if not isna(x) else ""
        )
        trials['Objective'] = trials['Objective'].apply(
            lambda x: '{:,.5f}'.format(x) if x != 100000 else ""
        )

        trials = trials.drop(columns=['is_initial_point', 'is_best', 'is_profit'])
        trials.to_csv(csv_file, index=False, header=True, mode='w', encoding='UTF-8')
        logger.info(f"CSV file created: {csv_file}")

    def has_space(self, space: str) -> bool:
        """
        Tell if the space value is contained in the configuration
        """
        # The 'trailing' space is not included in the 'default' set of spaces
        if space == 'trailing':
            return any(s in self.config['spaces'] for s in [space, 'all'])
        else:
            return any(s in self.config['spaces'] for s in [space, 'all', 'default'])

    def hyperopt_space(self, space: Optional[str] = None) -> List[Dimension]:
        """
        Return the dimensions in the hyperoptimization space.
        :param space: Defines hyperspace to return dimensions for.
        If None, then the self.has_space() will be used to return dimensions
        for all hyperspaces used.
        """
        spaces: List[Dimension] = []

        if space == 'buy' or (space is None and self.has_space('buy')):
            logger.debug("Hyperopt has 'buy' space")
            spaces += self.custom_hyperopt.indicator_space()

        if space == 'sell' or (space is None and self.has_space('sell')):
            logger.debug("Hyperopt has 'sell' space")
            spaces += self.custom_hyperopt.sell_indicator_space()

        if space == 'roi' or (space is None and self.has_space('roi')):
            logger.debug("Hyperopt has 'roi' space")
            spaces += self.custom_hyperopt.roi_space()

        if space == 'stoploss' or (space is None and self.has_space('stoploss')):
            logger.debug("Hyperopt has 'stoploss' space")
            spaces += self.custom_hyperopt.stoploss_space()

        if space == 'trailing' or (space is None and self.has_space('trailing')):
            logger.debug("Hyperopt has 'trailing' space")
            spaces += self.custom_hyperopt.trailing_space()

        return spaces

    def backtest_params(self, raw_params: List[Any], iteration=None) -> Dict:
        """
        Used Optimize function. Called once per epoch to optimize whatever is configured.
        Keep this function as optimized as possible!
        """
        params_dict = self._get_params_dict(raw_params)
        params_details = self._get_params_details(params_dict)

        if self.has_space('roi'):
            self.backtesting.strategy.minimal_roi = \
                self.custom_hyperopt.generate_roi_table(params_dict)

        if self.has_space('buy'):
            self.backtesting.strategy.advise_buy = \
                self.custom_hyperopt.buy_strategy_generator(params_dict)

        if self.has_space('sell'):
            self.backtesting.strategy.advise_sell = \
                self.custom_hyperopt.sell_strategy_generator(params_dict)

        if self.has_space('stoploss'):
            self.backtesting.strategy.stoploss = params_dict['stoploss']

        if self.has_space('trailing'):
            d = self.custom_hyperopt.generate_trailing_params(params_dict)
            self.backtesting.strategy.trailing_stop = d['trailing_stop']
            self.backtesting.strategy.trailing_stop_positive = d['trailing_stop_positive']
            self.backtesting.strategy.trailing_stop_positive_offset = \
                d['trailing_stop_positive_offset']
            self.backtesting.strategy.trailing_only_offset_is_reached = \
                d['trailing_only_offset_is_reached']

        processed = load(self.data_pickle_file)

        min_date, max_date = get_timerange(processed)

        backtesting_results = self.backtesting.backtest(
            processed=processed,
            stake_amount=self.config['stake_amount'],
            start_date=min_date,
            end_date=max_date,
            max_open_trades=self.max_open_trades,
            position_stacking=self.position_stacking,
        )
        return self._get_results_dict(backtesting_results, min_date, max_date, params_dict,
                                      params_details)

    def _get_results_dict(self, backtesting_results, min_date, max_date, params_dict,
                          params_details):
        results_metrics = self._calculate_results_metrics(backtesting_results)
        results_explanation = self._format_results_explanation_string(results_metrics)

        trade_count = results_metrics['trade_count']
        total_profit = results_metrics['total_profit']

        # If this evaluation contains too short amount of trades to be
        # interesting -- consider it as 'bad' (assigned max. loss value)
        # in order to cast this hyperspace point away from optimization
        # path. We do not want to optimize 'hodl' strategies.
        loss: float = VOID_LOSS
        if trade_count >= self.config['hyperopt_min_trades']:
            loss = self.calculate_loss(results=backtesting_results, trade_count=trade_count,
                                       min_date=min_date.datetime, max_date=max_date.datetime)
        return {
            'loss': loss,
            'params_dict': params_dict,
            'params_details': params_details,
            'results_metrics': results_metrics,
            'results_explanation': results_explanation,
            'total_profit': total_profit,
        }

    def _calculate_results_metrics(self, backtesting_results: DataFrame) -> Dict:
        return {
            'trade_count': len(backtesting_results.index),
            'avg_profit': backtesting_results.profit_percent.mean() * 100.0,
            'total_profit': backtesting_results.profit_abs.sum(),
            'profit': backtesting_results.profit_percent.sum() * 100.0,
            'duration': backtesting_results.trade_duration.mean(),
        }

    def _format_results_explanation_string(self, results_metrics: Dict) -> str:
        """
        Return the formatted results explanation in a string
        """
        stake_cur = self.config['stake_currency']
        return (f"{results_metrics['trade_count']:6d} trades. "
                f"Avg profit {results_metrics['avg_profit']: 6.2f}%. "
                f"Total profit {results_metrics['total_profit']: 11.8f} {stake_cur} "
                f"({results_metrics['profit']: 7.2f}\N{GREEK CAPITAL LETTER SIGMA}%). "
                f"Avg duration {results_metrics['duration']:5.1f} min."
                ).encode(locale.getpreferredencoding(), 'replace').decode('utf-8')

    @staticmethod
    def filter_void_losses(vals: List, opt: Optimizer) -> List:
        """ remove out of bound losses from the results """
        if opt.void_loss == VOID_LOSS and len(opt.yi) < 1:
            # only exclude results at the beginning when void loss is yet to be set
            void_filtered = list(filter(lambda v: v["loss"] != VOID_LOSS, vals))
        else:
            if opt.void_loss == VOID_LOSS:  # set void loss once
                opt.void_loss = max(opt.yi)
            void_filtered = []
            # default bad losses to set void_loss
            for k, v in enumerate(vals):
                if v["loss"] == VOID_LOSS:
                    vals[k]["loss"] = opt.void_loss
            void_filtered = vals
        return void_filtered

    def lie_strategy(self):
        """ Choose a strategy randomly among the supported ones, used in multi opt mode
        to increase the diversion of the searches of each optimizer """
        return LIE_STRATS[random.randrange(0, LIE_STRATS_N)]

    def estimators(self):
        return ESTIMATORS[random.randrange(0, ESTIMATORS_N)]

    def get_optimizer(self, random_state: int = None) -> Optimizer:
        " Construct an optimizer object "
        # https://github.com/scikit-learn/scikit-learn/issues/14265
        # lbfgs uses joblib threading backend so n_jobs has to be reduced
        # to avoid oversubscription
        if self.opt_acq_optimizer == 'lbfgs':
            n_jobs = 1
        else:
            n_jobs = self.n_jobs
        return Optimizer(
            self.dimensions,
            base_estimator=self.opt_base_estimator(),
            acq_optimizer=self.opt_acq_optimizer,
            n_initial_points=self.opt_n_initial_points,
            acq_optimizer_kwargs={'n_jobs': n_jobs},
            model_queue_size=self.n_models,
            random_state=random_state or self.random_state,
        )

    def run_backtest_parallel(self, parallel: Parallel, tries: int, first_try: int,
                              jobs: int):
        """ launch parallel in single opt mode, return the evaluated epochs """
        parallel(
            delayed(wrap_non_picklable_objects(self.parallel_objective))
            (asked, backend.results_list, i)
            for asked, i in zip(self.opt_ask_and_tell(jobs, tries),
                                range(first_try, first_try + tries)))

    def run_multi_backtest_parallel(self, parallel: Parallel, tries: int, first_try: int,
                                    jobs: int):
        """ launch parallel in multi opt mode, return the evaluated epochs"""
        parallel(
            delayed(wrap_non_picklable_objects(self.parallel_opt_objective))(
                i, backend.optimizers, jobs, backend.results_shared, backend.results_batch)
            for i in range(first_try, first_try + tries))

    def opt_ask_and_tell(self, jobs: int, tries: int):
        """
        loop to manage optimizer state in single optimizer mode, everytime a job is
        dispatched, we check the optimizer for points, to ask and to tell if any,
        but only fit a new model every n_points, because if we fit at every result previous
        points become invalid.
        """
        vals = []
        fit = False
        to_ask: deque = deque()
        evald: Set[Tuple] = set()
        opt = self.opt

        # this is needed because when we ask None points, the optimizer doesn't return a list
        if self.ask_points:
            def point():
                if to_ask:
                    return tuple(to_ask.popleft())
                else:
                    to_ask.extend(opt.ask(n_points=self.ask_points, strategy=self.lie_strat()))
                    return tuple(to_ask.popleft())
        else:
            def point():
                return tuple(opt.ask(strategy=self.lie_strat()))

        for r in range(tries):
            fit = (len(to_ask) < 1)
            if len(backend.results_list) > 0:
                vals.extend(backend.results_list)
                del backend.results_list[:]
            if vals:
                # filter losses
                void_filtered = Hyperopt.filter_void_losses(vals, opt)
                if void_filtered:  # again if all are filtered
                    opt.tell([Hyperopt.params_Xi(v) for v in void_filtered],
                             [v['loss'] for v in void_filtered],
                             fit=fit)  # only fit when out of points
                    self.batch_results.extend(void_filtered)
                del vals[:], void_filtered[:]

            a = point()
            # this usually happens at the start when trying to fit before the initial points
            if a in evald:
                logger.debug("this point was evaluated before...")
                opt.update_next()
                a = point()
                if a in evald:
                    break
            evald.add(a)
            yield a

    @staticmethod
    def opt_get_past_points(is_shared: bool, asked: dict, results_shared: Dict) -> Tuple[dict, int]:
        """ fetch shared results between optimizers """
        # a result is (y, counter)
        for a in asked:
            if a in results_shared:
                y, counter = results_shared[a]
                asked[a] = y
                counter -= 1
                if counter < 1:
                    del results_shared[a]
        return asked, len(results_shared)

    @staticmethod
    def opt_rand(opt: Optimizer, rand: int = None, seed: bool = True) -> Optimizer:
        """ return a new instance of the optimizer with modified rng """
        if seed:
            if not rand:
                rand = opt.rng.randint(0, VOID_LOSS)
            opt.rng.seed(rand)
        opt, opt.void_loss, opt.void, opt.rs = (
            opt.copy(random_state=opt.rng), opt.void_loss, opt.void, opt.rs
        )
        return opt

    @staticmethod
    def opt_state(shared: bool, optimizers: Queue) -> Optimizer:
        """ fetch an optimizer in multi opt mode """
        # get an optimizer instance
        opt = optimizers.get()
        if shared:
            # get a random number before putting it back to avoid
            # replication with other workers and keep reproducibility
            rand = opt.rng.randint(0, VOID_LOSS)
            optimizers.put(opt)
            # switch the seed to get a different point
            opt = Hyperopt.opt_rand(opt, rand)
        return opt

    @staticmethod
    def opt_clear(opt: Optimizer):
        """ clear state from an optimizer object """
        del opt.models[:], opt.Xi[:], opt.yi[:]
        return opt

    @staticmethod
    def opt_results(opt: Optimizer, void_filtered: list, jobs: int, is_shared: bool,
                    results_shared: Dict, results_batch: Queue, optimizers: Queue):
        """
        update the board used to skip already computed points,
        set the initial point status
        """
        # add points of the current dispatch if any
        if opt.void_loss != VOID_LOSS or len(void_filtered) > 0:
            void = False
        else:
            void = True
        # send back the updated optimizer only in non shared mode
        if not is_shared:
            opt = Hyperopt.opt_clear(opt)
            # is not a replica in shared mode
            optimizers.put(opt)
        # NOTE: some results at the beginning won't be published
        # because they are removed by filter_void_losses
        rs = opt.rs
        if not void:
            # the tuple keys are used to avoid computation of done points by any optimizer
            results_shared.update({tuple(Hyperopt.params_Xi(v)): (v["loss"], jobs - 1)
                                   for v in void_filtered})
            # in multi opt mode (non shared) also track results for each optimizer (using rs as ID)
            # this keys should be cleared after each batch
            Xi, yi = results_shared[rs]
            Xi = Xi + tuple((Hyperopt.params_Xi(v)) for v in void_filtered)
            yi = yi + tuple(v["loss"] for v in void_filtered)
            results_shared[rs] = (Xi, yi)
            # this is the counter used by the optimizer internally to track the initial
            # points evaluated so far..
            initial_points = opt._n_initial_points
            # set initial point flag and optimizer random state
            for n, v in enumerate(void_filtered):
                v['is_initial_point'] = initial_points - n > 0
                v['random_state'] = rs
            results_batch.put(void_filtered)

    def parallel_opt_objective(self, n: int, optimizers: Queue, jobs: int,
                               results_shared: Dict, results_batch: Queue):
        """
        objective run in multi opt mode, optimizers share the results as soon as they are completed
        """
        self.log_results_immediate(n)
        is_shared = self.shared
        opt = self.opt_state(is_shared, optimizers)
        sss = self.search_space_size
        asked: Dict[Tuple, Any] = {tuple([]): None}
        asked_d: Dict[Tuple, Any] = {}

        # fit a model with the known points, (the optimizer has no points here since
        # it was just fetched from the queue)
        rs = opt.rs
        Xi, yi = self.Xi[rs], self.yi[rs]
        # add the points discovered within this batch
        bXi, byi = results_shared[rs]
        Xi.extend(list(bXi))
        yi.extend(list(byi))
        if Xi:
            opt.tell(Xi, yi)
        told = 0  # told
        Xi_d = []  # done
        yi_d = []
        Xi_t = []  # to do
        # if opt.void == -1 the optimizer failed to give a new point (between dispatches), stop
        # if asked == asked_d  the points returned are the same, stop
        # if opt.Xi > sss the optimizer has more points than the estimated search space size, stop
        while opt.void != -1 and asked != asked_d and len(opt.Xi) < sss:
            asked_d = asked
            asked = opt.ask(n_points=self.ask_points, strategy=self.lie_strat())
            if not self.ask_points:
                asked = {tuple(asked): None}
            else:
                asked = {tuple(a): None for a in asked}
            # check if some points have been evaluated by other optimizers
            p_asked, _ = Hyperopt.opt_get_past_points(is_shared, asked, results_shared)
            for a in p_asked:
                if p_asked[a] is not None:
                    if a not in Xi_d:
                        Xi_d.append(a)
                        yi_d.append(p_asked[a])
                else:
                    Xi_t.append(a)
            # no points to do?
            if len(Xi_t) < self.n_points:
                len_Xi_d = len(Xi_d)
                # did other workers backtest some points?
                if len_Xi_d > told:
                    # if yes fit a new model with the new points
                    opt.tell(Xi_d[told:], yi_d[told:])
                    told = len_Xi_d
                else:  # or get new points from a different random state
                    opt = Hyperopt.opt_rand(opt)
            else:
                break
        # return early if there is nothing to backtest
        if len(Xi_t) < 1:
            if is_shared:
                opt = optimizers.get()
            opt.void = -1
            opt = Hyperopt.opt_clear(opt)
            optimizers.put(opt)
            return []
        # run the backtest for each point to do (Xi_t)
        results = [self.backtest_params(a) for a in Xi_t]
        # filter losses
        void_filtered = Hyperopt.filter_void_losses(results, opt)

        Hyperopt.opt_results(opt, void_filtered, jobs, is_shared,
                             results_shared, results_batch, optimizers)

    def parallel_objective(self, asked, results_list: List = [], n=0):
        """ objective run in single opt mode, run the backtest, store the results into a queue """
        self.log_results_immediate(n)
        v = self.backtest_params(asked)

        v['is_initial_point'] = n < self.opt_n_initial_points
        v['random_state'] = self.random_state
        results_list.append(v)

    def log_results_immediate(self, n) -> None:
        """ Signals that a new job has been scheduled"""
        print('.', end='')
        sys.stdout.flush()

    def log_results(self, batch_results, frame_start, total_epochs: int) -> int:
        """
        Log results if it is better than any previous evaluation
        """
        current = frame_start + 1
        i = 0
        for i, v in enumerate(batch_results, 1):
            is_best = self.is_best_loss(v, self.current_best_loss)
            current = frame_start + i
            v['is_best'] = is_best
            v['current_epoch'] = current
            logger.debug(f"Optimizer epoch evaluated: {v}")
            if is_best:
                self.current_best_loss = v['loss']
                self.update_max_epoch(v, current)
            self.print_results(v)
            self.trials.append(v)
        # Save results and optimizers after every batch
        self._save_results()
        # track new points if in multi mode
        if self.multi:
            self.track_points(trials=self.trials[frame_start:])
            # clear points used by optimizers intra batch
            backend.results_shared.update(self.opt_empty_tuple())
        # give up if no best since max epochs
        if current + 1 > self.epochs_limit():
            self.max_epoch_reached = True
        return i

    def setup_epochs(self) -> bool:
        """ used to resume the best epochs state from previous trials """
        len_trials = len(self.trials)
        if len_trials > 0:
            best_epochs = list(filter(lambda k: k["is_best"], self.trials))
            len_best = len(best_epochs)
            if len_best > 0:
                # sorting from lowest to highest, the first value is the current best
                best = sorted(best_epochs, key=lambda k: k["loss"])[0]
                self.current_best_epoch = best["current_epoch"]
                self.current_best_loss = best["loss"]
                self.avg_best_occurrence = len_trials // len_best
                return True
        return False

    @staticmethod
    def load_previous_results(results_file: Path) -> List:
        """
        Load data for epochs from the file if we have one
        """
        epochs: List = []
        if results_file.is_file() and results_file.stat().st_size > 0:
            epochs = Hyperopt._read_results(results_file)
            # Detection of some old format, without 'is_best' field saved
            if epochs[0].get('is_best') is None:
                raise OperationalException(
                    "The file with Hyperopt results is incompatible with this version "
                    "of Freqtrade and cannot be loaded.")
            logger.info(f"Loaded {len(epochs)} previous evaluations from disk.")
        return epochs

    @staticmethod
    def load_previous_optimizers(opts_file: Path) -> List:
        """ Load the state of previous optimizers from file """
        opts: List[Optimizer] = []
        if opts_file.is_file() and opts_file.stat().st_size > 0:
            opts = load(opts_file)
        n_opts = len(opts)
        if n_opts > 0 and type(opts[-1]) != Optimizer:
            raise OperationalException("The file storing optimizers state might be corrupted "
                                       "and cannot be loaded.")
        else:
            logger.info(f"Loaded {n_opts} previous {plural(n_opts, 'optimizer')} from disk.")
        return opts

    def _set_random_state(self, random_state: Optional[int]) -> int:
        return random_state or random.randint(1, 2**16 - 1)

    @staticmethod
    def calc_epochs(
        dimensions: List[Dimension], n_jobs: int, effort: float, total_epochs: int, n_points: int
    ):
        """ Compute a reasonable number of initial points and
        a minimum number of epochs to evaluate """
        n_dimensions = len(dimensions)
        n_parameters = 0
        opt_points = n_jobs * n_points
        # sum all the dimensions discretely, granting minimum values
        for d in dimensions:
            if type(d).__name__ == 'Integer':
                n_parameters += max(1, d.high - d.low)
            elif type(d).__name__ == 'Real':
                n_parameters += max(10, int(d.high - d.low))
            else:
                n_parameters += len(d.bounds)
        # guess the size of the search space as the count of the
        # unordered combination of the dimensions entries
        try:
            search_space_size = int(
                (factorial(n_parameters) /
                 (factorial(n_parameters - n_dimensions) * factorial(n_dimensions))))
        except OverflowError:
            search_space_size = VOID_LOSS
        # logger.info(f'Search space size: {search_space_size}')
        log_opt = int(log(opt_points, 2)) if opt_points > 4 else 2
        if search_space_size < opt_points:
            # don't waste if the space is small
            n_initial_points = opt_points // 3
            min_epochs = opt_points
        elif total_epochs > 0:
            # coefficients from total epochs
            log_epp = int(log(total_epochs, 2)) * log_opt
            n_initial_points = min(log_epp, total_epochs // 3)
            min_epochs = total_epochs
        else:
            # extract coefficients from the search space
            log_sss = int(log(search_space_size, 10)) * log_opt
            # never waste
            n_initial_points = min(log_sss, search_space_size // 3)
            # it shall run for this much, I say
            min_epochs = int(max(n_initial_points, opt_points) + 2 * n_initial_points)
        return int(n_initial_points * effort) or 1, int(min_epochs * effort), search_space_size

    def update_max_epoch(self, val: Dict, current: int):
        """ calculate max epochs: store the number of non best epochs
            between each best, and get the mean of that value """
        if val['is_initial_point'] is not True:
            self.epochs_since_last_best.append(current - self.current_best_epoch)
            self.avg_best_occurrence = (sum(self.epochs_since_last_best) //
                                        len(self.epochs_since_last_best))
            self.current_best_epoch = current
            self.max_epoch = int(
                (self.current_best_epoch + self.avg_best_occurrence + self.min_epochs) *
                max(1, self.effort))
            if self.max_epoch > self.search_space_size:
                self.max_epoch = self.search_space_size
        logger.debug(f'\nMax epoch set to: {self.epochs_limit()}')

    @staticmethod
    def params_Xi(v: dict):
        return list(v["params_dict"].values())

    def track_points(self, trials: List = None):
        """
        keep tracking of the evaluated points per optimizer random state
        """
        # if no trials are given, use saved trials
        if not trials:
            if len(self.trials) > 0:
                if self.config.get('hyperopt_continue_filtered', False):
                    raise ValueError()
                    # trials = filter_trials(self.trials, self.config)
                else:
                    trials = self.trials
            else:
                return
        for v in trials:
            rs = v["random_state"]
            try:
                self.Xi[rs].append(Hyperopt.params_Xi(v))
                self.yi[rs].append(v["loss"])
            except IndexError:  # Hyperopt was started with different random_state or number of jobs
                pass

    def setup_optimizers(self):
        """ Setup the optimizers objects, try to load from disk, or create new ones """
        # try to load previous optimizers
        opts = self.load_previous_optimizers(self.opts_file)
        n_opts = len(opts)

        if self.multi:
            max_opts = self.n_jobs
            rngs = []
            # when sharing results there is only one optimizer that gets copied
            if self.shared:
                max_opts = 1
            # put the restored optimizers in the queue
            # only if they match the current number of jobs
            if n_opts == max_opts:
                for n in range(n_opts):
                    rngs.append(opts[n].rs)
                    # make sure to not store points and models in the optimizer
                    backend.optimizers.put(Hyperopt.opt_clear(opts[n]))
            # generate as many optimizers as are still needed to fill the job count
            remaining = max_opts - backend.optimizers.qsize()
            if remaining > 0:
                opt = self.get_optimizer()
                rngs = []
                for _ in range(remaining):  # generate optimizers
                    # random state is preserved
                    rs = opt.rng.randint(0, iinfo(int32).max)
                    opt_copy = opt.copy(random_state=rs)
                    opt_copy.void_loss = VOID_LOSS
                    opt_copy.void = False
                    opt_copy.rs = rs
                    rngs.append(rs)
                    backend.optimizers.put(opt_copy)
                del opt, opt_copy
            # reconstruct observed points from epochs
            # in shared mode each worker will remove the results once all the workers
            # have read it (counter < 1)
            counter = self.n_jobs

            def empty_dict():
                return {rs: [] for rs in rngs}
            self.opt_empty_tuple = lambda: {rs: ((), ()) for rs in rngs}
            self.Xi.update(empty_dict())
            self.yi.update(empty_dict())
            self.track_points()
            # this is needed to keep track of results discovered within the same batch
            # by each optimizer, use tuples! as the SyncManager doesn't handle nested dicts
            Xi, yi = self.Xi, self.yi
            results = {tuple(X): [yi[r][n], counter] for r in Xi for n, X in enumerate(Xi[r])}
            results.update(self.opt_empty_tuple())
            backend.results_shared = backend.manager.dict(results)
        else:
            # if we have more than 1 optimizer but are using single opt,
            # pick one discard the rest...
            if n_opts > 0:
                self.opt = opts[-1]
            else:
                self.opt = self.get_optimizer()
                self.opt.void_loss = VOID_LOSS
                self.opt.void = False
                self.opt.rs = self.random_state
            # in single mode restore the points directly to the optimizer
            # but delete first in case we have filtered the starting list of points
            self.opt = Hyperopt.opt_clear(self.opt)
            rs = self.random_state
            self.Xi[rs] = []
            self.track_points()
            if len(self.Xi[rs]) > 0:
                self.opt.tell(self.Xi[rs], self.yi[rs], fit=False)
            # delete points since in single mode the optimizer state sits in the main
            # process and is not discarded
            self.Xi, self.yi = {}, {}
        del opts[:]

    def setup_points(self):
        self.n_initial_points, self.min_epochs, self.search_space_size = self.calc_epochs(
            self.dimensions, self.n_jobs, self.effort, self.total_epochs, self.n_points
        )
        logger.info(f"Min epochs set to: {self.min_epochs}")
        # reduce random points by n_points in multi mode because asks are per job
        if self.multi:
            self.opt_n_initial_points = self.n_initial_points // self.n_points
        else:
            self.opt_n_initial_points = self.n_initial_points
        logger.info(f'Initial points: {self.n_initial_points}')
        # if total epochs are not set, max_epoch takes its place
        if self.total_epochs < 1:
            self.max_epoch = int(self.min_epochs + len(self.trials))
        # initialize average best occurrence
        self.avg_best_occurrence = self.min_epochs // self.n_jobs

    def return_results(self):
        """
        results are passed by queue in multi mode, or stored by ask_and_tell in single mode
        """
        batch_results = []
        if self.multi:
            while not backend.results_batch.empty():
                worker_results = backend.results_batch.get()
                batch_results.extend(worker_results)
        else:
            batch_results.extend(self.batch_results)
            del self.batch_results[:]
        return batch_results

    def main_loop(self, jobs_scheduler):
        """ main parallel loop """
        try:
            with parallel_backend('loky', inner_max_num_threads=2):
                with Parallel(n_jobs=self.n_jobs, verbose=0, backend='loky') as parallel:
                    jobs = parallel._effective_n_jobs()
                    logger.info(f'Effective number of parallel workers used: {jobs}')
                    # update epochs count
                    opt_points = self.opt_points
                    prev_batch = -1
                    epochs_so_far = len(self.trials)
                    epochs_limit = self.epochs_limit
                    columns, _ = os.get_terminal_size()
                    columns -= 1
                    while epochs_so_far > prev_batch or epochs_so_far < self.min_epochs:
                        prev_batch = epochs_so_far
                        occurrence = int(self.avg_best_occurrence * max(1, self.effort))
                        # pad the batch length to the number of jobs to avoid desaturation
                        batch_len = (occurrence + jobs -
                                     occurrence % jobs)
                        # when using multiple optimizers each worker performs
                        # n_points (epochs) in 1 dispatch but this reduces the batch len too much
                        # if self.multi: batch_len = batch_len // self.n_points
                        # don't go over the limit
                        if epochs_so_far + batch_len * opt_points >= epochs_limit():
                            q, r = divmod(epochs_limit() - epochs_so_far, opt_points)
                            batch_len = q + r
                        print(
                            f"{epochs_so_far+1}-{epochs_so_far+batch_len}"
                            f"/{epochs_limit()}: ",
                            end='')
                        jobs_scheduler(parallel, batch_len, epochs_so_far, jobs)
                        batch_results = self.return_results()
                        print(end='\r')
                        saved = self.log_results(batch_results, epochs_so_far, epochs_limit())
                        print('\r', ' ' * columns, end='\r')
                        # stop if no epochs have been evaluated
                        if len(batch_results) < batch_len:
                            logger.warning("Some evaluated epochs were void, "
                                           "check the loss function and the search space.")
                        if (not saved and len(batch_results) > 1) or batch_len < 1 or \
                           (not saved and self.search_space_size < batch_len + epochs_limit()):
                            break
                        # log_results add
                        epochs_so_far += saved
                        if self.max_epoch_reached:
                            logger.info("Max epoch reached, terminating.")
                            break
        except KeyboardInterrupt:
            print('User interrupted..')

    def start(self) -> None:
        """ Broom Broom """
        self.random_state = self._set_random_state(self.config.get('hyperopt_random_state', None))
        logger.info(f"Using optimizer random state: {self.random_state}")
        self.hyperopt_table_header = -1
        data, timerange = self.backtesting.load_bt_data()

        preprocessed = self.backtesting.strategy.ohlcvdata_to_dataframe(data)

        # Trim startup period from analyzed dataframe
        for pair, df in preprocessed.items():
            preprocessed[pair] = trim_dataframe(df, timerange)
            self.n_candles += len(preprocessed[pair])
        min_date, max_date = get_timerange(data)

        logger.info(
            'Hyperopting with data from %s up to %s (%s days)..',
            min_date.isoformat(), max_date.isoformat(), (max_date - min_date).days
        )
        dump(preprocessed, self.data_pickle_file)

        # We don't need exchange instance anymore while running hyperopt
        self.backtesting.exchange = None  # type: ignore
        self.backtesting.pairlists = None  # type: ignore

        self.epochs = self.load_previous_results(self.results_file)
        self.setup_epochs()

        logger.info(f"Found {cpu_count()} CPU cores. Let's make them scream!")
        logger.info(f'Number of parallel jobs set as: {self.n_jobs}')

        self.dimensions: List[Dimension] = self.hyperopt_space()
        self.setup_points()

        if self.print_colorized:
            colorama_init(autoreset=True)

        self.setup_optimizers()

        if self.multi:
            jobs_scheduler = self.run_multi_backtest_parallel
        else:
            jobs_scheduler = self.run_backtest_parallel

        self.main_loop(jobs_scheduler)

        self._save_results()
        logger.info(f"{self.num_epochs_saved} {plural(self.num_epochs_saved, 'epoch')} "
                    f"saved to '{self.results_file}'.")

        if self.epochs:
            sorted_epochs = sorted(self.epochs, key=itemgetter('loss'))
            results = sorted_epochs[0]
            self.print_epoch_details(results, self.epochs_limit(), self.print_json)
        else:
            # This is printed when Ctrl+C is pressed quickly, before first epochs have
            # a chance to be evaluated.
            print("No epochs evaluated yet, no best result.")

    def __getstate__(self):
        state = self.__dict__.copy()
        del state['trials']
        return state
