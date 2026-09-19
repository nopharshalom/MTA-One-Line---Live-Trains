import json
import os
import datetime

import requests
from google.transit import gtfs_realtime_pb2


# --------------------------------------------------
# SETTINGS
# --------------------------------------------------

# No API key needed as of the MTA's current subway GTFS-realtime feeds.
FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs"

LIVE_LAYER_URL = (
    "https://services1.arcgis.com/SR1muQK0r6SVF2nb/"
    "arcgis/rest/services/live_trains/FeatureServer/0"
)

TOKEN_URL = "https://www.arcgis.com/sharing/rest/oauth2/token"

STATION_FILE = "route1_stations.geojson"

# App authentication credentials, set as GitHub Actions secrets.
# This account logs in via SSO, so there's no separate ArcGIS
# password to authenticate with, this client_id/client_secret
# pair authenticates as the app itself instead of as a user.
CLIENT_ID = os.environ["ARCGIS_CLIENT_ID"]
CLIENT_SECRET = os.environ["ARCGIS_CLIENT_SECRET"]


# --------------------------------------------------
# GET AN ACCESS TOKEN (client credentials flow)
# --------------------------------------------------

def get_access_token():
    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
            "f": "json",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if "access_token" not in data:
        raise RuntimeError(f"Failed to get access token: {data}")

    return data["access_token"]


ACCESS_TOKEN = get_access_token()
print("Got access token.")


# --------------------------------------------------
# ONE-TIME SCHEMA CHECK
# Confirms the layer's actual field names match what
# this script sends, using the layer's own metadata.
# --------------------------------------------------

schema_response = requests.get(
    LIVE_LAYER_URL,
    params={"f": "json", "token": ACCESS_TOKEN},
    timeout=30,
)
schema_response.raise_for_status()
layer_info = schema_response.json()

print("\nLive layer schema:")
field_names = set()
for f in layer_info.get("fields", []):
    print(f"  {f['name']}  ({f['type']})")
    field_names.add(f["name"])

required_fields = {"trip_id", "direction", "next_stop_name", "status", "last_updated"}
missing_fields = required_fields - field_names
if missing_fields:
    print(f"\n*** WARNING: layer is missing expected fields: {missing_fields} ***")
    print("*** Field names above must match this list exactly (case-sensitive). ***\n")


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
# PUSH LIVE TRAINS TO ARCGIS (raw REST, no arcgis package)
# --------------------------------------------------

def truncate_layer():
    response = requests.post(
        f"{LIVE_LAYER_URL}/deleteFeatures",
        data={
            "where": "1=1",
            "f": "json",
            "token": ACCESS_TOKEN,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def add_features(features):
    response = requests.post(
        f"{LIVE_LAYER_URL}/addFeatures",
        data={
            "features": json.dumps(features),
            "f": "json",
            "token": ACCESS_TOKEN,
        },
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


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

    truncate_result = truncate_layer()
    print("Truncate result:", truncate_result)

    result = add_features(features)

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
# every 5 minutes, so there's no loop or sleep here.
# --------------------------------------------------

print("\nFetching Route 1 trains...")
trains = fetch_route_1_trains()
print(f"Found {len(trains)} Route 1 trains")
push_update(trains)
print("Done.")
