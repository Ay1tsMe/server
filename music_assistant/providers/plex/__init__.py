"""Plex musicprovider support for MusicAssistant."""

from __future__ import annotations

import asyncio
import logging
from asyncio import Task, TaskGroup
from collections.abc import Awaitable
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

import plexapi.exceptions
import requests
from music_assistant_models.config_entries import (
    ConfigEntry,
    ConfigValueOption,
    ConfigValueType,
    ProviderConfig,
)
from music_assistant_models.enums import (
    ConfigEntryType,
    ContentType,
    ImageType,
    MediaType,
    ProviderFeature,
    StreamType,
)
from music_assistant_models.errors import (
    InvalidDataError,
    LoginFailed,
    MediaNotFoundError,
    SetupFailedError,
)
from music_assistant_models.media_items import (
    Album,
    Artist,
    AudioFormat,
    ItemMapping,
    MediaItem,
    MediaItemImage,
    MediaItemType,
    Playlist,
    ProviderMapping,
    SearchResults,
    Track,
    UniqueList,
)
from music_assistant_models.streamdetails import StreamDetails
from plexapi.audio import Album as PlexAlbum
from plexapi.audio import Artist as PlexArtist
from plexapi.audio import Playlist as PlexPlaylist
from plexapi.audio import Track as PlexTrack
from plexapi.base import PlexObject
from plexapi.myplex import MyPlexAccount, MyPlexPinLogin
from plexapi.server import PlexServer

from music_assistant.constants import UNKNOWN_ARTIST
from music_assistant.controllers.cache import use_cache
from music_assistant.helpers.auth import AuthenticationHelper
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.helpers.util import parse_title_and_version
from music_assistant.models.music_provider import MusicProvider
from music_assistant.providers.plex.helpers import discover_local_servers, get_libraries

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Coroutine

    from music_assistant_models.provider import ProviderManifest
    from plexapi.library import LibraryMediaTag as PlexCollection
    from plexapi.library import MusicSection as PlexMusicSection
    from plexapi.media import AudioStream as PlexAudioStream
    from plexapi.media import Media as PlexMedia
    from plexapi.media import MediaPart as PlexMediaPart

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType

CONF_ACTION_AUTH_MYPLEX = "auth_myplex"
CONF_ACTION_AUTH_LOCAL = "auth_local"
CONF_ACTION_CLEAR_AUTH = "auth"
CONF_ACTION_LIBRARY = "library"
CONF_ACTION_GDM = "gdm"

CONF_AUTH_TOKEN = "token"
CONF_LIBRARY_ID = "library_id"
CONF_LOCAL_SERVER_IP = "local_server_ip"
CONF_LOCAL_SERVER_PORT = "local_server_port"
CONF_LOCAL_SERVER_SSL = "local_server_ssl"
CONF_LOCAL_SERVER_VERIFY_CERT = "local_server_verify_cert"
CONF_IMPORT_COLLECTIONS = "import_collections"
CONF_COLLECTION_PREFIX = "collection_prefix"

FAKE_ARTIST_PREFIX = "_fake://"

AUTH_TOKEN_UNAUTH = "local_auth"

SUPPORTED_FEATURES = {
    ProviderFeature.LIBRARY_ARTISTS,
    ProviderFeature.LIBRARY_ALBUMS,
    ProviderFeature.LIBRARY_TRACKS,
    ProviderFeature.LIBRARY_PLAYLISTS,
    ProviderFeature.BROWSE,
    ProviderFeature.SEARCH,
    ProviderFeature.ARTIST_ALBUMS,
    ProviderFeature.ARTIST_TOPTRACKS,
    ProviderFeature.SIMILAR_TRACKS,
}


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    if not config.get_value(CONF_AUTH_TOKEN):
        msg = "Invalid login credentials"
        raise LoginFailed(msg)

    return PlexProvider(mass, manifest, config, SUPPORTED_FEATURES)


