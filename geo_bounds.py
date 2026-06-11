FOREST_LAT_MIN = 34.15
FOREST_LAT_MAX = 34.56
FOREST_LON_MIN = -118.36
FOREST_LON_MAX = -117.58

# Malibu collection includes the Point Mugu-to-Santa Monica coastal corridor.
MALIBU_LAT_MIN = 33.99
MALIBU_LAT_MAX = 34.34
MALIBU_LON_MIN = -119.10
MALIBU_LON_MAX = -118.45

REGION_BOUNDS = {
    "forest": (FOREST_LAT_MIN, FOREST_LAT_MAX, FOREST_LON_MIN, FOREST_LON_MAX),
    "malibu": (MALIBU_LAT_MIN, MALIBU_LAT_MAX, MALIBU_LON_MIN, MALIBU_LON_MAX),
}


def coordinates_in_region_bounds(latitude, longitude, region="forest"):
    if latitude is None or longitude is None:
        return False
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return False
    lat_min, lat_max, lon_min, lon_max = REGION_BOUNDS.get(region, REGION_BOUNDS["forest"])
    return (
        lat_min <= lat <= lat_max
        and lon_min <= lon <= lon_max
    )


def coordinates_in_forest_bounds(latitude, longitude):
    return coordinates_in_region_bounds(latitude, longitude, "forest")


def clear_coordinates_outside_region_bounds(record, region="forest"):
    if not coordinates_in_region_bounds(record.get("latitude"), record.get("longitude"), region):
        record["latitude"] = None
        record["longitude"] = None
    return record


def clear_coordinates_outside_forest_bounds(record):
    return clear_coordinates_outside_region_bounds(record, "forest")
