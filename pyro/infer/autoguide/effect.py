# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from typing import Callable, Tuple, Union

import torch
from torch.distributions import biject_to, constraints

import pyro.distributions as dist
from pyro.distributions.distribution import Distribution
from pyro.nn.module import PyroModule, PyroParam, pyro_method
from pyro.ops.tensor_utils import periodic_repeat
from pyro.poutine.guide import GuideMessenger
from pyro.poutine.runtime import get_plates

from .initialization import init_to_feasible, init_to_mean
from .utils import deep_getattr, deep_setattr, helpful_support_errors


class AutoMessengerMeta(type(GuideMessenger), type(PyroModule)):
    pass


class AutoMessenger(GuideMessenger, PyroModule, metaclass=AutoMessengerMeta):
    """
    Base class for :class:`~pyro.poutine.guide.GuideMessenger` autoguides.

    :param callable model: A Pyro model.
    :param tuple amortized_plates: A tuple of names of plates over which guide
        parameters should be shared. This is useful for subsampling, where a
        guide parameter can be shared across all plates.
    """

    def __init__(self, model: Callable, *, amortized_plates: Tuple[str, ...] = ()):
        self.amortized_plates = amortized_plates
        super().__init__(model)

    @pyro_method
    def __call__(self, *args, **kwargs):
        # Since this guide creates parameters lazily, we need to avoid batching
        # those parameters by a particle plate, in case the first time this
        # guide is called is inside a particle plate. We assume all plates
        # outside the model are particle plates.
        self._outer_plates = tuple(f.name for f in get_plates())
        try:
            return super().__call__(*args, **kwargs)
        finally:
            del self._outer_plates

    def call(self, *args, **kwargs):
        """
        Method that calls :meth:`forward` and returns parameter values of the
        guide as a `tuple` instead of a `dict`, which is a requirement for
        JIT tracing. Unlike :meth:`forward`, this method can be traced by
        :func:`torch.jit.trace_module`.

        .. warning::
            This method may be removed once PyTorch JIT tracer starts accepting
            `dict` as valid return types. See
            `issue <https://github.com/pytorch/pytorch/issues/27743>_`.
        """
        result = self(*args, **kwargs)
        return tuple(v for _, v in sorted(result.items()))

    @torch.no_grad()
    def _remove_outer_plates(self, value: torch.Tensor, event_dim: int) -> torch.Tensor:
        """
        Removes particle plates from initial values of parameters.
        """
        for f in get_plates():
            full_size = getattr(f, "full_size", f.size)
            dim = f.dim - event_dim
            if f in self._outer_plates or f.name in self.amortized_plates:
                if -value.dim() <= dim:
                    value = value.mean(dim, keepdim=True)
            elif f.size != full_size:
                value = periodic_repeat(value, full_size, dim).contiguous()
        for dim in range(value.dim() - event_dim):
            value = value.squeeze(0)
        return value


class AutoNormalMessenger(AutoMessenger):
    """
    :class:`AutoMessenger` with mean-field normal posterior.

    The mean-field posterior at any site is a transformed normal distribution.
    This posterior is equivalent to :class:`~pyro.infer.autoguide.AutoNormal`
    or :class:`~pyro.infer.autoguide.AutoDiagonalNormal`, but allows
    customization via subclassing.

    Derived classes may override the :meth:`get_posterior` behavior at
    particular sites and use the mean-field normal behavior simply as a
    default, e.g.::

        def model(data):
            a = pyro.sample("a", dist.Normal(0, 1))
            b = pyro.sample("b", dist.Normal(0, 1))
            c = pyro.sample("c", dist.Normal(a + b, 1))
            pyro.sample("obs", dist.Normal(c, 1), obs=data)

        class MyGuideMessenger(AutoNormalMessenger):
            def get_posterior(self, name, prior):
                if name == "c":
                    # Use a custom distribution at site c.
                    bias = pyro.param("c_bias", lambda: torch.zeros(()))
                    weight = pyro.param("c_weight", lambda: torch.ones(()),
                                        constraint=constraints.positive)
                    scale = pyro.param("c_scale", lambda: torch.ones(()),
                                       constraint=constraints.positive)
                    a = self.upstream_value("a")
                    b = self.upstream_value("b")
                    loc = bias + weight * (a + b)
                    return dist.Normal(loc, scale)
                # Fall back to mean field.
                return super().get_posterior(name, prior)

    Note that above we manually computed ``loc = bias + weight * (a + b)``.
    Alternatively we could reuse the model-side computation by setting ``loc =
    bias + weight * prior.loc``::

        class MyGuideMessenger_v2(AutoNormalMessenger):
            def get_posterior(self, name, prior):
                if name == "c":
                    # Use a custom distribution at site c.
                    bias = pyro.param("c_bias", lambda: torch.zeros(()))
                    scale = pyro.param("c_scale", lambda: torch.ones(()),
                                       constraint=constraints.positive)
                    weight = pyro.param("c_weight", lambda: torch.ones(()),
                                        constraint=constraints.positive)
                    loc = bias + weight * prior.loc
                    return dist.Normal(loc, scale)
                # Fall back to mean field.
                return super().get_posterior(name, prior)

    :param callable model: A Pyro model.
    :param callable init_loc_fn: A per-site initialization function.
        See :ref:`autoguide-initialization` section for available functions.
    :param float init_scale: Initial scale for the standard deviation of each
        (unconstrained transformed) latent variable.
    :param tuple amortized_plates: A tuple of names of plates over which guide
        parameters should be shared. This is useful for subsampling, where a
        guide parameter can be shared across all plates.
    """

    def __init__(
        self,
        model: Callable,
        *,
        init_loc_fn: Callable = init_to_mean(fallback=init_to_feasible),
        init_scale: float = 0.1,
        amortized_plates: Tuple[str, ...] = (),
    ):
        if not isinstance(init_scale, float) or not (init_scale > 0):
            raise ValueError("Expected init_scale > 0. but got {}".format(init_scale))
        super().__init__(model, amortized_plates=amortized_plates)
        self.init_loc_fn = init_loc_fn
        self._init_scale = init_scale
        self._computing_median = False

    def get_posterior(
        self, name: str, prior: Distribution
    ) -> Union[Distribution, torch.Tensor]:
        if self._computing_median:
            return self._get_posterior_median(name, prior)

        with helpful_support_errors({"name": name, "fn": prior}):
            transform = biject_to(prior.support)
        loc, scale = self._get_params(name, prior)
        posterior = dist.TransformedDistribution(
            dist.Normal(loc, scale).to_event(transform.domain.event_dim),
            transform.with_cache(),
        )
        return posterior

    def _get_params(self, name: str, prior: Distribution):
        try:
            loc = deep_getattr(self.locs, name)
            scale = deep_getattr(self.scales, name)
            return loc, scale
        except AttributeError:
            pass

        # Initialize.
        with torch.no_grad():
            transform = biject_to(prior.support)
            event_dim = transform.domain.event_dim
            constrained = self.init_loc_fn({"name": name, "fn": prior}).detach()
            unconstrained = transform.inv(constrained)
            init_loc = self._remove_outer_plates(unconstrained, event_dim)
            init_scale = torch.full_like(init_loc, self._init_scale)

        deep_setattr(self, "locs." + name, PyroParam(init_loc, event_dim=event_dim))
        deep_setattr(
            self,
            "scales." + name,
            PyroParam(init_scale, constraint=constraints.positive, event_dim=event_dim),
        )
        return self._get_params(name, prior)

    def median(self, *args, **kwargs):
        self._computing_median = True
        try:
            return self(*args, **kwargs)
        finally:
            self._computing_median = False

    def _get_posterior_median(self, name, prior):
        transform = biject_to(prior.support)
        loc, scale = self._get_params(name, prior)
        return transform(loc)