async def get_config_entries(  # noqa: PLR0915
    mass: MusicAssistant,
    instance_id: str | None = None,  # noqa: ARG001
    action: str | None = None,
    values: dict[str, ConfigValueType] | None = None,
) -> tuple[ConfigEntry, ...]:
    """
    Return Config entries to setup this provider.

    instance_id: id of an existing provider instance (None if new instance setup).
    action: [optional] action key called from config entries UI.
    values: the (intermediate) raw values for config entries sent with the action.
    """
    # handle action GDM discovery
    if action == CONF_ACTION_GDM:
        server_details = await discover_local_servers()
        if server_details and server_details[0] and server_details[1]:
            assert values
            values[CONF_LOCAL_SERVER_IP] = server_details[0]
            values[CONF_LOCAL_SERVER_PORT] = server_details[1]
            values[CONF_LOCAL_SERVER_SSL] = False
            values[CONF_LOCAL_SERVER_VERIFY_CERT] = False
        else:
            assert values
            values[CONF_LOCAL_SERVER_IP] = "Discovery failed, please add IP manually"
            values[CONF_LOCAL_SERVER_PORT] = 32400
            values[CONF_LOCAL_SERVER_SSL] = False
            values[CONF_LOCAL_SERVER_VERIFY_CERT] = True

    # handle action clear authentication
    if action == CONF_ACTION_CLEAR_AUTH:
        assert values
        values[CONF_AUTH_TOKEN] = None
        values[CONF_LOCAL_SERVER_IP] = None
        values[CONF_LOCAL_SERVER_PORT] = 32400
        values[CONF_LOCAL_SERVER_SSL] = False
        values[CONF_LOCAL_SERVER_VERIFY_CERT] = True

    # handle action MyPlex auth
    if action == CONF_ACTION_AUTH_MYPLEX:
        assert values
        values[CONF_AUTH_TOKEN] = None
        async with AuthenticationHelper(mass, str(values["session_id"])) as auth_helper:
            plex_auth = MyPlexPinLogin(headers={"X-Plex-Product": "Music Assistant"}, oauth=True)
            auth_url = plex_auth.oauthUrl(auth_helper.callback_url)
            await auth_helper.authenticate(auth_url)
            if not plex_auth.checkLogin():
                msg = "Authentication to MyPlex failed"
                raise LoginFailed(msg)
            # set the retrieved token on the values object to pass along
            values[CONF_AUTH_TOKEN] = plex_auth.token

    # handle action Local auth (no MyPlex)
    if action == CONF_ACTION_AUTH_LOCAL:
        assert values
        values[CONF_AUTH_TOKEN] = AUTH_TOKEN_UNAUTH

    # collect all config entries to show
    entries: list[ConfigEntry] = []

    # show GDM discovery (if we do not yet have any server details)
    if values is None or not values.get(CONF_LOCAL_SERVER_IP):
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_GDM,
                type=ConfigEntryType.ACTION,
                label="Use Plex GDM to discover local servers",
                description='Enable "GDM" to discover local Plex servers automatically.',
                action=CONF_ACTION_GDM,
                action_label="Use Plex GDM to discover local servers",
            )
        )

    # server details config entries (IP, port etc.)
    entries += [
        ConfigEntry(
            key=CONF_LOCAL_SERVER_IP,
            type=ConfigEntryType.STRING,
            label="Local server IP",
            description="The local server IP (e.g. 192.168.1.77)",
            required=True,
            value=cast("str", values.get(CONF_LOCAL_SERVER_IP)) if values else None,
        ),
        ConfigEntry(
            key=CONF_LOCAL_SERVER_PORT,
            type=ConfigEntryType.INTEGER,
            label="Local server port",
            description="The local server port (e.g. 32400)",
            required=True,
            default_value=32400,
            value=cast("int", values.get(CONF_LOCAL_SERVER_PORT)) if values else None,
        ),
        ConfigEntry(
            key=CONF_LOCAL_SERVER_SSL,
            type=ConfigEntryType.BOOLEAN,
            label="SSL (HTTPS)",
            description="Connect to the local server using SSL (HTTPS)",
            required=True,
            default_value=False,
        ),
        ConfigEntry(
            key=CONF_LOCAL_SERVER_VERIFY_CERT,
            type=ConfigEntryType.BOOLEAN,
            label="Verify certificate",
            description="Verify local server SSL certificate",
            required=True,
            default_value=True,
            depends_on=CONF_LOCAL_SERVER_SSL,
            category="advanced",
        ),
        ConfigEntry(
            key=CONF_AUTH_TOKEN,
            type=ConfigEntryType.SECURE_STRING,
            label=CONF_AUTH_TOKEN,
            action=CONF_AUTH_TOKEN,
            value=cast("str | None", values.get(CONF_AUTH_TOKEN)) if values else None,
            hidden=True,
        ),
    ]

    # config flow auth action/step to pick the library to use
    # because this call is very slow, we only show/calculate the dropdown if we do
    # not yet have this info or we/user invalidated it.
    if values and values.get(CONF_AUTH_TOKEN):
        conf_libraries = ConfigEntry(
            key=CONF_LIBRARY_ID,
            type=ConfigEntryType.STRING,
            label="Library",
            required=True,
            description="The library to connect to (e.g. Music)",
            depends_on=CONF_AUTH_TOKEN,
            action=CONF_ACTION_LIBRARY,
            action_label="Select Plex Music Library",
        )
        if action in (
            CONF_ACTION_LIBRARY,
            CONF_ACTION_AUTH_MYPLEX,
            CONF_ACTION_AUTH_LOCAL,
        ):
            token = mass.config.decrypt_string(str(values.get(CONF_AUTH_TOKEN)))
            server_http_ip = str(values.get(CONF_LOCAL_SERVER_IP))
            server_http_port = str(values.get(CONF_LOCAL_SERVER_PORT))
            server_http_ssl = bool(values.get(CONF_LOCAL_SERVER_SSL))
            server_http_verify_cert = bool(values.get(CONF_LOCAL_SERVER_VERIFY_CERT))
            if not (
                libraries := await get_libraries(
                    mass,
                    token,
                    server_http_ssl,
                    server_http_ip,
                    server_http_port,
                    server_http_verify_cert,
                )
            ):
                msg = "Unable to retrieve Servers and/or Music Libraries"
                raise LoginFailed(msg)
            conf_libraries.options = [
                # use the same value for both the value and the title
                # until we find out what plex uses as stable identifiers
                ConfigValueOption(
                    title=x,
                    value=x,
                )
                for x in libraries
            ]
            # select first library as (default) value
            conf_libraries.default_value = libraries[0]
            conf_libraries.value = libraries[0]
        entries.append(conf_libraries)

    # show authentication options
    if values is None or not values.get(CONF_AUTH_TOKEN):
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_AUTH_MYPLEX,
                type=ConfigEntryType.ACTION,
                label="Authenticate with MyPlex",
                description="Authenticate with MyPlex to access your library.",
                action=CONF_ACTION_AUTH_MYPLEX,
                action_label="Authenticate with MyPlex",
            )
        )
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_AUTH_LOCAL,
                type=ConfigEntryType.ACTION,
                label="Authenticate locally",
                description="Authenticate locally to access your library.",
                action=CONF_ACTION_AUTH_LOCAL,
                action_label="Authenticate locally",
            )
        )
    else:
        entries.append(
            ConfigEntry(
                key=CONF_ACTION_CLEAR_AUTH,
                type=ConfigEntryType.ACTION,
                label="Clear authentication",
                description="Clear the current authentication details.",
                action=CONF_ACTION_CLEAR_AUTH,
                action_label="Clear authentication",
                required=False,
            )
        )

    # Collection import options (advanced settings)
    entries.append(
        ConfigEntry(
            key=CONF_IMPORT_COLLECTIONS,
            type=ConfigEntryType.BOOLEAN,
            label="Import Collections",
            description="Import collections (tracks, albums, or artists) as playlists",
            default_value=False,
            category="advanced",
        )
    )
    entries.append(
        ConfigEntry(
            key=CONF_COLLECTION_PREFIX,
            type=ConfigEntryType.STRING,
            label="Collection Prefix",
            description="Prefix to add to collection names when imported as playlists",
            default_value="Collection: ",
            depends_on=CONF_IMPORT_COLLECTIONS,
            category="advanced",
        )
    )

    # return all config entries
    return tuple(entries)


