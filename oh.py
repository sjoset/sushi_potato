#!/usr/bin/env python3

import os
import pathlib
import sys
import logging as log
import numpy as np
import pandas as pd

from astropy.io import fits
from astropy.time import Time

from scipy.interpolate import interp1d

# import astropy.units as u
from photutils.aperture import (
    CircularAperture,
    ApertureStats,
    CircularAnnulus,
)

from argparse import ArgumentParser
from typing import Dict

# import matplotlib.pyplot as plt
#
# from astropy.visualization import (
#     ZScaleInterval,
# )

from read_swift_config import read_swift_config
from swift_types import (
    # SwiftData,
    # SwiftObservationLog,
    SwiftFilter,
    filter_to_string,
    # SwiftStackingMethod,
    # SwiftPixelResolution,
    SwiftUVOTImage,
    SwiftStackedUVOTImage,
)

# from swift_observation_log import (
#     read_observation_log,
#     match_within_timeframe,
# )
from stacking import (
    # stack_image_by_selection,
    # write_stacked_image,
    read_stacked_image,
    # includes_uvv_and_uw1_filters,
)
from dataclasses import dataclass


@dataclass
class AperturePhotometryResult:
    net_count: float
    net_count_rate: float
    source_count: float
    source_count_rate: float
    background_count: float
    background_count_rate: float


@dataclass
class FilterEffectiveArea:
    lambdas: np.ndarray
    responses: np.ndarray


@dataclass
class SolarSpectrum:
    lambdas: np.ndarray
    irradiances: np.ndarray


__version__ = "0.0.1"


def process_args():
    # Parse command-line arguments
    parser = ArgumentParser(
        usage="%(prog)s [options] [inputfile]",
        description=__doc__,
        prog=os.path.basename(sys.argv[0]),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--verbose", "-v", action="count", default=0, help="increase verbosity level"
    )
    parser.add_argument(
        "--config", "-c", default="config.yaml", help="YAML configuration file to use"
    )
    parser.add_argument(
        "observation_log_file", nargs=1, help="Filename of observation log input"
    )

    args = parser.parse_args()

    # handle verbosity
    if args.verbose >= 2:
        log.basicConfig(format="%(levelname)s: %(message)s", level=log.DEBUG)
    elif args.verbose == 1:
        log.basicConfig(format="%(levelname)s: %(message)s", level=log.INFO)
    else:
        log.basicConfig(format="%(levelname)s: %(message)s")

    return args


# def show_fits_scaled(image_data):
#     fig = plt.figure()
#     ax1 = fig.add_subplot(1, 1, 1)
#
#     zscale = ZScaleInterval()
#     vmin, vmax = zscale.get_limits(image_data)
#
#     im1 = ax1.imshow(image_data, vmin=vmin, vmax=vmax)
#     fig.colorbar(im1)
#
#     plt.show()


def read_effective_area(effective_area_path: pathlib.Path) -> FilterEffectiveArea:
    # TODO: tag with astropy units convert later?
    filter_ea_data = fits.open(effective_area_path)[1].data  # type: ignore
    ea_lambdas = (filter_ea_data["WAVE_MIN"] + filter_ea_data["WAVE_MAX"]) / 2
    # wavelengths are given in angstroms: convert to nm
    ea_lambdas = ea_lambdas / 10
    ea_responses = filter_ea_data["SPECRESP"]

    return FilterEffectiveArea(lambdas=ea_lambdas, responses=ea_responses)


def read_solar_spectrum(
    solar_spectrum_path: pathlib.Path, solar_spectrum_time: Time
) -> SolarSpectrum:
    # TODO: find the nearest solar spectrum to the stacked image's mid exposure time

    # get the solar spectrum for the given julian date
    solar_spectrum_df = pd.read_csv(solar_spectrum_path)
    solar_spectrum_df["time (Julian Date)"].map(lambda x: Time(x, format="jd"))
    solar_mask = solar_spectrum_df["time (Julian Date)"] == np.round(
        solar_spectrum_time.jd
    )
    solar_spectrum = solar_spectrum_df[solar_mask]
    solar_lambdas = solar_spectrum["wavelength (nm)"]
    solar_irradiances = solar_spectrum["irradiance (W/m^2/nm)"]

    return SolarSpectrum(lambdas=solar_lambdas, irradiances=solar_irradiances)


