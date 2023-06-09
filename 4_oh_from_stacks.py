#!/usr/bin/env python3

import os
import pathlib
import sys
import numpy as np
import logging as log

from astropy.time import Time

# from astropy.io import fits
from argparse import ArgumentParser
import matplotlib.pyplot as plt
from astropy.visualization import (
    ZScaleInterval,
)

# from typing import Tuple

from read_swift_config import read_swift_config
from swift_types import SwiftFilter, StackingMethod, SwiftUVOTImage
from reddening_correction import DustReddeningPercent
from stack_info import stacked_images_from_stackinfo
from fluorescence_OH import flux_OH_to_num_OH, read_gfactor_1au
from flux_OH import OH_flux_from_count_rate, OH_flux_from_count_rate_fixed_beta
from aperture_photometry import do_aperture_photometry
from num_OH_to_Q import num_OH_to_Q_vectorial

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
        "stackinfo",
        default=None,
        nargs="?",
        help="JSON file containing stacking information",
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


def show_fits_sum_and_median_scaled(
    image_sum,
    image_median,
    comet_aperture_radius,
    comet_center_x,
    comet_center_y,
    bg_aperture_x,
    bg_aperture_y,
    bg_aperture_radius,
):
    fig = plt.figure()
    ax1 = fig.add_subplot(1, 2, 1)
    ax2 = fig.add_subplot(1, 2, 2)

    zscale = ZScaleInterval()
    vmin1, vmax1 = zscale.get_limits(image_sum)
    vmin2, vmax2 = zscale.get_limits(image_median)

    im1 = ax1.imshow(image_sum, vmin=vmin1, vmax=vmax1)
    im2 = ax2.imshow(image_median, vmin=vmin2, vmax=vmax2)

    fig.colorbar(im1)
    fig.colorbar(im2)

    image_center_row = int(np.floor(image_sum.shape[0] / 2))
    image_center_col = int(np.floor(image_sum.shape[1] / 2))
    print(f"Image center: {image_center_col}, {image_center_row}")
    ax1.add_patch(
        plt.Circle(
            (comet_center_x, comet_center_y),
            radius=comet_aperture_radius,
            fill=False,
        )
    )
    ax1.axvline(image_center_col, color="w", alpha=0.3)
    ax1.axhline(image_center_row, color="w", alpha=0.3)

    ax2.add_patch(
        plt.Circle(
            (bg_aperture_x, bg_aperture_y),
            radius=bg_aperture_radius,
            fill=False,
        )
    )
    # plt.axvline(image_center_col, color="w", alpha=0.3)
    # plt.axhline(image_center_row, color="w", alpha=0.3)

    plt.show()


def show_fits_subtracted(uw1_stack, uvv_stack, beta):
    uw1 = uw1_stack.stacked_image / uw1_stack.exposure_time
    uvv = uvv_stack.stacked_image / uvv_stack.exposure_time

    # uw1, uvv = pad_to_match_sizes(uw1, uvv)
    dust_subtracted = uw1 - beta * uvv

    fig = plt.figure()
    ax1 = fig.add_subplot(1, 1, 1)

    zscale = ZScaleInterval()
    vmin, vmax = zscale.get_limits(dust_subtracted)

    im1 = ax1.imshow(dust_subtracted, vmin=vmin, vmax=vmax)
    # im2 = ax2.imshow(image_median, vmin=vmin, vmax=vmax)
    fig.colorbar(im1)

    # hdu = fits.PrimaryHDU(dust_subtracted)
    # hdu.writeto("subtracted.fits", overwrite=True)
    plt.show()


def get_float(prompt: str) -> float:
    user_input = None

    while user_input is None:
        raw_selection = input(prompt)
        try:
            selection = float(raw_selection)
        except ValueError:
            print("Numbers only, please\r")
            selection = None

        if selection is not None:
            user_input = selection

    return user_input


def get_aperture_photometry_results(stacked_images) -> dict:
    comet_aperture_radius = get_float("Comet aperture radius: ")
    bg_aperture_x = get_float("Background aperture x: ")
    bg_aperture_y = get_float("Background aperture y: ")
    bg_aperture_radius = get_float("Background aperture radius: ")

    # collect aperture results for uw1 and uvv filters from the stacked images
    aperture_results = {}
    for filter_type in [SwiftFilter.uvv, SwiftFilter.uw1]:
        summed_image = stacked_images[(filter_type, StackingMethod.summation)]
        median_image = stacked_images[(filter_type, StackingMethod.median)]

        aperture_results[filter_type] = do_aperture_photometry(
            stacked_sum=summed_image,
            stacked_median=median_image,
            comet_aperture_radius=comet_aperture_radius,
            bg_aperture_radius=bg_aperture_radius,
            bg_aperture_x=bg_aperture_x,
            bg_aperture_y=bg_aperture_y,
        )

        show_fits_sum_and_median_scaled(
            summed_image.stacked_image,
            median_image.stacked_image,
            comet_aperture_radius=comet_aperture_radius,
            comet_center_x=aperture_results[filter_type].comet_center_x,
            comet_center_y=aperture_results[filter_type].comet_center_y,
            bg_aperture_x=bg_aperture_x,
            bg_aperture_y=bg_aperture_y,
            bg_aperture_radius=bg_aperture_radius,
        )

    return aperture_results


