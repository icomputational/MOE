# -*- coding: utf-8 -*-
r"""Tools to compute log likelihood-like measures of model fit and optimize them (wrt the hyperparameters of covariance) to select the best model for a given set of historical data.

See the file comments in interfaces/log_likelihood_interface.py for an overview of log likelihood-like metrics and their role
in model selection. This file provides hooks to implementations of two such metrics in C++: Log Marginal Likelihood and
Leave One Out Cross Validation Log Pseudo-Likelihood.

.. Note: This is a copy of the file comments in gpp_model_selection_and_hyperparameter_optimization.hpp.
  See this file's comments and interfaces.log_likelihood_interface for more details as well as the hpp and corresponding .cpp file.

a) LOG MARGINAL LIKELIHOOD (LML):
(Rasmussen & Williams, 5.4.1)
The Log Marginal Likelihood measure comes from the ideas of Bayesian model selection, which use Bayesian inference
to predict distributions over models and their parameters.  The cpp file comments explore this idea in more depth.
For now, we will simply state the relevant result.  We can build up the notion of the "marginal likelihood":
probability(observed data GIVEN sampling points (``X``), model hyperparameters, model class (regression, GP, etc.)),
which is denoted: ``p(y|X,\theta,H_i)`` (see the cpp file comments for more).

So the marginal likelihood deals with computing the probability that the observed data was generated from (another
way: is easily explainable by) the given model.

The marginal likelihood is in part paramaterized by the model's hyperparameters; e.g., as mentioned above.  Thus
we can search for the set of hyperparameters that produces the best marginal likelihood and use them in our model.
Additionally, a nice property of the marginal likelihood optimization is that it automatically trades off between
model complexity and data fit, producing a model that is reasonably simple while still explaining the data reasonably
well.  See the cpp file comments for more discussion of how/why this works.

In general, we do not want a model with perfect fit and high complexity, since this implies overfit to input noise.
We also do not want a model with very low complexity and poor data fit: here we are washing the signal out with
(assumed) noise, so the model is simple but it provides no insight on the data.

This is not magic.  Using GPs as an example, if the covariance function is completely mis-specified, we can blindly
go through with marginal likelihood optimization, obtain an "optimal" set of hyperparameters, and proceed... never
realizing that our fundamental assumptions are wrong.  So care is always needed.

b) LEAVE ONE OUT CROSS VALIDATION (LOO-CV):
(Rasmussen & Williams, Chp 5.4.2)
In cross validation, we split the training data, X, into two sets--a sub-training set and a validation set.  Then we
train a model on the sub-training set and test it on the validation set.  Since the validation set comes from the
original training data, we can compute the error.  In effect we are examining how well the model explains itself.

Leave One Out CV works by considering n different validation sets, one at a time.  Each point of X takes a turn
being the sole member of the validation set.  Then for each validation set, we compute a log pseudo-likelihood, measuring
how probable that validation set is given the remaining training data and model hyperparameters.

Again, we can maximize this quanitity over hyperparameters to help us choose the "right" set for the GP.

"""
import copy
import numpy

import moe.build.GPP as C_GP
import moe.optimal_learning.EPI.src.python.cpp_wrappers.cpp_utils as cpp_utils
import moe.optimal_learning.EPI.src.python.geometry_utils as geometry_utils

from moe.optimal_learning.EPI.src.python.interfaces.log_likelihood_interface import LogLikelihoodInterface
from moe.optimal_learning.EPI.src.python.interfaces.optimization_interface import OptimizableInterface


