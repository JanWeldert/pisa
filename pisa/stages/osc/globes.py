"""
PISA pi stage wrapping GLoBES for the calculation of neutrino oscillation probabilities.

Allows for the calculation of sterile neutrino oscillation probabilities.
This needs Andrii's GLoBES wrapper, which has been forked to be
made compatible with Python3:

https://github.com/atrettin/GLoBES_wrapper

To import, this stage takes as input the path to the GLoBES wrapper. This is necessary
because GLoBES has to be imported while in the wrapper directory.
"""

from __future__ import absolute_import, print_function, division

import os
import sys

import numpy as np
from numba import guvectorize

from pisa import FTYPE, TARGET, ureg
from pisa.core.param import Param, ParamSet
from pisa.core.stage import Stage
from pisa.stages.osc.layers import Layers
from pisa.stages.osc.osc_params import OscParams
from pisa.utils.profiler import profile
from pisa.utils.resources import find_resource

__all__ = ['globes', 'init_test']


class globes(Stage):  # pylint: disable=invalid-name
    """
    GLoBES PISA Pi class

    Parameters
    ----------
    earth_model : PREM file path
    globes_wrapper : path to globes wrapper
    detector_depth : float
    prop_height : quantity (dimensionless)
    params : ParamSet or sequence with which to instantiate a ParamSet.
        Expected params .. ::

            theta12 : quantity (angle)
            theta13 : quantity (angle)
            theta23 : quantity (angle)
            deltam21 : quantity (mass^2)
            deltam31 : quantity (mass^2)
            deltam41 : quantity (mass^2)
            theta24 : quantity (angle)
            theta34 : quantity (angle)
            deltacp : quantity (angle)

        Expected container keys are .. ::

            "true_energy"
            "true_coszen"
            "nubar"
            "flav"
            "nu_flux"
            "weights"

    """
    def __init__(
        self,
        earth_model,
        globes_wrapper,
        detector_depth=2.*ureg.km,
        prop_height=20.*ureg.km,
        **std_kwargs,
    ):

        expected_params = (
            'theta12',
            'theta13',
            'theta23',
            'deltam21',
            'deltam31',
            'deltam41',
            'theta24',
            'theta34',
            'deltacp',
        )

        expected_container_keys = (
            'true_energy',
            'true_coszen',
            'nubar',
            'flav',
            'nu_flux',
            'weights'
        )

        # init base class
        super().__init__(
            expected_params=expected_params,
            expected_container_keys=expected_container_keys,
            **std_kwargs,
        )

        self.layers = None
        self.osc_params = None
        self.earth_model = earth_model
        self.globes_wrapper = globes_wrapper
        self.detector_depth = detector_depth
        self.prop_height = prop_height

        self.globes_calc = None

    @profile
    def setup_function(self):
        sys.path.append(self.globes_wrapper)
        import GLoBES
        ### you need to start GLoBES from the folder containing a dummy experiment
        # therefore we go to the folder, load GLoBES and then go back
        curdir = os.getcwd()
        os.chdir(self.globes_wrapper)
        self.globes_calc = GLoBES.GLoBESCalculator("calc")
        os.chdir(curdir)
        self.globes_calc.InitSteriles(2)
        # object for oscillation parameters
        self.osc_params = OscParams()
        earth_model = find_resource(self.earth_model)
        prop_height = self.prop_height.m_as('km')
        detector_depth = self.detector_depth.m_as('km')
        self.layers = Layers(earth_model, detector_depth, prop_height)
        # The electron fractions are taken into account internally by GLoBES/SNU.
        # See the SNU patch for details. It uses the density to decide
        # whether it is in the core or in the mantle. Therefore, we just multiply by
        # one to give GLoBES the raw densities.
        self.layers.setElecFrac(1., 1., 1.)

        # set the correct data mode
        self.data.representation = self.calc_mode

        # --- calculate the layers ---
        if self.data.is_map:
            # speed up calculation by adding links
            # as layers don't care about flavour
            self.data.link_containers('nu', ['nue_cc', 'numu_cc', 'nutau_cc',
                                             'nue_nc', 'numu_nc', 'nutau_nc',
                                             'nuebar_cc', 'numubar_cc', 'nutaubar_cc',
                                             'nuebar_nc', 'numubar_nc', 'nutaubar_nc'])

        for container in self.data:
            self.layers.calcLayers(container['true_coszen'])
            container['densities'] = self.layers.density.reshape((container.size, self.layers.max_layers))
            container['distances'] = self.layers.distance.reshape((container.size, self.layers.max_layers))

        # don't forget to un-link everything again
        self.data.unlink_containers()

        # setup probability containers
        for container in self.data:
            container['prob_e'] = np.empty((container.size), dtype=FTYPE)
            container['prob_mu'] = np.empty((container.size), dtype=FTYPE)
            container['prob_nonsterile'] = np.empty((container.size), dtype=FTYPE)
            if '_cc' in container.name:
                container['prob_nonsterile'] = np.ones(container.size)
            elif '_nc' in container.name:
                if 'nue' in container.name:
                    container['prob_e'] = np.ones(container.size)
                    container['prob_mu'] = np.zeros(container.size)
                elif 'numu' in container.name:
                    container['prob_e'] = np.zeros(container.size)
                    container['prob_mu'] = np.ones(container.size)
                elif 'nutau' in container.name:
                    container['prob_e'] = np.zeros(container.size)
                    container['prob_mu'] = np.zeros(container.size)
                else:
                    raise Exception('unknown container name: %s' % container.name)

    def calc_prob_e_mu(self, flav, nubar, energy, rho_array, len_array):
        '''Calculates probability for an electron/muon neutrino to oscillate into
        the flavour of a given event, including effects from sterile neutrinos.
        '''
        # We use the layers module to calculate lengths and densities.
        # The output must be converted into a regular python list.
        self.globes_calc.SetManualDensities(list(len_array), list(rho_array))
        # this calls the calculator without the calculation of layers
        # The flavour convention in GLoBES is that
        #  e = 1, mu = 2, tau = 3
        # while in PISA it's
        #  e = 0, mu = 1, tau = 2
        # which is why we add +1 to the flavour.
        # Nubar follows the same convention in PISA and GLoBES:
        #  +1 = particle, -1 = antiparticle
        nue_to_nux = self.globes_calc.MatterProbabilityPrevBaseline(1, flav+1, nubar, energy)
        numu_to_nux = self.globes_calc.MatterProbabilityPrevBaseline(2, flav+1, nubar, energy)
        return (nue_to_nux, numu_to_nux)

    def calc_prob_nonsterile(self, flav, nubar, energy, rho_array, len_array):
        '''Calculates the probability of a given neutrino to oscillate into
        another non-sterile flavour.
        '''
        # We use the layers module to calculate lengths and densities.
        # The output must be converted into a regular python list.
        self.globes_calc.SetManualDensities(list(len_array), list(rho_array))
        # this calls the calculator without the calculation of layers
        # The flavour convention in GLoBES is that
        #  e = 1, mu = 2, tau = 3
        # while in PISA it's
        #  e = 0, mu = 1, tau = 2
        # which is why we add +1 to the flavour.
        # Nubar follows the same convention in PISA and GLoBES:
        #  +1 = particle, -1 = antiparticle
        nux_to_nue = self.globes_calc.MatterProbabilityPrevBaseline(flav+1, 1, nubar, energy)
        nux_to_numu = self.globes_calc.MatterProbabilityPrevBaseline(flav+1, 2, nubar, energy)
        nux_to_nutau = self.globes_calc.MatterProbabilityPrevBaseline(flav+1, 3, nubar, energy)
        nux_to_nonsterile = nux_to_nue + nux_to_numu + nux_to_nutau
        return nux_to_nonsterile

    @profile
    def compute_function(self):
        # --- update mixing params ---
        params = [self.params.theta12.value.m_as('rad'),
                  self.params.theta13.value.m_as('rad'),
                  self.params.theta23.value.m_as('rad'),
                  self.params.deltacp.value.m_as('rad'),
                  self.params.deltam21.value.m_as('eV**2'),
                  self.params.deltam31.value.m_as('eV**2'),
                  self.params.deltam41.value.m_as('eV**2'),
                  0.0,
                  self.params.theta24.value.m_as('rad'),
                  self.params.theta34.value.m_as('rad'),
                  0.0,
                  0.0
                 ]
        self.globes_calc.SetParametersArr(params)
        # set the correct data mode
        self.data.representation = self.calc_mode

        for container in self.data:
            # standard oscillations are only applied to charged current events,
            # while the loss due to oscillation into sterile neutrinos is only
            # applied to neutral current events.
            # Accessing single entries from containers is very slow.
            # For this reason, we make a copy of the content we need that is
            # a simple numpy array.
            flav = container['flav']
            nubar = container['nubar']
            energies = np.array(container['true_energy'])
            densities = np.array(container['densities'])
            distances = np.array(container['distances'])
            prob_e = np.zeros(container.size)
            prob_mu = np.zeros(container.size)
            prob_nonsterile = np.zeros(container.size)
            if '_cc' in container.name:
                for i in range(container.size):
                    prob_e[i], prob_mu[i] = self.calc_prob_e_mu(flav,
                                                                nubar,
                                                                energies[i],
                                                                densities[i],
                                                                distances[i]
                                                               )
                container['prob_e'] = prob_e
                container['prob_mu'] = prob_mu
            elif '_nc' in container.name:
                for i in range(container.size):
                    prob_nonsterile[i] = self.calc_prob_nonsterile(flav,
                                                                   nubar,
                                                                   energies[i],
                                                                   densities[i],
                                                                   distances[i]
                                                                  )
                container['prob_nonsterile'] = prob_nonsterile
            else:
                raise Exception('unknown container name: %s' % container.name)
            container.mark_changed('prob_e')
            container.mark_changed('prob_mu')
            container.mark_changed('prob_nonsterile')

    @profile
    def apply_function(self):
        # update the outputted weights
        for container in self.data:
            apply_probs(container['nu_flux'],
                        container['prob_e'],
                        container['prob_mu'],
                        container['prob_nonsterile'],
                        out=container['weights'])
            container.mark_changed('weights')


