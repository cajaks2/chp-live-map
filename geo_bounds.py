FOREST_LAT_MIN = 34.15
FOREST_LAT_MAX = 34.56
FOREST_LON_MIN = -118.36
FOREST_LON_MAX = -117.58


def coordinates_in_forest_bounds(latitude, longitude):
    if latitude is None or longitude is None:
        return False
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError):
        return False
    return (
        FOREST_LAT_MIN <= lat <= FOREST_LAT_MAX
        and FOREST_LON_MIN <= lon <= FOREST_LON_MAX
    )


def clear_coordinates_outside_forest_bounds(record):
    if not coordinates_in_forest_bounds(record.get("latitude"), record.get("longitude")):
        record["latitude"] = None
        record["longitude"] = None
    return record