# TODO: rename this
# Convolution of the solar spectrum with the filter response?
def mag_sb_flux_from_spec(
    solar_spectrum_path: pathlib.Path,
    solar_spectrum_time: Time,
    effective_area_path: pathlib.Path,
):
    """use effective area and theoretical spectra
    to calculate apparent magnitude
    """

    # TODO: can we rewrite this in terms of np.convolve?
    # TODO: find the nearest solar spectrum to the stacked image's mid exposure time

    solar_spectrum = read_solar_spectrum(solar_spectrum_path, solar_spectrum_time)
    solar_lambdas = solar_spectrum.lambdas
    solar_irradiances = solar_spectrum.irradiances

    ea_data = read_effective_area(effective_area_path=effective_area_path)
    ea_lambdas = ea_data.lambdas
    ea_responses = ea_data.responses

    # interpolate ea to cater for spec
    ea_response_interpolated = interp1d(
        ea_lambdas, ea_responses, fill_value="extrapolate"  # type: ignore
    )
    responses_on_solar_lambdas = ea_response_interpolated(solar_lambdas)

    # decide integration bounds
    wave_min = max([np.min(solar_lambdas), np.min(ea_lambdas)])
    wave_max = min([np.max(solar_lambdas), np.max(ea_lambdas)])

    spec = np.c_[
        np.c_[solar_lambdas, solar_irradiances.T], responses_on_solar_lambdas.T
    ]
    spec_reduce = spec[spec[:, 0] > wave_min, :]
    spec_reduce = spec_reduce[spec_reduce[:, 0] < wave_max, :]
    spec = spec_reduce[spec_reduce[:, 2] > 0, :]

    # integral
    delta_wave = spec[1, 0] - spec[0, 0]
    cr = 0.0
    for i in range(len(spec)):
        cr += (
            spec[i, 0] * spec[i, 1] * spec[i, 2] * delta_wave * 1e7 * 5.034116651114543
        )  # 10^8 for Kurucz

    # cr to mag
    return cr


# # TODO: find this file for each filter
# def read_ea(filter_type: SwiftFilter):
#     ea_path = get_path("../data/auxil/arf_" + filt + ".fits")
#     ea_data = fits.open(ea_path)[1].data
#     ea_wave = (ea_data["WAVE_MIN"] + ea_data["WAVE_MAX"]) / 2
#     ea_area = ea_data["SPECRESP"]
#     return ea_wave, ea_area


# TODO:
def reddening_correction(
    effective_area_uw1_path: pathlib.Path,
    effective_area_uvv_path: pathlib.Path,
    dust_redness: float,
):
    """get the correction factor of beta
    r: %/100nm
    """

    ea_data_uw1 = read_effective_area(effective_area_path=effective_area_uw1_path)
    uw1_lambdas = ea_data_uw1.lambdas
    uw1_responses = ea_data_uw1.responses
    ea_data_uvv = read_effective_area(effective_area_path=effective_area_uvv_path)
    uvv_lambdas = ea_data_uvv.lambdas
    uvv_responses = ea_data_uvv.responses

    wave_uw1 = 0
    ea_uw1 = 0
    wave_v = 0
    ea_v = 0

    delta_wave_uw1 = uw1_lambdas[1] - uw1_lambdas[0]
    delta_wave_v = uvv_lambdas[1] - uvv_lambdas[0]
    for i in range(len(uw1_lambdas)):
        wave_uw1 += uw1_lambdas[i] * uw1_responses[i] * delta_wave_uw1
        ea_uw1 += uw1_responses[i] * delta_wave_uw1
    wave_uw1 = wave_uw1 / ea_uw1
    for i in range(len(uvv_lambdas)):
        wave_v += uvv_lambdas[i] * uvv_responses[i] * delta_wave_v
        ea_v += uvv_responses[i] * delta_wave_v
    wave_v = wave_v / ea_v
    # get reddening correction factor
    middle_factor = (wave_v - wave_uw1) * dust_redness / 200000
    return (1 - middle_factor) / (1 + middle_factor)