# vectorized function to apply (flux * prob)
# must be outside class
if FTYPE == np.float64:
    signature = '(f8[:], f8, f8, f8, f8[:])'
else:
    signature = '(f4[:], f4, f4, f4, f4[:])'
@guvectorize([signature], '(d),(),(),()->()', target=TARGET)
def apply_probs(flux, prob_e, prob_mu, prob_nonsterile, out):
    out[0] *= ((flux[0] * prob_e) + (flux[1] * prob_mu))*prob_nonsterile


def init_test(**param_kwargs):
    """Initialisation example"""
    param_set = ParamSet([
        Param(name='theta12', value=33*ureg.degree, **param_kwargs),
        Param(name='theta13', value=8*ureg.degree, **param_kwargs),
        Param(name='theta23', value=50*ureg.degree, **param_kwargs),
        Param(name='theta24', value=1*ureg.degree, **param_kwargs),
        Param(name='theta34', value=0*ureg.degree, **param_kwargs),
        Param(name='deltam21', value=8e-5*ureg.eV**2, **param_kwargs),
        Param(name='deltam31', value=3e-3*ureg.eV**2, **param_kwargs),
        Param(name='deltam41', value=1.*ureg.eV**2, **param_kwargs),
        Param(name='deltacp', value=180*ureg.degree, **param_kwargs), 
    ])
    return globes(
        earth_model='osc/PREM_12layer.dat',
        globes_wrapper='GLoBES_wrapper', #FIXME
        params=param_set
    )
