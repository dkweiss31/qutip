__all__ = ['nm_mcsolve', 'NonMarkovianMCSolver']

import functools
import numbers

import numpy as np
import scipy

from .multitraj import MultiTrajSolver
from .mcsolve import MCSolver, MCIntegrator
from .mesolve import MESolver, mesolve
from .result import NmmcResult, NmmcTrajectoryResult
from .cy.nm_mcsolve import RateShiftCoefficient, SqrtRealCoefficient
from ..core.coefficient import ConstantCoefficient
from ..core import (
    CoreOptions, Qobj, QobjEvo, isket, ket2dm, qeye, coefficient,
)


# The algorithm implemented here is based on the influence martingale approach
# described in
#     Nat Commun 13, 4140 (2022)
#     https://doi.org/10.1038/s41467-022-31533-8
#     https://arxiv.org/abs/2102.10355
# and
#     https://arxiv.org/abs/2209.08958


def nm_mcsolve(H, state, tlist, ops_and_rates=(), e_ops=None, ntraj=500, *,
               args=None, options=None, seeds=None, target_tol=None,
               timeout=None):
    """
    Monte-Carlo evolution corresponding to a Lindblad equation with "rates"
    that may be negative. Usage of this function is analogous to ``mcsolve``,
    but the ``c_ops`` parameter is replaced by an ``ops_and_rates`` parameter
    to allow for negative rates. Options for the underlying ODE solver are
    given by the Options class.

    Parameters
    ----------
    H : :class:`qutip.Qobj`, :class:`qutip.QobjEvo`, ``list``, callable.
        System Hamiltonian as a Qobj, QobjEvo. It can also be any input type
        that QobjEvo accepts (see :class:`qutip.QobjEvo`'s documentation).
        ``H`` can also be a superoperator (liouvillian) if some collapse
        operators are to be treated deterministically.

    state : :class:`qutip.Qobj`
        Initial state vector.

    tlist : array_like
        Times at which results are recorded.

    ops_and_rates : list
        A ``list`` of tuples ``(L, Gamma)``, where the Lindblad operator ``L``
        is a :class:`qutip.Qobj` and ``Gamma`` represents the corresponding
        rate, which is allowed to be negative. The Lindblad operators must be
        operators even if ``H`` is a superoperator. If none are given, the
        solver will defer to ``sesolve`` or ``mesolve``. Each rate ``Gamma``
        may be just a number (in the case of a constant rate) or, otherwise,
        specified using any format accepted by :func:`qutip.coefficient`.

    e_ops : list, [optional]
        A ``list`` of operator as Qobj, QobjEvo or callable with signature of
        (t, state: Qobj) for calculating expectation values. When no ``e_ops``
        are given, the solver will default to save the states.

    ntraj : int
        Maximum number of trajectories to run. Can be cut short if a time limit
        is passed with the ``timeout`` keyword or if the target tolerance is
        reached, see ``target_tol``.

    args : None / dict
        Arguments for time-dependent Hamiltonian and collapse operator terms.

    options : None / dict
        Dictionary of options for the solver.

        - store_final_state : bool, [False]
          Whether or not to store the final state of the evolution in the
          result class.
        - store_states : bool, NoneType, [None]
          Whether or not to store the state density matrices.
          On ``None`` the states will be saved if no expectation operators are
          given.
        - progress_bar : str {'text', 'enhanced', 'tqdm', ''}, ['text']
          How to present the solver progress.
          'tqdm' uses the python module of the same name and raise an error
          if not installed. Empty string or False will disable the bar.
        - progress_kwargs : dict, [{"chunk_size": 10}]
          kwargs to pass to the progress_bar. Qutip's bars use ``chunk_size``.
        - method : str {"adams", "bdf", "dop853", "vern9", etc.}, ["adams"]
          Which differential equation integration method to use.
        - keep_runs_results : bool, [False]
          Whether to store results from all trajectories or just store the
          averages.
        - map : str {"serial", "parallel", "loky"}, ["serial"]
          How to run the trajectories. "parallel" uses concurrent module to run
          in parallel while "loky" use the module of the same name to do so.
        - job_timeout : NoneType, int, [None]
          Maximum time to compute one trajectory.
        - num_cpus : NoneType, int, [None]
          Number of cpus to use when running in parallel. ``None`` detect the
          number of available cpus.
        - norm_t_tol, norm_tol, norm_steps : float, float, int, [1e-6, 1e-4, 5]
          Parameters used to find the collapse location. ``norm_t_tol`` and
          ``norm_tol`` are the tolerance in time and norm respectively.
          An error will be raised if the collapse could not be found within
          ``norm_steps`` tries.
        - mc_corr_eps : float, [1e-10]
          Small number used to detect non-physical collapse caused by numerical
          imprecision.
        - atol, rtol : float, [1e-8, 1e-6]
          Absolute and relative tolerance of the ODE integrator.
        - nsteps : int [2500]
          Maximum number of (internally defined) steps allowed in one ``tlist``
          step.
        - max_step : float, [0]
          Maximum length of one internal step. When using pulses, it should be
          less than half the width of the thinnest pulse.
        - completeness_rtol, completeness_atol : float, float, [1e-5, 1e-8]
          Parameters used in determining whether the given Lindblad operators
          satisfy a certain completeness relation. If they do not, an
          additional Lindblad operator is added automatically (with zero rate).
        - martingale_quad_limit : float or int, [100]
          An upper bound on the number of subintervals used in the adaptive
          integration of the martingale.

    seeds : int, SeedSequence, list, [optional]
        Seed for the random number generator. It can be a single seed used to
        spawn seeds for each trajectory or a list of seeds, one for each
        trajectory. Seeds are saved in the result and they can be reused with::

            seeds=prev_result.seeds

    target_tol : float, tuple, list, [optional]
        Target tolerance of the evolution. The evolution will compute
        trajectories until the error on the expectation values is lower than
        this tolerance. The maximum number of trajectories employed is
        given by ``ntraj``. The error is computed using jackknife resampling.
        ``target_tol`` can be an absolute tolerance or a pair of absolute and
        relative tolerance, in that order. Lastly, it can be a list of pairs of
        (atol, rtol) for each e_ops.

    timeout : float, [optional]
        Maximum time for the evolution in seconds. When reached, no more
        trajectories will be computed.

    Returns
    -------
    results : :class:`qutip.solver.NmmcResult`
        Object storing all results from the simulation. Compared to a result
        returned by ``mcsolve``, this result contains the additional field
        ``trace`` (and ``runs_trace`` if ``store_final_state`` is set). Note
        that the states on the individual trajectories are not normalized. This
        field contains the average of their trace, which will converge to one
        in the limit of sufficiently many trajectories.
    """
    H = QobjEvo(H, args=args, tlist=tlist)

    if len(ops_and_rates) == 0:
        if options is None:
            options = {}
        options = {
            key: options[key]
            for key in options
            if key in MESolver.solver_options
        }
        return mesolve(
            H, state, tlist, e_ops=e_ops, args=args, options=options,
        )

    ops_and_rates = [
        _parse_op_and_rate(op, rate, tlist=tlist, args=args or {})
        for op, rate in ops_and_rates
    ]

    nmmc = NonMarkovianMCSolver(H, ops_and_rates, options=options)
    result = nmmc.run(state, tlist=tlist, ntraj=ntraj, e_ops=e_ops,
                      seed=seeds, target_tol=target_tol, timeout=timeout)
    return result