def multistart_hyperparameter_optimization(log_likelihood_evaluator, hyperparameter_optimization_parameters, hyperparameter_domain=None, randomness=None, max_num_threads=1, status=None):
    r"""Select the hyperparameters that maximize the specified log likelihood measure of model fit (over the historical data) within the specified domain.

    See LogMarginalLikelihood and LeaveOneOutLogLikelihood for an overview of some example log likelihood-like measures.

    Optimizers are: null ('dumb' search), gradient descent, newton
    Newton is the suggested optimizer.

    'dumb' search means this will just evaluate the objective log likelihood measure at num_multistarts 'points'
    (hyperparameters) in the domain, uniformly sampled using latin hypercube sampling.
    The hyperparameter_optimization_parameters input specifies the desired optimization technique as well as parameters controlling
    its behavior (see cpp_wrappers.optimization_parameters.py).

    See gpp_python_common.cpp for C++ enum declarations laying out the options for objective and optimizer types.

    Currently, during optimization, we recommend that the coordinates of the initial guesses not differ from the
    coordinates of the optima by more than about 1 order of magnitude. This is a very (VERY!) rough guideline for
    sizing the domain and gd_parameters.num_multistarts; i.e., be wary of sets of initial guesses that cover the space too sparsely.

    Note that the domain here must be specified in LOG-10 SPACE!

    Solution is guaranteed to lie within the region specified by "domain"; note that this may not be a
    true optima (i.e., the gradient may be substantially nonzero).

    .. WARNING:: this function fails if NO improvement can be found!  In that case,
       the output will always be the first randomly chosen point. status will report failure.

    :param log_likelihood_evaluator: object specifying which log likelihood measure to optimize
    :type log_likelihood_evaluator: cpp_wrappers.log_likelihood.LogLikelihood
    :param hyperparameter_optimization_parameters: object specifying the desired optimization method and parameters controlling its behavior (e.g., tolerance, iterations, etc.)
    :type hyperparameter_optimization_parameters: cpp_wrappers.optimization_parameters.HyperparameterOptimizationParameters
    :param hyperparameter_domain: log10-space [min, max] bounds for each hyperparameter (e.g., [-2, 1] means the hyperparameter is restricted to [0.01, 10])
    :type hyperparameter_domain: iterable of num_hyperparameters ClosedInterval
    :param randomness: RNGs used by C++ to generate initial guesses
    :type randomness: RandomnessSourceContainer (C++ object; e.g., from C_GP.RandomnessSourceContainer())
    :param max_num_threads: maximum number of threads to use, >= 1
    :type max_num_threads: int
    :param status: status messages from C++ (e.g., reporting on optimizer success, etc.)
    :type status: dict
    :return: hyperparameters that maximize the specified log likelihood measure within the specified domain
    :rtype: 1d array[log_likelihood_evaluator.num_hyperparameters] of double

    """
    # Guess "reasonable" hyperparameter constraints if none are given.
    if not hyperparameter_domain:
        hyperparameter_domain = []
        for i in range(log_likelihood_evaluator.num_hyperparameters):
            hyperparameter_domain.append(geometry_utils.ClosedInterval(-2.0, 1.0))

    # Create enough randomness sources if none are specified.
    if randomness is None:
        randomness = C_GP.RandomnessSourceContainer(max_num_threads) # create randomness for max_num_threads
        randomness.SetRandomizedUniformGeneratorSeed(0) # set seed based on less repeatable factors (e.g,. time)
        randomness.SetRandomizedNormalRNGSeed(0) # set seed baesd on thread id & less repeatable factors (e.g,. time)

    # status must be an initialized dict for the call to C++.
    if status is None:
        status = {}

    hyperparameter_optimization_parameters.objective_type = log_likelihood_evaluator._log_likelihood_type
    hyperparameters_opt = C_GP.multistart_hyperparameter_optimization(
        hyperparameter_optimization_parameters, # HyperparameterOptimizationParameters object (see cpp_wrappers.optimization_parameters)
        cpp_utils.cppify(hyperparameter_domain), # domain of hyperparameters in LOG-10 SPACE
        cpp_utils.cppify(log_likelihood_evaluator._historical_data.points_sampled), # points already sampled
        cpp_utils.cppify(log_likelihood_evaluator._historical_data.points_sampled_value), # objective value at each sampled point
        log_likelihood_evaluator._historical_data.dim,
        log_likelihood_evaluator._historical_data.num_sampled,
        cpp_utils.cppify_hyperparameters(log_likelihood_evaluator._covariance.get_hyperparameters()), # hyperparameters, e.g., [signal variance, [length scales]]; see cppify_hyperparameter docs, C++ python interface docs
        cpp_utils.cppify(log_likelihood_evaluator._historical_data.noise_variance), # noise variance, one value per sampled point
        max_num_threads,
        randomness, # C++ RandomnessSourceContainer that holds a UniformRandomGenerator object
        status,
    )
    return numpy.array(hyperparameters_opt)