Param = ParamSpec("Param")
RetType = TypeVar("RetType")
PlexObjectT = TypeVar("PlexObjectT", bound=PlexObject)
MediaItemT = TypeVar("MediaItemT", bound=MediaItem)


class PlexProvider(MusicProvider):
    """Provider for a plex music library."""

    _plex_server: PlexServer = None
    _plex_library: PlexMusicSection = None
    _myplex_account: MyPlexAccount = None
    _baseurl: str

    async def handle_async_init(self) -> None:
        """Set up the music provider by connecting to the server."""
        # silence loggers
        logging.getLogger("plexapi").setLevel(self.logger.level + 10)
        _, library_name = str(self.config.get_value(CONF_LIBRARY_ID)).split(" / ", 1)

        def connect() -> PlexServer:
            try:
                session = requests.Session()
                session.verify = (
                    self.config.get_value(CONF_LOCAL_SERVER_VERIFY_CERT)
                    if self.config.get_value(CONF_LOCAL_SERVER_SSL)
                    else False
                )
                # Add Music Assistant client identification headers
                session.headers.update(
                    {
                        "X-Plex-Client-Identifier": self.instance_id,
                        "X-Plex-Product": "Music Assistant",
                        "X-Plex-Platform": "Music Assistant",
                        "X-Plex-Version": self.mass.version,
                    }
                )
                local_server_protocol = (
                    "https" if self.config.get_value(CONF_LOCAL_SERVER_SSL) else "http"
                )
                token = self.config.get_value(CONF_AUTH_TOKEN)
                plex_url = (
                    f"{local_server_protocol}://{self.config.get_value(CONF_LOCAL_SERVER_IP)}"
                    f":{self.config.get_value(CONF_LOCAL_SERVER_PORT)}"
                )
                if token == AUTH_TOKEN_UNAUTH:
                    # Doing local connection, not via plex.tv.
                    plex_server = PlexServer(plex_url, session=session)
                else:
                    plex_server = PlexServer(
                        plex_url,
                        token,
                        session=session,
                    )
                # I don't think PlexAPI intends for this to be accessible, but we need it.
                self._baseurl = plex_server._baseurl

            except plexapi.exceptions.BadRequest as err:
                if "Invalid token" in str(err):
                    # token invalid, invalidate the config
                    self.mass.create_task(
                        self.mass.config.remove_provider_config_value(
                            self.instance_id, CONF_AUTH_TOKEN
                        ),
                    )
                    msg = "Authentication failed"
                    raise LoginFailed(msg)
                raise LoginFailed from err
            return plex_server

        self._myplex_account = await self.get_myplex_account_and_refresh_token(
            str(self.config.get_value(CONF_AUTH_TOKEN))
        )
        try:
            self._plex_server = await self._run_async(connect)
            self._plex_library = await self._run_async(
                self._plex_server.library.section, library_name
            )
        except requests.exceptions.ConnectionError as err:
            raise SetupFailedError from err

    @property
    def is_streaming_provider(self) -> bool:
        """
        Return True if the provider is a streaming provider.

        This literally means that the catalog is not the same as the library contents.
        For local based providers (files, plex), the catalog is the same as the library content.
        It also means that data is if this provider is NOT a streaming provider,
        data cross instances is unique, the catalog and library differs per instance.

        Setting this to True will only query one instance of the provider for search and lookups.
        Setting this to False will query all instances of this provider for search and lookups.
        """
        return False

    async def resolve_image(self, path: str) -> str | bytes:
        """Return the full image URL including the auth token."""
        return str(self._plex_server.url(path, True))

    async def _run_async(
        self, call: Callable[Param, RetType], *args: Param.args, **kwargs: Param.kwargs
    ) -> RetType:
        await self.get_myplex_account_and_refresh_token(str(self.config.get_value(CONF_AUTH_TOKEN)))
        return await asyncio.to_thread(call, *args, **kwargs)

    async def _get_data(self, key: str, cls: type[PlexObjectT]) -> PlexObjectT:
        results = await self._run_async(self._plex_library.fetchItem, key, cls)
        return cast("PlexObjectT", results)

    def _get_item_mapping(self, media_type: MediaType, key: str, name: str) -> ItemMapping:
        """Get item mapping for a given media type, key, and name."""
        if not name:
            self.logger.info(
                "Received None or empty name for media item. Media type: %s, Key: %s",
                media_type,
                key,
            )
            name = "[Unknown]"

        mapped_name, mapped_version = parse_title_and_version(name)

        if not mapped_name:
            self.logger.info(
                "Failed to map name for media item. Media type: %s, Key: %s, Original name: %s",
                media_type,
                key,
                name,
            )
            mapped_name = "[Unknown]"
        if not mapped_version and media_type not in (MediaType.ALBUM, MediaType.TRACK):
            mapped_version = ""

        return ItemMapping(
            media_type=media_type,
            item_id=key,
            provider=self.lookup_key,
            name=mapped_name,
            version=mapped_version,
        )

    async def _get_or_create_artist_by_name(self, artist_name: str) -> Artist | ItemMapping:
        if library_items := await self.mass.music.artists._get_library_items_by_query(
            search=artist_name, provider=self.lookup_key
        ):
            return ItemMapping.from_item(library_items[0])

        artist_id = FAKE_ARTIST_PREFIX + artist_name
        return Artist(
            item_id=artist_id,
            name=artist_name or UNKNOWN_ARTIST,
            provider=self.lookup_key,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )

    async def _parse(self, plex_media: PlexObject) -> MediaItem | None:
        if plex_media.type == "artist":
            return await self._parse_artist(plex_media)
        elif plex_media.type == "album":
            return await self._parse_album(plex_media)
        elif plex_media.type == "track":
            return await self._parse_track(plex_media)
        elif plex_media.type == "playlist":
            return await self._parse_playlist(plex_media)
        return None

    async def _search_track(self, search_query: str | None, limit: int) -> list[PlexTrack]:
        return cast(
            "list[PlexTrack]",
            await self._run_async(self._plex_library.searchTracks, title=search_query, limit=limit),
        )

    async def _search_album(self, search_query: str, limit: int) -> list[PlexAlbum]:
        return cast(
            "list[PlexAlbum]",
            await self._run_async(self._plex_library.searchAlbums, title=search_query, limit=limit),
        )

    async def _search_artist(self, search_query: str, limit: int) -> list[PlexArtist]:
        return cast(
            "list[PlexArtist]",
            await self._run_async(
                self._plex_library.searchArtists, title=search_query, limit=limit
            ),
        )

    async def _search_playlist(self, search_query: str, limit: int) -> list[PlexPlaylist]:
        return cast(
            "list[PlexPlaylist]",
            await self._run_async(self._plex_library.playlists, title=search_query, limit=limit),
        )

    async def _search_track_advanced(self, limit: int, **kwargs: Any) -> list[PlexTrack]:
        return cast(
            "list[PlexPlaylist]",
            await self._run_async(self._plex_library.searchTracks, filters=kwargs, limit=limit),
        )

    async def _search_album_advanced(self, limit: int, **kwargs: Any) -> list[PlexAlbum]:
        return cast(
            "list[PlexPlaylist]",
            await self._run_async(self._plex_library.searchAlbums, filters=kwargs, limit=limit),
        )

    async def _search_artist_advanced(self, limit: int, **kwargs: Any) -> list[PlexArtist]:
        return cast(
            "list[PlexPlaylist]",
            await self._run_async(self._plex_library.searchArtists, filters=kwargs, limit=limit),
        )

    async def _search_playlist_advanced(self, limit: int, **kwargs: Any) -> list[PlexPlaylist]:
        return cast(
            "list[PlexPlaylist]",
            await self._run_async(self._plex_library.playlists, filters=kwargs, limit=limit),
        )

    async def _search_and_parse(
        self,
        search_coro: Awaitable[list[PlexObjectT]],
        parse_coro: Callable[[PlexObjectT], Coroutine[Any, Any, MediaItemT]],
    ) -> list[MediaItemT]:
        task_results: list[Task[MediaItemT]] = []
        async with TaskGroup() as tg:
            for item in await search_coro:
                task_results.append(tg.create_task(parse_coro(item)))

        results: list[MediaItemT] = []
        for task in task_results:
            results.append(task.result())

        return results

    async def _parse_album(self, plex_album: PlexAlbum) -> Album:
        """Parse a Plex Album response to an Album model object."""
        album_id = plex_album.key
        album = Album(
            item_id=album_id,
            provider=self.lookup_key,
            name=plex_album.title or "[Unknown]",
            provider_mappings={
                ProviderMapping(
                    item_id=str(album_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_album.getWebURL(self._baseurl),
                )
            },
        )
        # Only add 5-star rated albums to Favorites. rating will be 10.0 for those.
        # TODO: Let user set threshold?
        with suppress(KeyError):
            # suppress KeyError (as it doesn't exist for items without rating),
            # allow sync to continue
            album.favorite = plex_album._data.attrib["userRating"] == "10.0"

        if plex_album.year:
            album.year = plex_album.year
        if thumb := plex_album.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            album.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.lookup_key,
                        remotely_accessible=False,
                    )
                ]
            )
        if plex_album.summary:
            album.metadata.description = plex_album.summary

        album.artists.append(
            self._get_item_mapping(
                MediaType.ARTIST,
                plex_album.parentKey,
                plex_album.parentTitle or UNKNOWN_ARTIST,
            )
        )
        return album

    async def _parse_artist(self, plex_artist: PlexArtist) -> Artist:
        """Parse a Plex Artist response to Artist model object."""
        artist_id = plex_artist.key
        if not artist_id:
            msg = "Artist does not have a valid ID"
            raise InvalidDataError(msg)
        artist = Artist(
            item_id=artist_id,
            name=plex_artist.title or UNKNOWN_ARTIST,
            provider=self.lookup_key,
            provider_mappings={
                ProviderMapping(
                    item_id=str(artist_id),
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_artist.getWebURL(self._baseurl),
                )
            },
        )
        if plex_artist.summary:
            artist.metadata.description = plex_artist.summary
        if thumb := plex_artist.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            artist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.lookup_key,
                        remotely_accessible=False,
                    )
                ]
            )
        return artist

    async def _parse_playlist(self, plex_playlist: PlexPlaylist) -> Playlist:
        """Parse a Plex Playlist response to a Playlist object."""
        playlist = Playlist(
            item_id=plex_playlist.key,
            provider=self.lookup_key,
            name=plex_playlist.title or "[Unknown]",
            provider_mappings={
                ProviderMapping(
                    item_id=plex_playlist.key,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    url=plex_playlist.getWebURL(self._baseurl),
                )
            },
        )
        if plex_playlist.summary:
            playlist.metadata.description = plex_playlist.summary
        if thumb := plex_playlist.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.lookup_key,
                        remotely_accessible=False,
                    )
                ]
            )
        playlist.is_editable = not plex_playlist.smart
        return playlist

    async def _parse_collection(self, plex_collection: PlexCollection) -> Playlist:
        """Parse a Plex Collection response to a Playlist object."""
        # Get the configured collection prefix
        collection_prefix = str(self.config.get_value(CONF_COLLECTION_PREFIX) or "")

        # Collections are imported as playlists with the configured prefix
        playlist = Playlist(
            item_id=f"collection:{plex_collection.key}",
            provider=self.lookup_key,
            name=f"{collection_prefix}{plex_collection.title}",
            provider_mappings={
                ProviderMapping(
                    item_id=f"collection:{plex_collection.key}",
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                )
            },
        )
        # Add collection poster/thumbnail if available
        if thumb := plex_collection.firstAttr("thumb", "composite"):
            playlist.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.lookup_key,
                        remotely_accessible=False,
                    )
                ]
            )
        # Collections are not editable in Music Assistant
        playlist.is_editable = False
        return playlist

    async def _parse_track(self, plex_track: PlexTrack) -> Track:
        """Parse a Plex Track response to a Track model object."""
        if plex_track.media:
            available = True
            content = plex_track.media[0].container
        else:
            available = False
            content = None
        track = Track(
            item_id=plex_track.key,
            provider=self.lookup_key,
            name=plex_track.title or "[Unknown]",
            provider_mappings={
                ProviderMapping(
                    item_id=plex_track.key,
                    provider_domain=self.domain,
                    provider_instance=self.instance_id,
                    available=available,
                    audio_format=AudioFormat(
                        content_type=(
                            ContentType.try_parse(content) if content else ContentType.UNKNOWN
                        ),
                    ),
                    url=plex_track.getWebURL(self._baseurl),
                )
            },
            disc_number=plex_track.parentIndex or 0,
            track_number=plex_track.trackNumber or 0,
        )
        # Only add 5-star rated tracks to Favorites. userRating will be 10.0 for those.
        # TODO: Let user set threshold?
        with suppress(KeyError):
            # suppress KeyError (as it doesn't exist for items without rating),
            # allow sync to continue
            track.favorite = plex_track._data.attrib["userRating"] == "10.0"

        if plex_track.originalTitle and plex_track.originalTitle != plex_track.grandparentTitle:
            # The artist of the track if different from the album's artist.
            # For this kind of artist, we just know the name, so we create a fake artist,
            # if it does not already exist.
            track.artists.append(
                await self._get_or_create_artist_by_name(plex_track.originalTitle or UNKNOWN_ARTIST)
            )
        elif plex_track.grandparentKey:
            track.artists.append(
                self._get_item_mapping(
                    MediaType.ARTIST,
                    plex_track.grandparentKey,
                    plex_track.grandparentTitle or UNKNOWN_ARTIST,
                )
            )
        else:
            msg = "No artist was found for track"
            raise InvalidDataError(msg)

        if thumb := plex_track.firstAttr("thumb", "parentThumb", "grandparentThumb"):
            track.metadata.images = UniqueList(
                [
                    MediaItemImage(
                        type=ImageType.THUMB,
                        path=thumb,
                        provider=self.lookup_key,
                        remotely_accessible=False,
                    )
                ]
            )
        if plex_track.parentKey:
            track.album = self._get_item_mapping(
                MediaType.ALBUM, plex_track.parentKey, plex_track.parentTitle
            )
        if plex_track.duration:
            track.duration = int(plex_track.duration / 1000)
        if plex_track.chapters:
            pass  # TODO!

        return track

    @use_cache(3600)  # Cache for 1 hour
    async def search(
        self,
        search_query: str,
        media_types: list[MediaType],
        limit: int = 20,
    ) -> SearchResults:
        """Perform search on the plex library.

        :param search_query: Search query.
        :param media_types: A list of media_types to include.
        :param limit: Number of items to return in the search (per type).
        """
        artists = None
        albums = None
        tracks = None
        playlists = None

        async with TaskGroup() as tg:
            if MediaType.ARTIST in media_types:
                artists = tg.create_task(
                    self._search_and_parse(
                        self._search_artist(search_query, limit), self._parse_artist
                    )
                )

            if MediaType.ALBUM in media_types:
                albums = tg.create_task(
                    self._search_and_parse(
                        self._search_album(search_query, limit), self._parse_album
                    )
                )

            if MediaType.TRACK in media_types:
                tracks = tg.create_task(
                    self._search_and_parse(
                        self._search_track(search_query, limit), self._parse_track
                    )
                )

            if MediaType.PLAYLIST in media_types:
                playlists = tg.create_task(
                    self._search_and_parse(
                        self._search_playlist(search_query, limit),
                        self._parse_playlist,
                    )
                )

        search_results = SearchResults()

        if artists:
            search_results.artists = artists.result()

        if albums:
            search_results.albums = albums.result()

        if tracks:
            search_results.tracks = tracks.result()

        if playlists:
            search_results.playlists = playlists.result()

        return search_results

    async def get_library_artists(self) -> AsyncGenerator[Artist, None]:
        """Retrieve all library artists from Plex Music."""
        artists_obj = await self._run_async(self._plex_library.all)
        for artist in artists_obj:
            yield await self._parse_artist(artist)

    async def get_library_albums(self) -> AsyncGenerator[Album, None]:
        """Retrieve all library albums from Plex Music."""
        albums_obj = await self._run_async(self._plex_library.albums)
        for album in albums_obj:
            yield await self._parse_album(album)

    async def get_library_playlists(self) -> AsyncGenerator[Playlist, None]:
        """Retrieve all library playlists from the provider."""
        playlists_obj = await self._run_async(self._plex_library.playlists)
        for playlist in playlists_obj:
            yield await self._parse_playlist(playlist)

        # Import collections as playlists if enabled
        if self.config.get_value(CONF_IMPORT_COLLECTIONS):
            collections_obj = await self._run_async(self._plex_library.collections)
            for collection in collections_obj:
                yield await self._parse_collection(collection)

    async def get_library_tracks(self) -> AsyncGenerator[Track, None]:
        """Retrieve library tracks from Plex Music."""
        page_size = 500
        offset = 0
        while True:
            batch = cast(
                "list[PlexTrack]",
                await self._run_async(
                    self._plex_library.searchTracks,
                    title=None,
                    limit=page_size,
                    offset=offset,
                ),
            )
            if not batch:
                break
            for plex_track in batch:
                yield await self._parse_track(plex_track)
            offset += page_size

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_album(self, prov_album_id: str) -> Album:
        """Get full album details by id."""
        if plex_album := await self._get_data(prov_album_id, PlexAlbum):
            return await self._parse_album(plex_album)
        msg = f"Item {prov_album_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_album_tracks(self, prov_album_id: str) -> list[Track]:
        """Get album tracks for given album id."""
        plex_album: PlexAlbum = await self._get_data(prov_album_id, PlexAlbum)
        tracks = []
        for plex_track in await self._run_async(plex_album.tracks):
            track = await self._parse_track(
                plex_track,
            )
            tracks.append(track)
        return tracks

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_artist(self, prov_artist_id: str) -> Artist:
        """Get full artist details by id."""
        if prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            # This artist does not exist in plex, so we can just load it from DB.

            if db_artist := await self.mass.music.artists.get_library_item_by_prov_id(
                prov_artist_id, self.instance_id
            ):
                return db_artist
            msg = f"Artist not found: {prov_artist_id}"
            raise MediaNotFoundError(msg)

        if plex_artist := await self._get_data(prov_artist_id, PlexArtist):
            return await self._parse_artist(plex_artist)
        msg = f"Item {prov_artist_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_track(self, prov_track_id: str) -> Track:
        """Get full track details by id."""
        if plex_track := await self._get_data(prov_track_id, PlexTrack):
            return await self._parse_track(plex_track)
        msg = f"Item {prov_track_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_playlist(self, prov_playlist_id: str) -> Playlist:
        """Get full playlist details by id."""
        # Check if this is a collection (collections have the format "collection:<key>")
        if prov_playlist_id.startswith("collection:"):
            # Extract the collection key
            collection_key = prov_playlist_id.replace("collection:", "")
            # Fetch the collection
            if plex_collection := await self._run_async(
                self._plex_library.fetchItem, collection_key
            ):
                return await self._parse_collection(plex_collection)
            msg = f"Collection {prov_playlist_id} not found"
            raise MediaNotFoundError(msg)

        if plex_playlist := await self._get_data(prov_playlist_id, PlexPlaylist):
            return await self._parse_playlist(plex_playlist)
        msg = f"Item {prov_playlist_id} not found"
        raise MediaNotFoundError(msg)

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_playlist_tracks(self, prov_playlist_id: str, page: int = 0) -> list[Track]:
        """Get playlist tracks."""
        result: list[Track] = []
        if page > 0:
            # paging not supported, we always return the whole list at once
            return []

        # Check if this is a collection (collections have the format "collection:<key>")
        if prov_playlist_id.startswith("collection:"):
            # Extract the collection key
            collection_key = prov_playlist_id.replace("collection:", "")
            # Fetch the collection
            plex_collection = await self._run_async(self._plex_library.fetchItem, collection_key)
            if not plex_collection:
                msg = f"Collection {prov_playlist_id} not found"
                raise MediaNotFoundError(msg)
            if not (collection_items := await self._run_async(plex_collection.items)):
                return result
            # Collections can contain tracks, albums, or artists - we only want tracks
            for item in collection_items:
                if item.type == "track":
                    if track := await self._parse_track(item):
                        track.position = len(result) + 1
                        result.append(track)
                elif item.type == "album":
                    # If the collection contains albums, get all tracks from each album
                    album_tracks = await self.get_album_tracks(item.key)
                    for album_track in album_tracks:
                        album_track.position = len(result) + 1
                        result.append(album_track)
            return result

        plex_playlist: PlexPlaylist = await self._get_data(prov_playlist_id, PlexPlaylist)
        if not (playlist_items := await self._run_async(plex_playlist.items)):
            return result
        for index, plex_track in enumerate(playlist_items, 1):
            if track := await self._parse_track(plex_track):
                track.position = index
                result.append(track)
        return result

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_artist_albums(self, prov_artist_id: str) -> list[Album]:
        """Get a list of albums for the given artist."""
        if not prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            plex_artist = await self._get_data(prov_artist_id, PlexArtist)
            plex_albums = cast("list[PlexAlbum]", await self._run_async(plex_artist.albums))
            if plex_albums:
                albums = []
                for album_obj in plex_albums:
                    albums.append(await self._parse_album(album_obj))
                return albums
        return []

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_artist_toptracks(self, prov_artist_id: str) -> list[Track]:
        """Get top tracks for the given artist using Plex artist radio/station."""
        if prov_artist_id.startswith(FAKE_ARTIST_PREFIX):
            return []

        try:
            plex_artist = await self._get_data(prov_artist_id, PlexArtist)
            # Get the artist radio station which contains top/popular tracks
            if station := await self._run_async(plex_artist.station):
                # Get tracks from the station
                station_tracks = await self._run_async(station.items)
                tracks = []
                for plex_track in station_tracks[:25]:  # Limit to 25 top tracks
                    if track := await self._parse_track(plex_track):
                        tracks.append(track)
                self.logger.debug(
                    "Retrieved %d top tracks for artist %s", len(tracks), prov_artist_id
                )
                return tracks
            self.logger.warning("No station available for artist %s", prov_artist_id)
        except Exception as err:
            self.logger.warning("Error getting top tracks for artist %s: %s", prov_artist_id, err)
        return []

    @use_cache(3600 * 3)  # Cache for 3 hours
    async def get_similar_tracks(self, prov_track_id: str, limit: int = 25) -> list[Track]:
        """Get similar tracks using Plex's sonicallySimilar feature."""
        try:
            plex_track = await self._get_data(prov_track_id, PlexTrack)
            # Get sonically similar tracks
            similar_tracks = await self._run_async(plex_track.sonicallySimilar, limit=limit)
            tracks = []
            for similar_track in similar_tracks:
                if track := await self._parse_track(similar_track):
                    tracks.append(track)
            self.logger.debug(
                "Retrieved %d similar tracks for track %s", len(tracks), prov_track_id
            )
            return tracks
        except Exception as err:
            self.logger.warning("Error getting similar tracks for %s: %s", prov_track_id, err)
        return []

    async def get_stream_details(self, item_id: str, media_type: MediaType) -> StreamDetails:
        """Get streamdetails for a track."""
        plex_track = await self._get_data(item_id, PlexTrack)
        if not plex_track or not plex_track.media:
            msg = f"track {item_id} not found"
            raise MediaNotFoundError(msg)

        media: PlexMedia = plex_track.media[0]

        content_type = (
            ContentType.try_parse(media.container) if media.container else ContentType.UNKNOWN
        )
        media_part: PlexMediaPart = media.parts[0]
        audio_stream: PlexAudioStream = media_part.audioStreams()[0]

        stream_details = StreamDetails(
            item_id=plex_track.key,
            provider=self.lookup_key,
            audio_format=AudioFormat(
                content_type=content_type,
                channels=media.audioChannels,
            ),
            stream_type=StreamType.HTTP,
            duration=plex_track.duration,
            data=plex_track,
            can_seek=True,
            allow_seek=True,
        )

        if content_type != ContentType.M4A:
            stream_details.path = self._plex_server.url(media_part.key, True)
            if audio_stream.samplingRate:
                stream_details.audio_format.sample_rate = audio_stream.samplingRate
            if audio_stream.bitDepth:
                stream_details.audio_format.bit_depth = audio_stream.bitDepth

        else:
            url = plex_track.getStreamURL()
            media_info = await async_parse_tags(url)
            stream_details.path = url
            stream_details.audio_format.channels = media_info.channels
            stream_details.audio_format.content_type = ContentType.try_parse(media_info.format)
            stream_details.audio_format.sample_rate = media_info.sample_rate
            stream_details.audio_format.bit_depth = media_info.bits_per_sample

        return stream_details

    async def on_streamed(
        self,
        streamdetails: StreamDetails,
    ) -> None:
        """Handle callback when an item completed streaming."""

        def mark_played() -> None:
            """Mark the item as played in Plex."""
            try:
                item = streamdetails.data
                if not item:
                    self.logger.warning("No Plex item data in streamdetails, cannot scrobble")
                    return

                if not hasattr(item, "ratingKey"):
                    self.logger.warning(
                        "Streamdetails data is not a Plex item (missing ratingKey), cannot scrobble"
                    )
                    return

                params = {
                    "key": str(item.ratingKey),
                    "identifier": "com.plexapp.plugins.library",
                }
                self.logger.debug(
                    "Scrobbling track %s (ratingKey: %s) to Plex",
                    streamdetails.uri,
                    item.ratingKey,
                )
                self._plex_server.query("/:/scrobble", params=params)
                self.logger.info("Successfully scrobbled track %s to Plex", streamdetails.uri)
            except Exception as err:
                self.logger.exception(
                    "Failed to scrobble track %s to Plex: %s",
                    streamdetails.uri,
                    err,
                )

        await asyncio.to_thread(mark_played)

    async def on_played(
        self,
        media_type: MediaType,
        prov_item_id: str,
        fully_played: bool,
        position: int,
        media_item: MediaItemType,
        is_playing: bool = False,
    ) -> None:
        """
        Handle callback when a media item has been played.

        This is called periodically (every 30s) during playback and when playback stops.
        We use this to send timeline/progress updates to Plex.
        """
        if media_type != MediaType.TRACK:
            # Only handle tracks for now
            return

        def update_timeline() -> None:
            """Update Plex timeline with current playback progress."""
            try:
                self.logger.debug(
                    "on_played: prov_item_id=%s, pos=%s, fully_played=%s, is_playing=%s",
                    prov_item_id,
                    position,
                    fully_played,
                    is_playing,
                )

                # Extract ratingKey from the key path (e.g., "/library/metadata/12345" -> "12345")
                # The prov_item_id is the Plex key path, we need the ratingKey for API calls
                try:
                    rating_key = prov_item_id.split("/")[-1]
                    self.logger.debug(
                        "Extracted ratingKey %s from path %s", rating_key, prov_item_id
                    )
                except Exception as e:
                    self.logger.error("Failed to extract ratingKey from %s: %s", prov_item_id, e)
                    return

                # Fetch the track directly from server using ratingKey to avoid ambiguity
                # Using server.fetchItem() instead of library.fetchItem() is more reliable
                plex_track = self._plex_server.fetchItem(int(rating_key))
                if not plex_track:
                    self.logger.warning("Cannot find Plex item with ratingKey %s", rating_key)
                    return

                self.logger.debug(
                    "Found Plex item: '%s' by '%s' (type: %s, ratingKey: %s)",
                    plex_track.title if hasattr(plex_track, "title") else "unknown",
                    plex_track.grandparentTitle
                    if hasattr(plex_track, "grandparentTitle")
                    else "unknown",
                    plex_track.type if hasattr(plex_track, "type") else "unknown",
                    plex_track.ratingKey if hasattr(plex_track, "ratingKey") else "unknown",
                )

                # Verify this is actually a track, not a collection or other item
                if not hasattr(plex_track, "type") or plex_track.type != "track":
                    self.logger.warning(
                        "Item %s is not a track (type: %s), cannot update timeline",
                        rating_key,
                        plex_track.type if hasattr(plex_track, "type") else "unknown",
                    )
                    return

                # Convert position to milliseconds (Plex expects ms)
                position_ms = position * 1000

                # Determine playback state
                if fully_played:
                    state = "stopped"
                elif is_playing:
                    state = "playing"
                else:
                    state = "paused"

                # Send timeline update to Plex with current state
                # Client identification is set globally on the session headers
                params = {
                    "ratingKey": str(plex_track.ratingKey),
                    "key": prov_item_id,
                    "state": state,
                    "time": str(position_ms),
                    "duration": str(plex_track.duration)
                    if hasattr(plex_track, "duration")
                    else "0",
                }
                self.logger.debug("Sending Plex timeline update (state=%s): %s", state, params)
                self._plex_server.query("/:/timeline", params=params)

                # If fully played, also scrobble
                if fully_played:
                    scrobble_params = {
                        "key": str(plex_track.ratingKey),
                        "identifier": "com.plexapp.plugins.library",
                    }
                    self.logger.debug("Scrobbling track to Plex: %s", scrobble_params)
                    self._plex_server.query("/:/scrobble", params=scrobble_params)
                    self.logger.info("Track %s marked as played in Plex", prov_item_id)

                # If position is 0 and not playing, mark as unplayed
                if position == 0 and not is_playing and not fully_played:
                    unscrobble_params = {
                        "key": str(plex_track.ratingKey),
                        "identifier": "com.plexapp.plugins.library",
                    }
                    self.logger.debug("Unscrobbling track in Plex: %s", unscrobble_params)
                    self._plex_server.query("/:/unscrobble", params=unscrobble_params)
                    self.logger.info("Track %s marked as unplayed in Plex", prov_item_id)

            except Exception as err:
                self.logger.exception(
                    "Failed to update Plex timeline for track %s: %s",
                    prov_item_id,
                    err,
                )

        await asyncio.to_thread(update_timeline)

    async def get_myplex_account_and_refresh_token(self, auth_token: str) -> MyPlexAccount:
        """Get a MyPlexAccount object and refresh the token if needed."""
        if auth_token == AUTH_TOKEN_UNAUTH:
            return self._myplex_account

        def _refresh_plex_token() -> MyPlexAccount:
            if self._myplex_account is None:
                myplex_account = MyPlexAccount(token=auth_token)
                self._myplex_account = myplex_account
            self._myplex_account.ping()
            return self._myplex_account

        return await asyncio.to_thread(_refresh_plex_token)
