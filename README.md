# boreal_si

Spectral-index tools for a geographic tile, designed to run as MAAP DPS jobs. There are two scripts:

| Script | What it does | Input `--src` |
|---|---|---|
| `calc_si.py` | Computes per-scene spectral indices (NDVI, EVI, …) from surface reflectance. | `landsat-sr`, `hls-sr` |
| `composite_si.py` | Builds per-year median and maximum composites of precomputed index (VI) products. | `landsat-vi`, `hls-vi` |

Each result is an Int16 Cloud Optimized GeoTIFF with a STAC item. Each job also writes one STAC catalog.

## Setup

```bash
bash build_env.sh   # updates the 'python' conda env from dependencies.yml
```

Both scripts import `lpdaac_access.py` when they start. It reads `EARTHDATA_USERNAME` and `EARTHDATA_PASSWORD` from the MAAP secrets, logs in with `earthaccess`, gets temporary S3 credentials for LP DAAC and sets the GDAL options. The scripts therefore need to run in an environment where the MAAP secrets are available. HLS data is read from S3 with those credentials.

The temporary S3 credentials last about an hour. Both scripts check their age before reading each scene and get new ones when they are older than 45 minutes (`S3_CREDENTIAL_MAX_AGE` in `lpdaac_access.py`). If a refresh fails, a warning is logged and the old credentials are kept.

Landsat Collection 2 data is read from the requester-pays `usgs-landsat` S3 bucket, so the environment needs AWS credentials that can pay for those requests.

## Usage

### Per-scene indices (`calc_si.py`)

DPS (positional arguments):

```bash
bash run_calc.sh <tile> <indices> <start_year> <end_year> <start_date> <end_date> <src>
bash run_calc.sh 150W65N NDVI,EVI,NBR2 1990 2000 07-01 08-31 landsat-sr
bash run_calc.sh 14975W6525N NDVI 2020 2022 07-01 08-31 hls-sr
```

Directly:

```bash
python calc_si.py --tile 150W65N --indices NDVI,EVI --ys 1990 --ye 2000 \
    --ds 07-01 --de 08-31 --src landsat-sr --output ./output
```

