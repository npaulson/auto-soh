"""
Collection of online estimators

Here, we include base classes to facilitate  definition of online estimators and hidden states, as well as to establish
interfaces between online estimators and models, transient states, and A-SOH parameters.
"""
from abc import abstractmethod
from functools import cached_property
from typing import Tuple, Union, Collection, Optional

import numpy as np
from moirae.estimators.online.distributions import MultivariateRandomDistribution

from moirae.models.base import CellModel, GeneralContainer, InputQuantities, HealthVariable


# TODO (wardlt): Consider letting users pass a custom normalization function rather than implementing a subclass
class OnlineEstimator:
    """
    Defines the base structure of an online estimator.

    All estimators require...

    1. A :class:`~moirae.models.base.CellModel` which describes how the system state is expected to change and
        relate the current state to observable measurements.
    2. An initial estimate for the parameters of the system, which we refer to as the Advanced State of Health (ASOH).
    3. An initial estimate for the transient states of the system

    Different implementations may require other information, such as an initial guess for the
    probability distribution for the values of the states (transient or ASOH).

    Use the estimator by calling the :meth:`step` function to update the estimated state
    provided a new observation of the outputs of the system.

    Args:
        model: Model used to describe the underlying physics of the storage system
        initial_asoh: Initial estimates for the health parameters of the battery, those being estimated or not
        initial_transients: Initial estimates for the transient states of the battery
        initial_inputs: Initial inputs to the system
        updatable_asoh: Whether to estimate values for all updatable parameters (``True``),
            none of the updatable parameters (``False``),
            or only a select set of them (provide a list of names).
    """
    model: CellModel
    """Link to the model describing the known physics of the system"""

    def __init__(self,
                 model: CellModel,
                 initial_asoh: HealthVariable,
                 initial_transients: GeneralContainer,
                 initial_inputs: InputQuantities,
                 updatable_asoh: Union[bool, Collection[str]] = True):
        self.model = model
        self._asoh = initial_asoh.model_copy(deep=True)
        self._transients = initial_transients.model_copy(deep=True)
        self._inputs = initial_inputs.model_copy(deep=True)

        # Cache information about the outputs
        example_outputs = model.calculate_terminal_voltage(initial_inputs, self._transients, self._asoh)
        self._num_outputs = len(example_outputs)
        self._output_names = example_outputs.all_names

        # The batch size of the two components must be 1
        assert self._transients.batch_size == 1
        assert self._asoh.batch_size == 1

        # Determine which parameters to treat as updatable in the ASOH
        self._updatable_names: Optional[list[str]]
        if isinstance(updatable_asoh, bool):
            if updatable_asoh:
                self._updatable_names = self._asoh.updatable_names
            else:
                self._updatable_names = []
        else:
            self._updatable_names = list(updatable_asoh)

    @cached_property
    def num_hidden_dimensions(self) -> int:
        """ Expected dimensionality of hidden state """
        return self.num_transients + self._asoh.get_parameters(self._updatable_names).shape[-1]

    @cached_property
    def num_transients(self):
        """ Number of values from the hidden state which belong to the transients """
        return len(self._transients)

    @property
    def num_output_dimensions(self) -> int:
        """ Expected dimensionality of output measurements """
        return self._num_outputs

    @cached_property
    def state_names(self) -> Tuple[str, ...]:
        """ Names of each state variable """
        return self._transients.all_names + self._asoh.expand_names(self._updatable_names)

    @cached_property
    def output_names(self) -> Tuple[str, ...]:
        """ Names of each output variable """
        return self._output_names

    @cached_property
    def control_names(self) -> Tuple[str, ...]:
        """ Names for each of the control variables """
        return self._inputs.all_names

    def _denormalize_hidden_array(self, hidden_array: np.ndarray) -> np.ndarray:
        """Apply transformations to the hidden array which transform it from the
        form used by the estimator to the form used by the model

        Args:
            hidden_array: Input array of points to be evaluated with the model. Should not be modified
        Returns:
            An array ready for use in the model
        """
        return hidden_array

    def _normalize_hidden_array(self, hidden_array: np.ndarray) -> np.ndarray:
        """Apply transformations to the hidden array which transform it to the
        form used by the estimator to the form used by the model

        Args:
            hidden_array: Input array of points to be used by the filter. Should not be modified
        Returns:
            An array ready produced by the model
        """
        return hidden_array

    # TODO (wardlt): Re-establish allowing controls to be a list when we need it
    def update_hidden_states(self,
                             hidden_states: np.ndarray,
                             previous_controls: MultivariateRandomDistribution,
                             new_controls: MultivariateRandomDistribution) -> np.ndarray:
        """
        Function that updates the hidden state based on the control variables provided.

        Args:
            hidden_states: current hidden states of the system as a numpy.ndarray object
            previous_controls: controls at the time the hidden states are being reported
            new_controls: new controls to be used in the hidden state update

        Returns:
            new_hidden: updated hidden states as a numpy.ndarray object
        """

        # First, transform the controls into the input class used by the model
        previous_inputs = self._inputs.model_copy(deep=True)
        previous_inputs.from_numpy(previous_controls.get_mean())
        new_inputs = self._inputs.model_copy(deep=True)
        new_inputs.from_numpy(new_controls.get_mean())

        # Undo any normalizing
        hidden_states = self._denormalize_hidden_array(hidden_states)

        # Now, iterate through the hidden states to create ECMTransient states and update them
        output = hidden_states.copy()
        my_transients = self._transients.model_copy(deep=True)
        my_asoh = self._asoh.model_copy(deep=True)
        my_transients.from_numpy(hidden_states[:, :self.num_transients])
        my_asoh.update_parameters(hidden_states[:, self.num_transients:], self._updatable_names)
        new_transients = self.model.update_transient_state(previous_inputs, new_inputs=new_inputs,
                                                           transient_state=my_transients,
                                                           asoh=my_asoh)
        output[:, :self.num_transients] = new_transients.to_numpy()
        return self._normalize_hidden_array(output)

    def predict_measurement(self,
                            hidden_states: np.ndarray,
                            controls: MultivariateRandomDistribution) -> np.ndarray:
        """
        Function to predict measurement from the hidden state

        Args:
            hidden_states: current hidden states of the system as a numpy.ndarray object
            controls: controls to be used for predicting outputs

        Returns:
            pred_measurement: predicted measurements as a numpy.ndarray object
        """
        # First, transform the controls into ECM inputs
        inputs = self._inputs.model_copy(deep=True)
        inputs.from_numpy(controls.get_mean())

        # Denormalize
        hidden_states = self._denormalize_hidden_array(hidden_states)

        # Now, iterate through hidden states to compute terminal voltage
        my_transients = self._transients.model_copy(deep=True)
        my_asoh = self._asoh.model_copy(deep=True)
        my_transients.from_numpy(hidden_states[:, :self.num_transients])
        my_asoh.update_parameters(hidden_states[:, self.num_transients:], self._updatable_names)

        outputs = self.model.calculate_terminal_voltage(new_inputs=inputs, transient_state=my_transients, asoh=my_asoh)
        return outputs.to_numpy()

    @abstractmethod
    def step(self, u: MultivariateRandomDistribution, y: MultivariateRandomDistribution) \
            -> Tuple[MultivariateRandomDistribution, MultivariateRandomDistribution]:
        """
        Function to step the estimator, provided new control variables and output measurements.

        Args:
            u: control variables
            y: output measurements

        Returns:
            - Updated estimate of the hidden state, which includes the transient states and ASOH
            - Estimate of the measurements as predicted by the underlying model
        """
        raise NotImplementedError()
