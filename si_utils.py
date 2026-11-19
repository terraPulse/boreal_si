"""Shared helpers for boreal_si: index definitions, QA masking, warping, band loading and COG writing."""
import os
import time
import random
import logging
import numpy as np
from osgeo import gdal
from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from urllib3 import Retry
from tile_utils import tile_bounds

RES = 0.00025                 # output pixel size in degrees
NODATA = -19999

DEFAULT_MASK = ('dilated_cloud', 'cloud', 'shadow', 'snow', 'water')

INDEX_BANDS = {
	'NDVI': ['nir08', 'red'],
	'EVI': ['nir08', 'red', 'blue'],
	'SAVI': ['nir08', 'red'],
	'MSAVI': ['nir08', 'red'],
	'NDWI': ['green', 'nir08'],
	'NDMI': ['nir08', 'swir16'],
	'NBR2': ['swir16', 'swir22'],
	'TVI': ['nir08', 'red', 'green'],
	'NDSI': ['green', 'swir16'],
}

INDEX_EQUATIONS = {
	'NDVI': '(nir08-red)/(nir08+red)',
	'EVI': '2.5*(nir08-red)/(nir08+6*red-7.5*blue+1)',
	'SAVI': '1.5*(nir08-red)/(nir08+red+0.5)',
	'MSAVI': '0.5*(2*nir08+1-sqrt((2*nir08+1)*(2*nir08+1)-8*(nir08-red)))',
	'NDWI': '(green-nir08)/(green+nir08)',
	'NDMI': '(nir08-swir16)/(nir08+swir16)',
	'NBR2': '(swir16-swir22)/(swir16+swir22)',
	'TVI': '0.5*(120*(nir08-green)-200*(red-green))',
	'NDSI': '(green-swir16)/(green+swir16)',
}


class _LoggingRetry(Retry):
	"""urllib3 Retry that waits at least the server's Retry-After time, backs off further while the
	failures continue, adds random jitter so parallel jobs do not retry in lockstep, and logs each wait.
	"""

	def sleep(self, response=None):
		retry_after = self.get_retry_after(response) if response is not None else None
		wait = max(retry_after or 0, self.get_backoff_time())
		wait += random.uniform(0, 1 + 0.5 * wait)
		status = response.status if response is not None else 'connection error'
		logging.getLogger("boreal_si").warning(
			f'STAC request failed ({status}, Retry-After {retry_after}); waiting {wait:.0f} s, {self.total} retries left')
		time.sleep(wait)


def open_stac_client(url, max_retries=30):
	"""Open a STAC API client that waits and retries when the server throttles or fails.

	CMR (https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html) answers requests over its
	rate limit with HTTP 429 and a Retry-After header, and asks clients to wait that long before
	retrying. Each wait is at least the Retry-After time. While the failures continue the wait also
	grows as 2, 4, 8, ... seconds up to 120 s, plus random jitter of up to 50%. With 30 retries a
	persistent throttle is waited out for about 50 minutes before the request fails. HTTP 500, 502,
	503, 504 and connection errors are retried the same way. The search is a POST, so all methods
	are retried.
	"""
	retry = _LoggingRetry(total=max_retries, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
						  allowed_methods=None, respect_retry_after_header=True)
	return Client.open(url, stac_io=StacApiIO(max_retries=retry))


def parse_indices(indices):
	"""Split a comma-separated index string and validate every name."""
	names = [i.strip().upper() for i in indices.split(',') if i.strip()]
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
	return np.where(valid, source.scale_factor * dn.astype(np.float32) + source.offset, np.nan).astype(np.float32)


def load_si_band(item_dict, asset_key, source, band_options, qa_options):
	"""Read one precomputed index (VI) band as a masked array; pixels flagged in QA or outside the valid range are masked.

	Return (masked_array, geotransform, projection).
	"""
	href = asset_href(item_dict, asset_key)
	if href is None:
		raise KeyError(f'{asset_key} asset missing')
	if 'usgs-landsat-ard' in href:
		raise FileNotFoundError(f'{href} is an ARD path and is not supported')
	si, _, _ = read_warped(to_vsi(href), band_options)
	qa_mask, gt, wkt = load_qa_mask(item_dict, source, qa_options)
	invalid = qa_mask | (si < source.val_min) | (si > source.val_max)
	si = np.where(invalid, NODATA, si)
	return np.ma.masked_equal(si, NODATA), gt, wkt


def write_cog(array, gt, wkt, cog_file, nodata=NODATA):
	"""Write array to a COG, via a temporary name so a failure never leaves a partial output."""
	mem = gdal.GetDriverByName('MEM').Create('', array.shape[1], array.shape[0], 1, gdal.GDT_Int16)
	mem.SetGeoTransform(gt)
	mem.SetProjection(wkt)
	band = mem.GetRasterBand(1)
	band.WriteArray(array)
	band.SetNoDataValue(nodata)
	band.FlushCache()
	part = cog_file + '.part'
	try:
		gdal.Translate(part, mem, format='COG', creationOptions=['COMPRESS=LZW'])
		os.replace(part, cog_file)
	finally:
		mem = None
		if os.path.exists(part):
			os.remove(part)