class AutoRegressiveMessenger(AutoMessenger):
    """
    :class:`AutoMessenger` with recursively affine-transformed priors using
    prior dependency structure.

    The posterior at any site is a learned affine transform of the prior,
    conditioned on upstream posterior samples. The affine transform operates in
    unconstrained space. This supports only continuous latent variables.

    Derived classes may override the :meth:`get_posterior` behavior at
    particular sites and use the regressive behavior simply as a default,
    e.g.::

        class MyGuideMessenger(AutoRegressiveMessenger):
            def get_posterior(self, name, prior):
                if name == "x":
                    # Use a custom distribution at site x.
                    loc = pyro.param("x_loc", lambda: torch.zeros(prior.shape()))
                    scale = pyro.param("x_scale", lambda: torch.ones(prior.shape())),
                                       constraint=constraints.positive
                    return dist.Normal(loc, scale).to_event(prior.event_dim())
                # Fall back to autoregressive.
                return super().get_posterior(name, prior)

    .. warning:: This guide currently does not support jit-based elbos.

    :param callable model: A Pyro model.
    :param callable init_loc_fn: A per-site initialization function.
        See :ref:`autoguide-initialization` section for available functions.
    :param float init_scale: Initial scale for the standard deviation of each
        (unconstrained transformed) latent variable.
    :param tuple amortized_plates: A tuple of names of plates over which guide
        parameters should be shared. This is useful for subsampling, where a
        guide parameter can be shared across all plates.
    """

    def __init__(
        self,
        model: Callable,
        *,
        init_loc_fn: Callable = init_to_mean(fallback=init_to_feasible),
        init_scale: float = 0.1,
        amortized_plates: Tuple[str, ...] = (),
    ):
        if not isinstance(init_scale, float) or not (init_scale > 0):
            raise ValueError("Expected init_scale > 0. but got {}".format(init_scale))
        super().__init__(model, amortized_plates=amortized_plates)
        self.init_loc_fn = init_loc_fn
        self._init_scale = init_scale

    def get_posterior(
        self, name: str, prior: Distribution
    ) -> Union[Distribution, torch.Tensor]:
        with helpful_support_errors({"name": name, "fn": prior}):
            transform = biject_to(prior.support)
        loc, scale = self._get_params(name, prior)
        affine = dist.transforms.AffineTransform(
            loc, scale, event_dim=transform.domain.event_dim, cache_size=1
        )
        posterior = dist.TransformedDistribution(
            prior, [transform.inv.with_cache(), affine, transform.with_cache()]
        )
        return posterior

    def _get_params(self, name: str, prior: Distribution):
        try:
            loc = deep_getattr(self.locs, name)
            scale = deep_getattr(self.scales, name)
            return loc, scale
        except AttributeError:
            pass

        # Initialize.
        with torch.no_grad():
            transform = biject_to(prior.support)
            event_dim = transform.domain.event_dim
            constrained = self.init_loc_fn({"name": name, "fn": prior}).detach()
            unconstrained = transform.inv(constrained)
            # Initialize the distribution to be an affine combination:
            #   init_scale * prior + (1 - init_scale) * init_loc
            init_loc = self._remove_outer_plates(unconstrained, event_dim)
            init_loc = init_loc * (1 - self._init_scale)
            init_scale = torch.full_like(init_loc, self._init_scale)

        deep_setattr(self, "locs." + name, PyroParam(init_loc, event_dim=event_dim))
        deep_setattr(
            self,
            "scales." + name,
            PyroParam(init_scale, constraint=constraints.positive, event_dim=event_dim),
        )
        return self._get_params(name, prior)