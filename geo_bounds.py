FOREST_LAT_MIN = 34.15
FOREST_LAT_MAX = 34.56
FOREST_LON_MIN = -118.36
FOREST_LON_MAX = -117.58

# Malibu collection includes the Point Mugu-to-Santa Monica coastal corridor.
MALIBU_LAT_MIN = 33.99
MALIBU_LAT_MAX = 34.34
MALIBU_LON_MIN = -119.10
MALIBU_LON_MAX = -118.45

# Approximate US-101 corridor across the Malibu collection box, west to east.
# Malibu incidents should be coastal/Santa Monica Mountains side, not north of 101.
MALIBU_101_BOUNDARY = [
    (-119.10, 34.225),
    (-118.95, 34.190),
    (-118.82, 34.155),
    (-118.75, 34.145),
    (-118.64, 34.145),
    (-118.55, 34.155),
    (-118.45, 34.160),
]

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
        and (region != "malibu" or coordinates_south_of_malibu_101(lat, lon))
    )


def coordinates_south_of_malibu_101(latitude, longitude):
    boundary_lat = interpolated_malibu_101_latitude(float(longitude))
    if boundary_lat is None:
        return True
    return float(latitude) <= boundary_lat


def interpolated_malibu_101_latitude(longitude):
    lon = float(longitude)
    points = MALIBU_101_BOUNDARY
    if lon < points[0][0] or lon > points[-1][0]:
        return None
    for (left_lon, left_lat), (right_lon, right_lat) in zip(points, points[1:]):
        if left_lon <= lon <= right_lon:
            if right_lon == left_lon:
                return left_lat
            ratio = (lon - left_lon) / (right_lon - left_lon)
            return left_lat + ratio * (right_lat - left_lat)
    return points[-1][1]


def coordinates_in_forest_bounds(latitude, longitude):
    return coordinates_in_region_bounds(latitude, longitude, "forest")


def clear_coordinates_outside_region_bounds(record, region="forest"):
    if not coordinates_in_region_bounds(record.get("latitude"), record.get("longitude"), region):
        record["latitude"] = None
        record["longitude"] = None
    return record


def clear_coordinates_outside_forest_bounds(record):
    return clear_coordinates_outside_region_bounds(record, "forest")
