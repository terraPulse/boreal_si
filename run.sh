#!/bin/bash
# this is intended for running DPS jobs
# Usage: run.sh <tile e.g. 150W65N or 14975W6525N> <indices e.g. ndvi,evi> <start_year> <end_year> [collection: landsat|hls]
# collection defaults to landsat. hls is read over HTTPS and needs Earthdata Login credentials in ~/.netrc.

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
--collection "${5:-landsat}" \
--output "${OUTPUTDIR}"
