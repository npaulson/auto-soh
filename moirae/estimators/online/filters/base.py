""" Base definitions for all filters """
from abc import abstractmethod
from typing import Tuple

import numpy as np

from .distributions import MultivariateRandomDistribution


class ModelWrapper():
    """
    Base class that dictates how a model has to be wrapped to interface with the filters

    All inputs, whether hidden state or controols, are 2D arrays where the first
    dimension is the batch dimension. The batch dimension must be 1 or, if not,
    the same value as any other non-unity batch sizes for the purpose
    of `NumPy broadcasting <https://numpy.org/doc/stable/user/basics.broadcasting.html>`_.
    """

    @property
    @abstractmethod
    def num_hidden_dimensions(self) -> int:
        raise NotImplementedError('Please implement in child class!')

    @property
    @abstractmethod
    def num_output_dimensions(self) -> int:
        raise NotImplementedError('Please implement in child class!')

    @abstractmethod
    def update_hidden_states(self,
                             hidden_states: np.ndarray,
                             previous_controls: np.ndarray,
                             new_controls: np.ndarray) -> np.ndarray:
        """
        Method to update hidden states.

        Args:
            hidden_states: numpy array of hidden states, where each row is a hidden state array
            previous_controls: numpy array corresponding to the previous controls
            new_controls: numpy array corresponding to the new controls

        Returns:
            numpy array corresponding to updated hidden states
        """
        raise NotImplementedError('Please implement in child class!')

    @abstractmethod
    def predict_measurement(self, hidden_states: np.ndarray, controls: np.ndarray) -> np.ndarray:
        """
        Method to update hidden states.

        Args:
            hidden_states: numpy array of hidden states, where each row is a hidden state array
            controls: numpy array corresponding to the controls

        Returns:
            numpy array corresponding to predicted outputs
        """
        raise NotImplementedError('Please implement in child class!')


class BaseFilter:
    """
    Args:
        model: model that describes how to
            updated_hidden_state as a function of control
            calculate_output as a function of hidden state and control
        initial_hidden: initial hidden state of the system
        initial_controls: initial control state of the system
    """

    def __init__(self,
                 model: ModelWrapper,
                 initial_hidden: MultivariateRandomDistribution,
                 initial_controls: MultivariateRandomDistribution) -> None:
        self.model = model
        self.hidden = initial_hidden.model_copy(deep=True)
        self.controls = initial_controls.model_copy(deep=True)

    @abstractmethod
    def step(self,
             new_controls: MultivariateRandomDistribution,
             measurements: MultivariateRandomDistribution
             ) -> Tuple[MultivariateRandomDistribution, MultivariateRandomDistribution]:
        """
        Function to step the filter.

        Args:
            new_controls: new control state
            measurements: measurement obtained

        Returns:
            hidden_estimate: estimate of the hidden state
            output_prediction: predicted output, which is what the filter predicted the 'measurements' argument to be
        """
        raise NotImplementedError('Please implement in child class!')
