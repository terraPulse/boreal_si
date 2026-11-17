import os
os.environ['AWS_REQUEST_PAYER'] = 'requester'
os.environ['CPL_VSIL_CURL_NON_CACHED'] = '/vsis3/'
os.environ['GDAL_MAX_DATASET_POOL_SIZE'] = '4000'

import re
import argparse
import logging
from dataclasses import dataclass
import numpy as np
import numexpr as ne
from osgeo import gdal
from pystac_client import Client
from stac_output import write_stac_item, write_stac_catalog

gdal.UseExceptions()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger("boreal_si")

# GDAL configurations used to successfully access LP DAAC Cloud Assets via vsicurl.
# HLS is read over HTTPS and needs Earthdata Login credentials in ~/.netrc.
# GDAL/curl do not expand '~', so expand it here.
COOKIE_FILE = os.path.expanduser('~/cookies.txt')
gdal.SetConfigOption('GDAL_HTTP_COOKIEFILE', COOKIE_FILE)
gdal.SetConfigOption('GDAL_HTTP_COOKIEJAR', COOKIE_FILE)
gdal.SetConfigOption('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
gdal.SetConfigOption('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', 'TIF')
gdal.SetConfigOption('GDAL_HTTP_UNSAFESSL', 'YES')
gdal.SetConfigOption('GDAL_HTTP_MAX_RETRY', '10')
gdal.SetConfigOption('GDAL_HTTP_RETRY_DELAY', '0.5')

TILE_PATTERN_1DEG = re.compile(r'^(?P<lon>\d{3})(?P<ew>[EW])(?P<lat>\d{2})(?P<ns>[NS])$')    # e.g. 150W65N
TILE_PATTERN_15MIN = re.compile(r'^(?P<lon>\d{5})(?P<ew>[EW])(?P<lat>\d{4})(?P<ns>[NS])$')  # e.g. 14975W6525N
# (pattern, divisor applied to the corner numbers, tile edge in degrees)
TILE_FORMATS = ((TILE_PATTERN_1DEG, 1, 1.0), (TILE_PATTERN_15MIN, 100, 0.25))

RES = 0.00025                 # output pixel size in degrees
SEARCH_PAD = 0.0125           # bbox padding for the scene search (degrees)

SCALER = 10000
SI_MAX = 10000
SI_MIN = -10000
NODATA = -19999

DEFAULT_MASK = ('dilated_cloud', 'cloud', 'shadow', 'snow', 'water')

COMMON_BANDS = ('blue', 'green', 'red', 'nir08', 'swir16', 'swir22')


@dataclass(frozen=True)
class SourceConfig:
	"""Everything that differs between input collections."""
	stac_url: str
	collections: tuple
	band_assets: dict   # STAC collection id -> {common band name: asset key}
	qa_asset: str
	qa_bits: dict       # mask name -> bit position in the QA band
	qa_nodata: int
	band_nodata: int
	scale: float        # reflectance = scale * DN + offset
	offset: float
	val_min: int        # valid DN range (inclusive)
	val_max: int


SOURCES = {
	# Landsat Collection 2 Level-2 surface reflectance; QA_PIXEL bits
	'landsat': SourceConfig(
		stac_url='https://landsatlook.usgs.gov/stac-server',
		collections=('landsat-c2l2-sr',),
		band_assets={'landsat-c2l2-sr': {b: b for b in COMMON_BANDS}},
		qa_asset='qa_pixel',
		qa_bits={'fill': 0, 'dilated_cloud': 1, 'cloud': 3, 'shadow': 4, 'snow': 5, 'water': 7},
		qa_nodata=1, band_nodata=0,
		scale=0.0000275, offset=-0.2, val_min=7273, val_max=43636),
	# HLS v2.0 (Landsat L30 + Sentinel-2 S30); Fmask bits, 'dilated_cloud' = adjacent to cloud/shadow
	'hls': SourceConfig(
		stac_url='https://cmr.earthdata.nasa.gov/stac/LPCLOUD',
		collections=('HLSL30_2.0', 'HLSS30_2.0'),
		band_assets={
			'HLSL30_2.0': dict(zip(COMMON_BANDS, ('B02', 'B03', 'B04', 'B05', 'B06', 'B07'))),
			'HLSS30_2.0': dict(zip(COMMON_BANDS, ('B02', 'B03', 'B04', 'B8A', 'B11', 'B12'))),
		},
		qa_asset='Fmask',
		qa_bits={'cirrus': 0, 'cloud': 1, 'dilated_cloud': 2, 'shadow': 3, 'snow': 4, 'water': 5},
		qa_nodata=255, band_nodata=-9999,
		scale=0.0001, offset=0.0, val_min=0, val_max=10000),
}

INDEX_BANDS = {
	'ndvi': ['nir08', 'red'],
	'evi': ['nir08', 'red', 'blue'],
	'savi': ['nir08', 'red'],
	'msavi': ['nir08', 'red'],
	'ndwi': ['green', 'nir08'],
	'ndmi': ['nir08', 'swir16'],
	'nbr2': ['swir16', 'swir22'],
	'tvi': ['nir08', 'red', 'green'],
}
INDEX_EQUATIONS = {
	'ndvi': '(nir08-red)/(nir08+red)',
	'evi': '2.5*(nir08-red)/(nir08+6*red-7.5*blue+1)',
	'savi': '1.5*(nir08-red)/(nir08+red+0.5)',
	'msavi': '0.5*(2*nir08+1-sqrt((2*nir08+1)*(2*nir08+1)-8*(nir08-red)))',
	'ndwi': '(green-nir08)/(green+nir08)',
	'ndmi': '(nir08-swir16)/(nir08+swir16)',
	'nbr2': '(swir16-swir22)/(swir16+swir22)',
	'tvi': '0.5*(120*(nir08-green)-200*(red-green))',
}


def tile_bounds(tile):
	"""Return (xmin, ymin, xmax, ymax) for a tile id given by its upper-left corner.

	1-degree tile:  '150W65N'     -> 1.00 x 1.00 degrees, corner = 150 W, 65 N
	15-minute tile: '14975W6525N' -> 0.25 x 0.25 degrees, corner = 149.75 W, 65.25 N
	"""
	for pattern, divisor, size in TILE_FORMATS:
		m = pattern.match(tile)
		if m:
			break
	else:
		raise ValueError(f"Invalid tile '{tile}'; expected e.g. 150W65N or 14975W6525N")
	xmin = int(m['lon']) / divisor * (-1 if m['ew'] == 'W' else 1)
	ymax = int(m['lat']) / divisor * (-1 if m['ns'] == 'S' else 1)
	return (xmin, ymax - size, xmin + size, ymax)


def parse_indices(indices):
	"""Split a comma-separated index string and validate every name."""
	names = [i.strip().lower() for i in indices.split(',') if i.strip()]
	unknown = [n for n in names if n not in INDEX_EQUATIONS]
	if not names or unknown:
		raise ValueError(f"Unknown or empty indices {unknown or names}; valid: {sorted(INDEX_EQUATIONS)}")
	return list(dict.fromkeys(names))


def mask_qa(qa_arr, qa_bits, mask_list=DEFAULT_MASK):
	"""Boolean array, True where any of the listed QA flags is set."""
	msk = np.zeros(qa_arr.shape, dtype=bool)
	for m in mask_list:
		if m in qa_bits:
			msk |= (qa_arr & (1 << qa_bits[m])) > 0
	return msk


def make_warp_options(tile, nodata):
	xmin, ymin, xmax, ymax = tile_bounds(tile)
	return gdal.WarpOptions(xRes=RES, yRes=RES, srcNodata=nodata, dstNodata=nodata, dstSRS='EPSG:4326',
							outputBounds=[xmin, ymin, xmax, ymax], format='MEM', targetAlignedPixels=True)


def to_vsi(href):
	"""Map an s3:// or http(s):// href to the matching GDAL virtual file system path."""
	if href.startswith('s3://'):
		return '/vsis3/' + href[len('s3://'):]
	if href.startswith(('https://', 'http://')):
		return '/vsicurl/' + href
	return href


def asset_href(item_dict, asset_name):
	"""Return an asset's S3 alternate href if present (Landsat), else its main href (HLS); None if missing."""
	asset = item_dict['assets'].get(asset_name)
	if not asset:
		return None
	return asset.get('alternate', {}).get('s3', {}).get('href') or asset.get('href')


def read_warped(src, warp_options):
	"""Warp src to the tile grid in memory; return (array, geotransform, projection)."""
	ds = gdal.Warp('', src, options=warp_options)
	try:
		return ds.GetRasterBand(1).ReadAsArray(), ds.GetGeoTransform(), ds.GetProjectionRef()
	finally:
		ds = None


def load_qa_mask(item_dict, source, qa_options):
	href = asset_href(item_dict, source.qa_asset)
	if href is None:
		raise KeyError(f'{source.qa_asset} asset missing')
	qa_array, gt, wkt = read_warped(to_vsi(href).replace('usgs-landsat-ard/', 'usgs-landsat/'), qa_options)
	return mask_qa(qa_array, source.qa_bits), gt, wkt


def load_band(item_dict, asset_key, source, band_options):
	"""Read one band as float32 surface reflectance with out-of-range DNs set to NaN."""
	href = asset_href(item_dict, asset_key)
	if href is None:
		raise KeyError(f'{asset_key} asset missing')
	if 'usgs-landsat-ard' in href:
		raise FileNotFoundError(f'{href} is an ARD path and is not supported')
	dn, _, _ = read_warped(to_vsi(href), band_options)
	valid = (dn >= source.val_min) & (dn <= source.val_max)
	return np.where(valid, source.scale * dn.astype(np.float32) + source.offset, np.nan).astype(np.float32)


def compute_index(equation, input_arrays, mask_array):
	"""Evaluate an index and return a scaled Int16 array with all invalid pixels set to NODATA."""
	result = SCALER * ne.evaluate(equation, local_dict=input_arrays)
	invalid = np.isnan(result) | mask_array | (result > SI_MAX) | (result < SI_MIN)
	return np.where(invalid, NODATA, result).astype(np.int16)


def write_cog(array, gt, wkt, cog_file):
	"""Write array to a COG, via a temporary name so a failure never leaves a partial output."""
	mem = gdal.GetDriverByName('MEM').Create('', array.shape[1], array.shape[0], 1, gdal.GDT_Int16)
	mem.SetGeoTransform(gt)
	mem.SetProjection(wkt)
	band = mem.GetRasterBand(1)
	band.WriteArray(array)
	band.SetNoDataValue(NODATA)
	band.FlushCache()
	part = cog_file + '.part'
	try:
		gdal.Translate(part, mem, format='COG', creationOptions=['COMPRESS=LZW'])
		os.replace(part, cog_file)
	finally:
		mem = None
		if os.path.exists(part):
			os.remove(part)


def process_scene(item_dict, source, tile, year, equations, bands, band_options, qa_options, output):
	"""Compute every index for one scene. Return the list of COG paths written."""
	scene_id = item_dict['id']
	collection = item_dict.get('collection')
	if collection not in source.band_assets:
		raise KeyError(f"unexpected collection '{collection}'")
	assets = source.band_assets[collection]
	date_str = item_dict['properties']['datetime'][:10].replace('-', '')
	mask_array, gt, wkt = load_qa_mask(item_dict, source, qa_options)
	input_arrays = {band: load_band(item_dict, assets[band], source, band_options) for band in bands}
	written = []
	for si, equation in equations.items():
		output_array = compute_index(equation, input_arrays, mask_array)
		cog_file = f'{output}/{tile}_{year}_{scene_id}_{si}.tif'
		write_cog(output_array, gt, wkt, cog_file)
		try:
			write_stac_item(cog_file, tile, year, si, scene_id, date_str, equation, SCALER, NODATA, collection)
		except Exception as err:
			logger.error(f'STAC item failed for {cog_file}: {err}')
		written.append(cog_file)
	return written


def calc_index(tile, indices, ys, ye, output, collection='landsat'):
	source = SOURCES[collection]
	index_names = parse_indices(indices)
	xmin, ymin, xmax, ymax = tile_bounds(tile)
	equations = {i: INDEX_EQUATIONS[i] for i in index_names}
	bands = sorted({b for i in index_names for b in INDEX_BANDS[i]})
	band_options = make_warp_options(tile, source.band_nodata)
	qa_options = make_warp_options(tile, source.qa_nodata)
	search_bbox = (xmin - SEARCH_PAD, ymin - SEARCH_PAD, xmax + SEARCH_PAD, ymax + SEARCH_PAD)
	os.makedirs(output, exist_ok=True)

	catalog = Client.open(source.stac_url)
	item_files = []
	for year in range(ys, ye + 1):
		query = catalog.search(collections=list(source.collections), datetime=f'{year}-07/{year}-08',
							   limit=100, bbox=search_bbox)
		for page_dict in query.pages_as_dicts():
			for item_dict in page_dict['features']:
				scene_id = item_dict['id']
				logger.info(f'Found {scene_id} for {tile},{year}')
				try:
					written = process_scene(item_dict, source, tile, year, equations, bands,
											band_options, qa_options, output)
					item_files.extend(f[:-len('.tif')] + '.json' for f in written)
				except Exception as err:
					logger.warning(f'Skipping {scene_id}: {err}')
	try:
		write_stac_catalog(output, [p for p in item_files if os.path.exists(p)])
	except Exception as err:
		logger.error(f'STAC catalog failed: {err}')


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument('--tile', type=str, required=True)
	parser.add_argument('--indices', type=str, required=False, default=','.join(INDEX_EQUATIONS))
	parser.add_argument('--ys', type=int, required=True)
	parser.add_argument('--ye', type=int, required=True)
	parser.add_argument('--output', type=str, required=True)
	parser.add_argument('--collection', type=str.lower, choices=sorted(SOURCES), default='landsat')
	args = parser.parse_args()
	if args.ys > args.ye:
		parser.error('--ys must be <= --ye')
	calc_index(args.tile, args.indices, args.ys, args.ye, args.output, args.collection)


if __name__ == '__main__':
	main()
