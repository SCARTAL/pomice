from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from typing import Dict
from typing import List
from typing import Union

import aiohttp
import orjson as json

from .exceptions import *
from .objects import *

__all__ = ("Client",)

AM_URL_REGEX = re.compile(
    r"https?://music.apple.com/(?P<country>[a-zA-Z]{2})/(?P<type>album|playlist|song|artist)/(?P<name>.+)/(?P<id>[^?]+)",
)
AM_SINGLE_IN_ALBUM_REGEX = re.compile(
    r"https?://music.apple.com/(?P<country>[a-zA-Z]{2})/(?P<type>album|playlist|song|artist)/(?P<name>.+)/(?P<id>.+)(\?i=)(?P<id2>.+)",
)
AM_REQ_URL = "https://api.music.apple.com/v1/catalog/{country}/{type}s/{id}"
AM_BASE_URL = "https://api.music.apple.com"


class Client:
    """The base Apple Music client for Pomice.
    This will do all the heavy lifting of getting tracks from Apple Music
    and translating it to a valid Lavalink track. No client auth is required here.
    """

    def __init__(self) -> None:
        self.expiry: datetime = datetime(1970, 1, 1)
        self.token: str = ""
        self.headers: Dict[str, str] = {}
        self.session: aiohttp.ClientSession = None  # type: ignore
        self._log = logging.getLogger(__name__)

    async def request_token(self) -> None:
        if not self.session:
            self.session = aiohttp.ClientSession()

        async with self.session.get("https://music.apple.com/assets/index.919fe17f.js") as resp:
            if resp.status != 200:
                raise AppleMusicRequestException(
                    f"Error while fetching results: {resp.status} {resp.reason}",
                )
            text = await resp.text()
            match = re.search('"(eyJ.+?)"', text)
            if not match:
                raise AppleMusicRequestException(
                    "Could not find token in response.",
                )
            result = match.group(1)

            self.token = result
            self.headers = {
                "Authorization": f"Bearer {result}",
                "Origin": "https://apple.com",
            }
            token_split = self.token.split(".")[1]
            token_json = base64.b64decode(
                token_split + "=" * (-len(token_split) % 4),
            ).decode()
            token_data = json.loads(token_json)
            self.expiry = datetime.fromtimestamp(token_data["exp"])
            self._log.debug(f"Fetched Apple Music bearer token successfully")

    async def search(self, query: str) -> Union[Album, Playlist, Song, Artist]:
        if not self.token or datetime.utcnow() > self.expiry:
            await self.request_token()

        result = AM_URL_REGEX.match(query)
        if not result:
            raise InvalidAppleMusicURL(
                "The Apple Music link provided is not valid.",
            )

        country = result.group("country")
        type = result.group("type")
        id = result.group("id")

        if type == "album" and (sia_result := AM_SINGLE_IN_ALBUM_REGEX.match(query)):
            # apple music likes to generate links for singles off an album
            # by adding a param at the end of the url
            # so we're gonna scan for that and correct it
            id = sia_result.group("id2")
            type = "song"
            request_url = AM_REQ_URL.format(country=country, type=type, id=id)
        else:
            request_url = AM_REQ_URL.format(country=country, type=type, id=id)

        async with self.session.get(request_url, headers=self.headers) as resp:
            if resp.status != 200:
                raise AppleMusicRequestException(
                    f"Error while fetching results: {resp.status} {resp.reason}",
                )
            data: dict = await resp.json(loads=json.loads)
            self._log.debug(
                f"Made request to Apple Music API with status {resp.status} and response {data}",
            )

        data = data["data"][0]

        if type == "song":
            return Song(data)

        if type == "album":
            return Album(data)

        if type == "artist":
            async with self.session.get(
                f"{request_url}/view/top-songs",
                headers=self.headers,
            ) as resp:
                if resp.status != 200:
                    raise AppleMusicRequestException(
                        f"Error while fetching results: {resp.status} {resp.reason}",
                    )
                top_tracks: dict = await resp.json(loads=json.loads)
                artist_tracks: dict = top_tracks["data"]

            return Artist(data, tracks=artist_tracks)

        track_data: dict = data["relationships"]["tracks"]
        album_tracks: List[Song] = [Song(track) for track in track_data["data"]]

        if not len(album_tracks):
            raise AppleMusicRequestException(
                "This playlist is empty and therefore cannot be queued.",
            )

        _next = track_data.get("next")
        if _next:
            next_page_url = AM_BASE_URL + _next

            while next_page_url is not None:
                async with self.session.get(next_page_url, headers=self.headers) as resp:
                    if resp.status != 200:
                        raise AppleMusicRequestException(
                            f"Error while fetching results: {resp.status} {resp.reason}",
                        )

                    next_data: dict = await resp.json(loads=json.loads)

                album_tracks.extend(Song(track) for track in next_data["data"])

                _next = next_data.get("next")
                if _next:
                    next_page_url = AM_BASE_URL + _next
                else:
                    next_page_url = None

        return Playlist(data, album_tracks)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None  # type: ignore
