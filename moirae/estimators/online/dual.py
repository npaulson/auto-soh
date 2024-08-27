""" Framework for dual estimation of transient vector and A-SOH"""
from typing import Tuple, Optional, TypedDict
from typing_extensions import Self, NotRequired

import numpy as np

from moirae.models.base import InputQuantities, OutputQuantities, GeneralContainer, HealthVariable, CellModel
from .utils.model import CellModelWrapper, DegradationModelWrapper, convert_vals_model_to_filter
from moirae.estimators.online import OnlineEstimator
from .filters.base import BaseFilter
from .filters.distributions import MultivariateRandomDistribution, MultivariateGaussian
from .filters.kalman.unscented import UnscentedKalmanFilter as UKF
from .filters.kalman.unscented import UKFTuningParameters
from .filters.conversions import LinearConversionOperator, IdentityConversionOperator


class DualUKFTuningParameters(TypedDict):
    """
    Auxiliary class to help provide tuning parameters to each filter in the dual estimation framework defined by
    ~:class:`~moirae.estimators.online.dual.DualEstimator`

    Args:
        transient: tuning parameters for the transient filter
        asoh: tuning parameters for the A-SOH filter
    """
    transient: NotRequired[UKFTuningParameters]
    asoh: NotRequired[UKFTuningParameters]

    @classmethod
    def defaults(cls) -> Self:
        return {'transient': UKFTuningParameters.defaults(), 'asoh': UKFTuningParameters.defaults()}