| Argument | Required | Description |
|---|---|---|
| `--tile` | yes | Tile id; see [Tiles](#tiles). |
| `--indices` | no | Comma-separated index names, case-insensitive. Default: all indices in [Indices](#indices). |
| `--ys`, `--ye` | yes | First and last year (inclusive). |
| `--ds`, `--de` | yes | First and last day of the search window as `MM-DD`, applied to every year. |
| `--src` | yes | `landsat-sr` or `hls-sr`. |
| `--output` | yes | Output directory; created if missing. |

### Composites (`composite_si.py`)

DPS:

```bash
bash run_composite.sh <tile> <indices> <start_year> <end_year> <start_date> <end_date> <src>
bash run_composite.sh 150W65N NDVI,NDWI 2015 2020 07-01 08-31 hls-vi
```

Directly:

```bash
python composite_si.py --tile 150W65N --indices NDVI,NDWI --ys 2015 --ye 2020 \
    --ds 07-01 --de 08-31 --src hls-vi --output ./output
```

The arguments are the same as for `calc_si.py`, except that `--src` is `landsat-vi` or `hls-vi`. The default `--indices` is `NDVI,NDWI,NDMI,EVI,SAVI,MSAVI,NBR2,TVI`. NDSI is not available, because the VI products don't contain it.

Both run scripts write to `./output` in the working directory.

## Tiles

Tiles are named by their upper-left (north-west) corner:

| Format | Example | Corner | Size |
|---|---|---|---|
| 1 degree | `150W65N` | 150° W, 65° N | 1° × 1° |
| 15 minute | `14975W6525N` | 149.75° W, 65.25° N | 0.25° × 0.25° |

In the 15-minute format the numbers are degrees × 100: five digits for the longitude and four for the latitude. Every output is in EPSG:4326 at 0.00025° (about 28 m). A 1° tile is 4000 × 4000 pixels and a 15-minute tile is 1000 × 1000.

## Sources

Defined in `sources.py` (`SOURCES`).

| `--src` | Used by | STAC API | Collections | Data |
|---|---|---|---|---|
| `landsat-sr` | `calc_si.py` | `https://landsatlook.usgs.gov/stac-server` | `landsat-c2l2-sr` | Landsat Collection 2 Level-2 surface reflectance |
| `hls-sr` | `calc_si.py` | `https://cmr.earthdata.nasa.gov/stac/LPCLOUD` | `HLSL30_2.0`, `HLSS30_2.0` | HLS v2.0 surface reflectance |
| `landsat-vi` | `composite_si.py`, output scaling of `calc_si.py` | `https://dps-stac.maap-project.org` | `landsat-vi` | Precomputed Landsat indices |
| `hls-vi` | `composite_si.py`, output scaling of `calc_si.py` | `https://cmr.earthdata.nasa.gov/stac/LPCLOUD` | `HLSL30_VI_2.0`, `HLSS30_VI_2.0` | HLS v2.0 vegetation indices |

For `hls-vi`, `composite_si.py` does not use the STAC API. It searches CMR's granule API (`https://cmr.earthdata.nasa.gov/search/granules.umm_json`) directly, with one request per collection that covers the `--ds` to `--de` window of every year (up to 2000 granules per page). A 14-year job therefore sends 2 search requests instead of about 30. The results are the same scenes and asset links as the STAC search. This is set by `cmr_provider` and `cmr_collections` in `sources.py`; sources without them (`landsat-vi`) still use the STAC API, one search per year.

Before searching, each `composite_si.py` job waits a random 0 to 10 minutes (`SEARCH_START_JITTER`), so that many jobs started together do not search at the same moment.

STAC and CMR searches wait and retry when the server throttles them. CMR answers requests over its rate limit with HTTP 429 and a `Retry-After` header ([CMR request moderation](https://cmr.earthdata.nasa.gov/search/site/docs/search/api.html#request-moderation)). `open_stac_client` and `search_cmr_by_year` in `si_utils.py` wait at least the time in that header. While the failures continue, the wait also grows as 2, 4, 8, … seconds up to 120 s, plus random jitter of up to 50% so that parallel jobs do not retry at the same moment. It retries up to 30 times, so a persistent throttle is waited out for about 50 minutes. HTTP 500, 502, 503 and 504 and connection errors are retried the same way. Each wait is logged as a warning. If all retries fail, the job stops with an error.

For each source, `sources.py` defines the band or index assets, the QA asset and bits, the nodata values, `scale_factor` and `offset` (value = `scale_factor` × DN + `offset`), and the valid DN range (`val_min` to `val_max`).

`calc_si.py` reads the bands of the `-sr` source. It writes its results using the scale, valid range and nodata of the matching `-vi` source (`landsat-sr` → `landsat-vi`, `hls-sr` → `hls-vi`). Index values are therefore stored as `index / scale_factor` (that is, index × 10000) in Int16, truncated toward zero. Results outside `val_min` to `val_max` (−10000 to 10000) become nodata (−19999).

Pixels flagged in QA as cloud, cloud-adjacent (dilated cloud), cloud shadow, snow/ice or water are set to nodata.

## Indices

Index names are case-insensitive on the command line and are written in upper case in outputs.

| Name | Equation (surface reflectance) |
|---|---|
| `NDVI` | (nir08 − red) / (nir08 + red) |
| `EVI` | 2.5 (nir08 − red) / (nir08 + 6 red − 7.5 blue + 1) |
| `SAVI` | 1.5 (nir08 − red) / (nir08 + red + 0.5) |
| `MSAVI` | 0.5 (2 nir08 + 1 − √((2 nir08 + 1)² − 8 (nir08 − red))) |
| `NDWI` | (green − nir08) / (green + nir08) |
| `NDMI` | (nir08 − swir16) / (nir08 + swir16) |
| `NBR2` | (swir16 − swir22) / (swir16 + swir22) |
| `TVI` | 0.5 (120 (nir08 − green) − 200 (red − green)) |
| `NDSI` | (green − swir16) / (green + swir16) |

The equations and band lists are defined in `si_utils.py` (`INDEX_EQUATIONS`, `INDEX_BANDS`).

**NDSI is not fully supported yet.** `calc_si.py` applies the same QA mask as for the other indices, so snow/ice pixels are set to nodata, which removes the pixels NDSI is meant to measure. Writing the QA masks as separate files, instead of setting a fixed list of masked categories to nodata, is planned.

## Outputs

All COGs are Int16 and LZW-compressed. The nodata value is −19999, except for the `_cnt` composite counts (see below). Each COG has a STAC item next to it with the same name and a `.json` extension.

`calc_si.py`, for each scene and index:

- `<tile>_<year>_<scene_id>_<INDEX>.tif`

`composite_si.py`, for each year and index (per-pixel statistic over all usable scenes in the window, ignoring masked pixels):

- `<tile>_<year>_<INDEX>_med.tif`: median, truncated toward zero to an integer.
- `<tile>_<year>_<INDEX>_max.tif`: maximum.
- `<tile>_<year>_<INDEX>_cnt.tif`: number of valid scenes used for each pixel. Int16 with nodata 0 (not −19999) and no scaling, so its STAC raster `scale` is 1.

`composite_si.py` searches once per year and reuses the scene list for every index. It holds each usable scene as an Int16 array and computes the median and maximum in blocks of rows. The block size is chosen from the number of scenes so that the arrays stay within `MEMORY_BUDGET` (10 GiB, in `composite_si.py`). On a 16 GiB machine, a 1° tile with 100 scenes peaks at about 11 GiB. Above about 320 scenes per tile the held scenes alone exceed the budget, and a warning is logged. A pixel with no valid observation is nodata. In `composite_si.py` a pixel is masked when it is flagged in QA or outside `val_min` to `val_max`.

For each job, `catalog.json` is a self-contained STAC catalog that links all items written by the job.

Each STAC item uses the projection and raster extensions, with raster `scale` equal to `scale_factor` (0.0001), and these properties:

| Property | Value |
|---|---|
| `boreal_si:tile` | Tile id. |
| `boreal_si:year` | Year. |
| `boreal_si:index` | Index name. |
| `boreal_si:composite` | `None` for per-scene outputs. For composites a description such as `Median of 07-01:08-31`. |
| `boreal_si:scale_factor` | The source's `scale_factor`. |
| `boreal_si:source_item` | Source scene id, or `Composite`. |
| `boreal_si:source_collection` | Source STAC collection (comma-separated for composites). |

The item `datetime` is the scene's acquisition date for per-scene outputs. For composites it is the first day of the search window (`<year>-<ds>`).

If a scene has a missing or unreadable asset, it is skipped with a warning and the job continues. If a STAC item cannot be written, an error is logged and the COG is kept.

## Files

| File | Purpose |
|---|---|
| `calc_si.py` | Scene search, index calculation, COG and STAC writing (per scene) |
| `composite_si.py` | Median and maximum composites of VI products |
| `sources.py` | Source configurations (`SOURCES`, `SourceConfig`) |
| `si_utils.py` | Index definitions, QA masking, warping, band loading, COG writing |
| `tile_utils.py` | Tile id parsing and bounds |
| `stac_output.py` | STAC item and catalog generation |
| `lpdaac_access.py` | Earthdata login, LP DAAC S3 credentials (refreshed every 45 minutes) and GDAL options; logs in on import |
| `run_calc.sh` | DPS entry point for `calc_si.py` |
| `run_composite.sh` | DPS entry point for `composite_si.py` |
| `build_env.sh` | Conda environment update |
| `dependencies.yml` | Conda dependencies |