def evaluate_log_likelihood_at_hyperparameter_list(log_likelihood_evaluator, hyperparameters_to_evaluate, max_num_threads=1):
    """Compute the specified log likelihood measure at each input set of hyperparameters.

    Generally gradient descent is preferred but when they fail to converge this may be the only "robust" option.
    This function is also useful for plotting or debugging purposes (just to get a bunch of log likelihood values).

    Calls into evaluate_log_likelihood_at_hyperparameter_list() in src/cpp/GPP_python_model_selection.cpp.

    :param log_likelihood_evaluator: object specifying which log likelihood measure to evaluate
    :type log_likelihood_evaluator: cpp_wrappers.log_likelihood.LogLikelihood
    :param hyperparameters_to_evaluate: the hyperparameters at which to compute the specified log likelihood
    :type hyperparameters_to_evaluate: 2d array[num_to_eval][log_likelihood_evaluator.num_hyperparameters] of double
    :param max_num_threads: maximum number of threads to use, >= 1
    :type max_num_threads: int
    :return: log likelihood value at each specified set of hyperparameters
    :rtype: 1d array[hyperparameters_to_evaluate.shape[0]] of double

    """
    # We could just call log_likelihood_evaluator.compute_log_likelihood() in a loop, but instead we do
    # the looping in C++ where it can be multithreaded.
    log_likelihood_list = C_GP.evaluate_log_likelihood_at_hyperparameter_list(
        cpp_utils.cppify(hyperparameters_to_evaluate), # hyperparameters at which to compute log likelihood
        cpp_utils.cppify(log_likelihood_evaluator._historical_data.points_sampled), # points already sampled
        cpp_utils.cppify(log_likelihood_evaluator._historical_data.points_sampled_value), # objective value at each sampled point
        log_likelihood_evaluator._historical_data.dim,
        log_likelihood_evaluator._historical_data.num_sampled,
        log_likelihood_evaluator._log_likelihood_type, # log likelihood measure to eval (e.g., LogLikelihoodTypes.log_marginal_likelihood, see gpp_python_common.cpp for enum declaration)
        cpp_utils.cppify_hyperparameters(log_likelihood_evaluator._covariance.get_hyperparameters()), # hyperparameters, e.g., [signal variance, [length scales]]; see cppify_hyperparameter docs, C++ python interface docs
        cpp_utils.cppify(log_likelihood_evaluator._historical_data.noise_variance), # noise variance, one value per sampled point
        hyperparameters_to_evaluate.shape[0], # number of hyperparameter points to evaluate
        max_num_threads,
    )
    return numpy.array(log_likelihood_list)