class DualEstimator(OnlineEstimator):
    """
    In dual estimation, the transient vector and A-SOH are estimated by separate filters. This framework generally
    avoids numerical errors related to the magnitude differences between values pertaining to transient quantities and
    to the A-SOH parameters. However, correlations between these two types of quantities are partially lost, and the
    framework is more involved.

    Args:
        transient_filter: base filter to estimate transient vector
        asoh_filter: base filter to estimate A-SOH parameters
    """

    def __init__(self, transient_filter: BaseFilter, asoh_filter: BaseFilter) -> None:
        if not isinstance(transient_filter.model, CellModelWrapper):
            raise ValueError('The dual estimator only works with a filter which uses a CellModelWrapper to describe the'
                             ' dynamics of the transient states')
        if not isinstance(asoh_filter.model, DegradationModelWrapper):
            raise ValueError('The dual estimator only works with a filter which uses a DegradationModelWrapper to '
                             'describe the degradation of the A-SOH!')

        # Storing necessary objects
        cell_wrapper = transient_filter.model
        asoh_wrapper = asoh_filter.model
        super().__init__(
            cell_model=cell_wrapper.cell_model,
            initial_asoh=asoh_wrapper.asoh,
            initial_transients=cell_wrapper.transients,
            initial_inputs=cell_wrapper.inputs,
            updatable_asoh=asoh_wrapper.asoh_inputs
        )
        self.trans_filter = transient_filter
        self.asoh_filter = asoh_filter
        self.cell_wrapper = cell_wrapper
        self.asoh_wrapper = asoh_wrapper

    def _get_converted_states(self) -> Tuple[MultivariateRandomDistribution, MultivariateRandomDistribution]:
        """
        Helper function to convert values in transient and A-SOH from filter representations

        Returns:
            transient_converted: representation of the transient
                :class:`~moirae.estimators.online.filters.distributions.MultivariateRandomDistribution` in
                model-coordinates
            asoh_converted: representation of the A-SOH
                :class:`~moirae.estimators.online.filters.distributions.MultivariateRandomDistribution` in
                model-coordinates
        """
        # We need to get the hidden states on both filters
        transient_hidden = self.trans_filter.hidden.model_copy(deep=True)
        asoh_hidden = self.asoh_filter.hidden.model_copy(deep=True)

        # We need to transform them accordingly
        return transient_hidden.convert(conversion_operator=self.trans_filter.model.hidden_conversion), \
            asoh_hidden.convert(conversion_operator=self.asoh_filter.model.hidden_conversion)

    @property
    def state(self) -> MultivariateRandomDistribution:
        # Get converted states
        transient_converted, asoh_converted = self._get_converted_states()
        return transient_converted.combine_with(asoh_converted)

    def get_estimated_state(self) -> Tuple[GeneralContainer, HealthVariable]:
        # Get converted states
        transient_converted, asoh_converted = self._get_converted_states()

        # Translate to model object
        estimated_transient = self.transients.make_copy(values=transient_converted.get_mean())
        estimated_asoh = self.asoh.make_copy(values=asoh_converted.get_mean())
        return estimated_transient, estimated_asoh

    def step(self, inputs: InputQuantities, measurements: OutputQuantities) -> \
            Tuple[MultivariateRandomDistribution, MultivariateRandomDistribution]:
        # Refactor inputs and measurements to filter-lingo
        # TODO (vventuri): convert this later to allow for uncertain input quantities
        refactored_inputs = convert_vals_model_to_filter(inputs)
        refactored_measurements = convert_vals_model_to_filter(measurements)

        # Collect posteriors from previous states to communicate between wrappers
        previous_trans_posterior, previous_asoh_posterior = self.get_estimated_state()

        # Give these to the wrappers
        self.cell_wrapper.asoh = previous_asoh_posterior.model_copy(deep=True)
        self.asoh_wrapper.transients = previous_trans_posterior.model_copy(deep=True)

        # Now, we can safely step each filter on its own
        transient_estimate, output_pred_trans = self.trans_filter.step(
            new_controls=refactored_inputs.convert(
                conversion_operator=self.cell_wrapper.control_conversion, inverse=True),  # Convert to filter coordinate
            measurements=refactored_measurements.convert(
                conversion_operator=self.cell_wrapper.output_conversion, inverse=True))  # Convert to filter coordinates
        asoh_estimate, output_pred_asoh = self.asoh_filter.step(
            new_controls=refactored_inputs.convert(
                conversion_operator=self.asoh_wrapper.control_conversion, inverse=True),  # Convert to filter coordinate
            measurements=refactored_measurements.convert(
                conversion_operator=self.asoh_wrapper.output_conversion, inverse=True))  # Convert to filter coordinates

        # Convert estimates back to model-coordinates
        transient_estimate = transient_estimate.convert(conversion_operator=self.cell_wrapper.hidden_conversion)
        asoh_estimate = asoh_estimate.convert(conversion_operator=self.asoh_wrapper.hidden_conversion)
        output_pred_trans = output_pred_trans.convert(conversion_operator=self.cell_wrapper.output_conversion)
        output_pred_asoh = output_pred_asoh.convert(conversion_operator=self.asoh_wrapper.output_conversion)

        # TODO (vventuri): how to we adequately combine the output predictions from both filters?
        return transient_estimate.combine_with((asoh_estimate,)), output_pred_trans

    @classmethod
    def initialize_unscented_kalman_filter(
        cls,
        cell_model: CellModel,
        # TODO (vventuri): add degradation_model as an option here
        initial_asoh: HealthVariable,
        initial_transients: GeneralContainer,
        initial_inputs: InputQuantities,
        covariance_transient: np.ndarray,
        covariance_asoh: np.ndarray,
        inputs_uncertainty: Optional[np.ndarray] = None,
        transient_covariance_process_noise: Optional[np.ndarray] = None,
        asoh_covariance_process_noise: Optional[np.ndarray] = None,
        covariance_sensor_noise: Optional[np.ndarray] = None,
        normalize_asoh: bool = False,
        filter_args: Optional[DualUKFTuningParameters] = DualUKFTuningParameters.defaults()
    ) -> Self:
        """
        Function to help the user initialize a UKF-based dual estimation without needing to define each filter and
        model wrapper individually.

        Args:
            cell_model: CellModel to be used
            degratation_model: DegradationModel to be used; if None is passed, assume A-SOH degration estimated solely
                                from data
            initial_asoh: initial A-SOH
            initial_transients: initial transient vector
            initial_inputs: initial input quantities
            covariance_transient: specifies the raw (un-normalized) covariance of the transient state; it is not used if
                covariance_joint was provided
            covariance_asoh: specifies the raw (un-normalized) covariance of the A-SOH; it is not used if
                covariance_joint was provided
            inputs_uncertainty: uncertainty matrix of the inputs; if not provided, assumes inputs are exact
            transient_covariance_process_noise: process noise for transient update; only used if
                joint_covariance_process_noise was not provided
            asoh_covariance_process_noise: process noise for A-SOH update; only used if joint_covariance_process_noise
                was not provided
            covariance_sensor_noise: sensor noise for outputs; if denoising is applied, must match the proper
                dimensionality
            normalize_asoh: determines whether A-SOH parameters should be normalized in the filter
            filter_args: dictionary where keys must be either 'transient' or 'asoh', and values must be a dictionary in
                which keys are keyword arguments for the UKFs (such as `alpha_param`)
        """
        # Start by checking if A-SOH needs to be normalized, in which case, create conversion operator
        asoh_normalizer = IdentityConversionOperator()
        if normalize_asoh:
            asoh_values = initial_asoh.get_parameters()
            # Check parameters that are 0 and leave them un-normalized
            asoh_values = np.where(asoh_values == 0, 1., asoh_values)
            asoh_normalizer = LinearConversionOperator(multiplicative_array=asoh_values.flatten())

        # Assemble the wrappers
        cell_wrapper = CellModelWrapper(cell_model=cell_model,
                                        asoh=initial_asoh,
                                        transients=initial_transients,
                                        inputs=initial_inputs)
        asoh_wrapper = DegradationModelWrapper(cell_model=cell_model,
                                               asoh=initial_asoh,
                                               transients=initial_transients,
                                               inputs=initial_inputs,
                                               converters={'hidden_conversion_operator': asoh_normalizer})

        # Prepare raw multivariate Gaussians
        transients_hidden = convert_vals_model_to_filter(model_quantities=initial_transients,
                                                         uncertainty_matrix=covariance_transient)
        asoh_hidden = MultivariateGaussian(mean=initial_asoh.get_parameters().flatten(),
                                           covariance=covariance_asoh)
        initial_controls = convert_vals_model_to_filter(model_quantities=initial_inputs)

        # Additional setup/conversions
        trans_proc_noise = transient_covariance_process_noise
        if trans_proc_noise is not None:
            trans_proc_noise = cell_wrapper.hidden_conversion.inverse_transform_covariance(
                transformed_covariance=trans_proc_noise)
        asoh_proc_noise = asoh_covariance_process_noise
        if asoh_proc_noise is not None:
            asoh_proc_noise = asoh_wrapper.hidden_conversion.inverse_transform_covariance(
                transformed_covariance=asoh_proc_noise)
        trans_sensor_noise = covariance_sensor_noise
        asoh_sensor_noise = covariance_sensor_noise
        if covariance_sensor_noise is not None:
            trans_sensor_noise = cell_wrapper.output_conversion.inverse_transform_covariance(
                transformed_covariance=trans_sensor_noise)
            asoh_sensor_noise = asoh_wrapper.output_conversion.inverse_transform_covariance(
                transformed_covariance=asoh_sensor_noise)

        # Initialize filters
        trans_filter = UKF(
            model=cell_wrapper,
            initial_hidden=transients_hidden.convert(
                conversion_operator=cell_wrapper.hidden_conversion, inverse=True),
            initial_controls=initial_controls.convert(
                conversion_operator=cell_wrapper.control_conversion, inverse=True),
            covariance_process_noise=trans_proc_noise,
            covariance_sensor_noise=trans_sensor_noise,
            **filter_args.get('transient', UKFTuningParameters.defaults()))
        asoh_filter = UKF(
            model=asoh_wrapper,
            initial_hidden=asoh_hidden.convert(
                conversion_operator=asoh_wrapper.hidden_conversion, inverse=True),
            initial_controls=initial_controls.convert(
                conversion_operator=asoh_wrapper.control_conversion, inverse=True),
            covariance_process_noise=asoh_proc_noise,
            covariance_sensor_noise=asoh_sensor_noise,
            **filter_args.get('asoh', UKFTuningParameters.defaults()))

        return DualEstimator(transient_filter=trans_filter, asoh_filter=asoh_filter)
