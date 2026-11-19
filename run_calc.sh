#!/bin/bash
# this is intended for running DPS jobs
# Usage: run_calc.sh <tile e.g. 150W65N or 14975W6525N> <indices e.g. ndvi,evi> <start_year> <end_year> <start_date MM-DD> <end_date MM-DD> <src: landsat-sr|hls-sr>
# Dates are MM-DD (e.g. 07-01 08-31) and apply to every year. Earthdata credentials come from the MAAP secrets.

set -ex
set -o pipefail

source activate python

# Avoid a PROJ_LIB from the base image conflicting with the conda env's GDAL/PROJ data
unset PROJ_LIB

mkdir -p output

basedir=$( cd "$(dirname "$0")" ; pwd -P )

# Work dir is always from where your script is called
# Base dir is always the relative dir within the run*.sh script
OUTPUTDIR="${PWD}/output"

python "${basedir}/calc_si.py" \
--tile "${1}" \
--indices "${2}" \
--ys "${3}" \
--ye "${4}" \
--ds "${5}" \
--de "${6}" \
--src "${7}" \
--output "${OUTPUTDIR}"