class LogLikelihood(LogLikelihoodInterface, OptimizableInterface):

    r"""Class for computing log likelihood-like measures of model fit via C++ wrappers (currently log marginal and leave one out cross validation).

    See LogMarginalLikelihood and LeaveOneOutLogLikelihood classes below for some more details on these metrics. Users may find
    it more convenient to construct these objects instead of a LogLikelihood object directly. Since these various metrics
    are fairly different, the member function docs in this class will remain generic.

    See gpp_model_selection_and_hyperparameter_optimization.hpp/cpp for further overview and in-depth discussion, respectively.

    """

    def __init__(self, covariance_function, historical_data, log_likelihood_type=C_GP.LogLikelihoodTypes.log_marginal_likelihood):
        """Construct a LogLikelihood object that knows how to call C++ for evaluation of member functions.

        :param covariance_function: covariance object encoding assumptions about the GP's behavior on our data
        :type covariance_function: Covariance object exposing hyperparameters (e.g., from cpp_wrappers.covariance)
        :param historical_data: object specifying the already-sampled points, the objective value at those points, and the noise variance associated with each observation
        :type historical_data: HistoricalData object
        :param log_likelihood_type: enum specifying which log likelihood measure to compute
        :type log_likelihood_type: GPP.LogLikelihoodTypes

        """
        self._covariance = copy.deepcopy(covariance_function)
        self._historical_data = copy.deepcopy(historical_data)

        self._log_likelihood_type = log_likelihood_type

    @property
    def dim(self):
        """Return the number of spatial dimensions."""
        return self._historical_data.dim

    @property
    def num_hyperparameters(self):
        """Return the number of hyperparameters."""
        return self._covariance.num_hyperparameters

    @property
    def problem_size(self):
        """Return the number of independent parameters to optimize."""
        return self.num_hyperparameters

    def compute_log_likelihood(self, hyperparameters):
        r"""Compute the _log_likelihood_type measure at the specified hyperparameters.

        :param hyperparameters: hyperparameters with which to compute the desired log likelihood measure
        :type current_point: 1d array[num_hyperparameters] of double
        :return: value of log_likelihood evaluated at hyperparameters (``LL(y | X, \theta)``)
        :rtype: double

        """
        self._covariance.set_hyperparameters(hyperparameters)
        return C_GP.compute_log_likelihood(
            cpp_utils.cppify(self._historical_data.points_sampled), # points already sampled
            cpp_utils.cppify(self._historical_data.points_sampled_value), # objective value at each sampled point
            self.dim,
            self._historical_data.num_sampled,
            self._log_likelihood_type, # log likelihood measure to eval (e.g., LogLikelihoodTypes.log_marginal_likelihood, see gpp_python_common.cpp for enum declaration)
            cpp_utils.cppify_hyperparameters(hyperparameters), # hyperparameters, e.g., [signal variance, [length scales]]; see _cppify_hyperparameter docs, C++ python interface docs
            cpp_utils.cppify(self._historical_data.noise_variance), # noise variance, one value per sampled point
        )

    def compute_objective_function(self, current_point):
        """Wrapper for compute_log_likelihood; see that function's docstring."""
        return self.compute_log_likelihood(current_point)

    def compute_grad_log_likelihood(self, hyperparameters):
        r"""Compute the gradient (wrt hyperparameters) of the _log_likelihood_type measure at the specified hyperparameters.

        :param hyperparameters: hyperparameters with which to compute the desired log likelihood measure
        :type hyperparameters: 1d array[num_hyperparameters] of double
        :return: grad_log_likelihood: i-th entry is ``\pderiv{LL(y | X, \theta)}{\theta_i}``
        :rtype: 1d array[num_hyperparameters] of double

        """
        self._covariance.set_hyperparameters(hyperparameters)
        grad_log_marginal = C_GP.compute_hyperparameter_grad_log_likelihood(
            cpp_utils.cppify(self._historical_data.points_sampled), # points already sampled
            cpp_utils.cppify(self._historical_data.points_sampled_value), # objective value at each sampled point
            self.dim,
            self._historical_data.num_sampled,
            self._log_likelihood_type, # log likelihood measure to eval (e.g., LogLikelihoodTypes.log_marginal_likelihood, see gpp_python_common.cpp for enum declaration)
            cpp_utils.cppify_hyperparameters(hyperparameters), # hyperparameters, e.g., [signal variance, [length scales]]; see _cppify_hyperparameter docs, C++ python interface docs
            cpp_utils.cppify(self._historical_data.noise_variance), # noise variance, one value per sampled point
        )
        return numpy.array(grad_log_marginal)

    def compute_grad_objective_function(self, current_point):
        """Wrapper for compute_grad_log_likelihood; see that function's docstring."""
        return self.compute_grad_log_likelihood(current_point)

    def compute_hessian_log_likelihood(self, hyperparameters):
        """We do not currently support computation of the (hyperparameter) hessian of log likelihood-like metrics."""
        raise NotImplementedError('Currently C++ does not expose Hessian computation of log likelihood-like metrics.')

    def compute_hessian_objective_function(self, current_point):
        """Wrapper for compute_hessian_log_likelihood; see that function's docstring."""
        return self.compute_hessian_log_likelihood(current_point)


