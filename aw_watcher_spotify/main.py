#!/usr/bin/env python3

import sys
import logging
import traceback
from typing import Optional
from time import sleep
from datetime import datetime, timezone, timedelta
import json
import argparse

from requests import ConnectionError
from spotipy.exceptions import SpotifyException
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth, SpotifyOauthError

from aw_core import dirs
from aw_core.models import Event
from aw_client.client import ActivityWatchClient
from aw_core.log import setup_logging


DEFAULT_CONFIG = """
[aw-watcher-spotify]
username = ""
client_id = ""
client_secret = ""
poll_time = 5.0"""


def get_current_track(sp) -> Optional[dict]:
    current_track = sp.currently_playing(additional_types=["episode"])
    if current_track and current_track["is_playing"]:
        return current_track
    return None


def data_from_track(track: dict, sp) -> dict:
    song_name = track["item"]["name"]
    try:
        data = sp.audio_features(track["item"]["id"])[0] or {}
    except SpotifyException:
        data = {}
    data["title"] = song_name
    data["uri"] = track["item"]["uri"]

    if track["item"]["type"] == "track":
        artist_name = track["item"]["artists"][0]["name"]
        album_name = track["item"]["album"]["name"]
        data["popularity"] = track["item"]["popularity"] or -1
        data["album"] = album_name
        data["artist"] = artist_name
        logging.debug("TRACK: {} - {} ({})".format(song_name, artist_name, album_name))
    elif track["item"]["type"] == "episode":
        publisher = track["item"]["show"]["publisher"]
        data["artist"] = publisher
        data["album"] = track["item"]["show"]["name"]
        logging.debug("EPISODE: {} - {}".format(song_name, publisher))

    return data


def auth(username: str, client_id: str, client_secret: str) -> Spotify:
    scope = "user-read-currently-playing"
    try:
        auth_manager = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri="http://127.0.0.1:8088",
            scope=scope,
            cache_path=f".cache-{username}"
        )
        return Spotify(auth_manager=auth_manager)
    except SpotifyOauthError as e:
        sys.exit(1)


def load_config():
    from aw_core.config import load_config_toml as _load_config

    return _load_config("aw-watcher-spotify", DEFAULT_CONFIG)


def print_statusline(msg):
    last_msg_length = (
        len(print_statusline.last_msg) if hasattr(print_statusline, "last_msg") else 0
    )
    print(" " * last_msg_length, end="\r")
    print(msg, end="\r")
    print_statusline.last_msg = msg


def main():
    argparser = argparse.ArgumentParser()
    argparser.add_argument("--testing", action="store_true")
    argparser.add_argument("--verbose", action="store_true")
    args = argparser.parse_args()
    setup_logging(
    name="aw-watcher-spotify",
    testing=args.testing,
    verbose=args.verbose,
    log_stderr=True,
    log_file=True,
    )
    config_dir = dirs.get_config_dir("aw-watcher-spotify")

    config = load_config()
    poll_time = float(config["aw-watcher-spotify"].get("poll_time"))
    username = config["aw-watcher-spotify"].get("username", None)
    client_id = config["aw-watcher-spotify"].get("client_id", None)
    client_secret = config["aw-watcher-spotify"].get("client_secret", None)
    if not username or not client_id or not client_secret:
        logging.warning(
            "username, client_id or client_secret not specified in config file (in folder {}). Get your client_id and client_secret here: https://developer.spotify.com/my-applications/".format(
                config_dir
            )
        )
        sys.exit(1)

    # TODO: Fix --testing flag and set testing as appropriate
    aw = ActivityWatchClient("aw-watcher-spotify", testing=False)
    bucketname = "{}_{}".format(aw.client_name, aw.client_hostname)
    aw.create_bucket(bucketname, "currently-playing", queued=True)
    aw.connect()

    sp = auth(username, client_id=client_id, client_secret=client_secret)
    last_track = None
    track = None
    while True:
        try:
            track = get_current_track(sp)
            # from pprint import pprint
            # pprint(track)
        except SpotifyException as e:
            print_statusline("\nToken expired, trying to refresh\n")
            sp = auth(username, client_id=client_id, client_secret=client_secret)
            continue
        except ConnectionError as e:
            logging.error(
                "Connection error while trying to get track, check your internet connection."
            )
            sleep(poll_time)
            continue
        except json.JSONDecodeError as e:
            logging.error("Error trying to decode")
            sleep(0.1)
            continue
        except Exception as e:
            logging.error("Unknown Error")
            logging.error(traceback.format_exc())
            sleep(0.1)
            continue

        try:
            # Outputs a new line when a song ends, giving a short history directly in the log
            if last_track:
                last_track_data = data_from_track(last_track, sp)
                if not track or (
                    track
                    and last_track_data["uri"] != data_from_track(track, sp)["uri"]
                ):
                    song_td = timedelta(seconds=last_track["progress_ms"] / 1000)
                    song_time = int(song_td.seconds / 60), int(song_td.seconds % 60)
                    print_statusline(
                        "Track ended ({}:{:02d}): {title} - {artist} ({album})\n".format(
                            *song_time, **last_track_data
                        )
                    )

            if track:
                track_data = data_from_track(track, sp)
                song_td = timedelta(seconds=track["progress_ms"] / 1000)
                song_time = int(song_td.seconds / 60), int(song_td.seconds % 60)

                print_statusline(
                    "Current track ({}:{:02d}): {title} - {artist} ({album})".format(
                        *song_time, **track_data
                    )
                )

                event = Event(timestamp=datetime.now(timezone.utc), data=track_data)
                aw.heartbeat(bucketname, event, pulsetime=poll_time + 1, queued=True)
            else:
                print_statusline("Waiting for track to start playing...")

            last_track = track
        except Exception as e:
            print("An exception occurred: {}".format(e))
            traceback.print_exc()
        sleep(poll_time)


if __name__ == "__main__":
    main()
