# boreal_si

Computes per-scene spectral indices (NDVI, EVI, …) over a geographic tile from
Landsat Collection 2 or HLS v2.0 surface reflectance. The scenes come from a
July–August search for each year. Each index is written as an Int16 Cloud
Optimized GeoTIFF with a STAC item, and the job writes one STAC catalog.
The scripts are designed to run as a MAAP DPS job.

## Setup

```bash
bash build_env.sh   # updates the 'python' conda env from dependencies.yml
```

HLS data is read over HTTPS from LP DAAC, so it needs an Earthdata Login account
in `~/.netrc`:

```
machine urs.earthdata.nasa.gov login <username> password <password>
```

Landsat data is read from the requester-pays `usgs-landsat` S3 bucket, so it
needs AWS credentials that can pay for those requests.

## Usage

DPS (positional arguments):

```bash
bash run.sh <tile> <indices> <start_year> <end_year> [collection]
bash run.sh 150W65N ndvi,evi,nbr2 1990 2000            # Landsat (default)
bash run.sh 14975W6525N ndvi 2020 2022 hls             # HLS
```

Outputs go to `./output` in the working directory. In `run.sh` the indices
argument is required; an empty value is rejected.

Directly:

```bash
python calc_si.py --tile 150W65N --indices ndvi,evi --ys 1990 --ye 2000 \
    --collection landsat --output ./output
```

| Argument | Required | Description |
|---|---|---|
| `--tile` | yes | Tile id; see [Tiles](#tiles). |
| `--indices` | no | Comma-separated index names, case-insensitive. Default: all indices. |
| `--ys`, `--ye` | yes | First and last year (inclusive). Each year searches July 1 – August 31. |
| `--collection` | no | `landsat` (default) or `hls`. |
| `--output` | yes | Output directory; created if missing. |

## Tiles

Tiles are named by their upper-left (north-west) corner:

| Format | Example | Corner | Size |
|---|---|---|---|
| 1 degree | `150W65N` | 150° W, 65° N | 1° × 1° |
| 15 minute | `14975W6525N` | 149.75° W, 65.25° N | 0.25° × 0.25° |

In the 15-minute format the numbers are degrees × 100: five digits for the
longitude and four for the latitude. Every output is in EPSG:4326 at 0.00025°
(about 28 m). A 1° tile is 4000 × 4000 pixels and a 15-minute tile is
1000 × 1000.

## Collections

| `--collection` | STAC API | Collections |
|---|---|---|
| `landsat` | `https://landsatlook.usgs.gov/stac-server` | `landsat-c2l2-sr` |
| `hls` | `https://cmr.earthdata.nasa.gov/stac/LPCLOUD` | `HLSL30_2.0`, `HLSS30_2.0` |

Band names, QA bits, nodata values and reflectance scaling for each source
are defined in `SOURCES` in `calc_si.py`. Pixels flagged in QA as cloud,
cloud-adjacent (dilated cloud), cloud shadow, snow/ice or water are set to
nodata.

## Indices

| Name | Equation (surface reflectance) |
|---|---|
| `ndvi` | (nir08 − red) / (nir08 + red) |
| `evi` | 2.5 (nir08 − red) / (nir08 + 6 red − 7.5 blue + 1) |
| `savi` | 1.5 (nir08 − red) / (nir08 + red + 0.5) |
| `msavi` | 0.5 (2 nir08 + 1 − √((2 nir08 + 1)² − 8 (nir08 − red))) |
| `ndwi` | (green − nir08) / (green + nir08) |
| `ndmi` | (nir08 − swir16) / (nir08 + swir16) |
| `nbr2` | (swir16 − swir22) / (swir16 + swir22) |
| `tvi` | 0.5 (120 (nir08 − green) − 200 (red − green)) |

Values are stored as `round-toward-zero(index × 10000)` in Int16. Results outside
[−10000, 10000] are set to nodata (−19999).

## Outputs

For each scene and index:

- `<tile>_<year>_<scene_id>_<index>.tif`: Int16 COG, LZW-compressed, nodata −19999.
- `<tile>_<year>_<scene_id>_<index>.json`: STAC item next to the COG.

For each job:

- `catalog.json`: self-contained STAC catalog linking all items written by the job.

Each STAC item uses the projection and raster extensions (with `scale` 0.0001)
and these properties: `boreal_si:tile`, `boreal_si:year`, `boreal_si:index`,
`boreal_si:equation`, `boreal_si:scale_factor`, `boreal_si:source_item` and
`boreal_si:source_collection`. The last one is the source scene's collection
(`landsat-c2l2-sr`, `HLSL30_2.0` or `HLSS30_2.0`). The item `datetime` is the
scene's acquisition date.

If a scene has a missing or unreadable asset, it is skipped with a warning and
the job continues.

## Files

| File | Purpose |
|---|---|
| `calc_si.py` | Scene search, QA masking, index calculation, COG writing |
| `stac_output.py` | STAC item and catalog generation |
| `run.sh` | DPS entry point |
| `build_env.sh` | Conda environment update |
| `dependencies.yml` | Conda dependencies |
