import os
import re
import time
import random
import argparse
import logging
import numpy as np
from concurrent.futures import ThreadPoolExecutor
from stac_output import write_stac_item, write_stac_catalog
from tile_utils import tile_bounds
from sources import SOURCES
from si_utils import (open_stac_client, search_cmr_by_year, NODATA, parse_indices, make_warp_options,
                      load_si_acquisition, write_cog)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("boreal_si")

import lpdaac_access  # logs in and sets up S3 credentials and GDAL config on import; refreshes them during long runs

SEARCH_PAD = 0.0125           # bbox padding for the scene search (degrees)
SEARCH_START_JITTER = 120     # seconds; random wait before the search so that many jobs do not search at once
READ_THREADS = 8              # scenes read from S3 at the same time (S3 reads do not count toward the CMR rate limit)

# Memory model for the block-wise statistics (bytes per scene per pixel):
#  - every acquisition is held as an Int16 array for the whole year/index (2 bytes);
#  - computing median, maximum and count on a block of rows needs about BLOCK_BYTES per scene per
#    pixel of the block (measured: the stacked block, its in-place sort and the valid-pixel test).
# MEMORY_BUDGET is the total for these arrays. It leaves more than 5 GiB of a 16 GiB machine for
# the OS, Python, the GDAL cache and the READ_THREADS reads in flight.
MEMORY_BUDGET = 10 * 2**30
HELD_BYTES = 2
BLOCK_BYTES = 4

# HLS granule ids: HLS-VI.<sensor>.T<MGRS tile>.<acquisition time>.v2.0. Granules of one overpass
# share sensor and acquisition time and differ only in the MGRS tile.
HLS_ID = re.compile(r'^HLS(?:-VI)?\.(?P<sensor>[LS]30)\.T\w{5}\.(?P<time>\d{7}T\d{6})\.')


def acquisition_key(item_dict):
	"""Key shared by the granules of one acquisition; other ids are their own acquisition."""
	m = HLS_ID.match(item_dict['id'])
	return f"{m['sensor']}.{m['time']}" if m else item_dict['id']


def group_acquisitions(items):
	"""Group items by acquisition, keeping the search order. Return {key: [item, ...]}."""
	groups = {}
	for item in items:
		groups.setdefault(acquisition_key(item), []).append(item)
	return groups


