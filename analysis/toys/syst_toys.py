#!/usr/bin/env python
# -*- coding: utf-8 -*-
# =============================================================================
# @file   syst_toys.py
# @author Albert Puig (albert.puig@cern.ch)
# @date   24.10.2017
# =============================================================================
"""Toys for handling systematics.

These generate and fit in one go with some random variation of one or more parameters.

"""
from __future__ import print_function, division, absolute_import

import argparse
import os
from timeit import default_timer
import copy

import pandas as pd
import numpy as np

import ROOT

from analysis.utils.logging_color import get_logger
from analysis.utils.monitoring import memory_usage
from analysis.utils.random import get_urandom_int
from analysis.data.hdf import modify_hdf
from analysis.physics import configure_model
from analysis.efficiency import get_acceptance
from analysis.fit import fit
from analysis.fit.result import FitResult
from analysis.batch import get_job_id
from analysis.toys import get_randomizer
import analysis.utils.paths as _paths
import analysis.utils.config as _config
import analysis.utils.root as _root

logger = get_logger('analysis.toys.syst')


def run(config_files, link_from, verbose):
    """Run the script.

    Run a generate/fit sequence as many times as requested.

    Arguments:
        config_files (list[str]): Path to the configuration files.
        link_from (str): Path to link the results from.
        verbose (bool): Give verbose output?

    Raise:
        OSError: If the configuration file or some other input does not exist.
        AttributeError: If the input data are incompatible with a previous fit.
        KeyError: If some configuration data are missing.
        ValueError: If there is any problem in configuring the PDF factories.
        RuntimeError: If there is a problem during the fitting.

    """
    try:
        config = _config.load_config(*config_files,
                                     validate=['fit/nfits',
                                               'name',
                                               'randomizer'])
    except OSError:
        raise OSError("Cannot load configuration files: {}".format(config_files))
    except _config.ConfigError as error:
        if 'fit/nfits' in error.missing_keys:
            logger.error("Number of fits not specified")
        if 'name' in error.missing_keys:
            logger.error("No name was specified in the config file!")
        if 'randomizer' in error.missing_keys:
            logger.error("No randomizer configuration specified in config file!")
        raise KeyError("ConfigError raised -> {}".format(error.missing_keys))
    except KeyError as error:
        logger.error("YAML parsing error -> %s", error)
    model_name = config['fit'].get('model', 'model')  # TODO: 'model' returns name?
    try:
        model_config = config[model_name]
    except KeyError as error:
        logger.error("Missing model configuration -> %s", str(error))
        raise KeyError("Missing model configuration")
    # Load fit model
    try:
        fit_model = configure_model(copy.deepcopy(model_config))
        randomizer_model = configure_model(copy.deepcopy(model_config))
    except KeyError:
        logger.exception('Error loading model')
        raise ValueError('Error loading model')
    # Some info
    nfits = config['fit'].get('nfits-per-job', config['fit']['nfits'])
    logger.info("Doing %s generate/fit sequences", nfits)
    logger.info("Fit job name: %s", config['name'])
    if link_from:
        config['link-from'] = link_from
    if 'link-from' in config:
        logger.info("Linking toy data from %s", config['link-from'])
    else:
        logger.debug("No linking specified")
    # Now load the acceptance
    try:
        acceptance = get_acceptance(config['acceptance']) \
            if 'acceptance' in config \
            else None
    except _config.ConfigError as error:
        raise KeyError("Error loading acceptance -> {}".format(error))
    # Fit strategy
    fit_strategy = config['fit'].get('strategy', 'simple')
    # Load randomizer configuration
    randomizer = get_randomizer(config['syst'])(model=randomizer_model,
                                                config=config['syst'],
                                                acceptance=acceptance)
    # Set seed
    job_id = get_job_id()
    # Start looping
    fit_results = {}
    logger.info("Starting sampling-fit loop (print frequency is 20)")
    initial_mem = memory_usage()
    initial_time = default_timer()
    do_extended = config['fit'].get('extended', False)
    do_minos = config['fit'].get('minos', False)
    for fit_num in range(nfits):
        # Logging
        if (fit_num+1) % 20 == 0:
            logger.info("  Fitting event %s/%s", fit_num+1, nfits)
        # Generate a dataset
        seed = get_urandom_int(4)
        np.random.seed(seed=seed)
        ROOT.RooRandom.randomGenerator().SetSeed(seed)
        try:
            # Get a randomized dataset and fit it with the nominal fit
            dataset = randomizer.get_dataset(randomize=True)
            gen_values = randomizer.get_current_values()
            fit_result_nominal = fit(fit_model,
                                     model_name,
                                     fit_strategy,
                                     dataset,
                                     verbose,
                                     Extended=do_extended,
                                     Minos=do_minos)
            # Fit the randomized dataset with the randomized values as nominal
            fit_result_rand = fit(randomizer_model,
                                  model_name,
                                  fit_strategy,
                                  dataset,
                                  verbose,
                                  Extended=do_extended,
                                  Minos=do_minos)
            # randomizer.reset_values()  # Not really needed, but cleaner
        except ValueError:
            raise RuntimeError()
        except Exception:
            # logger.exception()
            raise RuntimeError()  # TODO: provide more information?
        result = {}
        result['fitnum'] = fit_num
        result['seed'] = seed
        # Save the results of the randomized fit
        result_roofit_rand = FitResult.from_roofit(fit_result_rand)
        result['param_names'] = result_roofit_rand.get_fit_parameters().keys()
        result['rand'] = result_roofit_rand.to_plain_dict(skip_cov=False)
        _root.destruct_object(fit_result_rand)
        # Save the results of the nominal fit
        result_roofit_nominal = FitResult.from_roofit(fit_result_nominal)
        result['nominal'] = result_roofit_nominal.to_plain_dict(skip_cov=False)
        result['gen'] = gen_values
        _root.destruct_object(result_roofit_nominal)
        _root.destruct_object(dataset)
        fit_results[fit_num] = result
        logger.debug("Cleaning up")
    logger.info("Fitting loop over")
    logger.info("--> Memory leakage: %.2f MB/sample-fit", (memory_usage() - initial_mem)/nfits)
    logger.info("--> Spent %.0f ms/sample-fit", (default_timer() - initial_time)*1000.0/nfits)
    logger.info("Saving to disk")
    data_res = []
    cov_matrices = {}
    # Get covariance matrices
    for fit_num, fit_res in fit_results.items():
        fit_res = {'fitnum': fit_res['fit_num'],
                   'seed': fit_res['seed'],
                   'model_name': model_name,
                   'fit_strategy': fit_strategy}
        param_names = fit_res.pop('param_names')
        cov_folder_rand = os.path.join(str(job_id), str(fit_res['fitnum']), 'rand')
        cov_matrices[cov_folder_rand] = pd.DataFrame(fit_res['rand'].pop('cov_matrix'),
                                                     index=param_names,
                                                     columns=param_names)
        cov_folder_nominal = os.path.join(str(job_id), str(fit_res['fitnum']), 'nominal')
        cov_matrices[cov_folder_nominal] = pd.DataFrame(fit_res['nominal'].pop('cov_matrix'),
                                                        index=param_names,
                                                        columns=param_names)
        for res_name, res_value in result['rand'].items():
            fit_res['{}_rand' % res_name] = res_value
        for res_name, res_value in result['nominal'].items():
            fit_res['{}_nominal' % res_name] = res_value
        for res_name, res_value in result['gen'].items():
            fit_res['{}_gen' % res_name] = res_value
        data_res.append(fit_res)
    data_frame = pd.DataFrame(data_res)
    fit_result_frame = pd.concat([data_frame,
                                  pd.concat([pd.DataFrame({'jobid': [job_id]})]
                                            * data_frame.shape[0]).reset_index(drop=True)],
                                 axis=1)
    try:
        # pylint: disable=E1101
        with _paths.work_on_file(config['name'],
                                 path_func=_paths.get_toy_fit_path,
                                 link_from=config.get('link-from', None)) as toy_fit_file:
            with modify_hdf(toy_fit_file) as hdf_file:
                # First fit results
                hdf_file.append('fit_results', fit_result_frame)
                # Save covarinance matrix under 'covariance/jobid/fitnum
                for cov_folder, cov_matrix in cov_matrices.items():
                    cov_path = os.path.join('covariance', cov_folder)
                    hdf_file.append(cov_path, cov_matrix)
                # Generator info
                hdf_file.append('input_values', pd.DataFrame(randomizer.get_input_values()))

            logger.info("Written output to %s", toy_fit_file)
            if 'link-from' in config:
                logger.info("Linked to %s", config['link-from'])
    except OSError as excp:
        logger.error(str(excp))
        raise
    except ValueError as error:
        logger.exception("Exception on dataset saving")
        raise RuntimeError(str(error))