def main():
    args = process_args()

    swift_config = read_swift_config(pathlib.Path(args.config))
    if swift_config is None:
        print("Error reading config file {args.config}, exiting.")
        return 1

    if args.stackinfo is None:
        print("Stack info file not specified, trying default filename stack.json ...")
        stackinfo_path = pathlib.Path("stack.json")
    else:
        stackinfo_path = args.stackinfo

    print(f"Loading stacked image information from {stackinfo_path}")
    stacked_images = stacked_images_from_stackinfo(stackinfo_path=stackinfo_path)

    aperture_results = get_aperture_photometry_results(stacked_images=stacked_images)

    img = stacked_images[(SwiftFilter.uw1, StackingMethod.summation)]
    print(
        f"\nHeliocentric comet distance: {img.helio_r_au} at {img.observation_mid_time}"
    )

    # dust_redness = DustReddeningPercent(10)
    dust_redness_list = list(
        map(lambda x: DustReddeningPercent(x), [0, 5, 10, 15, 20, 25])
    )
    for dust_redness in dust_redness_list:
        # calculate OH flux based on the aperture results in uw1 and uvv filters
        print(f"\nCalculating OH flux with dust redness {dust_redness.reddening}% ...")
        flux_OH_1, beta_1 = OH_flux_from_count_rate(
            solar_spectrum_path=swift_config["solar_spectrum_path"],
            # solar_spectrum_time=Time("2457422", format="jd"),
            # solar_spectrum_time=Time("2016-02-01"),
            solar_spectrum_time=Time(
                stacked_images[
                    (SwiftFilter.uw1, StackingMethod.summation)
                ].observation_mid_time,
                format="fits",
            ),
            effective_area_uw1_path=swift_config["effective_area_uw1_path"],
            effective_area_uvv_path=swift_config["effective_area_uvv_path"],
            result_uw1=aperture_results[SwiftFilter.uw1],
            result_uvv=aperture_results[SwiftFilter.uvv],
            dust_redness=dust_redness,
        )
        flux_OH_2, beta_2 = OH_flux_from_count_rate_fixed_beta(
            effective_area_uw1_path=swift_config["effective_area_uw1_path"],
            effective_area_uvv_path=swift_config["effective_area_uvv_path"],
            result_uw1=aperture_results[SwiftFilter.uw1],
            result_uvv=aperture_results[SwiftFilter.uvv],
            dust_redness=dust_redness,
        )
        print("\tSolar spectrum method:")
        print(f"\t\tBeta = {beta_1}, flux of OH = {flux_OH_1}")
        print("\tFixed beta method:")
        print(f"\t\tBeta = {beta_2}, flux of OH = {flux_OH_2}")
        print("")

        fluorescence_data = read_gfactor_1au(swift_config["oh_fluorescence_path"])
        num_OH_1 = flux_OH_to_num_OH(
            flux_OH=flux_OH_1,
            helio_r_au=img.helio_r_au,
            helio_v_kms=img.helio_v_kms,
            delta_au=img.delta_au,
            fluorescence_data=fluorescence_data,
        )
        num_OH_2 = flux_OH_to_num_OH(
            flux_OH=flux_OH_2,
            helio_r_au=img.helio_r_au,
            helio_v_kms=img.helio_v_kms,
            delta_au=img.delta_au,
            fluorescence_data=fluorescence_data,
        )
        print(f"\tTotal number of OH, beta from solar spectrum method: {num_OH_1}")
        print(f"\tTotal number of OH, fixed beta method: {num_OH_2}")

        Q1 = num_OH_to_Q_vectorial(
            helio_r=img.helio_r_au,
            num_OH=num_OH_1,
            vectorial_model_path=swift_config["vectorial_model_path"],
        )
        Q2 = num_OH_to_Q_vectorial(
            helio_r=img.helio_r_au,
            num_OH=num_OH_2,
            vectorial_model_path=swift_config["vectorial_model_path"],
        )
        print("")
        print(f"\tQ(H2O) from N(OH), solar spectrum method: {Q1} mol/s")
        print(f"\tQ(H2O) from N(OH), fixed beta method: {Q2} mol/s")

    show_fits_subtracted(
        stacked_images[(SwiftFilter.uw1, StackingMethod.median)],
        stacked_images[(SwiftFilter.uvv, StackingMethod.median)],
        beta=0.101483,
    )


if __name__ == "__main__":
    sys.exit(main())
