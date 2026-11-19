"""Parsing Tile id."""
import re

TILE_PATTERN_1DEG = re.compile(r'^(?P<lon>\d{3})(?P<ew>[EW])(?P<lat>\d{2})(?P<ns>[NS])$')    # e.g. 150W65N
TILE_PATTERN_15MIN = re.compile(r'^(?P<lon>\d{5})(?P<ew>[EW])(?P<lat>\d{4})(?P<ns>[NS])$')  # e.g. 14975W6525N
# (pattern, divisor applied to the corner numbers, tile edge in degrees)
TILE_FORMATS = ((TILE_PATTERN_1DEG, 1, 1.0), (TILE_PATTERN_15MIN, 100, 0.25))


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