class LogMarginalLikelihood(LogLikelihood):

    r"""Class for computing the Log Marginal Likelihood, ``log(p(y | X, \theta))``.

    That is, the probability of observing the training values, y, given the training points, X,
    and hyperparameters (of the covariance function), ``\theta``.

    This is a measure of how likely it is that the observed values came from our Gaussian Process Prior.

    .. Note: This is a copy of LogMarginalLikelihoodEvaluator's class comments in gpp_model_selection_and_hyperparameter_optimization.hpp.
      See this file's comments and interfaces.log_likelihood_interface for more details as well as the hpp and corresponding .cpp file.

    Given a particular covariance function (including hyperparameters) and
    training data ((point, function value, measurement noise) tuples), the log marginal likelihood is the log probability that
    the data were observed from a Gaussian Process would have generated the observed function values at the given measurement
    points.  So log marginal likelihood tells us "the probability of the observations given the assumptions of the model."
    Log marginal sits well with the Bayesian Inference camp.
    (Rasmussen & Williams p118)

    This quantity primarily deals with the trade-off between model fit and model complexity.  Handling this trade-off is automatic
    in the log marginal likelihood calculation.  See Rasmussen & Williams 5.2 and 5.4.1 for more details.

    We can use the log marginal likelihood to determine how good our model is.  Additionally, we can maximize it by varying
    hyperparameters (or even changing covariance functions) to improve our model quality.  Hence this class provides access
    to functions for computing log marginal likelihood and its hyperparameter gradients.

    """

    def __init__(self, covariance_function, historical_data):
        """Construct a LogLikelihood object configured for Log Marginal Likelihood computation; see superclass ctor for details."""
        super(LogMarginalLikelihood, self).__init__(covariance_function, historical_data, log_likelihood_type=C_GP.LogLikelihoodTypes.log_marginal_likelihood)


class LeaveOneOutLogLikelihood(LogLikelihood):

    r"""Class for computing the Leave-One-Out Cross Validation (LOO-CV) Log Pseudo-Likelihood.

    Given a particular covariance function (including hyperparameters) and training data ((point, function value, measurement noise)
    tuples), the log LOO-CV pseudo-likelihood expresses how well the model explains itself.

    .. Note: This is a copy of LeaveOneOutLogLikelihoodEvaluator's class comments in gpp_model_selection_and_hyperparameter_optimization.hpp.
      See this file's comments and interfaces.log_likelihood_interface for more details as well as the hpp and corresponding .cpp file.

    That is, cross validation involves splitting the training set into a sub-training set and a validation set.  Then we measure
    the log likelihood that a model built on the sub-training set could produce the values in the validation set.

    Leave-One-Out CV does this process ``|y|`` times: on the i-th run, the sub-training set is (X,y) with the i-th point removed
    and the validation set is the i-th point.  Then the predictive performance of each sub-model are aggregated into a
    psuedo-likelihood.

    This quantity primarily deals with the internal consistency of the model--how well it explains itself.  The LOO-CV
    likelihood gives an "estimate for the predictive probability, whether or not the assumptions of the model may be
    fulfilled." It is a more frequentist view of model selection. (Rasmussen & Williams p118)
    See Rasmussen & Williams 5.3 and 5.4.2 for more details.

    As with the log marginal likelihood, we can use this quantity to measure the performance of our model.  We can also
    maximize it (via hyperparameter modifications or covariance function changes) to improve model performance.
    It has also been argued that LOO-CV is better at detecting model mis-specification (e.g., wrong covariance function)
    than log marginal measures (Rasmussen & Williams p118).

    """

    def __init__(self, covariance_function, historical_data):
        """Construct a LogLikelihood object configured for Leave One Out Cross Validation Log Pseudo-Likelihood computation; see superclass ctor for details."""
        super(LeaveOneOutLogLikelihood, self).__init__(covariance_function, historical_data, log_likelihood_type=C_GP.LogLikelihoodTypes.leave_one_out_log_likelihood)

    def compute_hessian_log_likelihood(self, hyperparameters):
        """The (hyperparameter) hessian of LOO-CV has not been implemented in C++ yet."""
        raise NotImplementedError('Currently C++ does not support Hessian computation of LOO-CV.')