def OH_flux_from_count_rate(
    solar_spectrum_path: pathlib.Path,
    solar_spectrum_time: Time,
    effective_area_uw1_path: pathlib.Path,
    effective_area_uvv_path: pathlib.Path,
    result_uw1: AperturePhotometryResult,
    result_uvv: AperturePhotometryResult,
    dust_redness,
):
    alpha = 2.0

    print("---------")

    # print("Getting magnitude of sun in uw1 ...")
    sun_count_rate_in_uw1 = mag_sb_flux_from_spec(
        solar_spectrum_path=solar_spectrum_path,
        solar_spectrum_time=solar_spectrum_time,
        effective_area_path=effective_area_uw1_path,
        # filter_type=SwiftFilter.uw1,
    )
    # print("Getting magnitude of sun in uvv ...")
    sun_count_rate_in_uvv = mag_sb_flux_from_spec(
        solar_spectrum_path=solar_spectrum_path,
        solar_spectrum_time=solar_spectrum_time,
        effective_area_path=effective_area_uvv_path,
        # filter_type=SwiftFilter.uvv,
    )

    print(f"solar count rate in uw1: {sun_count_rate_in_uw1}")
    print(f"solar count rate in uvv: {sun_count_rate_in_uvv}")
    beta = sun_count_rate_in_uw1 / sun_count_rate_in_uvv
    beta = (
        reddening_correction(
            effective_area_uw1_path, effective_area_uvv_path, dust_redness
        )
        * beta
    )
    print(f"Beta: {beta}")

    oh_flux = alpha * (result_uw1.net_count_rate - beta * result_uvv.net_count_rate)
    print(f"OH flux: {oh_flux}")
    print("---------")

    # TODO: cr_to_flux.py -> error_prop
    # propogate error for beta * count_rate_uvv
    # propogate that into oh_count_rate

    return oh_flux


def OH_flux_from_count_rate1(
    solar_spectrum_path: pathlib.Path,
    solar_spectrum_time: Time,
    effective_area_uw1_path: pathlib.Path,
    effective_area_uvv_path: pathlib.Path,
    result_uw1: AperturePhotometryResult,
    result_uvv: AperturePhotometryResult,
    dust_redness,
):
    """get OH flux from OH cr"""
    beta = 0.09276191501510327
    beta = (
        reddening_correction(
            effective_area_uw1_path=effective_area_uw1_path,
            effective_area_uvv_path=effective_area_uvv_path,
            dust_redness=dust_redness,
        )
        * beta
    )
    print(f"Beta: {beta}")

    cr_ref_uw1 = beta * result_uvv.net_count_rate
    # cr_ref_uw1_err = beta * cr_v_err
    cr_OH = result_uw1.net_count_rate - cr_ref_uw1
    # cr_OH_err = error_prop("sub", cr_uw1, cr_uw1_err, cr_ref_uw1, cr_ref_uw1_err)

    flux_OH = cr_OH * 1.2750906353215913e-12
    print(f"OH flux: {flux_OH}")
    # flux_OH_err = cr_OH_err * 1.2750906353215913e-12

    # flux_uw1, flux_uw1_err = flux_ref_uw1(
    #     spec_name_sun, spec_name_OH, cr_uw1, cr_uw1_err, cr_v, cr_v_err, r
    # )
    # flux_v, flux_v_err = flux_ref_v(
    #     spec_name_sun, spec_name_OH, cr_uw1, cr_uw1_err, cr_v, cr_v_err, r
    # )
    # if if_show == True:
    #     print(
    #         "flux of uw1 (reflection): " + str(flux_uw1) + " +/- " + str(flux_uw1_err)
    #     )
    #     print("flux of v: " + str(flux_v) + " +/- " + str(flux_v_err))
    # return flux_OH, flux_OH_err
    return flux_OH


