"""Earthdata login, LP DAAC S3 credentials and GDAL configuration.

Importing this module has side effects: it reads the Earthdata secrets from MAAP,
logs in with earthaccess, and sets the GDAL options used to read LP DAAC data.
"""
import os
import time
import logging
import earthaccess
from maap.maap import MAAP
from osgeo import gdal

maap = MAAP()
os.environ["EARTHDATA_USERNAME"] = maap.secrets.get_secret("EARTHDATA_USERNAME")
os.environ["EARTHDATA_PASSWORD"] = maap.secrets.get_secret("EARTHDATA_PASSWORD")
os.environ['AWS_REQUEST_PAYER'] = 'requester'
os.environ['CPL_VSIL_CURL_NON_CACHED'] = '/vsis3/'
os.environ['GDAL_MAX_DATASET_POOL_SIZE'] = '4000'
earthaccess.login(strategy="environment")

S3_CREDENTIAL_MAX_AGE = 45 * 60   # seconds; the temporary LP DAAC credentials last about an hour
_last_refresh = 0.0


def refresh_s3_credentials(force=False):
	"""Get new temporary LP DAAC S3 credentials if the current ones are older than S3_CREDENTIAL_MAX_AGE.

	If a refresh fails, log a warning and keep the old credentials.
	"""
	global _last_refresh
	if not force and time.monotonic() - _last_refresh < S3_CREDENTIAL_MAX_AGE:
		return
	try:
		creds = earthaccess.get_s3_credentials(daac="LPDAAC")
	except Exception as err:
		if force:
			raise
		logging.getLogger("boreal_si").warning(f'S3 credential refresh failed, keeping the old credentials: {err}')
		return
	gdal.SetConfigOption("AWS_ACCESS_KEY_ID", creds["accessKeyId"])
	gdal.SetConfigOption("AWS_SECRET_ACCESS_KEY", creds["secretAccessKey"])
	gdal.SetConfigOption("AWS_SESSION_TOKEN", creds["sessionToken"])
	gdal.SetConfigOption("AWS_REGION", "us-west-2")
	_last_refresh = time.monotonic()


refresh_s3_credentials(force=True)
COOKIE_FILE = os.path.expanduser('~/cookies.txt')
gdal.SetConfigOption('GDAL_HTTP_COOKIEFILE', COOKIE_FILE)
gdal.SetConfigOption('GDAL_HTTP_COOKIEJAR', COOKIE_FILE)
gdal.SetConfigOption('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
gdal.SetConfigOption('CPL_VSIL_CURL_ALLOWED_EXTENSIONS', 'TIF')
gdal.SetConfigOption('GDAL_HTTP_UNSAFESSL', 'YES')
gdal.SetConfigOption('GDAL_HTTP_MAX_RETRY', '10')
gdal.SetConfigOption('GDAL_HTTP_RETRY_DELAY', '0.5')
gdal.SetConfigOption('AWS_REQUEST_PAYER', 'requester')
gdal.SetConfigOption('VSI_CACHE', 'TRUE')
gdal.UseExceptions()
