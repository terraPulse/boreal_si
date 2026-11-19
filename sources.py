"""Input collection configurations (Landsat, HLS) for boreal_si."""
from dataclasses import dataclass

COMMON_BANDS = ('blue', 'green', 'red', 'nir08', 'swir16', 'swir22')
COMMON_VIS = ('NDVI', 'NDWI', 'NDMI', 'EVI', 'SAVI', 'MSAVI','NBR2','TVI')
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
	scale_factor: float        # value = scale_factor * DN + offset
	offset: float
	val_min: int        # valid DN range (inclusive)
	val_max: int


SOURCES = {
	# Landsat Collection 2 Level-2 surface reflectance; QA_PIXEL bits
	'landsat-sr': SourceConfig(
		stac_url='https://landsatlook.usgs.gov/stac-server',
		collections=('landsat-c2l2-sr',),
		band_assets={'landsat-c2l2-sr': {b: b for b in COMMON_BANDS}},
		qa_asset='qa_pixel',
		qa_bits={'fill': 0, 'dilated_cloud': 1, 'cloud': 3, 'shadow': 4, 'snow': 5, 'water': 7},
		qa_nodata=1, band_nodata=0,
		scale_factor=0.0000275, offset=-0.2, val_min=7273, val_max=43636),
	'landsat-vi': SourceConfig(
		stac_url='https://dps-stac.maap-project.org',
		collections=('landsat-vi',),
		band_assets={'landsat-vi': dict(zip(COMMON_VIS, COMMON_VIS)),},
		qa_asset='Fmask',
		qa_bits={'fill': 0, 'dilated_cloud': 1, 'cloud': 3, 'shadow': 4, 'snow': 5, 'water': 7},
		qa_nodata=1, band_nodata=-19999,
		scale_factor=0.0001, offset=0.0, val_min=-10000, val_max=10000),
	# HLS v2.0 (Landsat L30 + Sentinel-2 S30); Fmask bits, 'dilated_cloud' = adjacent to cloud/shadow
	'hls-sr': SourceConfig(
		stac_url='https://cmr.earthdata.nasa.gov/stac/LPCLOUD',
		collections=('HLSL30_2.0', 'HLSS30_2.0'),
		band_assets={
			'HLSL30_2.0': dict(zip(COMMON_BANDS, ('s3_B02', 's3_B03', 's3_B04', 's3_B05', 's3_B06', 's3_B07'))),
			'HLSS30_2.0': dict(zip(COMMON_BANDS, ('s3_B02', 's3_B03', 's3_B04', 's3_B8A', 's3_B11', 's3_B12'))),
		},
		qa_asset='Fmask',
		qa_bits={'cirrus': 0, 'cloud': 1, 'dilated_cloud': 2, 'shadow': 3, 'snow': 4, 'water': 5},
		qa_nodata=255, band_nodata=-9999,
		scale_factor=0.0001, offset=0.0, val_min=0, val_max=10000),
	# HLS v2.0 vegetation indices (VI) products; Fmask bits as for hls-sr
	'hls-vi': SourceConfig(
		stac_url='https://cmr.earthdata.nasa.gov/stac/LPCLOUD',
		collections=('HLSL30_VI_2.0', 'HLSS30_VI_2.0'),
		band_assets={
			'HLSL30_VI_2.0': dict(zip(COMMON_VIS, ('s3_NDVI', 's3_NDWI', 's3_NDMI', 's3_EVI', 's3_SAVI', 's3_MSAVI', 's3_NBR2', 's3_TVI'))),
			'HLSS30_VI_2.0': dict(zip(COMMON_VIS, ('s3_NDVI', 's3_NDWI', 's3_NDMI', 's3_EVI', 's3_SAVI', 's3_MSAVI', 's3_NBR2', 's3_TVI'))),
		},
		qa_asset='s3_Fmask',
		qa_bits={'cirrus': 0, 'cloud': 1, 'dilated_cloud': 2, 'shadow': 3, 'snow': 4, 'water': 5},
		qa_nodata=255, band_nodata=-19999,
		scale_factor=0.0001, offset=0.0, val_min=-10000, val_max=10000),
}
