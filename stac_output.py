"""Write STAC items (and a catalog) beside the spectral-index COGs produced by calc_si.py."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pystac
from osgeo import gdal, osr
from pystac.extensions.projection import ProjectionExtension
from pystac.extensions.raster import DataType, RasterBand, RasterExtension

gdal.UseExceptions()


def _footprint(ds):
	"""Return (bbox, geometry) in EPSG:4326 from the raster header only."""
	gt = ds.GetGeoTransform()
	w, h = ds.RasterXSize, ds.RasterYSize
	corners = [(gt[0] + gt[1] * x + gt[2] * y, gt[3] + gt[4] * x + gt[5] * y)
			   for x, y in ((0, 0), (w, 0), (w, h), (0, h))]
	src = osr.SpatialReference()
	src.ImportFromWkt(ds.GetProjectionRef())
	dst = osr.SpatialReference()
	dst.ImportFromEPSG(4326)
	src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
	dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
	if not src.IsSame(dst):
		tr = osr.CoordinateTransformation(src, dst)
		corners = [tr.TransformPoint(x, y)[:2] for x, y in corners]
	xs, ys = [c[0] for c in corners], [c[1] for c in corners]
	bbox = [min(xs), min(ys), max(xs), max(ys)]
	ring = [list(c) for c in corners] + [list(corners[0])]
	return bbox, {"type": "Polygon", "coordinates": [ring]}


def write_stac_item(cog_path, tile, year, index, source_id, date_str, equation, scale_factor, nodata,
					source_collection):
	"""Write <cog>.json beside the COG and return the path to the item."""
	cog = Path(cog_path).resolve()
	item_path = cog.with_suffix(".json")
	dt = datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=timezone.utc)

	ds = gdal.Open(str(cog))
	bbox, geometry = _footprint(ds)
	shape = [ds.RasterYSize, ds.RasterXSize]
	transform = list(ds.GetGeoTransform())
	transform = [transform[1], transform[2], transform[0], transform[4], transform[5], transform[3]]
	srs = osr.SpatialReference()
	srs.ImportFromWkt(ds.GetProjectionRef())
	srs.AutoIdentifyEPSG()
	code = srs.GetAuthorityCode(None)
	epsg = int(code) if code else None
	wkt2 = None if epsg else srs.ExportToWkt()
	ds = None

	item = pystac.Item(
		id=cog.stem, geometry=geometry, bbox=bbox, datetime=dt,
		properties={
			"boreal_si:tile": tile,
			"boreal_si:year": int(year),
			"boreal_si:index": index,
			"boreal_si:equation": equation,
			"boreal_si:scale_factor": scale_factor,
			"boreal_si:source_item": source_id,
			"boreal_si:source_collection": source_collection,
		})
	proj = ProjectionExtension.ext(item, add_if_missing=True)
	proj.epsg = epsg
	if wkt2:
		proj.wkt2 = wkt2
	proj.shape = shape
	proj.transform = transform
	proj.bbox = None

	asset = pystac.Asset(href=cog.name, media_type=pystac.MediaType.COG,
						 roles=["data"], title=f"{index.upper()} {source_id}")
	item.add_asset(index, asset)
	RasterExtension.ext(asset, add_if_missing=True).bands = [
		RasterBand.create(data_type=DataType.INT16, nodata=nodata, scale=1.0 / scale_factor, unit="1")]
	asset.extra_fields["bands"] = [{"name": index, "description": f"{index.upper()} scaled by {scale_factor}"}]

	item.set_self_href(str(item_path))
	json.dumps(item.to_dict(), allow_nan=False)
	item.save_object(include_self_link=False, dest_href=str(item_path))
	return str(item_path)


def write_stac_catalog(output_dir, item_paths=None):
	"""Write catalog.json in output_dir linking the given item JSONs (default: all *.json except the catalog)."""
	out = Path(output_dir).resolve()
	catalog_path = out / "catalog.json"
	catalog = pystac.Catalog(id="DPS", description="Boreal spectral index outputs",
							 catalog_type=pystac.CatalogType.SELF_CONTAINED)
	catalog.set_self_href(str(catalog_path))
	if item_paths is None:
		item_paths = [p for p in out.glob("*.json") if p.name != "catalog.json"]
	for p in sorted(Path(p).resolve() for p in item_paths):
		item = pystac.Item.from_file(str(p))
		catalog.add_item(item)
		# add_item applies PySTAC's nested layout; restore the sidecar location.
		item.set_self_href(str(p))
	catalog.make_all_asset_hrefs_relative()
	catalog.validate_all()
	catalog.save(catalog_type=pystac.CatalogType.SELF_CONTAINED)
	return str(catalog_path)
