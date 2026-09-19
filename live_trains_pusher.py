import json
import os
import datetime

import requests
from google.transit import gtfs_realtime_pb2
from arcgis.gis import GIS
from arcgis.features import FeatureLayer


# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

# No API key needed as of the MTA's current subway GTFS-realtime feeds.
FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"

LIVE_LAYER_URL = (
    "https://services1.arcgis.com/SR1muQK0r6SVF2nb/"
    "arcgis/rest/services/live_trains/FeatureServer/0"
)

STATION_FILE = "route1_stations.geojson"

# Credentials come from environment variables (set as GitHub Actions
# secrets), never hardcoded, since this file lives in a repo.
ARCGIS_USERNAME = os.environ["ARCGIS_USERNAME"]
ARCGIS_PASSWORD = os.environ["ARCGIS_PASSWORD"]


# --------------------------------------------------
# CONNECT TO ARCGIS ONLINE
# --------------------------------------------------

gis = GIS(
    "https://www.arcgis.com",
    ARCGIS_USERNAME,
    ARCGIS_PASSWORD
)

print("Successfully logged in as:", gis.properties.user.username)

live_layer = FeatureLayer(
    LIVE_LAYER_URL,
    gis=gis
)

print("Connected to layer:", live_layer.properties.name)

# --------------------------------------------------
# ONE-TIME SCHEMA CHECK
# Confirms the layer's actual field names/types match
# what this script sends. Run once at startup so any
# schema mismatch shows up immediately in the console.
# --------------------------------------------------

print("\nLive layer schema:")
field_names = set()
for f in live_layer.properties.fields:
    print(f"  {f.name}  ({f.type})")
    field_names.add(f.name)

required_fields = {"trip_id", "direction", "next_stop_name", "status", "last_updated"}
missing_fields = required_fields - field_names
if missing_fields:
    print(f"\n*** WARNING: layer is missing expected fields: {missing_fields} ***")
    print("*** Field names below must match this list exactly (case-sensitive). ***\n")


# --------------------------------------------------
# LOAD STATION LOOKUP
# --------------------------------------------------

with open(STATION_FILE, "r") as f:
    station_data = json.load(f)

stop_lookup = {}

for feature in station_data["features"]:
    stop_id = feature["properties"]["stop_id"]
    name = feature["properties"]["name"]
    coords = feature["geometry"]["coordinates"]

    stop_lookup[stop_id] = {
        "name": name,
        "coords": coords
    }

print(f"Loaded {len(stop_lookup)} stations")


# --------------------------------------------------
# FETCH LIVE MTA DATA
# --------------------------------------------------

def fetch_route_1_trains():

    response = requests.get(FEED_URL, timeout=30)
    response.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)

    trains = []

    for entity in feed.entity:

        if not entity.HasField("trip_update"):
            continue

        trip = entity.trip_update.trip

        # Only Route 1
        if trip.route_id != "1":
            continue

        stop_updates = entity.trip_update.stop_time_update

        if not stop_updates:
            continue

        next_stop = stop_updates[0]

        stop_id = next_stop.stop_id

        if stop_id.endswith("N"):
            direction = "Uptown / Bronx-bound"
        elif stop_id.endswith("S"):
            direction = "Downtown / Brooklyn-bound"
        else:
            direction = "Unknown"

        trains.append({
            "trip_id": trip.trip_id,
            "direction": direction,
            "next_stop_id": next_stop.stop_id
        })

    return trains


# --------------------------------------------------
# PUSH LIVE TRAINS TO ARCGIS
# --------------------------------------------------

def push_update(trains):

    features = []

    # Avoid uploading the exact same trip more than once
    # if duplicate records appear in the feed.
    seen_trips = set()

    for train in trains:

        trip_id = train["trip_id"]

        if trip_id in seen_trips:
            continue

        seen_trips.add(trip_id)

        # Live MTA stop IDs end in N or S.
        # Our station GeoJSON uses the base stop ID.
        station_id = train["next_stop_id"][:-1]

        station = stop_lookup.get(station_id)

        if not station:
            print(
                f"Station not found for "
                f"{train['next_stop_id']} ({trip_id})"
            )
            continue

        coords = station["coords"]

        features.append({
            "geometry": {
                "x": coords[0],
                "y": coords[1],
                # Explicit spatial reference. Without this, some
                # layers can silently place points incorrectly if
                # their default spatial reference isn't WGS84.
                "spatialReference": {"wkid": 4326}
            },
            "attributes": {
                "trip_id": trip_id,
                "direction": train["direction"],
                "next_stop_name": station["name"],
                "status": "In service",
                "last_updated": int(
                    datetime.datetime.now().timestamp() * 1000
                )
            }
        })

    if not features:
        print("No valid features to push this run (see 'Station not found' warnings above, if any).")
        return None

    # Remove the previous train positions
    truncate_result = live_layer.manager.truncate()
    print("Truncate result:", truncate_result)

    # Add the newest train positions
    result = live_layer.edit_features(
        adds=features
    )

    # Check success/failure per feature instead of just counting
    # how many we attempted to send.
    add_results = result.get("addResults", [])
    successes = [r for r in add_results if r.get("success")]
    failures = [r for r in add_results if not r.get("success")]

    print(f"Attempted {len(features)} | Succeeded {len(successes)} | Failed {len(failures)}")

    if failures:
        print("Failure details:")
        for f in failures:
            print(" ", f.get("error"))

    return result


# --------------------------------------------------
# RUN ONCE AND EXIT
# GitHub Actions' schedule triggers this file fresh
# every 5 minutes, so there's no loop or sleep here
# anymore, that repetition is now GitHub's job.
# --------------------------------------------------

print("\nFetching Route 1 trains...")
trains = fetch_route_1_trains()
print(f"Found {len(trains)} Route 1 trains")
push_update(trains)
print("Done.")