def _parse_op_and_rate(op, rate, **kw):
    """ Sanity check the op and convert rates to coefficients. """
    if not isinstance(op, Qobj):
        raise ValueError("NonMarkovianMCSolver ops must be of type Qobj")
    if isinstance(rate, numbers.Number):
        rate = ConstantCoefficient(rate)
    else:
        rate = coefficient(rate, **kw)
    return op, rate


class InfluenceMartingale:
    def __init__(self, nm_solver, a_parameter, quad_limit):
        self._nm_solver = nm_solver
        self._quad_limit = quad_limit
        self._a_parameter = a_parameter
        self.reset()

    def reset(self):
        self._t_prev = None
        self._continuous_martingale_at_t_prev = None
        self._precomputed_continuous_martingale = {}
        self._discrete_martingale = None

    def initialize(self, t0, cache='clear'):
        # `cache` may be 'clear', 'keep' or a new list of times for which
        #  to pre-compute the continuous contribution to the martingale
        self._t_prev = t0
        self._continuous_martingale_at_t_prev = 1
        self._discrete_martingale = 1

        if np.array_equal(cache, 'clear'):
            self._precomputed_continuous_martingale = {}
            return
        if np.array_equal(cache, 'keep'):
            return

        self._precomputed_continuous_martingale = {}
        mu_c0 = 1
        for t1 in cache:
            mu_c1 = mu_c0 * self._compute_continuous_martingale(t0, t1)
            self._precomputed_continuous_martingale[t1] = mu_c1
            t0, mu_c0 = t1, mu_c1

    def add_collapse(self, collapse_time, collapse_channel):
        if self._t_prev is None:
            raise RuntimeError("The `start` method must called first.")

        rate = self._nm_solver.rate(collapse_time, collapse_channel)
        shift = self._nm_solver.rate_shift(collapse_time)
        factor = rate / (rate + shift)
        self._discrete_martingale *= factor

    def value(self, t):
        if self._t_prev is None:
            raise RuntimeError("The `start` method must called first.")

        # find value of continuous martingale at given time
        if t in self._precomputed_continuous_martingale:
            mu_c = self._precomputed_continuous_martingale[t]
        else:
            mu_c = (
                self._continuous_martingale_at_t_prev *
                self._compute_continuous_martingale(self._t_prev, t)
            )
        self._t_prev = t
        self._continuous_martingale_at_t_prev = mu_c

        return self._discrete_martingale * mu_c

    def _compute_continuous_martingale(self, t1, t2):
        if t1 == t2:
            return 1

        integral, _, *info = scipy.integrate.quad(
            self._nm_solver.rate_shift, t1, t2,
            limit=self._quad_limit,
            full_output=True,
        )
        if len(info) > 1:
            raise ValueError(
                f"Failed to integrate the continuous martingale: {info[1]}"
            )
        return np.exp(self._a_parameter * integral)