def main():
    """Toy systematic fitting application.

    Parses the command line and fits the toys, catching intermediate
    errors and transforming them to status codes.

    Status codes:
        0: All good.
        1: Error in the configuration files.
        2: Files missing (configuration or toys).
        3: Error configuring physics factories.
        4: Error in the event generation. An exception is logged.
        5: Input data is inconsistent with previous fits.
        128: Uncaught error. An exception is logged.

    """
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose',
                        action='store_true',
                        help="Verbose output")
    parser.add_argument('--link-from',
                        action='store', type=str, default='',
                        help="Folder to actually store the fit results")
    parser.add_argument('config',
                        action='store', type=str, nargs='+',
                        help="Configuration files")
    args = parser.parse_args()
    if args.verbose:
        get_logger('analysis').setLevel(1)
        logger.setLevel(1)
    else:
        ROOT.RooMsgService.instance().setGlobalKillBelow(ROOT.RooFit.WARNING)
    try:
        exit_status = 0
        run(args.config, args.link_from, args.verbose)
    except KeyError:
        exit_status = 1
        logger.exception("Bad configuration given")
    except OSError as error:
        exit_status = 2
        logger.error(str(error))
    except ValueError:
        exit_status = 3
        logger.exception("Problem configuring physics factories")
    except RuntimeError as error:
        exit_status = 4
        logger.error("Error in fitting events")
    except AttributeError as error:
        exit_status = 5
        logger.error("Inconsistent input data -> %s", error)
    # pylint: disable=W0703
    except Exception as error:
        exit_status = 128
        logger.exception('Uncaught exception -> %s', repr(error))
    finally:
        parser.exit(exit_status)


if __name__ == "__main__":
    main()

# EOF