def block_rows(n_scenes, ny, nx, budget=MEMORY_BUDGET):
	"""Largest number of rows per block that keeps the held scenes plus one block within budget."""
	held = HELD_BYTES * n_scenes * ny * nx
	if held >= budget:
		logger.warning(f'{n_scenes} scenes of {ny}x{nx} pixels need {held / 2**30:.1f} GiB, '
					   f'more than the {budget / 2**30:.1f} GiB budget; using 1-row blocks')
		return 1
	return int(max(1, min(ny, (budget - held) // (BLOCK_BYTES * n_scenes * nx))))


def block_stats(si_arrays, fill_value, budget=MEMORY_BUDGET):
	"""Per-pixel median, maximum and count over scenes, ignoring NODATA, computed in blocks of rows.

	si_arrays is a list of 2D Int16 arrays of the same shape with NODATA where a scene has no
	valid value. Return (median, maximum, count) as Int16 arrays. count is the number of valid
	scenes that went into the median and maximum of each pixel; pixels with no valid scene get
	fill_value in median and maximum and 0 in count. The median of an even count is the mean of the
	two middle values, truncated toward zero (as np.ma.median(...).astype(np.int16)).

	NODATA (-19999) is below every valid value, so after sorting each pixel's values the n - count
	NODATA values come first and the valid values follow in order. This avoids masked arrays, which
	are about 6 times slower and need several times the memory.
	"""
	n_scenes = len(si_arrays)
	ny, nx = si_arrays[0].shape
	rows = block_rows(n_scenes, ny, nx, budget)
	logger.info(f'Statistics for {n_scenes} scenes in blocks of {rows} of {ny} rows')
	med = np.full((ny, nx), fill_value, dtype=np.int16)
	max_ = np.full((ny, nx), fill_value, dtype=np.int16)
	count = np.zeros((ny, nx), dtype=np.int16)
	for r0 in range(0, ny, rows):
		r1 = min(r0 + rows, ny)
		block = np.stack([a[r0:r1] for a in si_arrays], axis=0)
		cnt = np.count_nonzero(block != NODATA, axis=0).astype(np.int32)
		block.sort(axis=0)
		first = n_scenes - cnt                                   # index of the smallest valid value
		lo = np.clip(first + (cnt - 1) // 2, 0, n_scenes - 1)[None]
		hi = np.clip(first + cnt // 2, 0, n_scenes - 1)[None]
		mid = (np.take_along_axis(block, lo, 0)[0].astype(np.float32)
			   + np.take_along_axis(block, hi, 0)[0].astype(np.float32)) / 2
		has = cnt > 0
		med[r0:r1] = np.where(has, mid, fill_value).astype(np.int16)
		max_[r0:r1] = np.where(has, block[-1], fill_value)
		count[r0:r1] = cnt
		del block, cnt, first, lo, hi, mid, has
	return med, max_, count


def read_acquisitions(groups, si, source, tile, qa_cache):
	"""Read index si for every acquisition in parallel. Return (arrays, gt, wkt, n_empty, n_failed)."""
	def read_one(key):
		lpdaac_access.refresh_s3_credentials()
		# Warp options are built per call so that no GDAL options object is shared between threads.
		band_options = make_warp_options(tile, source.band_nodata, multithread=True)
		qa_options = make_warp_options(tile, source.qa_nodata, multithread=True)
		try:
			return key, load_si_acquisition(groups[key], si, source, band_options, qa_options, qa_cache, key), None
		except Exception as err:
			return key, None, err

	arrays, gt, wkt, n_empty, n_failed = [], None, None, 0, 0
	with ThreadPoolExecutor(max_workers=READ_THREADS) as pool:
		for key, result, err in pool.map(read_one, list(groups)):    # results come back in input order
			ids = ','.join(item['id'] for item in groups[key])
			if err is not None:
				n_failed += 1
				logger.warning(f'Skipping {ids} for {si}: {err}')
			elif result is None:
				n_empty += 1
				logger.info(f'No valid pixels in {ids} for {si}')
			else:
				array, gt, wkt = result
				arrays.append(array)
				logger.info(f'Read {ids} for {si}')
	return arrays, gt, wkt, n_empty, n_failed


def composite_index(tile, indices, ys, ye, ds, de, src, output):
	source = SOURCES[src]
	index_names = parse_indices(indices)
	xmin, ymin, xmax, ymax = tile_bounds(tile)
	search_bbox = (xmin - SEARCH_PAD, ymin - SEARCH_PAD, xmax + SEARCH_PAD, ymax + SEARCH_PAD)
	os.makedirs(output, exist_ok=True)

	delay = random.uniform(0, SEARCH_START_JITTER)
	logger.info(f'Waiting {delay:.0f} s before searching, to spread out searches from parallel jobs')
	time.sleep(delay)
	if source.cmr_collections:
		items_by_year = search_cmr_by_year(source, search_bbox, ys, ye, ds, de)    # all years at once
	else:
		catalog = open_stac_client(source.stac_url)
	item_files = []
	collections = ','.join(source.collections)
	for year in range(ys, ye + 1):
		if source.cmr_collections:
			items = items_by_year[year]
		else:
			query = catalog.search(collections=list(source.collections), datetime=f'{year}-{ds}/{year}-{de}',
								   limit=100, bbox=search_bbox)
			items = [f for page in query.pages_as_dicts() for f in page['features']]    # once per year, not per index
		groups = group_acquisitions(items)
		qa_cache = {}    # acquisition -> packed QA mask (or None when nothing is usable), shared by all indices
		logger.info(f'{len(items)} granules in {len(groups)} acquisitions found for {tile},{year}')
		for si in index_names:
			si_arrays, gt, wkt, n_empty, n_failed = read_acquisitions(groups, si, source, tile, qa_cache)
			logger.info(f'{tile},{year},{si}: {len(si_arrays)} acquisitions used, {n_empty} without valid pixels, '
						f'{n_failed} failed')
			if not si_arrays:
				logger.warning(f'No usable scenes for {tile},{year},{si}')
				continue
			si_med, si_max, si_cnt = block_stats(si_arrays, source.band_nodata)
			del si_arrays                             # frees the per-scene arrays
			for stat, si_out, desc, scale_factor, nodata in (
					('med', si_med, f'Median of {ds}:{de}', source.scale_factor, source.band_nodata),
					('max', si_max, f'Maximum of {ds}:{de}', source.scale_factor, source.band_nodata),
					('cnt', si_cnt, f'Count of valid scenes, {ds}:{de}', 1, 0)):
				out_file = f'{output}/{tile}_{year}_{si}_{stat}.tif'
				write_cog(si_out, gt, wkt, out_file, nodata)
				try:
					write_stac_item(out_file, tile, year, si, 'Composite', f'{year}-{ds}', desc,
									scale_factor, nodata, collections)
				except Exception as err:
					logger.error(f'STAC item failed for {out_file}: {err}')
				item_files.append(out_file[:-len('.tif')] + '.json')
			del si_med, si_max, si_cnt
		del qa_cache
	try:
		write_stac_catalog(output, 'Boreal SI Composite Outputs', [p for p in item_files if os.path.exists(p)])
	except Exception as err:
		logger.error(f'STAC catalog failed: {err}')


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument('--tile', type=str, required=True)
	parser.add_argument('--indices', type=str, required=False, default='NDVI,NDWI,NDMI,EVI,SAVI,MSAVI,NBR2,TVI')
	parser.add_argument('--ys', type=int, required=True)
	parser.add_argument('--ye', type=int, required=True)
	parser.add_argument('--ds', type=str, required=True)
	parser.add_argument('--de', type=str, required=True)
	parser.add_argument('--src', type=str.lower, choices=['landsat-vi','hls-vi'], required=True)
	parser.add_argument('--output', type=str, required=True)

	args = parser.parse_args()
	if not args.indices.strip():    # DPS passes an empty string when no indices are given
		args.indices = parser.get_default('indices')
	if args.ys > args.ye:
		parser.error('--ys must be <= --ye')
	composite_index(args.tile, args.indices, args.ys, args.ye, args.ds, args.de, args.src, args.output)


if __name__ == '__main__':
	main()