class NmMCIntegrator(MCIntegrator):
    def __init__(self, *args, **kwargs):
        self._martingale = kwargs.pop("__martingale")
        super().__init__(*args, **kwargs)

    def _do_collapse(self, *args, **kwargs):
        # _do_collapse might not append a new collapse, so we need to check
        # whether one was added before calculating the martingales.
        num_collapse_old = len(self.collapses)
        super()._do_collapse(*args, **kwargs)
        if len(self.collapses) > num_collapse_old:
            collapse_time, collapse_channel = self.collapses[-1]
            self._martingale.add_collapse(collapse_time, collapse_channel)

    def set_state(self, t, *args, **kwargs):
        super().set_state(t, *args, **kwargs)
        self._martingale.initialize(t, cache='keep')


class NonMarkovianMCSolver(MCSolver):
    """
    Monte Carlo Solver for Lindblad equations with "rates" that may be
    negative. The ``c_ops`` parameter of :class:`qutip.MCSolver` is replaced by
    an ``ops_and_rates`` parameter to allow for negative rates. Options for the
    underlying ODE solver are given by the Options class.

    Parameters
    ----------
    H : :class:`qutip.Qobj`, :class:`qutip.QobjEvo`, ``list``, callable.
        System Hamiltonian as a Qobj, QobjEvo. It can also be any input type
        that QobjEvo accepts (see :class:`qutip.QobjEvo` documentation).
        ``H`` can also be a superoperator (liouvillian) if some collapse
        operators are to be treated deterministically.

    ops_and_rates : list
        A ``list`` of tuples ``(L, Gamma)``, where the Lindblad operator ``L``
        is a :class:`qutip.Qobj` and ``Gamma`` represents the corresponding
        rate, which is allowed to be negative. The Lindblad operators must be
        operators even if ``H`` is a superoperator. Each rate ``Gamma`` may be
        just a number (in the case of a constant rate) or, otherwise, specified
        using any format accepted by :func:`qutip.coefficient`.

    args : None / dict
        Arguments for time-dependent Hamiltonian and collapse operator terms.

    options : SolverOptions, [optional]
        Options for the evolution.

    seed : int, SeedSequence, list, [optional]
        Seed for the random number generator. It can be a single seed used to
        spawn seeds for each trajectory or a list of seed, one for each
        trajectory. Seeds are saved in the result and can be reused with::

            seeds=prev_result.seeds
    """
    name = "nm_mcsolve"
    resultclass = NmmcResult
    solver_options = {
        **MCSolver.solver_options,
        "completeness_rtol": 1e-5,
        "completeness_atol": 1e-8,
        "martingale_quad_limit": 100,
    }

    # both classes will be partially initialized in constructor
    trajectory_resultclass = NmmcTrajectoryResult
    mc_integrator_class = NmMCIntegrator

    def __init__(
        self, H, ops_and_rates, *_args, args=None, options=None, **kwargs,
    ):
        self.options = options

        ops_and_rates = [
            _parse_op_and_rate(op, rate, args=args or {})
            for op, rate in ops_and_rates
        ]
        a_parameter, L = self._check_completeness(ops_and_rates)
        if L is not None:
            ops_and_rates.append((L, ConstantCoefficient(0)))

        self.ops = [op for op, _ in ops_and_rates]
        self._martingale = InfluenceMartingale(
            self, a_parameter, self.options["martingale_quad_limit"]
        )

        # Many coefficients. These should not be publicly exposed
        # and will all need to be updated in _arguments():
        self._rates = [rate for _, rate in ops_and_rates]
        self._rate_shift = RateShiftCoefficient(self._rates)
        self._sqrt_shifted_rates = [
            SqrtRealCoefficient(rate + self._rate_shift)
            for rate in self._rates
        ]

        c_ops = [
            QobjEvo([op, sqrt_shifted_rate])
            for op, sqrt_shifted_rate
            in zip(self.ops, self._sqrt_shifted_rates)
        ]
        self.trajectory_resultclass = functools.partial(
            NmmcTrajectoryResult, __nm_solver=self,
        )
        self.mc_integrator_class = functools.partial(
            NmMCIntegrator, __martingale=self._martingale,
        )
        super().__init__(H, c_ops, *_args, options=options, **kwargs)

    def _check_completeness(self, ops_and_rates):
        """
        Checks whether ``sum(Li.dag() * Li)`` is proportional to the identity
        operator. If not, creates an extra Lindblad operator so that it is.

        Returns the proportionality factor a, and the extra Lindblad operator
        (or None if no extra Lindblad operator is necessary).
        """
        op = sum((L.dag() * L) for L, _ in ops_and_rates)

        a_candidate = op.tr() / op.shape[0]
        with CoreOptions(rtol=self.options["completeness_rtol"],
                         atol=self.options["completeness_atol"]):
            if op == a_candidate * qeye(op.dims[0]):
                return np.real(a_candidate), None

        a = max(op.eigenenergies())
        L = (a * qeye(op.dims[0]) - op).sqrtm()  # new Lindblad operator
        return a, L

    def current_martingale(self):
        """
        Returns the value of the influence martingale along the current
        trajectory. The value of the martingale is the product of the
        continuous and the discrete contribution. The current time and the
        collapses that have happened are read out from the internal integrator.
        """
        t, *_ = self._integrator.get_state(copy=False)
        return self._martingale.value(t)

    def _argument(self, args):
        self._rates = [rate.replace_arguments(args) for rate in self._rates]
        self._rate_shift = self._rate_shift.replace_arguments(args)
        self._sqrt_shifted_rates = [
            rate.replace_arguments(args) for rate in self._sqrt_shifted_rates
        ]
        super()._argument(args)

    def rate_shift(self, t):
        """
        Return the rate shift at time ``t``.

        The rate shift is ``2 * abs(min([0, rate_1(t), rate_2(t), ...]))``.

        Parameters
        ----------
        t : float
            The time at which to calculate the rate shift.

        Returns
        -------
        rate_shift : float
            The rate shift amount.
        """
        return self._rate_shift.as_double(t)

    def rate(self, t, i):
        """
        Return the i'th unshifted rate at time ``t``.

        Parameters
        ----------
        t : float
            The time at which to calculate the rate.
        i : int
            Which rate to calculate.

        Returns
        -------
        rate : float
            The value of rate ``i`` at time ``t``.
        """
        return np.real(self._rates[i](t))

    def sqrt_shifted_rate(self, t, i):
        """
        Return the square root of the i'th shifted rate at time ``t``.

        Parameters
        ----------
        t : float
            The time at wich to calculate the shifted rate.
        i : int
            Which shifted rate to calculate.

        Returns
        -------
        rate : float
            The square root of the shifted value of rate ``i`` at time ``t``.
        """
        return np.real(self._sqrt_shifted_rates[i](t))

    # MCSolver (and NonMarkovianMCSolver) offer two interfaces, i.e., two ways
    # of interacting with them: either call `start` first and then manually
    # integrate a single trajectory with subsequent calls to `step`, or call
    # `run` to integrate a large number of trajectories, saving the results in
    # an `NmmcResult`.
    # We are responsible for (a) keeping our `_martingale` object in the
    # correct state throughout and (b) multiplying all state density matrices
    # with the martingale before passing them on to the user.
    #
    # Regarding (a), we firstly assume that start, step and run are only
    # accessed by a single thread. start and step thus cannot be called while
    # run is being executed. Secondly, we reset the martingale object at the
    # beginning and end of run, requiring the user to call start again after
    # calling run before calling step. Internal state of the martingale
    # object accumulated by using one interface can thus not influence
    # computations with the other interface.
    # Note that the start/step-interface allows updating the `args` dictionary
    # at each step. This action does not mess up the martingale state since we
    # do not precompute any martingale values in this interface. In the
    # run-interface we do precompute the values of the continuous part of the
    # martingale, but the `args` dictionary cannot be changed in the middle of
    # the run.
    #
    # Regarding (b), in the start/step-interface we just include the martingale
    # in the step method. In order to include the martingale in the
    # run-interface, we use a custom trajectory-resultclass that grabs the
    # martingale value from the NonMarkovianMCSolver whenever a state is added.

    def start(self, state, t0, seed=None):
        self._martingale.initialize(t0, cache='clear')
        return super().start(state, t0, seed=seed)

    # The returned state will be a density matrix with trace=mu the martingale
    def step(self, t, *, args=None, copy=True):
        state = super().step(t, args=args, copy=copy)
        if isket(state):
            state = ket2dm(state)
        return state * self.current_martingale()

    def run(self, state, tlist, *args, **kwargs):
        # update `args` dictionary before precomputing martingale
        if 'args' in kwargs:
            self._argument(kwargs.pop('args'))

        self._martingale.initialize(tlist[0], cache=tlist)
        result = super().run(state, tlist, *args, **kwargs)
        self._martingale.reset()

        return result

    start.__doc__ = MultiTrajSolver.start.__doc__
    step.__doc__ = MultiTrajSolver.step.__doc__
    run.__doc__ = MultiTrajSolver.run.__doc__
