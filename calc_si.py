import os
import argparse
import logging
import numpy as np
import numexpr as ne
from stac_output import write_stac_item, write_stac_catalog
from tile_utils import tile_bounds
from sources import SOURCES
from si_utils import (open_stac_client, INDEX_BANDS, INDEX_EQUATIONS, parse_indices, make_warp_options,
                      load_qa_mask, load_band, write_cog)


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("boreal_si")

import lpdaac_access  # logs in and sets up S3 credentials and GDAL config on import; refreshes them during long runs

SEARCH_PAD = 0.0125           # bbox padding for the scene search (degrees)



def compute_index(equation, input_arrays, mask_array, tgt_source):
	"""Evaluate an index and return a scaled Int16 array with all invalid pixels set to NODATA."""
	result = ne.evaluate(equation, local_dict=input_arrays) / tgt_source.scale_factor
	invalid = np.isnan(result) | mask_array | (result > tgt_source.val_max) | (result < tgt_source.val_min)
	return np.where(invalid, tgt_source.band_nodata, result).astype(np.int16)


def process_scene(item_dict, source, tgt_source, tile, year, equations, bands, band_options, qa_options, output):
	"""Compute every index for one scene. Return the list of COG paths written."""
	scene_id = item_dict['id']
	collection = item_dict.get('collection')
	if collection not in source.band_assets:
		raise KeyError(f"unexpected collection '{collection}'")
	assets = source.band_assets[collection]
	date_str = item_dict['properties']['datetime'][:10]
	mask_array, gt, wkt = load_qa_mask(item_dict, source, qa_options)
	input_arrays = {band: load_band(item_dict, assets[band], source, band_options) for band in bands}
	written = []
	for si, equation in equations.items():
		output_array = compute_index(equation, input_arrays, mask_array, tgt_source)
		cog_file = f'{output}/{tile}_{year}_{scene_id}_{si}.tif'
		write_cog(output_array, gt, wkt, cog_file)
		try:
			write_stac_item(cog_file, tile, year, si, scene_id, date_str, 'None', tgt_source.scale_factor, tgt_source.band_nodata, collection)
		except Exception as err:
			logger.error(f'STAC item failed for {cog_file}: {err}')
		written.append(cog_file)
	return written


def calc_index(tile, indices, ys, ye, ds, de, src, output):
	source = SOURCES[src]
	tgt_source = SOURCES[src.replace('sr','vi')]
	index_names = parse_indices(indices)
	xmin, ymin, xmax, ymax = tile_bounds(tile)
	equations = {i: INDEX_EQUATIONS[i] for i in index_names}
	bands = sorted({b for i in index_names for b in INDEX_BANDS[i]})
	band_options = make_warp_options(tile, source.band_nodata)
	qa_options = make_warp_options(tile, source.qa_nodata)
	search_bbox = (xmin - SEARCH_PAD, ymin - SEARCH_PAD, xmax + SEARCH_PAD, ymax + SEARCH_PAD)
	os.makedirs(output, exist_ok=True)

	catalog = open_stac_client(source.stac_url)
	item_files = []
	for year in range(ys, ye + 1):
		query = catalog.search(collections=list(source.collections), datetime=f'{year}-{ds}/{year}-{de}',
							   limit=100, bbox=search_bbox)
		for page_dict in query.pages_as_dicts():
			for item_dict in page_dict['features']:
				scene_id = item_dict['id']
				logger.info(f'Found {scene_id} for {tile},{year}')
				lpdaac_access.refresh_s3_credentials()
				try:
					written = process_scene(item_dict, source, tgt_source, tile, year, equations, bands,
											band_options, qa_options, output)
					item_files.extend(f[:-len('.tif')] + '.json' for f in written)
				except Exception as err:
					logger.warning(f'Skipping {scene_id}: {err}')
	try:
		write_stac_catalog(output, 'Boreal SI Outputs', [p for p in item_files if os.path.exists(p)])
	except Exception as err:
		logger.error(f'STAC catalog failed: {err}')


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument('--tile', type=str, required=True)
	parser.add_argument('--indices', type=str, required=False, default=','.join(INDEX_EQUATIONS))
	parser.add_argument('--ys', type=int, required=True)
	parser.add_argument('--ye', type=int, required=True)
	parser.add_argument('--ds', type=str, required=True)
	parser.add_argument('--de', type=str, required=True)
	parser.add_argument('--src', type=str.lower, choices=['landsat-sr','hls-sr'], required=True)
	parser.add_argument('--output', type=str, required=True)
	
	args = parser.parse_args()
	if not args.indices.strip():    # DPS passes an empty string when no indices are given
		args.indices = parser.get_default('indices')
	if args.ys > args.ye:
		parser.error('--ys must be <= --ye')
	calc_index(args.tile, args.indices, args.ys, args.ye, args.ds, args.de, args.src, args.output)


if __name__ == '__main__':
	main()
