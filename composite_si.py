import os
import argparse
import logging
import numpy as np
from pystac_client import Client
from stac_output import write_stac_item, write_stac_catalog
from tile_utils import tile_bounds
from sources import SOURCES
from si_utils import (NODATA, parse_indices, make_warp_options, load_si_band, write_cog)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("boreal_si")

import lpdaac_access  # logs in and sets up S3 credentials and GDAL config on import; refreshes them during long runs

SEARCH_PAD = 0.0125           # bbox padding for the scene search (degrees)

# Memory model for the block-wise statistics (bytes per scene per pixel):
#  - every scene is held as an Int16 array for the whole year/index (2 bytes);
#  - computing the median and maximum on a block of rows needs about 16 bytes per scene
#    per pixel of the block (measured: stack, mask and the sorted copies made by np.ma.median).
# MEMORY_BUDGET is the total for these arrays. Measured peaks run about 5-6% above the model,
# so 10 GiB gives a peak of about 10.7 GiB and leaves more than 5 GiB of a 16 GiB machine for
# the OS, Python, the GDAL cache and the per-scene reads.
MEMORY_BUDGET = 10 * 2**30
HELD_BYTES = 2
BLOCK_BYTES = 16


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
	fill_value in median and maximum and 0 in count.
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
		block = np.ma.masked_equal(np.stack([a[r0:r1] for a in si_arrays], axis=0), NODATA)
		med[r0:r1] = np.ma.median(block, axis=0).filled(fill_value).astype(np.int16)
		max_[r0:r1] = np.ma.max(block, axis=0).filled(fill_value).astype(np.int16)
		count[r0:r1] = block.count(axis=0)
		del block
	return med, max_, count




def composite_index(tile, indices, ys, ye, ds, de, output, src):
	source = SOURCES[src]
	index_names = parse_indices(indices)
	xmin, ymin, xmax, ymax = tile_bounds(tile)
	band_options = make_warp_options(tile, source.band_nodata)
	qa_options = make_warp_options(tile, source.qa_nodata)
	search_bbox = (xmin - SEARCH_PAD, ymin - SEARCH_PAD, xmax + SEARCH_PAD, ymax + SEARCH_PAD)
	os.makedirs(output, exist_ok=True)

	catalog = Client.open(source.stac_url)
	item_files = []
	collections = ','.join(source.collections)
	for year in range(ys, ye + 1):
		query = catalog.search(collections=list(source.collections), datetime=f'{year}-{ds}/{year}-{de}',
                               limit=100, bbox=search_bbox)
		pages = list(query.pages_as_dicts())    # search once per year, not once per index
		logger.info(f'{sum(len(p["features"]) for p in pages)} scenes found for {tile},{year}')
		for si in index_names:
			si_arrays = []
			gt = wkt = None
			for page_dict in pages:
				for item_dict in page_dict['features']:
					scene_id = item_dict['id']
					logger.info(f'Found {scene_id} at {tile},{year} for {si}')
					lpdaac_access.refresh_s3_credentials()
					try:
						asset_key = source.band_assets[item_dict.get('collection')][si]
						si_array, gt, wkt = load_si_band(item_dict, asset_key, source, band_options, qa_options)
						si_arrays.append(np.asarray(si_array.data, dtype=np.int16))   # masked pixels hold NODATA
					except Exception as err:
						logger.warning(f'Skipping {scene_id} at {tile},{year} for {si}: {err}')
						continue
			if not si_arrays:
				logger.warning(f'No usable scenes for {tile},{year},{si}')
				continue
			logger.info(f'{len(si_arrays)} scenes used for {tile},{year},{si}')
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