# TODO: cite these from the swift documentation
# TODO: figure out what 'cf' stands for
# TODO: Make SwiftFilterParameters a dataclass?  Use tying.Final to attempt to make these constants?
# TODO: these are all technically a function of time, so we should incorporate that
def get_filter_parameters(filter_type: SwiftFilter) -> Dict:
    filter_params = {
        SwiftFilter.uvv: {
            "fwhm": 769,
            "zero_point": 17.89,
            "zero_point_err": 0.013,
            "cf": 2.61e-16,
            "cf_err": 2.4e-18,
        },
        SwiftFilter.ubb: {
            "fwhm": 975,
            "zero_point": 19.11,
            "zero_point_err": 0.016,
            "cf": 1.32e-16,
            "cf_err": 9.2e-18,
        },
        SwiftFilter.uuu: {
            "fwhm": 785,
            "zero_point": 18.34,
            "zero_point_err": 0.020,
            "cf": 1.5e-16,
            "cf_err": 1.4e-17,
        },
        SwiftFilter.uw1: {
            "fwhm": 693,
            "zero_point": 17.49,
            "zero_point_err": 0.03,
            "cf": 4.3e-16,
            "cf_err": 2.1e-17,
            "rf": 0.1375,
        },
        SwiftFilter.um2: {
            "fwhm": 498,
            "zero_point": 16.82,
            "zero_point_err": 0.03,
            "cf": 7.5e-16,
            "cf_err": 1.1e-17,
        },
        SwiftFilter.uw2: {
            "fwhm": 657,
            "zero_point": 17.35,
            "zero_point_err": 0.04,
            "cf": 6.0e-16,
            "cf_err": 6.4e-17,
        },
    }
    return filter_params[filter_type]


# TODO: error propogation
def magnitude_from_count_rate(count_rate, filter_type) -> float:
    filter_params = get_filter_parameters(filter_type)
    mag = filter_params["zero_point"] - 2.5 * np.log10(count_rate)
    # mag_err_1 = 2.5*cr_err/(np.log(10)*cr)
    # mag_err_2 = filt_para(filt)['zero_point_err']
    # mag_err = np.sqrt(mag_err_1**2 + mag_err_2**2)
    # return mag, mag_err
    return mag


def determine_background(image_data: SwiftUVOTImage) -> float:
    # make the background aperture start at r=80 out to r=160
    comet_aperture_radius = 80

    image_center_row = np.ceil(image_data.shape[0] / 2)
    image_center_col = np.ceil(image_data.shape[1] / 2)

    comet_aperture = CircularAnnulus(
        (image_center_row, image_center_col),
        r_in=comet_aperture_radius,
        r_out=2 * comet_aperture_radius,
    )

    aperture_stats = ApertureStats(image_data, comet_aperture)
    return aperture_stats.median  # type: ignore


def do_aperture_photometry(
    stacked_sum: SwiftStackedUVOTImage, stacked_median: SwiftStackedUVOTImage
) -> AperturePhotometryResult:
    image_sum = stacked_sum.stacked_image
    image_median = stacked_median.stacked_image

    print("\n---------")
    print(f"Aperture photometry for {filter_to_string(stacked_sum.filter_type)}")
    # assume comet is centered in the stacked image, and that the stacked image has on odd number of pixels (the stacker should ensure this during stacking)
    image_center_row = np.ceil(image_sum.shape[0] / 2)
    image_center_col = np.ceil(image_sum.shape[1] / 2)
    print(f"Aperture center: {image_center_row}, {image_center_col}")

    # aperture radius: hard-coded
    comet_aperture_radius = 80
    comet_aperture = CircularAperture(
        (image_center_row, image_center_col), r=comet_aperture_radius
    )

    # use the aperture on the image
    aperture_stats = ApertureStats(image_sum, comet_aperture)
    print(f"Centroid of aperture: {aperture_stats.centroid}")
    # print("mean, median, std, pixel area, total count:")
    # print(
    #     aperture_stats.mean,
    #     aperture_stats.median,
    #     aperture_stats.std,
    #     aperture_stats.sum_aper_area.value,
    #     aperture_stats.sum,
    # )

    # try using median-stacked image for getting the background
    background_counts_per_pixel = determine_background(image_median)
    print(f"Background: {background_counts_per_pixel}")

    total_background_counts = (
        aperture_stats.sum_aper_area.value * background_counts_per_pixel
    )
    print(f"Background counts in aperture: {total_background_counts}")

    net_counts = aperture_stats.sum - total_background_counts
    print(f"Total counts in aperture, background corrected: {net_counts}")
    print(f"Count rate: {net_counts/stacked_sum.exposure_time} counts per second")

    print(stacked_sum.filter_type)
    comet_magnitude = magnitude_from_count_rate(net_counts, stacked_sum.filter_type)
    print(f"Magnitude: {comet_magnitude}")
    print("---------\n")

    return AperturePhotometryResult(
        net_count=net_counts,
        net_count_rate=net_counts / stacked_sum.exposure_time,
        source_count=aperture_stats.sum,  # type: ignore
        source_count_rate=aperture_stats.sum / stacked_sum.exposure_time,  # type: ignore
        background_count=total_background_counts,
        background_count_rate=total_background_counts / stacked_sum.exposure_time,
    )


