import os
import argparse
import logging
import numpy as np
from pystac_client import Client
from stac_output import write_stac_item, write_stac_catalog
from tile_utils import tile_bounds
from sources import SOURCES
from si_utils import (parse_indices, make_warp_options, load_si_band, write_cog)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("boreal_si")

import lpdaac_access  # logs in and sets up S3 credentials and GDAL config on import; refreshes them during long runs

SEARCH_PAD = 0.0125           # bbox padding for the scene search (degrees)


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
						si_arrays.append(si_array)
					except Exception as err:
						logger.warning(f'Skipping {scene_id} at {tile},{year} for {si}: {err}')
						continue
			if not si_arrays:
				logger.warning(f'No usable scenes for {tile},{year},{si}')
				continue
			si_ts = np.ma.stack(si_arrays, axis=0)    # shape (n_scenes_ok, ny, nx)
			del si_arrays                             # frees the per-scene arrays
			logger.info(f'{si_ts.shape[0]} scenes used for {tile},{year},{si}')

			for stat, func, desc in (('med', np.ma.median, f'Median of {ds}:{de}'),
                                     ('max', np.ma.max, f'Maximum of {ds}:{de}')):
				si_out = func(si_ts, axis=0).filled(source.band_nodata).astype(np.int16)
				out_file = f'{output}/{tile}_{year}_{si}_{stat}.tif'
				write_cog(si_out, gt, wkt, out_file)
				del si_out
				try:
					write_stac_item(out_file, tile, year, si, 'Composite', f'{year}-{ds}', desc,
                                    source.scale_factor, source.band_nodata, collections)
				except Exception as err:
					logger.error(f'STAC item failed for {out_file}: {err}')
				item_files.append(out_file[:-len('.tif')] + '.json')
			del si_ts
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
	if args.ys > args.ye:
		parser.error('--ys must be <= --ye')
	composite_index(args.tile, args.indices, args.ys, args.ye, args.ds, args.de, args.src, args.output)


if __name__ == '__main__':
	main()
