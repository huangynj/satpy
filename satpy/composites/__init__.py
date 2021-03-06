#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2015-2016

# Author(s):

#   Martin Raspaud <martin.raspaud@smhi.se>
#   David Hoese <david.hoese@ssec.wisc.edu>

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Base classes for composite objects.
"""

import logging
import os

import numpy as np
import six
import yaml

from satpy.config import (CONFIG_PATH, config_search_paths,
                          recursive_dict_update)
from satpy.projectable import InfoObject, Projectable, combine_info
from satpy.readers import DatasetID
from satpy.tools import sunzen_corr_cos

try:
    import configparser
except:
    from six.moves import configparser

LOG = logging.getLogger(__name__)


class IncompatibleAreas(Exception):

    """
    Error raised upon compositing things of different shapes.
    """
    pass


class CompositorLoader(object):

    """Read composites using the configuration files on disk.
    """

    def __init__(self, ppp_config_dir=CONFIG_PATH):
        from satpy.config import glob_config

        self.modifiers = {}
        self.compositors = {}
        self.ppp_config_dir = ppp_config_dir

    def load_sensor_composites(self, sensor_names):
        config_filenames = [sensor_name + ".yaml"
                            for sensor_name in sensor_names]
        for config_filename in config_filenames:
            LOG.debug("Looking for composites config file %s", config_filename)
            composite_configs = config_search_paths(
                os.path.join("composites", config_filename),
                self.ppp_config_dir)
            if not composite_configs:
                LOG.debug("No composite config found called %s",
                          config_filename)
                continue
            self._load_config(composite_configs)

    def load_compositor(self, key, sensor_names):
        for sensor_name in sensor_names:
            if sensor_name not in self.compositors:
                self.load_sensor_composites([sensor_name])
            try:
                return self.compositors[sensor_name][key]
            except KeyError:
                continue
        raise KeyError

    def load_compositors(self, sensor_names):
        res = {}
        for sensor_name in sensor_names:
            if sensor_name not in self.compositors:
                self.load_sensor_composites([sensor_name])

            res.update(self.compositors[sensor_name])
        return res

    def _process_composite_config(self, composite_name, conf,
                                  composite_type, sensor_id, composite_config, **kwargs):

        compositors = self.compositors[sensor_id]
        modifiers = self.modifiers[sensor_id]

        options = conf[composite_type][composite_name]

        try:
            loader = options.pop('compositor')
        except KeyError:
            if composite_name in compositors or composite_name in modifiers:
                return conf
            raise ValueError("'compositor' missing or empty in %s" %
                             composite_config)

        options['name'] = composite_name

        # fix prerequisites in case of modifiers

        for prereq_type in ['prerequisites', 'optional_prerequisites']:
            prereqs = []
            for item in options.get(prereq_type, []):
                if isinstance(item, dict):

                    mods = item.get('modifiers', tuple())

                    key = DatasetID(item.get('name'),
                                    item.get('wavelength'),
                                    item.get('resolution'),
                                    item.get('polarization'),
                                    item.get('calibration'),
                                    mods)

                    comp_id = DatasetID(item.get('name'),
                                        item.get('wavelength'),
                                        item.get('resolution'),
                                        item.get('polarization'),
                                        item.get('calibration'),
                                        tuple())
                    comp_name = item.get('name')
                    mods = key.modifiers
                    for modifier in mods:
                        prev_comp_name = comp_name
                        prev_comp_id = comp_id
                        new_mods = tuple(
                            list(comp_id.modifiers or []) + [modifier])
                        comp_id = DatasetID(key.name,
                                            key.wavelength,
                                            key.resolution,
                                            key.polarization,
                                            key.calibration,
                                            new_mods)
                        comp_name = key.name

                        try:
                            mloader, moptions = modifiers[modifier]
                        except KeyError:
                            self._process_composite_config(modifier, conf,
                                                           composite_type, sensor_id, composite_config, **kwargs)
                            mloader, moptions = modifiers[modifier]

                        moptions = moptions.copy()
                        moptions.update(**kwargs)
                        moptions['name'] = modifier
                        moptions['id'] = comp_id
                        moptions['prerequisites'] = (
                            [prev_comp_id] + moptions['prerequisites'])
                        moptions['sensor'] = sensor_id
                        compositors[comp_id] = mloader(**moptions)
                    prereqs.append(comp_id)
                else:
                    prereqs.append(item)
            options[prereq_type] = prereqs

        if composite_type == 'composites':
            options.update(**kwargs)
            options['id'] = options['name']
            comp = loader(**options)
            compositors[composite_name] = comp
        elif composite_type == 'modifiers':
            modifiers[composite_name] = loader, options

    def _load_config(self, composite_configs, **kwargs):
        if not isinstance(composite_configs, (list, tuple)):
            composite_configs = [composite_configs]

        conf = {}
        for composite_config in composite_configs:
            with open(composite_config) as conf_file:
                conf = recursive_dict_update(conf, yaml.load(conf_file))
        try:
            sensor_name = conf['sensor_name']
        except KeyError:
            LOG.debug('No "sensor_name" tag found in %s, skipping.',
                      composite_config)
            return

        sensor_id = sensor_name.split('/')[-1]
        sensor_deps = sensor_name.split('/')[:-1]

        compositors = self.compositors.setdefault(sensor_id, {})
        modifiers = self.modifiers.setdefault(sensor_id, {})

        for sensor_dep in reversed(sensor_deps):
            if sensor_dep not in self.compositors or sensor_dep not in self.modifiers:
                self.load_sensor_composites([sensor_dep])

        try:
            compositors.update(self.compositors[sensor_deps[-1]])
            modifiers.update(self.modifiers[sensor_deps[-1]])
        except IndexError:
            # No deps, so no updating is needed
            pass

        for composite_type in ['modifiers', 'composites']:
            if composite_type not in conf:
                continue
            for composite_name in conf[composite_type]:
                self._process_composite_config(composite_name, conf,
                                               composite_type, sensor_id, composite_config, **kwargs)

        return conf


class CompositeBase(InfoObject):

    def __init__(self,
                 name,
                 prerequisites=[],
                 optional_prerequisites=[],
                 metadata_requirements=[],
                 **kwargs):
        # Required info
        kwargs["name"] = name
        kwargs["prerequisites"] = prerequisites
        kwargs["optional_prerequisites"] = optional_prerequisites
        kwargs["metadata_requirements"] = metadata_requirements
        super(CompositeBase, self).__init__(**kwargs)

    def __call__(self, datasets, optional_datasets=None, **info):
        raise NotImplementedError()

    def __str__(self):
        from pprint import pformat
        return pformat(self.info)

    def __repr__(self):
        from pprint import pformat
        return pformat(self.info)

    def apply_modifier_info(self, origin, destination):
        mods = origin.info.get('modifiers', [])
        if mods is None:
            mods = []
        else:
            mods = list(mods)

        mods.append(self.info['name'])
        old_id = origin.info['id']
        did = DatasetID(old_id.name,
                        old_id.wavelength,
                        old_id.resolution,
                        old_id.polarization,
                        old_id.calibration,
                        tuple(mods))

        destination.info['modifiers'] = mods
        destination.info['id'] = did


class SunZenithCorrector(CompositeBase):
    # FIXME: the cache should be cleaned up
    coszen = {}

    def __call__(self, projectables, **info):
        vis = projectables[0]
        if vis.info.get("sunz_corrected"):
            LOG.debug("Sun zen correction already applied")
            return vis

        if hasattr(vis.info["area"], 'name'):
            area_name = vis.info["area"].name
        else:
            area_name = 'swath' + str(vis.info["area"].lons.shape)
        key = (vis.info["start_time"], area_name)
        LOG.debug("Applying sun zen correction")
        if len(projectables) == 1:
            if key not in self.coszen:
                from pyorbital.astronomy import cos_zen
                LOG.debug("Computing sun zenith angles.")
                self.coszen[key] = np.ma.masked_outside(cos_zen(vis.info["start_time"],
                                                                *vis.info["area"].get_lonlats()),
                                                        # about 88 degrees.
                                                        0.035,
                                                        1,
                                                        copy=False)
            coszen = self.coszen[key]
        else:
            coszen = np.cos(np.deg2rad(projectables[1]))

        if vis.shape != coszen.shape:
            # assume we were given lower resolution szen data than band data
            LOG.debug(
                "Interpolating coszen calculations for higher resolution band")
            factor = int(vis.shape[1] / coszen.shape[1])
            coszen = np.repeat(
                np.repeat(coszen, factor, axis=0), factor, axis=1)

        # sunz correction will be in place so we need a copy
        proj = vis.copy()
        proj = sunzen_corr_cos(proj, coszen)
        vis.mask[coszen < 0] = True
        self.apply_modifier_info(vis, proj)
        return proj


class PSPRayleighReflectance(CompositeBase):

    def __call__(self, projectables, optional_datasets=None, **info):
        """Get the corrected reflectance when removing Rayleigh scattering. Uses
        pyspectral.
        """

        from pyspectral.rayleigh import Rayleigh

        (vis,) = projectables
        try:
            (sata, satz, suna, sunz) = optional_datasets
        except ValueError:
            from pyorbital.astronomy import get_alt_az, sun_zenith_angle
            from pyorbital.orbital import get_observer_look
            sunalt, suna = get_alt_az(
                vis.info['start_time'], *vis.info['area'].get_lonlats())
            sunz = sun_zenith_angle(
                vis.info['start_time'], *vis.info['area'].get_lonlats())
            lons, lats = vis.info['area'].get_lonlats()
            sata, satel = get_observer_look(vis.info['satellite_longitude'],
                                            vis.info['satellite_latitude'],
                                            vis.info['satellite_altitude'],
                                            vis.info['start_time'],
                                            lons, lats, 0)
            satz = 90 - satel

        LOG.info('Removing Rayleigh scattering')

        ssadiff = np.abs(suna - sata)
        ssadiff = np.where(np.greater(ssadiff, 180), 360 - ssadiff, ssadiff)

        corrector = Rayleigh(
            vis.info['platform_name'], vis.info['sensor'], atmosphere='us-standard', rural_aerosol=False)

        refl_cor_band = corrector.get_reflectance(
            sunz, satz, ssadiff, vis.info['id'].wavelength[1], vis)

        proj = Projectable(vis - refl_cor_band,
                           copy=False,
                           **vis.info)
        self.apply_modifier_info(vis, proj)

        return proj


class NIRReflectance(CompositeBase):

    def __call__(self, projectables, optional_datasets=None, **info):
        """Get the reflectance part of an NIR channel. Not supposed to be used
        for wavelength outside [3, 4] µm.
        """
        try:
            from pyspectral.near_infrared_reflectance import Calculator
        except ImportError:
            LOG.info("Couldn't load pyspectral")
            raise

        nir, tb11 = projectables
        LOG.info('Getting reflective part of %s', nir.info['name'])

        sun_zenith = None
        tb13_4 = None

        for dataset in optional_datasets:
            if dataset.info["standard_name"] == "solar_zenith_angle":
                sun_zenith = dataset
            elif (dataset.info['units'] == 'K' and
                  "wavelengh" in dataset.info and
                  dataset.info["wavelength"][0] <= 13.4 <= dataset.info["wavelength"][2]):
                tb13_4 = dataset

        # Check if the sun-zenith angle was provided:
        if sun_zenith is None:
            from pyorbital.astronomy import sun_zenith_angle as sza
            lons, lats = nir.info["area"].get_lonlats()
            sun_zenith = sza(nir.info['start_time'], lons, lats)

        refl39 = Calculator(nir.info['platform_name'],
                            nir.info['sensor'], nir.info['name'])

        proj = Projectable(refl39.reflectance_from_tbs(sun_zenith, nir,
                                                       tb11, tb13_4),
                           **nir.info)
        self.apply_modifier_info(nir, proj)

        return proj


class CO2Corrector(CompositeBase):

    def __call__(self, projectables, optional_datasets=None, **info):
        """CO2 correction of the brightness temperature of the MSG 3.9um
        channel.

        .. math::

          T4_CO2corr = (BT(IR3.9)^4 + Rcorr)^0.25
          Rcorr = BT(IR10.8)^4 - (BT(IR10.8)-dt_CO2)^4
          dt_CO2 = (BT(IR10.8)-BT(IR13.4))/4.0
        """
        (ir_039, ir_108, ir_134) = projectables
        LOG.info('Applying CO2 correction')
        dt_co2 = (ir_108 - ir_134) / 4.0
        rcorr = ir_108**4 - (ir_108 - dt_co2)**4
        t4_co2corr = ir_039**4 + rcorr
        t4_co2corr = np.ma.where(t4_co2corr > 0.0, t4_co2corr, 0)
        t4_co2corr = t4_co2corr**0.25

        info = ir_039.info.copy()

        proj = Projectable(t4_co2corr, mask=t4_co2corr.mask, **info)

        self.apply_modifier_info(ir_039, proj)

        return proj


class RGBCompositor(CompositeBase):

    def __call__(self, projectables, nonprojectables=None, **info):
        if len(projectables) != 3:
            raise ValueError("Expected 3 datasets, got %d" %
                             (len(projectables), ))
        try:
            the_data = np.rollaxis(
                np.ma.dstack([projectable for projectable in projectables]),
                axis=2)
        except ValueError:
            raise IncompatibleAreas
        # info = projectables[0].info.copy()
        # info.update(projectables[1].info)
        # info.update(projectables[2].info)
        info = combine_info(*projectables)
        info.update(self.info)
        info['id'] = DatasetID(self.info['name'])
        # FIXME: should this be done here ?
        info["wavelength_range"] = None
        info.pop("units", None)
        sensor = set()
        for projectable in projectables:
            current_sensor = projectable.info.get("sensor", None)
            if current_sensor:
                if isinstance(current_sensor, (str, bytes, six.text_type)):
                    sensor.add(current_sensor)
                else:
                    sensor |= current_sensor
        if len(sensor) == 0:
            sensor = None
        elif len(sensor) == 1:
            sensor = list(sensor)[0]
        info["sensor"] = sensor
        info["mode"] = "RGB"
        return Projectable(data=the_data, **info)


class Airmass(RGBCompositor):

    def __call__(self, projectables, *args, **kwargs):
        """Make an airmass RGB image composite.

        +--------------------+--------------------+--------------------+
        | Channels           | Temp               | Gamma              |
        +====================+====================+====================+
        | WV6.2 - WV7.3      |     -25 to 0 K     | gamma 1            |
        +--------------------+--------------------+--------------------+
        | IR9.7 - IR10.8     |     -40 to 5 K     | gamma 1            |
        +--------------------+--------------------+--------------------+
        | WV6.2              |   243 to 208 K     | gamma 1            |
        +--------------------+--------------------+--------------------+
        """
        try:
            res = RGBCompositor.__call__(self, (projectables[0] - projectables[1],
                                                projectables[2] -
                                                projectables[3],
                                                projectables[0]), *args, **kwargs)
        except ValueError:
            raise IncompatibleAreas
        return res


class Convection(RGBCompositor):

    def __call__(self, projectables, *args, **kwargs):
        """Make a Severe Convection RGB image composite.

        +--------------------+--------------------+--------------------+
        | Channels           | Span               | Gamma              |
        +====================+====================+====================+
        | WV6.2 - WV7.3      |     -30 to 0 K     | gamma 1            |
        +--------------------+--------------------+--------------------+
        | IR3.9 - IR10.8     |      0 to 55 K     | gamma 1            |
        +--------------------+--------------------+--------------------+
        | IR1.6 - VIS0.6     |    -70 to 20 %     | gamma 1            |
        +--------------------+--------------------+--------------------+
        """
        try:
            res = RGBCompositor.__call__(self, (projectables[3] - projectables[4],
                                                projectables[2] -
                                                projectables[5],
                                                projectables[1] - projectables[0]),
                                         *args, **kwargs)
        except ValueError:
            raise IncompatibleAreas
        return res


class Dust(RGBCompositor):

    def __call__(self, projectables, *args, **kwargs):
        """Make a Dust RGB image composite.

        +--------------------+--------------------+--------------------+
        | Channels           | Temp               | Gamma              |
        +====================+====================+====================+
        | IR12.0 - IR10.8    |     -4 to 2 K      | gamma 1            |
        +--------------------+--------------------+--------------------+
        | IR10.8 - IR8.7     |     0 to 15 K      | gamma 2.5          |
        +--------------------+--------------------+--------------------+
        | IR10.8             |   261 to 289 K     | gamma 1            |
        +--------------------+--------------------+--------------------+
        """
        try:
            res = RGBCompositor.__call__(self, (projectables[2] - projectables[1],
                                                projectables[1] -
                                                projectables[0],
                                                projectables[1]), *args, **kwargs)
        except ValueError:
            raise IncompatibleAreas

        return res