def main():
    args = process_args()

    swift_config = read_swift_config(pathlib.Path(args.config))
    if swift_config is None:
        print("Error reading config file {args.config}, exiting.")
        return 1

    # stacked_image_dir = swift_config["stacked_image_dir"]
    #
    # swift_data = SwiftData(
    #     data_path=pathlib.Path(swift_config["swift_data_dir"]).expanduser().resolve()
    # )
    #
    # obs_log = read_observation_log(args.observation_log_file[0])

    uw1_median_path = pathlib.Path(
        "stacked_test/00034423001_through_00034832003_uw1_median.fits"
    )
    uw1_median_info_path = pathlib.Path(
        "stacked_test/00034423001_through_00034832003_uw1_median.json"
    )
    uw1_sum_path = pathlib.Path(
        "stacked_test/00034423001_through_00034832003_uw1_sum.fits"
    )
    uw1_sum_info_path = pathlib.Path(
        "stacked_test/00034423001_through_00034832003_uw1_sum.json"
    )

    uvv_sum_path = pathlib.Path(
        "stacked_test/00034422002_through_00034832002_uvv_sum.fits"
    )
    uvv_sum_info_path = pathlib.Path(
        "stacked_test/00034422002_through_00034832002_uvv_sum.json"
    )
    uvv_median_path = pathlib.Path(
        "stacked_test/00034422002_through_00034832002_uvv_median.fits"
    )
    uvv_median_info_path = pathlib.Path(
        "stacked_test/00034422002_through_00034832002_uvv_median.json"
    )

    uw1_sum = read_stacked_image(
        stacked_image_path=uw1_sum_path, stacked_image_info_path=uw1_sum_info_path
    )
    uw1_median = read_stacked_image(
        stacked_image_path=uw1_median_path, stacked_image_info_path=uw1_median_info_path
    )
    uvv_sum = read_stacked_image(
        stacked_image_path=uvv_sum_path, stacked_image_info_path=uvv_sum_info_path
    )
    uvv_median = read_stacked_image(
        stacked_image_path=uvv_median_path, stacked_image_info_path=uvv_median_info_path
    )

    if uw1_sum is None or uw1_median is None:
        return

    if uvv_sum is None or uvv_median is None:
        return

    result_uw1 = do_aperture_photometry(uw1_sum, uw1_median)
    result_uvv = do_aperture_photometry(uvv_sum, uvv_median)

    print("Calculating OH flux ...")
    OH_flux_from_count_rate(
        solar_spectrum_path=swift_config["solar_spectrum_path"],
        solar_spectrum_time=Time("2457753", format="jd"),
        effective_area_uw1_path=swift_config["effective_area_uw1_path"],
        effective_area_uvv_path=swift_config["effective_area_uvv_path"],
        result_uw1=result_uw1,
        result_uvv=result_uvv,
        dust_redness=0.50,
    )
    OH_flux_from_count_rate1(
        solar_spectrum_path=swift_config["solar_spectrum_path"],
        solar_spectrum_time=Time("2457753", format="jd"),
        effective_area_uw1_path=swift_config["effective_area_uw1_path"],
        effective_area_uvv_path=swift_config["effective_area_uvv_path"],
        result_uw1=result_uw1,
        result_uvv=result_uvv,
        dust_redness=0.50,
    )

    # show_fits_scaled(uw1.stacked_image)


if __name__ == "__main__":
    sys.exit(main())
