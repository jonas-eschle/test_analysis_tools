#!/usr/bin/env python
# -*- coding: utf-8 -*-
# =============================================================================
# @file   result.py
# @author Albert Puig (albert.puig@cern.ch)
# @date   19.05.2017
# =============================================================================
"""Analyze and store fit results."""
from __future__ import print_function, division, absolute_import

from collections import OrderedDict
import copy

import numpy as np

from analysis.utils.config import load_config, write_config, ConfigError
from analysis.utils.root import iterate_roocollection
from analysis.utils.paths import get_fit_result_path

_SUFFIXES = ('', '_err_hesse', '_err_plus', '_err_minus')


def ensure_initialized(method):
    """Make sure the fit result is initialized."""

    def wrapper(self, *args, **kwargs):
        """Check result is empty. Raise otherwise."""
        if not self.get_result():
            raise NotInitializedError("Trying to export a non-initialized fit result")
        return method(self, *args, **kwargs)

    return wrapper


def ensure_non_initialized(method):
    """Make sure the fit result is not initialized."""

    def wrapper(self, *args, **kwargs):
        """Check result is non empty. Raise otherwise."""
        if self.get_result():
            raise AlreadyInitializedError("Trying to overwrite an initialized fit result")
        return method(self, *args, **kwargs)

    return wrapper


class FitResult(object):
    """Manager for fit results.

    Transforms `RooFitResult`s into something easier to manage and
    allows to save them into YAML format.

    """

    def __init__(self):
        """Initialize internal variables."""
        self._result = None

    def get_result(self):
        """Get the full fit result information.

        Return:
            dict: Full fit result information.

        """
        return self._result

    @ensure_non_initialized
    def from_roofit(self, roofit_result):
        """Load the `RooFitResult` into the internal format.

        Arguments:
            roofit_result (`ROOT.RooFitResult`): Fit result.

        Return:
            self

        Raise:
            AlreadyInitializedError: If the FitResult had already been initialized.

        """
        result = {}
        # Fit parameters
        result['const-parameters'] = OrderedDict((fit_par.GetName(), fit_par.getVal())
                                                 for fit_par
                                                 in iterate_roocollection(roofit_result.constPars()))
        result['fit-parameters'] = OrderedDict((fit_par.GetName(), (fit_par.getVal(),
                                                                    fit_par.getError(),
                                                                    fit_par.getErrorLo(),
                                                                    fit_par.getErrorHi()))
                                               for fit_par
                                               in iterate_roocollection(roofit_result.floatParsFinal()))
        result['fit-parameters-initial'] = OrderedDict((fit_par.GetName(), fit_par.getVal())
                                                       for fit_par
                                                       in iterate_roocollection(roofit_result.floatParsInit()))
        # Covariance matrix
        covariance_matrix = roofit_result.covarianceMatrix()
        cov_matrix = {'quality': roofit_result.covQual(),
                      'matrix': np.matrix([[covariance_matrix[row][col]
                                            for col in range(covariance_matrix.GetNcols())]
                                           for row in range(covariance_matrix.GetNrows())])}
        result['covariance-matrix'] = cov_matrix
        # Status
        result['status'] = OrderedDict((roofit_result.statusLabelHistory(cycle), roofit_result.statusCodeHistory(cycle))
                                       for cycle in range(roofit_result.numStatusHistory()))
        result['edm'] = roofit_result.edm()
        self._result = result
        return self

    @ensure_non_initialized
    def from_yaml(self, yaml_dict):
        """Initialize from a YAML dictionary.

        Arguments:
            yaml_dict (dict, OrderedDict): YAML information to load.

        Return:
            self

        Raise:
            KeyError: If any of the FitResult data is missing from the YAML dictionary.
            AlreadyInitializedError: If the FitResult had already been initialized.

        """
        if not set(yaml_dict.keys()).issuperset({'fit-parameters',
                                                 'fit-parameters-initial',
                                                 'const-parameters',
                                                 'covariance-matrix',
                                                 'status'}):
            raise KeyError("Missing keys in YAML input")
        if not set(yaml_dict['covariance-matrix'].keys()).issuperset({'quality', 'matrix'}):
            raise KeyError("Missing keys in covariance matrix in YAML input")
        # Build matrix
        yaml_dict['covariance-matrix']['matrix'] = np.asmatrix(
            np.array(yaml_dict['covariance-matrix']['matrix']).reshape(len(yaml_dict['fit-parameters']),
                                                                       len(yaml_dict['fit-parameters'])))
        self._result = yaml_dict
        return self

    @ensure_non_initialized
    def from_yaml_file(self, name):
        """Initialize from a YAML file.

        File name is determined by get_fit_result_path.

        Arguments:
            name (str): Name of the fit result.

        Return:
            self

        Raise:
            OSError: If the file cannot be found.
            KeyError: If any of the FitResult data is missing from the input file.
            AlreadyInitializedError: If the FitResult had already been initialized.

        """
        try:
            self._result = dict(load_config(get_fit_result_path(name),
                                            validate=('fit-parameters',
                                                      'fit-parameters-initial',
                                                      'const-parameters',
                                                      'covariance-matrix/quality',
                                                      'covariance-matrix/matrix',
                                                      'status')))
        except ConfigError as error:
            raise KeyError("Missing keys in input file -> {}".format(','.join(error.missing_keys)))
        return self

    @ensure_non_initialized
    def from_hdf(self, name):  # TODO: which path func?
        """Initialize from a hdf file.

        Arguments:
            name (str):

        Return:
            self

        """

        return self

    @ensure_initialized
    def to_yaml(self):
        """Convert fit result to YAML format.

        Return:
            str: Output dictionary in YAML format.

        Raise:
            NotInitializedError: If the fit result has not been initialized.

        """
        result = copy.deepcopy(self._result)
        result['covariance-matrix']['matrix'] = self._result['covariance-matrix']['matrix'].getA1()
        return result

    @ensure_initialized
    def to_yaml_file(self, name):
        """Convert fit result to YAML format.

        File name is determined by get_fit_result_path.

        Arguments:
            name (str): Name of the fit result.

        Return:
            str: Output file name.

        Raise:
            NotInitializedError: If the fit result has not been initialized.

        """
        file_name = get_fit_result_path(name)
        write_config(self.to_yaml(), file_name)
        return file_name

    @ensure_initialized
    def to_plain_dict(self, skip_cov=True):
        """Convert fit result into a pandas-friendly format.

        Blablabla

        Arguments:
            skip_cov (bool, optional): Skip the covariance matrix. Defaults to True.

        Return:
            pandas.DataFrame

        """
        pandas_dict = OrderedDict(((param_name + suffix, val)
                                   for param_name, param in self._result['fit-parameters'].items()
                                   for val, suffix in zip(param, _SUFFIXES)))
        pandas_dict.update(OrderedDict((param_name, val) for param_name, val
                            in self._result['const-parameters'].items()))
        pandas_dict['status_migrad'] = self._result['status'].get('MIGRAD', -1)
        pandas_dict['status_hesse'] = self._result['status'].get('HESSE', -1)
        pandas_dict['status_minos'] = self._result['status'].get('MINOS', -1)
        pandas_dict['cov_quality'] = self._result['covariance-matrix']['quality']
        pandas_dict['edm'] = self._result['edm']
        if not skip_cov:
            pandas_dict['cov_matrix'] = self._result['covariance-matrix']['matrix'].getA1()
        return pandas_dict

    @ensure_initialized
    def get_fit_parameter(self, name):
        """Get the fit parameter and its errors.

        Arguments:
            name (str): Name of the fit parameter.

        Return:
            tuple (float): Parameter value, Hesse error and upper and lower Minos errors.
                If the two latter have not been calculated, they are 0.

        Raise:
            KeyError: If the parameter is unknown.

        """
        return self._result['fit-parameters'][name]

    @ensure_initialized
    def get_const_parameter(self, name):
        """Get the const parameter.

        Arguments:
            name (str): Name of the fit parameter.

        Return:
            float: Parameter value.

        Raise:
            KeyError: If the parameter is unknown.

        """
        return self._result['const-parameters'][name]

    @ensure_initialized
    def get_fit_parameters(self):
        """Get the full list of fit parameters.

        Return:
            OrderedDict: Parameters as keys and their values and errors as values.

        """
        return self._result['fit-parameters']

    @ensure_initialized
    def get_covariance_matrix(self):
        """Get the fit covariance matrix.

        Return:
            `numpy.matrix`: Covariance matrix.

        Raise:
            NotInitializedError: If the FitResult has not been initialized.

        """
        return self._result['covariance-matrix']['matrix']

    @ensure_initialized
    def get_edm(self):
        """Get the fit EDM.

        Return:
            float

        """
        return self._result['edm']

    @ensure_initialized
    def has_converged(self):
        """Determine wether the fit has converged properly.

        All steps have to have converged and the covariance matrix quality needs to be
        good.

        """
        return not any(status for status in self._result['status'].values()) and \
               self._result['covariance-matrix']['quality'] == 3

    @ensure_initialized
    def generate_random_pars(self, include_const=False):
        """Generate random variation of the fit parameters.

        Use a multivariate Gaussian according to the covariance matrix.

        Arguments:
            include_const (bool, optional): Return constant parameters? Defaults to False.

        Return:
            OrderedDict

        """
        # pylint: disable=E1101
        output = OrderedDict(zip(self._result['fit-parameters'].keys(),
                                 np.random.multivariate_normal([param[0]
                                                                for param in self._result['fit-parameters'].values()],
                                                               self._result['covariance-matrix']['matrix'])))
        if include_const:
            for name, param in self._result['const-parameters'].items():
                output[name] = param
        return output


class AlreadyInitializedError(Exception):
    """Used when the internal fit result has already been initialized."""


class NotInitializedError(Exception):
    """Use when the FitResult has not been initialized."""

# EOF
