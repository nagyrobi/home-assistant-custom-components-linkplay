"""
Support for Linkplay based devices.

For more details about this platform, please refer to the documentation at
https://github.com/nagyrobi/home-assistant-custom-components-linkplay
"""

import asyncio
from asyncio import CancelledError
import async_timeout
import voluptuous as vol

from datetime import timedelta
import logging
import socket
from json import loads, dumps
import binascii
import urllib.request
import string
import aiohttp

from http import HTTPStatus
from aiohttp.client_exceptions import ClientError
from aiohttp.hdrs import CONNECTION, KEEP_ALIVE

from async_upnp_client.client_factory import UpnpFactory
from async_upnp_client.aiohttp import AiohttpRequester
import xml.etree.ElementTree as ET

import re
import struct
import chardet

from homeassistant.util import Throttle
from homeassistant.util.dt import utcnow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from homeassistant.components.media_player import (
    PLATFORM_SCHEMA, 
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerDeviceClass,
    BrowseMedia,
)

from homeassistant.components import media_source
from homeassistant.components.media_player.browse_media import (
    async_process_play_media_url,
)

from homeassistant.components.media_player.const import (
    ATTR_GROUP_MEMBERS,
    MEDIA_TYPE_MUSIC,
    MEDIA_TYPE_URL,
    MEDIA_TYPE_TRACK,
    MEDIA_CLASS_DIRECTORY,
    MEDIA_CLASS_MUSIC,
    REPEAT_MODE_ALL,
    REPEAT_MODE_OFF,
    REPEAT_MODE_ONE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_DEVICE_CLASS,
    CONF_HOST,
    CONF_NAME,
    CONF_PORT,
    CONF_PROTOCOL,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_UNKNOWN,
    STATE_UNAVAILABLE,
)

from . import DOMAIN, ATTR_MASTER

_LOGGER = logging.getLogger(__name__)

ICON_DEFAULT = 'mdi:speaker'
ICON_PLAYING = 'mdi:speaker-wireless'
ICON_MUTED = 'mdi:speaker-off'
ICON_MULTIROOM = 'mdi:speaker-multiple'
ICON_BLUETOOTH = 'mdi:speaker-bluetooth'
ICON_PUSHSTREAM = 'mdi:cast-audio'
ICON_TTS = 'mdi:speaker-message'

ATTR_SLAVE = 'slave'
ATTR_LINKPLAY_GROUP = 'linkplay_group'
ATTR_FWVER = 'firmware'
ATTR_TRCNT = 'tracks_local'
ATTR_TRCRT = 'track_current'
ATTR_STURI = 'stream_uri'
ATTR_UUID = 'uuid'
ATTR_TTS = 'tts_active'
ATTR_SNAPSHOT = 'snapshot_active'
ATTR_SNAPSPOT = 'snapshot_spotify'
ATTR_DEBUG = 'debug_info'

CONF_NAME = 'name'
CONF_LASTFM_API_KEY = 'lastfm_api_key'
CONF_SOURCES = 'sources'
CONF_COMMONSOURCES = 'common_sources'
CONF_ICECAST_METADATA = 'icecast_metadata'
CONF_MULTIROOM_WIFIDIRECT = 'multiroom_wifidirect'
CONF_VOLUME_STEP = 'volume_step'
CONF_LEDOFF = 'led_off'
CONF_UUID = 'uuid'

DEFAULT_ICECAST_UPDATE = 'StationName'
DEFAULT_MULTIROOM_WIFIDIRECT = False
DEFAULT_LEDOFF = False
DEFAULT_VOLUME_STEP = 5

DEBUGSTR_ATTR = True
LASTFM_API_BASE = 'http://ws.audioscrobbler.com/2.0/?method='
MAX_VOL = 100
FW_MROOM_RTR_MIN = '4.2.8020'
FW_RAKOIT_UART_MIN = '4.2.9326'
FW_SLOW_STREAMS = '4.6'
ROOTDIR_USB = '/media/sda1/'
UUID_ARYLIC = 'FF31F09E'
TCPPORT = 8899
UPNP_TIMEOUT = 2
API_TIMEOUT = 2
SCAN_INTERVAL = timedelta(seconds=3)
ICE_THROTTLE = timedelta(seconds=45)
LFM_THROTTLE = timedelta(seconds=4)
UNA_THROTTLE = timedelta(seconds=20)
MROOM_UJWDIR = timedelta(seconds=20)
MROOM_UJWROU = timedelta(seconds=3)
SPOTIFY_PAUSED_TIMEOUT = timedelta(seconds=300)
AUTOIDLE_STATE_TIMEOUT = timedelta(seconds=1)
#PARALLEL_UPDATES = 0

CUT_EXTENSIONS = ['mp3', 'mp2', 'm2a', 'mpg', 'wav', 'aac', 'flac', 'flc', 'm4a', 'ape', 'wma', 'ac3', 'ogg']

SOUND_MODES = {'0': 'Normal', '1': 'Classic', '2': 'Pop', '3': 'Jazz', '4': 'Vocal'}

SOURCES = {'bluetooth': 'Bluetooth', 
           'line-in': 'Line-in', 
           'line-in2': 'Line-in 2', 
           'optical': 'Optical', 
           'co-axial': 'Coaxial', 
           'HDMI': 'HDMI', 
           'udisk': 'USB disk', 
           'TFcard': 'SD card', 
           'RCA': 'RCA', 
           'XLR': 'XLR', 
           'FM': 'FM', 
           'cd': 'CD',
           'PCUSB': 'USB DAC'}

SOURCES_MAP = {'-1': 'Idle', 
               '0': 'Idle', 
               '1': 'Airplay', 
               '2': 'DLNA',
               '3': 'QPlay',
               '10': 'Network', 
               '11': 'udisk', 
               '16': 'TFcard',
               '20': 'API', 
               '21': 'udisk', 
               '30': 'Alarm', 
               '31': 'Spotify', 
               '40': 'line-in', 
               '41': 'bluetooth', 
               '43': 'optical',
               '44': 'RCA',
               '45': 'co-axial',
               '46': 'FM',
               '47': 'line-in2', 
               '48': 'XLR',
               '49': 'HDMI',
               '50': 'cd',
               '51': 'USB DAC',
               '52': 'TFcard',
               '60': 'Talk',
               '99': 'Idle'}

SOURCES_LIVEIN = ['-1', '0', '40', '41', '43', '44', '45', '46', '47', '48', '49', '50', '51', '99']
SOURCES_STREAM = ['1', '2', '3', '10', '30']
SOURCES_LOCALF = ['11', '16', '20', '21', '52', '60']

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_PROTOCOL): vol.In(['http', 'https']),
        vol.Optional(CONF_ICECAST_METADATA, default=DEFAULT_ICECAST_UPDATE): vol.In(['Off', 'StationName', 'StationNameSongTitle']),
        vol.Optional(CONF_MULTIROOM_WIFIDIRECT, default=DEFAULT_MULTIROOM_WIFIDIRECT): cv.boolean,
        vol.Optional(CONF_LEDOFF, default=DEFAULT_LEDOFF): cv.boolean,
        vol.Optional(CONF_SOURCES): cv.ensure_list,
        vol.Optional(CONF_COMMONSOURCES): cv.ensure_list,
        vol.Optional(CONF_LASTFM_API_KEY): cv.string,
        vol.Optional(CONF_UUID, default=''): cv.string,
        vol.Optional(CONF_VOLUME_STEP, default=DEFAULT_VOLUME_STEP): vol.All(int, vol.Range(min=1, max=25)),
    }
)

class LinkPlayData:
    """Storage class for platform global data."""
    def __init__(self):
        """Initialize the data."""
        self.entities = []

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the LinkPlayDevice platform."""

    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = LinkPlayData()

    name = config.get(CONF_NAME)
    host = config.get(CONF_HOST)
    protocol = config.get(CONF_PROTOCOL)
    sources = config.get(CONF_SOURCES)
    common_sources = config.get(CONF_COMMONSOURCES)
    icecast_metadata = config.get(CONF_ICECAST_METADATA)
    multiroom_wifidirect = config.get(CONF_MULTIROOM_WIFIDIRECT)
    led_off = config.get(CONF_LEDOFF)
    volume_step = config.get(CONF_VOLUME_STEP)
    lastfm_api_key = config.get(CONF_LASTFM_API_KEY)
    uuid = config.get(CONF_UUID)

    default_protocol = False
    if protocol is None:
        protocol = "http"
        default_protocol = True

    state = STATE_IDLE

    websession = async_get_clientsession(hass)
    response = None
    
    try:
        initurl = "{}://{}/httpapi.asp?command=getStatus"
        response = await websession.get(initurl.format(protocol,host))

    except (asyncio.TimeoutError, aiohttp.ClientError) as error:
        if default_protocol:
            try:
                protocol = "https"
                initurl = "{}://{}/httpapi.asp?command=getStatusEx"
                response = await websession.get(initurl.format(protocol,host), ssl=False)

            except (asyncio.TimeoutError, aiohttp.ClientError) as error:
                _LOGGER.warning(
                    "Failed communicating with LinkPlayDevice (start) '%s': uuid: %s %s", host, uuid, type(error)
                )
                state = STATE_UNAVAILABLE
        else:
            _LOGGER.warning(
                "Failed communicating with LinkPlayDevice (start) '%s': uuid: %s %s", host, uuid, type(error)
            )
            state = STATE_UNAVAILABLE

    if response and response.status == HTTPStatus.OK:
        data = await response.json(content_type=None)
        _LOGGER.debug("HOST: %s DATA response: %s", host, data)

        if 'uuid' in data:
            uuid = data['uuid']

        if 'DeviceName' in data and name == None:
            name = data['DeviceName']

    else:
        _LOGGER.warning(
            "Get Status UUID failed, response code: %s Full message: %s",
            response.status if response is not None else "Unknown",
            response,
        )
        state = STATE_UNAVAILABLE

    linkplay = LinkPlayDevice(name, 
                            host, 
                            protocol, 
                            sources, 
                            common_sources, 
                            icecast_metadata, 
                            multiroom_wifidirect,
                            led_off,
                            volume_step,
                            lastfm_api_key,
                            uuid,
                            state,
                            hass)

    async_add_entities([linkplay])

class LinkPlayDevice(MediaPlayerEntity):
    """LinkPlayDevice Player Object."""

    def __init__(self, 
                 name, 
                 host,
                 protocol, 
                 sources, 
                 common_sources, 
                 icecast_metadata, 
                 multiroom_wifidirect,
                 led_off,
                 volume_step,
                 lastfm_api_key,
                 uuid,
                 state,
                 hass):
        """Initialize the media player."""
        self._uuid = uuid
        self._fw_ver = '1.0.0'
        self._mcu_ver = ''
        requester = AiohttpRequester(UPNP_TIMEOUT)
        self._factory = UpnpFactory(requester)
        self._upnp_device = None
        self._service = None
        self._features = None
        self._preset_key = 4
        self._name = name
        self._host = host
        self._protocol = protocol
        self._icon = ICON_DEFAULT
        self._state = state
        self._volume = 0
        self._volume_step = volume_step
        self._led_off = led_off
        self._fadevol = False
        self._source = None
        self._prev_source = None
        if sources is not None and sources != {}:
            self._source_list = loads(dumps(sources).strip('[]'))
        else:
            self._source_list = SOURCES.copy()
        if common_sources is not None and common_sources != {}:
            commonsources = loads(dumps(common_sources).strip('[]'))
            localsources = self._source_list
            self._source_list = {**localsources, **commonsources}
        self._sound_mode = None
        self._muted = False
        self._playhead_position = 0
        self._duration = 0
        self._position_updated_at = None
        self._spotify_paused_at = None
        self._idletime_updated_at = None
        self._shuffle = False
        self._repeat = REPEAT_MODE_OFF
        self._media_album = None
        self._media_artist = None
        self._media_prev_artist = None
        self._media_title = None
        self._media_prev_title = None
        self._media_image_url = None
        self._media_uri = None
        self._media_uri_final = None
        self._media_source_uri = None
        self._nometa = False
        self._player_statdata = {}
        self._lastfm_api_key = lastfm_api_key
        self._first_update = True
        self._slave_mode = False
        self._slave_ip = None
        self._trackq = []
        self._trackc = None
        self._master = None
        self._is_master = False
        self._wifi_channel = None
        self._ssid = None
        self._playing_localfile = True
        self._playing_stream = False
        self._playing_liveinput = False
        self._playing_spotify = False
        self._playing_webplaylist = False
        self._playing_tts = False
        self._playing_mediabrowser = False
        self._slave_list = None
        self._multiroom_wifidirect = multiroom_wifidirect
        self._multiroom_group = []
        self._multiroom_prevsrc = None
        self._multiroom_unjoinat = None
        self._wait_for_mcu = 0
        self._new_song = True
        self._unav_throttle = False
        self._icecast_name = None
        self._icecast_meta = icecast_metadata
        self._ice_skip_throt = False
        self._snapshot_active = False
        self._snap_source = None
        self._snap_uri = None
        self._snap_state = STATE_UNKNOWN
        self._snap_volume = 0
        self._snap_spotify = False
        self._snap_spotify_volumeonly = False
        self._snap_nometa = False
        self._snap_playing_mediabrowser = False
        self._snap_media_source_uri = None
        self._snap_seek = False
        self._snap_playhead_position = 0
        
    async def async_added_to_hass(self):
        """Record entity."""
        self.hass.data[DOMAIN].entities.append(self)

    async def call_linkplay_httpapi(self, cmd, jsn, protocol = None):
        """Get the latest data from HTTPAPI service."""
        if protocol is None and self._protocol is None:
            _LOGGER.warning("Protocol not known. Skipping communication with LinkPlayDevice '%s'", self._name)
            return False

        url = "{}://{}/httpapi.asp?command={}".format(self._protocol if protocol is None else protocol, self._host, cmd)
        
        if self._first_update:
            timeout = 10
        else:
            timeout = API_TIMEOUT
        
        try:
            websession = async_get_clientsession(self.hass)
            async with async_timeout.timeout(timeout):
                response = await websession.get(url, ssl=False)

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.warning(
                "Failed communicating with LinkPlayDevice (httpapi) '%s': %s", self._name, type(error)
            )
            return False

        if response.status == HTTPStatus.OK:
            if jsn:
                data = await response.json(content_type=None)
            else:
                data = await response.text()
                _LOGGER.debug("For: %s  cmd: %s  resp: %s", self._name, cmd, data)
        else:
            _LOGGER.error(
                "For: %s (%s) Get failed, response code: %s Full message: %s",
                self._name,
                self._host,
                response.status,
                response,
            )
            return False

        return data

    async def call_linkplay_tcpuart(self, cmd):
        """Get the latest data from TCP UART service."""
        LENC = format(len(cmd), '02x')
        HED1 = '18 96 18 20 '
        HED2 = ' 00 00 00 c1 02 00 00 00 00 00 00 00 00 00 00 '
        CMHX = ' '.join(hex(ord(c))[2:] for c in cmd)
        data = None
        _LOGGER.debug("For: %s Sending to %s TCP UART command: %s", self._name, self._host, cmd)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(API_TIMEOUT)
                s.connect((self._host, TCPPORT))
                s.send(bytes.fromhex(HED1 + LENC + HED2 + CMHX))
                data = str(repr(s.recv(1024))).encode().decode("unicode-escape")

            pos = data.find("AXX")
            if pos == -1:
                pos = data.find("MCU")

            data = data[pos:(len(data)-2)]
            _LOGGER.debug("For: %s Received from %s TCP UART command result: %s", self._name, self._host, data)
            try:
                s.close()
            except:
                pass

        except socket.error as ex:
            _LOGGER.debug("For: %s Error sending TCP UART command: %s with %s", self._name, cmd, ex)
            data = None

        return data

    @Throttle(UNA_THROTTLE)
    async def async_get_status(self):
        resp = await self.call_linkplay_httpapi("getPlayerStatus", True)
        if resp is False:
            _LOGGER.debug('Unable to connect to device: %s, %s', self.entity_id, self._name)
            self._state = STATE_UNAVAILABLE
            self._unav_throttle = True
            self._wait_for_mcu = 0
            self._playhead_position = None
            self._duration = None
            self._position_updated_at = None
            self._media_title = None
            self._media_artist = None
            self._media_album = None
            self._media_image_url = None
            self._media_uri = None
            self._media_uri_final = None
            self._media_source_uri = None
            self._playing_mediabrowser = False
            self._playing_stream = False
            self._icecast_name = None
            self._source = None
            self._upnp_device = None
            self._first_update = True
            self._slave_mode = False
            self._is_master = False
            self._player_statdata = None
            return
        self._player_statdata = resp.copy()

    async def async_trigger_schedule_update(self, before):
        await self.async_schedule_update_ha_state(before)

    async def async_update(self):
        """Update state."""

        # If we couldn't determine our protocol on startup, then attempt to do it now as our speaker might be available
        if self._protocol is None:
            device_status = await self.call_linkplay_httpapi("getStatusEx", True, "https")
            if device_status is not None and device_status != False:
                self._protocol = "https"
            else:
                device_status = await self.call_linkplay_httpapi("getStatus", True, "http")
                if device_status is not None and device_status != False:
                    self._protocol = "http"
                else:
                    return False

        #_LOGGER.debug("01 Start update %s, %s", self.entity_id, self._name)
        if self._master is None:
            self._slave_mode = False

        if self._slave_mode: # or self._snapshot_active:
            return True

        if self._multiroom_unjoinat is not None:
            #_LOGGER.debug("01 Update mroomunjoinat %s, %s", self.entity_id, self._name)
            if self._multiroom_wifidirect:
                waittim = MROOM_UJWDIR
            else:
                waittim = MROOM_UJWROU

            if utcnow() <= (self._multiroom_unjoinat + waittim):
                self._source = None
                self._media_title = None
                self._media_artist = None
                self._media_uri = None
                self._media_uri_final = None
                self._media_image_url = None
                self._state = STATE_IDLE
                return True
            else:
                self._multiroom_unjoinat = None
                self._playhead_position = 0
                self._duration = 0
                self._position_updated_at = utcnow()
                self._idletime_updated_at = self._position_updated_at
#                await self.async_restore_previous_source()
                await self.async_select_source(self._multiroom_prevsrc)
                self._multiroom_prevsrc = None
                return True

        # if self._wait_for_mcu > 0:  # have wait for the hardware unit to finish processing command, otherwise some reported status values will be incorrect
            # await asyncio.sleep(self._wait_for_mcu)
            # self._wait_for_mcu = 0

        if self._unav_throttle:
            await self.async_get_status()
        else:
            await self.async_get_status(no_throttle=True)

        if self._player_statdata is None:
            _LOGGER.debug("First update/No response from api: %s, %s", self.entity_id, self._player_statdata)
            return

        if isinstance(self._player_statdata, dict):
            self._unav_throttle = False
            if self._first_update or (self._state == STATE_UNAVAILABLE or self._multiroom_wifidirect):
                #_LOGGER.debug("03 Update first time getStatus %s, %s", self.entity_id, self._name)
                if self._protocol == "https":
                    device_status = await self.call_linkplay_httpapi("getStatusEx", True)
                else:
                    device_status = await self.call_linkplay_httpapi("getStatus", True)
                if device_status is not None:
                    if isinstance(device_status, dict):
                        if self._state == STATE_UNAVAILABLE:
                            self._state = STATE_IDLE
                        self._wifi_channel = device_status['WifiChannel']
                        self._ssid = binascii.hexlify(device_status['ssid'].encode('utf-8'))
                        self._ssid = self._ssid.decode()

                        try:
                            self._uuid = device_status['uuid']
                        except KeyError:
                            pass

                        try:
                            self._name = device_status['DeviceName']
                        except KeyError:
                            pass

                        try:
                            self._fw_ver = device_status['firmware']
                        except KeyError:
                            self._fw_ver = '1.0.0'

                        try:
                            self._mcu_ver = device_status['mcu_ver']
                        except KeyError:
                            self._mcu_ver = ''

                        try:
                            self._preset_key = int(device_status['preset_key'])
                        except KeyError:
                            self._preset_key = 4

                        if self._led_off and self._uuid != '':
                            if self._uuid.startswith(UUID_ARYLIC) and self._fwvercheck(self._fw_ver) >= self._fwvercheck(FW_RAKOIT_UART_MIN):
                                value = await self.call_linkplay_tcpuart('MCU+PAS+RAKOIT:LED:0&')
                                _LOGGER.debug("LED turn off: %s, %s, response: %s", self.entity_id, self._name, value)

                        if not self._multiroom_wifidirect and self._fw_ver:
                            if self._fwvercheck(self._fw_ver) < self._fwvercheck(FW_MROOM_RTR_MIN):
                                self._multiroom_wifidirect = True

                        if self._upnp_device is None: # and self._name is not None:
                            url = "http://{0}:49152/description.xml".format(self._host)
                            try:
                                self._upnp_device = await self._factory.async_create_device(url)
                            except:
                                _LOGGER.warning(
                                    "Failed communicating with LinkPlayDevice (UPnP) '%s': %s", self._name, type(error)
                                )

                        if self._first_update:
                            self._duration = 0
                            self._playhead_position = 0
                            self._idletime_updated_at = utcnow()
                            if "udisk" in self._source_list:
                                await self.async_tracklist_via_upnp("USB")
                            self._first_update = False

            self._position_updated_at = utcnow()

            if self._player_statdata['type'] == '0':
                self._slave_mode = False

            if self._multiroom_group == []:
                self._slave_mode = False
                self._is_master = False
                self._master = None

            if not self._is_master:
                self._master = None
                self._multiroom_group = []

            #_LOGGER.debug("04 Update VOL, Shuffle, Repeat, STATE %s, %s", self.entity_id, self._name)
            self._volume = self._player_statdata['vol']
            self._muted = bool(int(self._player_statdata['mute'])) 
            self._sound_mode = SOUND_MODES.get(self._player_statdata['eq'])

            self._shuffle = {
                '2': True,
                '3': True,
                '5': True,
            }.get(self._player_statdata['loop'], False)

            self._repeat = {
                '0': REPEAT_MODE_ALL,
                '1': REPEAT_MODE_ONE,
                '2': REPEAT_MODE_ALL,
                '5': REPEAT_MODE_ONE,
            }.get(self._player_statdata['loop'], REPEAT_MODE_OFF)

            # self._state = {
                # 'stop': STATE_IDLE,
                # 'load': STATE_PLAYING,
                # 'play': STATE_PLAYING,
                # 'pause': STATE_PAUSED,
            # }.get(self._player_statdata['status'], STATE_IDLE)
            
            if (self._player_statdata['mode'] in ['-1', '0', '99'] or self._player_statdata['status'] == 'stop'): 
                if utcnow() >= (self._idletime_updated_at + AUTOIDLE_STATE_TIMEOUT):
                    self._state = STATE_IDLE
                    #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)
            elif self._player_statdata['status'] in ['play', 'load']:
                self._state = STATE_PLAYING
                #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)
            elif self._player_statdata['status'] == 'pause':
                self._state = STATE_PAUSED
                #_LOGGER.debug("05 DETECTED %s, %s", self.entity_id, self._state)

            if self._state in [STATE_PLAYING, STATE_PAUSED]:
                self._duration = int(int(self._player_statdata['totlen']) / 1000)
                self._playhead_position = int(int(self._player_statdata['curpos']) / 1000)
                #_LOGGER.debug("04 Update DUR, POS %s, %s, %s, %s, %s", self.entity_id, self._name, self._state, self._duration, self._playhead_position)
            else:
                self._duration = 0
                self._playhead_position = 0

            #_LOGGER.debug("05 Update self._playing_whatever %s, %s", self.entity_id, self._name)
            self._playing_spotify = bool(self._player_statdata['mode'] == '31')
            self._playing_liveinput = self._player_statdata['mode'] in SOURCES_LIVEIN
            self._playing_stream = self._player_statdata['mode'] in SOURCES_STREAM
            self._playing_localfile = self._player_statdata['mode'] in SOURCES_LOCALF

            if bool(self._player_statdata['mode'] != '10'):
                #self._playing_tts = False
                self._playing_mediabrowser = False

            if not (self._playing_liveinput or self._playing_stream or self._playing_spotify):
                self._playing_localfile = True

            try:
                if self._playing_stream and self._player_statdata['uri'] != "":
                    _LOGGER.debug("06 Update URI final detect %s, %s", self.entity_id, self._name)
                    try:
                        self._media_uri_final = str(bytearray.fromhex(self._player_statdata['uri']).decode('utf-8'))
                    except ValueError:
                        self._media_uri_final = self._player_statdata['uri']
                    if not self._media_uri:
                        self._media_uri = self._media_uri_final
            except KeyError:
                pass

            if self._media_uri:
                #_LOGGER.debug("07 Detect CDN %s, %s", self.entity_id, self._name)
                # Detect web music service by their CDN subdomains in the URL
                # Tidal, Deezer
                self._playing_webplaylist = \
                    bool(self._media_uri.find('audio.tidal.') != -1) or \
                    bool(self._media_uri.find('.dzcdn.') != -1) or \
                    bool(self._media_uri.find('.deezer.') != -1)

            if not self._playing_webplaylist:
                #_LOGGER.debug("07 Set Name to Source: %s, %s", self.entity_id, self._name)
                source_t = SOURCES_MAP.get(self._player_statdata['mode'], 'Network')
                source_n = None
                if source_t == 'Network':
                    if self._media_uri:
                        source_n = self._source_list.get(self._media_uri, 'Network')
                else:
                    source_n = self._source_list.get(source_t, None)

                if source_n != None:
                    self._source = source_n
                else:
                    self._source = source_t
            else:
                self._source = 'Web playlist'

            if self._source != 'Network' and not (self._playing_stream or self._playing_localfile or self._playing_spotify):
                #_LOGGER.debug("08 Line Inputs: %s, %s", self.entity_id, self._name)
                if self._source == 'Idle':
                    self._state = STATE_IDLE
                    self._media_title = None
                else:
                    self._state = STATE_PLAYING
                    self._media_title = self._source

                self._media_artist = None
                self._media_album = None
                self._media_image_url = None
                self._icecast_name = None

            if self._player_statdata['mode'] in ['1', '2', '3']:
                #_LOGGER.debug("08 Line Inputs name playing: %s, %s", self.entity_id, self._name)
                self._state = STATE_PLAYING
                self._media_title = self._source

            if self._playing_spotify and self._state == STATE_IDLE:
                self._source = None

            if self._spotify_paused_at != None:
                if utcnow() >= (self._spotify_paused_at + SPOTIFY_PAUSED_TIMEOUT):
                    # Prevent sticking in Pause mode for a long time (Spotify doesn't have a stop button on the app)
                    await self.async_media_stop()
                    return

            if self._player_statdata['mode'] in ['11', '16'] and len(self._trackq) <= 0:
                if int(self._player_statdata['curpos']) > 6000 and self._state == STATE_PLAYING:
                    await self.async_tracklist_via_upnp("USB")

            if self._playing_spotify:
                #_LOGGER.debug("09 it's playing spotifty: %s, %s", self.entity_id, self._name)
                if self._state != STATE_IDLE:
                    await self.async_update_via_upnp()
                if self._state == STATE_PAUSED:
                    if self._spotify_paused_at == None:
                        self._spotify_paused_at = utcnow()
                else:
                    self._spotify_paused_at = None
            # else:
                # self._trackc = None
                # self._media_uri_final = None

            elif self._playing_webplaylist:
                if self._state != STATE_IDLE:
                    self.async_update_via_upnp()

            else:
                #_LOGGER.debug("09 it's playing something else: %s, %s", self.entity_id, self._name)
                self._spotify_paused_at = None
                if self._state not in [STATE_PLAYING, STATE_PAUSED]:
                    self._media_title = None
                    self._media_artist = None
                    self._media_album = None
                    self._media_image_url = None
                    self._icecast_name = None
                    self._playing_tts = False

                if self._playing_localfile and self._state in [STATE_PLAYING, STATE_PAUSED] and not self._playing_tts:
                    #_LOGGER.debug("10 Update async_get_playerstatus_metadata FILE %s, %s", self.entity_id, self._name)
                    await self.async_get_playerstatus_metadata(self._player_statdata)

                    if self._media_title is not None and self._media_artist is None:
                        querywords = self._media_title.split('.')
                        resultwords  = [word for word in querywords if word.lower() not in CUT_EXTENSIONS]
                        title = ' '.join(resultwords)
                        title.replace('_', ' ')
                        if title.find(' - ') != -1:
                            titles = title.split(' - ')
                            self._media_artist = string.capwords(titles[0].strip().strip('-'))
                            self._media_title = string.capwords(titles[1].strip().strip('-'))
                        else:
                            self._media_title = string.capwords(title.strip().strip('-'))
                    else:
                        self._media_title = self._source

                elif self._state == STATE_PLAYING and self._media_uri and int(self._player_statdata['totlen']) > 0 and not self._snapshot_active and not self._playing_tts and not self._playing_mediabrowser:
                    #_LOGGER.debug("10 Update async_get_playerstatus_metadata media_URI %s, %s", self.entity_id, self._name)
                    if not self._nometa:
                        await self.async_get_playerstatus_metadata(self._player_statdata)

                elif self._state == STATE_PLAYING and self._media_uri_final and int(self._player_statdata['totlen']) <= 0 and not self._snapshot_active and not self._playing_tts:
                    #_LOGGER.debug("10 Update async_update_from_icecast FILE %s, %s", self.entity_id, self._name)
                    if self._ice_skip_throt:
                        await self.async_update_from_icecast(no_throttle=True)
                        self._ice_skip_throt = False
                    else:
                        await self.async_update_from_icecast()
                
                elif self._state == STATE_PLAYING and self._playing_mediabrowser and self._media_source_uri is not None:
                    if not self._nometa:
                        await self.async_get_local_mediasource_metadata_from_path()

                self._new_song = await self.async_is_playing_new_track()
                if self._lastfm_api_key is not None and self._new_song:
                    #_LOGGER.debug("11 Update async_get_lastfm_coverart %s, %s", self.entity_id, self._name)
                    await self.async_get_lastfm_coverart()

            self._media_prev_artist = self._media_artist
            self._media_prev_title = self._media_title

        else:
            _LOGGER.error("Erroneous JSON during update and process self._player_statdata: %s, %s", self.entity_id, self._name)


        # Get multiroom slave information #
        
        slave_list = await self.call_linkplay_httpapi("multiroom:getSlaveList", True)
        if slave_list is None:
            self._is_master = False
            self._slave_list = None
            self._multiroom_group = []
            return True

        self._slave_list = []
        self._multiroom_group = []
        if isinstance(slave_list, dict):
            if int(slave_list['slaves']) > 0:
                self._multiroom_group.append(self.entity_id)
                self._is_master = True
                for slave in slave_list['slave_list']:
                    for device in self.hass.data[DOMAIN].entities:
                        if device._name == slave['name']:
                            self._multiroom_group.append(device.entity_id)
                            await device.async_set_master(self)
                            await device.async_set_is_master(False)
                            await device.async_set_slave_mode(True)
                            await device.async_set_media_title(self._media_title)
                            await device.async_set_media_artist(self._media_artist)
                            await device.async_set_volume(slave['volume'])
                            #await device.async_set_muted(slave['mute'])
                            await device.async_set_state(self.state)
                            await device.async_set_slave_ip(slave['ip'])
                            await device.async_set_media_image_url(self._media_image_url)
                            await device.async_set_playhead_position(self.media_position)
                            await device.async_set_duration(self.media_duration)
                            await device.async_set_position_updated_at(self.media_position_updated_at)
                            await device.async_set_source(self._source)
                            await device.async_set_sound_mode(self._sound_mode)
                            await device.async_set_features(self._features)

                    for slave in slave_list['slave_list']:
                        for device in self.hass.data[DOMAIN].entities:
                            if device.entity_id in self._multiroom_group:
                                await device.async_set_multiroom_group(self._multiroom_group)

        else:
            _LOGGER.debug("Erroneous JSON during slave list parsing and processing: %s, %s", self.entity_id, self._name)

        return True

    @property
    def name(self):
        """Return the name of the device."""
        if self._slave_mode:
            for dev in self._multiroom_group:
                for device in self.hass.data[DOMAIN].entities:
                    if device._is_master:
                        return self._name + ' [' + device._name + ']'
        else:
            return self._name
        return self._name

    @property
    def icon(self):
        """Return the icon of the device."""

        if self._playing_tts:
            return ICON_TTS

        if self._state in [STATE_PAUSED, STATE_UNAVAILABLE, STATE_IDLE, STATE_UNKNOWN]:
            return ICON_DEFAULT

        if self._muted:
            return ICON_MUTED

        if self._slave_mode or self._is_master:
            return ICON_MULTIROOM

        if self._source == "Bluetooth":
            return ICON_BLUETOOTH

        if self._source == "DLNA" or self._source == "Airplay" or self._source == "Spotify":
            return ICON_PUSHSTREAM

        if self._state == STATE_PLAYING:
            return ICON_PLAYING

        return ICON_DEFAULT

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return int(self._volume) / MAX_VOL

    @property
    def is_volume_muted(self):
        """Return boolean if volume is currently muted."""
        return self._muted

    @property
    def source(self):
        """Return the current input source."""
        if self._source not in ['Idle', 'Network']:
            return self._source
        else:
            return None

    @property
    def source_list(self):
        """Return the list of available input sources. If only one source exists, don't show it, as it's one and only one - WiFi shouldn't be listed."""
        source_list = self._source_list.copy()
        if 'wifi' in source_list:
            del source_list['wifi']

        if len(self._source_list) > 0:
            return list(source_list.values())
        else:
            return None

    @property
    def sound_mode(self):
        """Return the current sound mode."""
        return self._sound_mode

    @property
    def sound_mode_list(self):
        """Return the available sound modes."""
        return sorted(list(SOUND_MODES.values()))

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        """Flag media player features that are supported."""
        if self._slave_mode and self._features:
            return self._features

        if self._playing_localfile or self._playing_spotify or self._playing_webplaylist:
            if self._state in [STATE_PLAYING, STATE_PAUSED]:
                self._features = (
                    MediaPlayerEntityFeature.SELECT_SOURCE 
                    | MediaPlayerEntityFeature.SELECT_SOUND_MODE 
                    | MediaPlayerEntityFeature.PLAY_MEDIA 
                    | MediaPlayerEntityFeature.GROUPING 
                    | MediaPlayerEntityFeature.BROWSE_MEDIA 
                    | MediaPlayerEntityFeature.VOLUME_SET 
                    | MediaPlayerEntityFeature.VOLUME_STEP 
                    | MediaPlayerEntityFeature.VOLUME_MUTE 
                    | MediaPlayerEntityFeature.STOP 
                    | MediaPlayerEntityFeature.PLAY 
                    | MediaPlayerEntityFeature.PAUSE 
                    | MediaPlayerEntityFeature.NEXT_TRACK 
                    | MediaPlayerEntityFeature.PREVIOUS_TRACK 
                    | MediaPlayerEntityFeature.SHUFFLE_SET 
                    | MediaPlayerEntityFeature.REPEAT_SET 
                    | MediaPlayerEntityFeature.SEEK
                )
            else:
                self._features = (
                    MediaPlayerEntityFeature.SELECT_SOURCE 
                    | MediaPlayerEntityFeature.SELECT_SOUND_MODE 
                    | MediaPlayerEntityFeature.PLAY_MEDIA 
                    | MediaPlayerEntityFeature.GROUPING 
                    | MediaPlayerEntityFeature.BROWSE_MEDIA 
                    | MediaPlayerEntityFeature.VOLUME_SET 
                    | MediaPlayerEntityFeature.VOLUME_STEP 
                    | MediaPlayerEntityFeature.VOLUME_MUTE 
                    | MediaPlayerEntityFeature.STOP 
                    | MediaPlayerEntityFeature.PLAY 
                    | MediaPlayerEntityFeature.PAUSE 
                    | MediaPlayerEntityFeature.NEXT_TRACK 
                    | MediaPlayerEntityFeature.PREVIOUS_TRACK 
                    | MediaPlayerEntityFeature.SHUFFLE_SET 
                    | MediaPlayerEntityFeature.REPEAT_SET
                )

        elif self._playing_stream or self._playing_mediabrowser:
            self._features = (
                MediaPlayerEntityFeature.SELECT_SOURCE 
                | MediaPlayerEntityFeature.SELECT_SOUND_MODE 
                | MediaPlayerEntityFeature.PLAY_MEDIA 
                | MediaPlayerEntityFeature.GROUPING 
                | MediaPlayerEntityFeature.BROWSE_MEDIA 
                | MediaPlayerEntityFeature.VOLUME_SET 
                | MediaPlayerEntityFeature.VOLUME_STEP 
                | MediaPlayerEntityFeature.VOLUME_MUTE 
                | MediaPlayerEntityFeature.STOP 
                | MediaPlayerEntityFeature.PLAY 
                | MediaPlayerEntityFeature.PAUSE
                | MediaPlayerEntityFeature.SEEK
                )

        elif self._playing_liveinput:
            self._features = (
                MediaPlayerEntityFeature.SELECT_SOURCE 
                | MediaPlayerEntityFeature.SELECT_SOUND_MODE 
                | MediaPlayerEntityFeature.PLAY_MEDIA 
                | MediaPlayerEntityFeature.GROUPING 
                | MediaPlayerEntityFeature.BROWSE_MEDIA 
                | MediaPlayerEntityFeature.VOLUME_SET 
                | MediaPlayerEntityFeature.VOLUME_STEP 
                | MediaPlayerEntityFeature.VOLUME_MUTE 
                | MediaPlayerEntityFeature.STOP
                )

        return self._features

    @property
    def media_position(self):
        """Time in seconds of current playback head position."""
        if (self._playing_localfile or self._playing_spotify or self._slave_mode or self._playing_mediabrowser) and self._state != STATE_UNAVAILABLE:
            return self._playhead_position
        else:
            return None

    @property
    def media_duration(self):
        """Time in seconds of current song duration."""
        if (self._playing_localfile or self._playing_spotify or self._slave_mode or self._playing_mediabrowser) and self._state != STATE_UNAVAILABLE:
            return self._duration
        else:
            return None

    @property
    def media_position_updated_at(self):
        """When the seek position was last updated."""
        if not self._playing_liveinput and self._state == STATE_PLAYING:
            return self._position_updated_at
        else:
            return None

    @property
    def shuffle(self):
        """Return True if shuffle mode is enabled."""
        return self._shuffle

    @property
    def repeat(self):
        """Return repeat mode."""
        return self._repeat

    @property
    def media_title(self):
        """Return title of the current track."""
        return self._media_title

    @property
    def media_artist(self):
        """Return name of the current track artist."""
        return self._media_artist

    @property
    def media_album_name(self):
        """Return name of the current track album."""
        return self._media_album

    @property
    def media_image_url(self):
        """Return name the image for the current track."""
        return self._media_image_url

    @property
    def media_content_type(self):
        """Content type of current playing media. Has to be MEDIA_TYPE_MUSIC in order for Lovelace to show both artist and title."""
        return MEDIA_TYPE_MUSIC

    @property
    def ssid(self):
        """SSID to use for multiroom configuration."""
        return self._ssid

    @property
    def wifi_channel(self):
        """Wifi channel to use for multiroom configuration."""
        return self._wifi_channel

    @property
    def slave_ip(self):
        """Ip used in multiroom configuration."""
        return self._slave_ip

    @property
    def slave(self):
        """Return true if it is a slave."""
        return self._slave_mode

    @property
    def master(self):
        """master's entity id used in multiroom configuration."""
        return self._master

    @property
    def is_master(self):
        """Return true if it is a master."""
        return self._is_master

    @property
    def device_class(self) -> MediaPlayerDeviceClass:
        return MediaPlayerDeviceClass.SPEAKER

    @property
    def extra_state_attributes(self):
        """List members in group and set master and slave state."""
        attributes = {}
        if self._multiroom_group != []:
            attributes[ATTR_LINKPLAY_GROUP] = self._multiroom_group
            attributes[ATTR_GROUP_MEMBERS] = self._multiroom_group

        attributes[ATTR_MASTER] = self._is_master
        if self._slave_mode:
            attributes[ATTR_SLAVE] = self._slave_mode
        if self._media_uri_final:
            attributes[ATTR_STURI] = self._media_uri_final
        if len(self._trackq) > 0:
            attributes[ATTR_TRCNT] = len(self._trackq) - 1
        if self._trackc:
            attributes[ATTR_TRCRT] = self._trackc
        if self._uuid != '':
            attributes[ATTR_UUID] = self._uuid

        attributes[ATTR_TTS] = self._playing_tts
        attributes[ATTR_SNAPSHOT] = self._snapshot_active
        attributes[ATTR_SNAPSPOT] = self._snap_spotify

        if DEBUGSTR_ATTR:
            atrdbg = ""
            if self._playing_localfile:
                atrdbg = atrdbg + " _playing_localfile"

            if self._playing_spotify:
                atrdbg = atrdbg + " _playing_spotify"

            if self._playing_webplaylist:
                atrdbg = atrdbg + " _playing_webplaylist"

            if self._playing_stream:
                atrdbg = atrdbg + " _playing_stream"

            if self._playing_liveinput:
                atrdbg = atrdbg + " _playing_liveinput"

            if self._playing_tts:
                atrdbg = atrdbg + " _playing_tts"
                
            if self._playing_mediabrowser:
                atrdbg = atrdbg + " _playing_mediabrowser"

            attributes[ATTR_DEBUG] = atrdbg

        if self._state != STATE_UNAVAILABLE:
            attributes[ATTR_FWVER] = self._fw_ver + "." + self._mcu_ver

        return attributes

    @property
    def host(self):
        """Self ip."""
        return self._host

    @property
    def track_count(self):
        """List of tracks present on the device."""
        if len(self._trackq) > 0:
            return len(self._trackq) - 1
        else:
            return 0

    @property
    def unique_id(self):
        """Return the unique id."""
        if self._uuid != '':
            return "linkplay_media_" + self._uuid

    @property
    def fw_ver(self):
        """Return the firmware version number of the device."""
        return self._fw_ver

    async def async_media_next_track(self):
        """Send media_next command to media player."""
        if not self._slave_mode:
            value = await self.call_linkplay_httpapi("setPlayerCmd:next", None)
            self._playhead_position = 0
            self._duration = 0
            self._position_updated_at = utcnow()
            self._trackc = None
            self._wait_for_mcu = 2
            if value != "OK":
                _LOGGER.warning("Failed skip to next track. Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_media_next_track()

    async def async_media_previous_track(self):
        """Send media_previous command to media player."""
        if not self._slave_mode:
            value = await self.call_linkplay_httpapi("setPlayerCmd:prev", None)
            self._playhead_position = 0
            self._duration = 0
            self._position_updated_at = utcnow()
            self._trackc = None
            self._wait_for_mcu = 2
            if value != "OK":
                _LOGGER.warning("Failed to skip to previous track." " Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_media_previous_track()

    async def async_media_play(self):
        """Send media_play command to media player."""
        if not self._slave_mode:
            if self._state == STATE_PAUSED:
                value = await self.call_linkplay_httpapi("setPlayerCmd:resume", None)

            elif self._prev_source != None:
                temp_source = next((k for k in self._source_list if self._source_list[k] == self._prev_source), None)
                if temp_source == None:
                    return

                if temp_source.startswith('http') or temp_source == 'udisk' or temp_source == 'TFcard':
                    self.select_source(self._prev_source)
                    if self._source != None:
                        self._source = None
                        value = "OK"
                else:
                    value = await self.call_linkplay_httpapi("setPlayerCmd:play", None)
            else:
                value = await self.call_linkplay_httpapi("setPlayerCmd:play", None)

            if value == "OK":
                self._state = STATE_PLAYING
                self._unav_throttle = False
                #self._playing_tts = False
                self._position_updated_at = utcnow()
                self._idletime_updated_at = self._position_updated_at
                if self._slave_list is not None:
                    for slave in self._slave_list:
                        await slave.async_set_state(self._state)
                        await slave.async_set_position_updated_at(self.media_position_updated_at)
            else:
                _LOGGER.warning("Failed to start or resume playback. Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_media_play()

    async def async_media_pause(self):
        """Send media_pause command to media player."""
        if not self._slave_mode:
            if self._playing_stream and not self._playing_mediabrowser:
                # Pausing a live stream will cause a buffer overrun in hardware. Stop is the correct procedure in this case.
                # If the stream is configured as an input source, when pressing Play after this, it will be started again (using self._prev_source).
                await self.async_media_stop()
                return

            value = await self.call_linkplay_httpapi("setPlayerCmd:pause", None)
            if value == "OK":
                self._position_updated_at = utcnow()
                self._idletime_updated_at = self._position_updated_at
                if self._playing_spotify:
                    self._spotify_paused_at = utcnow()
                self._state = STATE_PAUSED
                if self._slave_list is not None:
                    for slave in self._slave_list:
                        await slave.async_set_state(self._state)
                        await slave.async_set_position_updated_at(self.media_position_updated_at)
#                #await self.async_schedule_update_ha_state(True)
            else:
                _LOGGER.warning("Failed to pause playback. Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_media_pause()

    async def async_media_stop(self):
        """Send stop command."""
        if not self._slave_mode:
 
            if self._playing_spotify or self._playing_liveinput:
                if self._fwvercheck(self._fw_ver) >= self._fwvercheck(FW_SLOW_STREAMS):
                    await self.call_linkplay_httpapi("setPlayerCmd:pause", None)

                await self.call_linkplay_httpapi("setPlayerCmd:switchmode:wifi", None)
                # self._wait_for_mcu = 1.2

            if self._playing_stream:  #recent firmwares don't stop the previous stream quickly enough
                if self._fwvercheck(self._fw_ver) >= self._fwvercheck(FW_SLOW_STREAMS):
                    await self.call_linkplay_httpapi("setPlayerCmd:pause", None)
                    await self.call_linkplay_httpapi("setPlayerCmd:switchmode:wifi", None)

            value = await self.call_linkplay_httpapi("setPlayerCmd:stop", None)
            if value == "OK":
                self._state = STATE_IDLE
                self._playhead_position = 0
                self._duration = 0
                self._media_title = None
                self._prev_source = self._source
                self._source = None
                self._nometa = False
                self._media_artist = None
                self._media_album = None
                self._icecast_name = None
                self._media_uri = None
                self._media_uri_final = None
                self._media_source_uri = None
                self._playing_mediabrowser = False
                self._playing_stream = False
                self._trackc = None
                self._media_image_url = None
                self._position_updated_at = utcnow()
                self._idletime_updated_at = self._position_updated_at
                self._spotify_paused_at = None
                #await self.async_schedule_update_ha_state(True)
                if self._slave_list is not None:
                    for slave in self._slave_list:
                        await slave.async_set_state(self._state)
                        await slave.async_set_position_updated_at(self.media_position_updated_at)
            else:
                _LOGGER.warning("Failed to stop playback. Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_media_stop()

    async def async_media_seek(self, position):
        """Send media_seek command to media player."""
        if not self._slave_mode:
            _LOGGER.debug("Seek. Device: %s, DUR: %s POS: %", self.name, self._duration, position)
            if self._duration > 0 and position >= 0 and position <= self._duration:
                value = await self.call_linkplay_httpapi("setPlayerCmd:seek:{0}".format(str(position)), None)
                self._position_updated_at = utcnow()
                self._idletime_updated_at = self._position_updated_at
                self._wait_for_mcu = 0.2
                if value != "OK":
                    _LOGGER.warning("Failed to seek. Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_media_seek(position)

    async def async_clear_playlist(self):
        """Clear players playlist."""
        pass

    async def async_play_media(self, media_type, media_id, **kwargs):
        """Play media from a URL or localfile."""
        _LOGGER.debug("Trying to play media. Device: %s, Media_type: %s, Media_id: %s", self.entity_id, media_type, media_id)
        if not self._slave_mode:

            if not (media_type in [MEDIA_TYPE_MUSIC, MEDIA_TYPE_URL, MEDIA_TYPE_TRACK] or media_source.is_media_source_id(media_id)):
                _LOGGER.warning("For: %s Invalid media type %s. Only %s and %s is supported", self._name, media_type, MEDIA_TYPE_MUSIC, MEDIA_TYPE_URL)
                await self.async_media_stop()
                return False
            
            if not self._snapshot_active:
                self._playing_mediabrowser = False
                self._nometa = False

            if media_source.is_media_source_id(media_id):
                play_item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
                if media_id.find('radio_browser') != -1:  # radios are an exception, be treated by server redirect checker and icecast metadata parser
                    self._playing_mediabrowser = False
                else:
                    self._playing_mediabrowser = True

                if media_id.find('media_source/local') != -1:
                    self._media_source_uri = media_id
                else:
                    self._media_source_uri = None

                media_id = play_item.url
                if not play_item.mime_type in ['audio/basic',
                                               'audio/mpeg', 
                                               'audio/mp3', 
                                               'audio/mpeg3', 
                                               'audio/x-mpeg-3',
                                               'audio/x-mpegurl', 
                                               'audio/mp4', 
                                               'audio/aac', 
                                               'audio/x-aac',
                                               'audio/x-hx-aac-adts', 
                                               'audio/x-aiff', 
                                               'audio/ogg', 
                                               'audio/vorbis', 
                                               'application/ogg', 
                                               'audio/opus', 
                                               'audio/webm', 
                                               'audio/wav', 
                                               'audio/x-wav', 
                                               'audio/vnd.wav', 
                                               'audio/flac',
                                               'audio/x-flac', 
                                               'audio/x-ms-wma']:
                    _LOGGER.warning("For: %s Invalid media type, %s is not supported", self._name, play_item.mime_type)
                    self._playing_mediabrowser = False
                    return False
                
                media_id = async_process_play_media_url(self.hass, media_id)
                _LOGGER.debug("Trying to play HA media. Device: %s, Play_Item: %s, Media_id: %s", self._name, play_item, media_id)

            media_id_check = media_id.lower()

            if media_id_check.startswith('http'):
                media_type = MEDIA_TYPE_URL

            if media_id_check.endswith('.m3u') or media_id_check.endswith('.m3u8'):
                _LOGGER.debug("For: %s, Detected M3U list: %s, Media_id: %s", self._name, media_id)
                media_id = await self.async_parse_m3u_url(media_id)

            if media_id_check.endswith('.pls'):
                _LOGGER.debug("For: %s, Detected PLS list: %s, Media_id: %s", self._name, media_id)
                media_id = await self.async_parse_pls_url(media_id)

            if media_type == MEDIA_TYPE_URL:
                if self._playing_mediabrowser:
                    media_id_final = media_id
                else:
                    media_id_final = await self.async_detect_stream_url_redirection(media_id)

                if self._fwvercheck(self._fw_ver) >= self._fwvercheck(FW_SLOW_STREAMS) and self._state == STATE_PLAYING:
                    await self.call_linkplay_httpapi("setPlayerCmd:pause", None)
                
                if self._playing_spotify:  # disconnect from Spotify before playing new http source
                    await self.call_linkplay_httpapi("setPlayerCmd:switchmode:wifi", None)

                value = await self.call_linkplay_httpapi("setPlayerCmd:play:{0}".format(media_id_final), None)
                if value != "OK":
                    _LOGGER.warning("Failed to play media type URL. Device: %s, Got response: %s, Media_Id: %s", self.entity_id, value, media_id)
                    return False

            elif media_type in [MEDIA_TYPE_MUSIC, MEDIA_TYPE_TRACK]:
                value = await self.call_linkplay_httpapi("setPlayerCmd:playLocalList:{0}".format(media_id), None)
                if value != "OK":
                    _LOGGER.warning("Failed to play media type music. Device: %s, Got response: %s, Media_Id: %s", self.entity_id, value, media_id)
                    return False

            self._state = STATE_PLAYING
            if media_id.find('tts_proxy') != -1:
                #_LOGGER.debug("Setting TTS: %s, %s", self.entity_id, self._name)
                self._playing_tts = True
                self._playing_mediabrowser = False
                self._playing_stream = False
            else:
                self._playing_tts = False
            self._media_title = None
            self._media_artist = None
            self._media_album = None
            self._icecast_name = None
            self._playhead_position = 0
            self._duration = 0
            self._trackc = None
            self._position_updated_at = utcnow()
            self._idletime_updated_at = self._position_updated_at
            self._media_image_url = None
            self._ice_skip_throt = True
            self._unav_throttle = False
            if media_type == MEDIA_TYPE_URL:
                self._media_uri = media_id
                self._media_uri_final = media_id_final
            elif media_type == MEDIA_TYPE_MUSIC:
                self._media_uri = None
                self._media_uri_final = None
                self._wait_for_mcu = 0.4
            return True

        else:
            if not self._snapshot_active:
                await self._master.async_play_media(media_type, media_id)

    async def async_select_source(self, source):
        """Select input source."""
        if not self._slave_mode:
            self._nometa = False
            temp_source = next((k for k in self._source_list if self._source_list[k] == source), None)
            if temp_source == None:
                return

            if self._playing_spotify:  # disconnect from Spotify before selecting new source
                if self._fwvercheck(self._fw_ver) >= self._fwvercheck(FW_SLOW_STREAMS):
                    await self.call_linkplay_httpapi("setPlayerCmd:pause", None)
                await self.call_linkplay_httpapi("setPlayerCmd:switchmode:wifi", None)

            if temp_source == "udisk":
                await self.async_tracklist_via_upnp("USB")

            if len(self._source_list) > 0:
                prev_source = next((k for k in self._source_list if self._source_list[k] == self._source), None)

            if prev_source and prev_source.startswith('http') and temp_source in ['line-in', 'line-in2', 'optical', 'bluetooth', 'co-axial', 'HDMI', 'cd', 'udisk', 'RCA']:
                self._wait_for_mcu = 1

            self._unav_throttle = False
            if temp_source.startswith('http'):
                temp_source_final = await self.async_detect_stream_url_redirection(temp_source)

                if self._fwvercheck(self._fw_ver) >= self._fwvercheck(FW_SLOW_STREAMS) and self._state == STATE_PLAYING:
                    await self.call_linkplay_httpapi("setPlayerCmd:pause", None)  #recent firmwares don't stop the previous stream while loading the new one, can take several seconds

                value = await self.call_linkplay_httpapi("setPlayerCmd:play:{0}".format(temp_source_final), None)
                if value == "OK":
                    self._state = STATE_PLAYING
                    if prev_source and prev_source.find('http') == -1:
                        self._wait_for_mcu = 2  # switching from live to stream input -> time to report correct volume value at update
                    else:
                        self._wait_for_mcu = 0.5
                    self._playing_tts = False
                    self._source = source
                    self._media_uri = temp_source
                    self._media_uri_final = temp_source_final
                    self._playhead_position = 0
                    self._duration = 0
                    self._trackc = None
                    self._position_updated_at = utcnow()
                    self._idletime_updated_at = self._position_updated_at
                    self._media_title = None
                    self._media_artist = None
                    self._media_album = None
                    self._icecast_name = None
                    self._media_image_url = None
                    self._ice_skip_throt = True
                    if self._slave_list is not None:
                        for slave in self._slave_list:
                            await slave.async_set_source(source)
                else:
                    _LOGGER.warning("Failed to select http source and play. Device: %s, Got response: %s", self.entity_id, value)
            else:
                value = await self.call_linkplay_httpapi("setPlayerCmd:switchmode:{0}".format(temp_source), None)
                if value == "OK":
                    self._state = STATE_PLAYING
                    # if temp_source and temp_source in ['udisk', 'TFcard']:
                        # self._wait_for_mcu = 2    # switching to locally stored files -> time to report correct volume value at update
                    # else:
                        # self._wait_for_mcu = 0.6  # switching to a physical input -> time to report correct volume value at update
                    self._source = source
                    self._media_uri = None
                    self._media_uri_final = None
                    self._playhead_position = 0
                    self._duration = 0
                    self._trackc = None
                    self._position_updated_at = utcnow()
                    self._idletime_updated_at = self._position_updated_at
                    if self._slave_list is not None:
                        for slave in self._slave_list:
                            await slave.async_set_source(source)
                else:
                    _LOGGER.warning("Failed to select source. Device: %s, Got response: %s", self.entity_id, value)

            #await self.async_schedule_update_ha_state(True)
        else:
            await self._master.async_select_source(source)

    async def async_select_sound_mode(self, sound_mode):
        """Set Sound Mode for device."""
        if not self._slave_mode:
            mode = list(SOUND_MODES.keys())[list(
                SOUND_MODES.values()).index(sound_mode)]
            value = await self.call_linkplay_httpapi("setPlayerCmd:equalizer:{0}".format(mode), None)
            if value == "OK":
                self._sound_mode = sound_mode
                if self._slave_list is not None:
                    for slave in self._slave_list:
                        await slave.async_set_sound_mode(sound_mode)
            else:
                _LOGGER.warning("Failed to set sound mode. Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_select_sound_mode(sound_mode)

    async def async_set_shuffle(self, shuffle):
        """Change the shuffle mode."""
        if not self._slave_mode:
            if shuffle:
                self._shuffle = shuffle
                mode = '2'
            else:
                if self._repeat == REPEAT_MODE_OFF:
                    mode = '0'
                elif self._repeat == REPEAT_MODE_ALL:
                    mode = '3'
                elif self._repeat == REPEAT_MODE_ONE:
                    mode = '1'
            value = await self.call_linkplay_httpapi("setPlayerCmd:loopmode:{0}".format(mode), None)
            if value != "OK":
                _LOGGER.warning("Failed to change shuffle mode. Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_set_shuffle(shuffle)

    async def async_set_repeat(self, repeat):
        """Change the repeat mode."""
        if not self._slave_mode:
            self._repeat = repeat
            if repeat == REPEAT_MODE_OFF:
                mode = '0'
            elif repeat == REPEAT_MODE_ALL:
                mode = '2' if self._shuffle else '3'
            elif repeat == REPEAT_MODE_ONE:
                mode = '1'
            value = await self.call_linkplay_httpapi("setPlayerCmd:loopmode:{0}".format(mode), None)
            if value != "OK":
                _LOGGER.warning("Failed to change repeat mode. Device: %s, Got response: %s", self.entity_id, value)
        else:
            await self._master.async_set_repeat(repeat)

    async def async_volume_up(self):
        """Increase volume one step"""
        if int(self._volume) == 100 and not self._muted:
            return

        volume = int(self._volume) + int(self._volume_step)
        if volume > 100:
            volume = 100

        if not (self._slave_mode and self._multiroom_wifidirect):

            if self._is_master:
                value = await self.call_linkplay_httpapi("setPlayerCmd:slave_vol:{0}".format(str(volume)), None)
            else:
                value = await self.call_linkplay_httpapi("setPlayerCmd:vol:{0}".format(str(volume)), None)

            if value == "OK":
                self._volume = volume
            else:
                _LOGGER.warning("Failed to set volume_up. Device: %s, Got response: %s", self.entity_id, value)
        else:
            if self._snapshot_active:
                return
            value = await self._master.call_linkplay_httpapi("multiroom:SlaveVolume:{0}:{1}".format(self._slave_ip, str(volume)), None)
            if value == "OK":
                self._volume = volume
            else:
                _LOGGER.warning("Failed to set volume_up. Device: %s, Got response: %s", self.entity_id, value)

    async def async_volume_down(self):
        """Decrease volume one step."""
        if int(self._volume) == 0:
            return

        volume = int(self._volume) - int(self._volume_step)
        if volume < 0:
            volume = 0

        if not (self._slave_mode and self._multiroom_wifidirect):

            if self._is_master:
                value = await self.call_linkplay_httpapi("setPlayerCmd:slave_vol:{0}".format(str(volume)), None)
            else:
                value = await self.call_linkplay_httpapi("setPlayerCmd:vol:{0}".format(str(volume)), None)

            if value == "OK":
                self._volume = volume
            else:
                _LOGGER.warning("Failed to set volume_down. Device: %s, Got response: %s", self.entity_id, value)
        else:
            if self._snapshot_active:
                return
            value = await self._master.call_linkplay_httpapi("multiroom:SlaveVolume:{0}:{1}".format(self._slave_ip, str(volume)), None)
            if value == "OK":
                self._volume = volume
            else:
                _LOGGER.warning("Failed to set volume_down. Device: %s, Got response: %s", self.entity_id, value)

    async def async_set_volume_level(self, volume):
        """Set volume level, input range 0..1, linkplay device 0..100."""
        volume = str(round(int(volume * MAX_VOL)))
        if not (self._slave_mode and self._multiroom_wifidirect):

            # if self._fadevol:
                # voldiff = int(self._volume) - int(volume)
                # steps = 1
                # if voldiff < 33:
                    # steps = 2
                # elif voldiff >= 33 and voldiff < 66:
                    # steps = 4
                # elif voldiff > 66:
                    # steps = 6
                # volstep = int(round(voldiff / steps))
                # voltemp = int(self._volume)
# #                self._wait_for_mcu = 1  # set delay in update routine for the fade to finish
                # for v in (range(0, steps - 1)):
                    # voltemp = voltemp - volstep
                    # # self._lpapi.call('GET', 'setPlayerCmd:vol:{0}'.format(str(voltemp)))
                    # value = await self.call_linkplay_httpapi("setPlayerCmd:vol:{0}".format(str(voltemp)), None)
                    # await asyncio.sleep(0.6 / steps)

            if self._is_master:
                value = await self.call_linkplay_httpapi("setPlayerCmd:slave_vol:{0}".format(str(volume)), None)
            else:
                if self._snapshot_active:
                    await asyncio.sleep(1)
                value = await self.call_linkplay_httpapi("setPlayerCmd:vol:{0}".format(str(volume)), None)

            if value == "OK":
                self._volume = volume
            else:
                _LOGGER.warning("Failed to set volume. Device: %s, Got response: %s", self.entity_id, value)
        else:
            if self._snapshot_active:
                return
            value = await self._master.call_linkplay_httpapi("multiroom:SlaveVolume:{0}:{1}".format(self._slave_ip, str(volume)), None)
            if value == "OK":
                self._volume = volume
            else:
                _LOGGER.warning("Failed to set volume. Device: %s, Got response: %s", self.entity_id, value)


    async def async_mute_volume(self, mute):
        """Mute (true) or unmute (false) media player."""
        if not (self._slave_mode and self._multiroom_wifidirect):
            if self._is_master:
                value = await self.call_linkplay_httpapi("setPlayerCmd:slave_mute:{0}".format(str(int(mute))), None)
            else:
                value = await self.call_linkplay_httpapi("setPlayerCmd:mute:{0}".format(str(int(mute))), None)
            
            if value == "OK":
                self._muted = bool(int(mute))
            else:
                _LOGGER.warning("Failed mute/unmute volume. Device: %s, Got response: %s", self.entity_id, value)
        else:
            value = await self._master.call_linkplay_httpapi("multiroom:SlaveVolume:{0}:{1}".format(self._slave_ip, str(int(mute))), None)
            if value == "OK":
                self._muted = bool(int(mute))
            else:
                _LOGGER.warning("Failed mute/unmute volume. Device: %s, Got response: %s", self.entity_id, value)

    async def call_update_lastfm(self, cmd, params):
        """Update LastFM metadata."""
        url = "{0}{1}&{2}&api_key={3}&format=json".format(LASTFM_API_BASE, cmd, params, self._lastfm_api_key)
        #_LOGGER.debug("Updating LastFM from URL: %s", url)
        
        try:
            websession = async_get_clientsession(self.hass)
            response = await websession.get(url)
            if response.status == HTTPStatus.OK:
                data = await response.json(content_type=None) #response.text()

            else:
                _LOGGER.error(
                    "Get failed, response code: %s Full message: %s",
                    response.status,
                    response,
                )
                return False

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.error(
                "Failed communicating with LastFM '%s': %s", self._name, type(error)
            )
            return False
        return data

    @Throttle(LFM_THROTTLE)
    async def async_get_lastfm_coverart(self):
        """Get cover art from last.fm."""
        if self._media_title is None or self._media_artist is None:
            self._media_image_url = None
            return

        coverart_url = None
        lfmdata = await self.call_update_lastfm('track.getInfo', "artist={0}&track={1}".format(self._media_artist, self._media_title))

        try:
            coverart_url = lfmdata['track']['album']['image'][3]['#text']
        except (ValueError, KeyError):
            coverart_url = None

        if coverart_url == '' or coverart_url == None:
            self._media_image_url = None
        else:
            if coverart_url.find('2a96cbd8b46e442fc41c2b86b821562f') != -1:
                # don't show the sheriff star empty cover https://lastfm.freetls.fastly.net/i/u/300x300/2a96cbd8b46e442fc41c2b86b821562f.png
                _LOGGER.debug("LastFM ratelimited")
                self._media_image_url = None
            else:
                self._media_image_url = coverart_url

    async def async_get_local_mediasource_metadata_from_path(self):
        if self._media_source_uri is not None:
            rootdir = "media-source://media_source/local/"            
            self._trackc = self._media_source_uri.replace(rootdir, '')
            titleuri = self._trackc.split('/')
            if len(titleuri) > 1:
                titles = titleuri[-2:]
                self._media_artist = string.capwords(titles[0].strip().strip('-').replace('_', ' '))
                self._media_title = string.capwords(titles[1].strip().strip('-').replace('_', ' '))
            else:
                self._media_title = string.capwords(titleuri[0].strip().strip('-').replace('_', ' '))
            querywords = self._media_title.split('.')
            resultwords  = [word for word in querywords if word.lower() not in CUT_EXTENSIONS]
            self._media_title = ' '.join(resultwords)
            return True
        else:
            return False

    async def async_get_playerstatus_metadata(self, plr_stat):
        try:
            if plr_stat['uri'] != "":
                rootdir = ROOTDIR_USB
                try:
                    self._trackc = str(bytearray.fromhex(plr_stat['uri']).decode('utf-8')).replace(rootdir, '')
                except ValueError:
                    self._trackc = plr_stat['uri'].replace(rootdir, '')
        except KeyError:
            pass
        if plr_stat['Title'] != '':
            try:
                title = str(bytearray.fromhex(plr_stat['Title']).decode('utf-8'))
            except ValueError:
                title = plr_stat['Title']
            if title.lower() != 'unknown':
                self._media_title = string.capwords(title)
                if self._trackc == None:
                    self._trackc = self._media_title
            else:
                self._media_title = None
        if plr_stat['Artist'] != '':
            try:
                artist = str(bytearray.fromhex(plr_stat['Artist']).decode('utf-8'))
            except ValueError:
                artist = plr_stat['Artist']
            if artist.lower() != 'unknown':
                self._media_artist = string.capwords(artist)
            else:
                self._media_artist = None
        if plr_stat['Album'] != '':
            try:
                album = str(bytearray.fromhex(plr_stat['Album']).decode('utf-8'))
            except ValueError:
                album = plr_stat['Album']
            if album.lower() != 'unknown':
                self._media_album = string.capwords(album)
            else:
                self._media_album = None

        if self._media_title is not None and self._media_artist is not None:
            return True
        else:
            return False

    @Throttle(ICE_THROTTLE)
    async def async_update_from_icecast(self):
        """Update track info from icecast stream."""
        if self._icecast_meta == 'Off':
            return True

        #_LOGGER.debug('For: %s Looking for IceCast metadata in: %s', self._name, self._media_uri_final)

#        def NiceToICY(self):
#            class InterceptedHTTPResponse():
#                pass
#            import io
#            line = self.fp.readline().replace(b"ICY 200 OK\r\n", b"HTTP/1.0 200 OK\r\n")
#            InterceptedSelf = InterceptedHTTPResponse()
#            InterceptedSelf.fp = io.BufferedReader(io.BytesIO(line))
#            InterceptedSelf.debuglevel = self.debuglevel
#            InterceptedSelf._close_conn = self._close_conn
#            return ORIGINAL_HTTP_CLIENT_READ_STATUS(InterceptedSelf)

#        ORIGINAL_HTTP_CLIENT_READ_STATUS = urllib.request.http.client.HTTPResponse._read_status
#        urllib.request.http.client.HTTPResponse._read_status = NiceToICY

        try:
            request = urllib.request.Request(self._media_uri_final, headers={'Icy-MetaData': '1','User-Agent': 'VLC/3.0.16 LibVLC/3.0.16'})  # request metadata
            response = await self.hass.async_add_executor_job(urllib.request.urlopen, request)
        except:  # (urllib.error.HTTPError)
            _LOGGER.debug('For: %s Metadata error: %s', self._name, self._media_uri_final)
            self._media_title = None
            self._media_artist = None
            self._icecast_name = None
            self._media_image_url = None
            return True

        icy_name = response.headers['icy-name']
        if icy_name is not None and icy_name != 'no name' and icy_name != 'Unspecified name' and icy_name != '-':
            try:  # 'latin1' # default: iso-8859-1 for mp3 and utf-8 for ogg streams
                self._icecast_name = icy_name.encode('latin1').decode('utf-8')
            except (UnicodeDecodeError):
                self._icecast_name = icy_name
            #_LOGGER.debug('For: %s found icy_name: %s', self._name, '"' + icy_name + '"')

        else:
            self._icecast_name = None

        if self._icecast_meta == 'StationName':
            self._media_title = self._icecast_name
            self._media_artist = None
            self._media_image_url = None
            return True

        icy_metaint_header = response.headers['icy-metaint']
        if icy_metaint_header is not None:
            metaint = int(icy_metaint_header)
            for _ in range(10):  # title may be empty initially, try several times
                response.read(metaint)  # skip to metadata
                metadata_length = struct.unpack('B', response.read(1))[0] * 16  # length byte
                metadata = response.read(metadata_length).rstrip(b'\0')
                #_LOGGER.debug('For: %s found metadata: %s', self._name, metadata)

                # extract title from the metadata
                # m = re.search(br"StreamTitle='([^']*)';", metadata)
                m = re.search(br"StreamTitle='(.*)';", metadata)
                #_LOGGER.debug('For: %s found m: %s', self._name, m)
                if m:
                    title = m.group(0)
                    #_LOGGER.debug('For: %s found title: %s', self._name, title)

                    if title:
                        code_detect = chardet.detect(title)['encoding']
                        title = title.decode(code_detect, errors='ignore')
                        titlek = title.split("';")
                        title = titlek[0]
                        titlem = title.split("='")
                        title = titlem[1]
                        #_LOGGER.debug('For: %s found decoded title: %s', self._name, title)

                        title = re.sub(r'\[.*?\]\ *', '', title)  #  "\s*\[.*?\]\s*"," ",title)
                        if title.find('~~~~~') != -1:  # for United Music Subasio servers
                            titles = title.split('~')
                            self._media_artist = string.capwords(titles[0].strip().strip('-'))
                            self._media_title = string.capwords(titles[1].strip().strip('-'))
                        elif title.find(' - ') != -1:  # for ordinary Icecast servers
                            titles = title.split(' - ')
                            self._media_artist = string.capwords(titles[0].strip().strip('-'))
                            self._media_title = string.capwords(titles[1].strip().strip('-'))
                        else:
                            if self._icecast_name is not None:
                                self._media_artist = '[' + self._icecast_name + ']'
                            else:
                                self._media_artist = None
                            self._media_title = string.capwords(title)

                        if self._media_artist == '-':
                            self._media_artist = None
                        if self._media_title == '-':
                            self._media_title = None

                        if self._media_artist is not None:
                            self._media_artist.replace('/', ' / ')
                            self._media_artist.replace('  ', ' ')

                        if self._media_title is not None:
                            self._media_title.replace('/', ' / ')
                            self._media_title.replace('  ', ' ')

                        break
                else:
                    if self._icecast_name is not None:
                        self._media_title = self._icecast_name
                    else:
                        self._media_title = None
                    self._media_artist = None
                    self._media_image_url = None

        else:
            if self._icecast_name is not None:
                self._media_title = self._icecast_name
            else:
                self._media_title = None
            self._media_artist = None
            self._media_image_url = None

        #_LOGGER.debug('For: %s stated media_title: %s', self._name, self._media_title)
        #_LOGGER.debug('For: %s stated media_artist: %s', self._name, self._media_artist)
        
    async def async_detect_stream_url_redirection(self, uri):
        if uri.find('tts_proxy') != -1: # skip redirect check for local TTS streams
            return uri
        _LOGGER.debug('For: %s detect URI redirect-from:   %s', self._name, uri)
        redirect_detect = True
        check_uri = uri
        try:
            while redirect_detect:
                headsession = async_get_clientsession(self.hass)
                response_location = await headsession.head(check_uri, allow_redirects=False, headers={'User-Agent': 'VLC/3.0.16 LibVLC/3.0.16'})
                _LOGGER.debug('For: %s detecting URI redirect code: %s', self._name, str(response_location.status))
                if response_location.status in [301, 302, 303, 307, 308] and 'Location' in response_location.headers:
                    _LOGGER.debug('For: %s detecting URI redirect location: %s', self._name, response_location.headers['Location']) 
                    check_uri = response_location.headers['Location']
                else:
                    _LOGGER.debug('For: %s detecting URI redirect - result: %s', self._name, check_uri)
                    redirect_detect = False
        except Exception as error:
            _LOGGER.debug("Exception:" + str(error))

        _LOGGER.debug('For: %s detect URI redirect - to:   %s', self._name, check_uri)
        return check_uri

    async def async_parse_m3u_url(self, playlist):
        """Parse an M3U playlist URL for actual streams, and return the first one"""
        try:
            websession = async_get_clientsession(self.hass)
            async with async_timeout.timeout(10):
                response = await websession.get(playlist)

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.warning(
                "For: %s unable to get the M3U playlist: %s", self._name, playlist
            )
            return playlist

        if response.status == HTTPStatus.OK:
            data = await response.text()
            _LOGGER.debug("For: %s M3U playlist: %s  contents: %s", self._name, playlist, data)

            lines = [line.strip("\n\r") for line in data.split("\n") if line.strip("\n\r") != ""]
            if len(lines) > 0:
                _LOGGER.debug("For: %s M3U playlist: %s  lines: %s", self._name, playlist, lines)
                urls = [u for u in lines if u.startswith('http')]
                _LOGGER.debug("For: %s M3U playlist: %s  urls: %s", self._name, playlist, urls)
                if len(urls) > 0:
                    return urls[0]
                else:
                    _LOGGER.error("For: %s M3U playlist: %s No valid http URL in the playlist!!!", self._name, playlist)
                    self._nometa = True
            else:
                _LOGGER.error("For: %s M3U playlist: %s No content to parse!!!", self._name, playlist)

        else:
            _LOGGER.error(
                "For: %s (%s) Get failed, response code: %s Full message: %s",
                self._name,
                self._host,
                response.status,
                response,
            )

        return playlist

    async def async_parse_pls_url(self, playlist):
        """Parse a PLS playlist URL for actual streams, and return the first one"""
        try:
            websession = async_get_clientsession(self.hass)
            async with async_timeout.timeout(10):
                response = await websession.get(playlist)

        except (asyncio.TimeoutError, aiohttp.ClientError) as error:
            _LOGGER.warning(
                "For: %s unable to get the PLS playlist: %s", self._name, playlist
            )
            return playlist

        if response.status == HTTPStatus.OK:
            data = await response.text()
            _LOGGER.debug("For: %s PLS playlist: %s  contents: %s", self._name, playlist, data)

            lines = [line.strip("\n\r") for line in data.split("\n") if line.strip("\n\r") != ""]
            if len(lines) > 0:
                _LOGGER.debug("For: %s PLS playlist: %s  lines: %s", self._name, playlist, lines)
                urls = [u for u in lines if u.startswith('File')]
                _LOGGER.debug("For: %s PLS playlist: %s  urls: %s", self._name, playlist, urls)
                if len(urls) > 0:
                    url = urls[0].split('=')
                    if len(url) > 1:
                        return url[1]
                else:
                    _LOGGER.error("For: %s PLS playlist: %s No valid http URL in the playlist!!!", self._name, playlist)
                    self._nometa = True
            else:
                _LOGGER.error("For: %s PLS playlist: %s No content to parse!!!", self._name, playlist)

        else:
            _LOGGER.error(
                "For: %s (%s) Get failed, response code: %s Full message: %s",
                self._name,
                self._host,
                response.status,
                response,
            )

        return playlist

    def _fwvercheck(self, v):
        filled = []
        for point in v.split("."):
            filled.append(point.zfill(8))
        return tuple(filled)

    async def async_is_playing_new_track(self):
        """Check if track is changed since last update."""
        if self._playing_mediabrowser and self._media_source_uri is not None:
            # don't trigger new track flag for local mediabrowser files
            return False
        
        if self._icecast_name != None:
            import unicodedata
            artmed = unicodedata.normalize('NFKD', str(self._media_artist) + str(self._media_title)).lower()
            artmedd = u"".join([c for c in artmed if not unicodedata.combining(c)])
            if artmedd.find(self._icecast_name.lower()) != -1 or artmedd.find(self._source.lower()) != -1:
                # don't trigger new track flag for icecast streams where track name contains station name or source name; save some energy by not quering last.fm with this
                self._media_image_url = None
                return False

        if self._media_artist != self._media_prev_artist or self._media_title != self._media_prev_title:
            return True
        else:
            return False

    async def async_set_multiroom_group(self, multiroom_group):
        """Set multiroom group info."""
        self._multiroom_group = multiroom_group

    async def async_set_master(self, master):
        """Set master device for multiroom configuration."""
        self._master = master

    async def async_set_is_master(self, is_master):
        """Set master device for multiroom configuration."""
        self._is_master = is_master

    async def async_set_multiroom_unjoinat(self, tme):
        """The moment when unjoin has happened. Needs some time for the MCU to finish unjoining first"""
        self._multiroom_unjoinat = tme

    async def async_set_slave_mode(self, slave_mode):
        """Set current device as slave in a multiroom configuration."""
        self._slave_mode = slave_mode
        ##await self.async_schedule_update_ha_state(True)

    async def async_set_previous_source(self, srcbool):
        """Memorize what was the previous source before entering multiroom."""
        if srcbool:
            self._multiroom_prevsrc = self._source
        else:
            self._multiroom_prevsrc = None

    async def async_restore_previous_source(self):
        """Set to the last known source after exiting multiroom."""
        self.select_source(self._multiroom_prevsrc)
        self._multiroom_prevsrc = None

    async def async_set_media_title(self, title):
        """Set the media title property."""
        self._media_title = title

    async def async_set_media_artist(self, artist):
        """Set the media artist property."""
        self._media_artist = artist

    async def async_set_volume(self, volume):
        """Set the volume property."""
        self._volume = volume

    async def async_set_muted(self, mute):
        """Set the muted property."""
        self._muted = mute

    async def async_set_state(self, state):
        """Set the state property."""
        self._state = state

    async def async_set_slave_ip(self, slave_ip):
        """Set the slave ip property."""
        self._slave_ip = slave_ip

    async def async_set_playhead_position(self, position):
        """Set the playhead position property."""
        self._playhead_position = position

    async def async_set_duration(self, duration):
        """Set the duration property."""
        self._duration = duration

    async def async_set_position_updated_at(self, time):
        """Set the position updated at property."""
        self._position_updated_at = time

    async def async_set_source(self, source):
        """Set the source property."""
        self._source = source
        ##await self.async_schedule_update_ha_state(True)

    async def async_set_sound_mode(self, mode):
        """Set the sound mode property."""
        self._sound_mode = mode

    async def async_set_media_image_url(self, url):
        """Set the media image URL property."""
        self._media_image_url = url

    async def async_set_media_uri(self, uri):
        """Set the media URL property."""
        self._media_uri = uri

    async def async_set_features(self, features):
        """Set the self features property."""
        self._features = features

    async def async_set_wait_for_mcu(self, wait_for_mcu):
        """Set the wait for mcu processing duration property."""
        self._wait_for_mcu = wait_for_mcu

    async def async_set_unav_throttle(self, unav_throttle):
        """Set update throttle property."""
        self._unav_throttle = unav_throttle

    async def async_preset_button(self, preset):
        """Simulate pressing a physical preset button."""
        if self._preset_key != None and preset != None:
            if not self._slave_mode:
                if int(preset) > 0 and int(preset) <= self._preset_key:
                    value = await self.call_linkplay_httpapi("MCUKeyShortClick:{0}".format(str(preset)), None)
                    # self._wait_for_mcu = 2
                    # await self.async_schedule_update_ha_state(True)
                    if value != "OK":
                        _LOGGER.warning("Failed to recall preset %s. " "Device: %s, Got response: %s", self.entity_id, preset, value)
                else:
                    _LOGGER.warning("Wrong preset number %s. Device: %s, has to be integer between 1 and %s", self.entity_id, preset, self._preset_key)
            else:
                await self._master.async_preset_button(preset)

    async def async_join_players(self, slaves):
        """Join `group_members` as a player group with the current player (standard HA)."""
        entities = self.hass.data[DOMAIN].entities
        entities = [e for e in entities if e.entity_id in slaves]
        await self.async_join(entities)

    async def async_join(self, slaves):
        """Add selected slaves to multiroom configuration (original implementation)."""
        _LOGGER.debug("Multiroom JOIN request: Master: %s, Slaves: %s", self.entity_id, slaves)
        if self._state == STATE_UNAVAILABLE:
            return

        if self.entity_id not in self._multiroom_group:
            self._multiroom_group.append(self.entity_id)
            self._is_master = True
            self._wait_for_mcu = 2

        for slave in slaves:
            if slave._is_master:
                _LOGGER.debug("Multiroom: slave has master flag set. Unjoining it from where it is. Master: %s, Slave: %s", self.entity_id, slave.entity_id)
                await slave.async_unjoin_all()

            if slave.entity_id not in self._multiroom_group:
                if slave._slave_mode:
                    _LOGGER.debug("Multiroom: slave already has slave flag set. Unjoining it from where it is. Master: %s, Slave: %s", self.entity_id, slave.entity_id)
                    await slave.async_unjoin_me()

                await slave.async_set_previous_source(True)
                if self._multiroom_wifidirect:
                    _LOGGER.debug("Multiroom: Join in WiFi drect mode. Master: %s, Slave: %s", self.entity_id, slave.entity_id)
                    cmd = "ConnectMasterAp:ssid={0}:ch={1}:auth=OPEN:".format(self._ssid, self._wifi_channel) + "encry=NONE:pwd=:chext=0"
                else:
                    _LOGGER.debug("Multiroom: Join in multiroom mode. Master: %s, Slave: %s", self.entity_id, slave.entity_id)
                    cmd = 'ConnectMasterAp:JoinGroupMaster:eth{0}:wifi0.0.0.0'.format(self._host)

                value = await slave.call_linkplay_httpapi(cmd, None)
                
                _LOGGER.debug("Multiroom: command result: %s Master: %s, Slave: %s", value, self.entity_id, slave.entity_id)
                if value == "OK":
#                    await slave.async_set_volume(self._volume)
#                    await slave.async_set_volume_level(self._volume)
                    await slave.async_set_master(self)
                    await slave.async_set_is_master(False)
                    await slave.async_set_slave_mode(True)
                    await slave.async_set_media_title(self._media_title)
                    await slave.async_set_media_artist(self._media_artist)
#                    await slave.async_set_muted(self._muted)
                    await slave.async_set_state(self.state)
                    await slave.async_set_slave_ip(self._host)
                    await slave.async_set_media_image_url(self._media_image_url)
                    await slave.async_set_playhead_position(self.media_position)
                    await slave.async_set_duration(self.media_duration)
                    await slave.async_set_source(self._source)
                    await slave.async_set_sound_mode(self._sound_mode)
                    await slave.async_set_features(self._features)
                    self._multiroom_group.append(slave.entity_id)
                else:
                    await slave.async_set_previous_source(False)
                    _LOGGER.warning("Failed to join multiroom. command result: %s Master: %s, Slave: %s", value, self.entity_id, slave.entity_id)

        for slave in slaves:
            if slave.entity_id in self._multiroom_group:
                await slave.async_set_multiroom_group(self._multiroom_group)
##                await slave.async_set_position_updated_at(utcnow())
##                await slave.async_trigger_schedule_update(True)

        self._position_updated_at = utcnow()
        # await self.async_schedule_update_ha_state(True)

    async def async_unjoin_all(self):
        """Disconnect everybody from the multiroom configuration because i'm the master."""
        if self._state == STATE_UNAVAILABLE:
            return

        cmd = "multiroom:Ungroup"
        value = await self.call_linkplay_httpapi(cmd, None)
        if value == "OK":
            self._is_master = False
            for slave_id in self._multiroom_group:
                for device in self.hass.data[DOMAIN].entities:
                    if device.entity_id == slave_id and device.entity_id != self.entity_id:
                        await device.async_set_slave_mode(False)
                        await device.async_set_is_master(False)
                        await device.async_set_slave_ip(None)
                        await device.async_set_master(None)
                        await device.async_set_multiroom_unjoinat(utcnow())
                        await device.async_set_multiroom_group([])
                        # await device.async_trigger_schedule_update(True)
            self._multiroom_group = []
            self._position_updated_at = utcnow()
            # await self.async_schedule_update_ha_state(True)

        else:
            _LOGGER.warning("Failed to unjoin_all multiroom. " "Device: %s, Got response: %s", self.entity_id, value)

    async def async_unjoin_player(self):
        """Remove this player from any group (standard HA)."""
        if self._is_master:
            await self.async_unjoin_all()

        if self._slave_mode:
            await self.async_unjoin_me()

    async def async_unjoin_me(self):
        """Disconnect myself from the multiroom configuration."""
        if self._multiroom_wifidirect:
            for dev in self._multiroom_group:
                for device in self.hass.data[DOMAIN].entities:
                    if device._is_master:    ## TODO!!!
                        cmd = "multiroom:SlaveKickout:{0}".format(self._slave_ip)
                        value = await self._master.call_linkplay_httpapi(cmd, None)
                        self._master._position_updated_at = utcnow()

        else:
            cmd = "multiroom:Ungroup"
            value = await self.call_linkplay_httpapi(cmd, None)

        if value == "OK":
            if self._master is not None:
                await self._master.async_remove_from_group(self)
                self._master._wait_for_mcu = 1
                # await self._master.async_schedule_update_ha_state(True)
            self._multiroom_unjoinat = utcnow()
            self._master = None
            self._is_master = False
            self._slave_mode = False
            self._slave_ip = None
            self._multiroom_group = []
            # await self.async_schedule_update_ha_state(True)

        else:
            _LOGGER.warning("Failed to unjoin_me from multiroom. " "Device: %s, Got response: %s", self.entity_id, value)

    async def async_remove_from_group(self, device):
        """Remove a certain device for multiroom lists."""
        if device.entity_id in self._multiroom_group:
            self._multiroom_group.remove(device.entity_id)
#            await self.async_schedule_update_ha_state(True)

        if len(self._multiroom_group) <= 1:
            self._multiroom_group = []
            self._is_master = False
            self._slave_list = None

        for member in self._multiroom_group:
            for player in self.hass.data[DOMAIN].entities:
                if player.entity_id == member and player.entity_id != self.entity_id:
                    await player.async_set_multiroom_group(self._multiroom_group)
#                    await player.async_trigger_schedule_update(True)
#                    await player.async_set_position_updated_at(utcnow())

    async def async_execute_command(self, command, notif):
        """Execute desired command against the player using factory API."""
        if command.startswith('MCU'):
            value = await self.call_linkplay_tcpuart(command)
        elif command == 'PromptEnable':
            value = await self.call_linkplay_httpapi("PromptEnable", None)
        elif command == 'PromptDisable':
            value = await self.call_linkplay_httpapi("PromptDisable", None)
        elif command == 'RouterMultiroomEnable':
            value = await self.call_linkplay_httpapi("setMultiroomLogic:1", None)
        elif command == 'SetRandomWifiKey':
            from random import choice
            from string import ascii_letters
            newkey = (''.join(choice(ascii_letters) for i in range(16)))
            value = await self.call_linkplay_httpapi("setNetwork:1:{0}".format(newkey), None)
            if value == 'OK':
                value = value + ", key: " + newkey
            else:
                value = "key: " + newkey
        elif command.startswith('SetApSSIDName:'):
            ssidnam = command.replace('SetApSSIDName:', '').strip()
            if ssidnam != '':
                value = await self.call_linkplay_httpapi("setSSID:{0}".format(ssidnam), None)
                if value == 'OK':
                    value = value + ", SoftAP SSID set to: " + ssidnam
            else:
                value == "SSID not specified correctly. You need 'SetApSSIDName: NewWifiName'"
        elif command.startswith('WriteDeviceNameToUnit:'):
            devnam = command.replace('WriteDeviceNameToUnit:', '').strip()
            if devnam != '':
                value = await self.call_linkplay_httpapi("setDeviceName:{0}".format(devnam), None)
                if value == 'OK':
                    self._name = devnam
                    value = value + ", name set to: " + self._name
            else:
                value == "Device name not specified correctly. You need 'WriteDeviceNameToUnit: My Device Name'"
        elif command == 'TimeSync':
            import time
            tme = time.strftime('%Y%m%d%H%M%S')
            value = await self.call_linkplay_httpapi("timeSync:{0}".format(tme), None)
            if value == 'OK':
                value = value + ", time: " + tme
        elif command == 'Rescan':
            self._unav_throttle = False
            self._first_update = True
            # await self.async_schedule_update_ha_state(True)
            value = "Scheduled to Rescan"
        elif command == 'Update':
            # await self.async_schedule_update_ha_state(True)
            value = "Scheduled to Update state"
        elif command == 'reboot':
            value = await self.call_linkplay_httpapi("reboot;", None)
        elif command == 'restoreToDefault':
            value = await self.call_linkplay_httpapi("restoreToDefault", None)
        else:
            value = "No such command implemented."
            _LOGGER.warning("Player %s command: %s, result: %s", self.entity_id, command, value)

        _LOGGER.debug("Player %s executed command: %s, result: %s", self.entity_id, command, value)

        if notif:
            self.hass.components.persistent_notification.async_create("<b>Executed command:</b><br>{0}<br><b>Result:</b><br>{1}".format(command, value), title=self.entity_id)

    async def async_snapshot(self, switchinput):
        """Snapshot the current input source and the volume level of it """
        if self._state == STATE_UNAVAILABLE:
            return

        if not self._slave_mode:
            self._snapshot_active = True
            self._snap_source = self._source
            self._snap_state = self._state
            self._snap_nometa = self._nometa
            self._snap_playing_mediabrowser = self._playing_mediabrowser
            self._snap_media_source_uri = self._media_source_uri
            self._snap_playhead_position = self._playhead_position

            if self._playing_localfile or self._playing_spotify or self._playing_webplaylist:
                if self._state in [STATE_PLAYING, STATE_PAUSED]:
                    self._snap_seek = True

            elif self._playing_stream or self._playing_mediabrowser:
                if self._state in [STATE_PLAYING, STATE_PAUSED] and self._playing_mediabrowser:
                    self._snap_seek = True

            _LOGGER.debug("Player %s snaphsot source: %s, volume: %s, uri: %s, seek: %s, pos: %s", self.name, self._source, self._snap_volume, self._media_uri_final, self._snap_seek, self._playhead_position)

            if self._source == "Network":
                self._snap_uri = self._media_uri_final
                

            if self._playing_spotify:
                if not switchinput:
                    await self.async_preset_snap_via_upnp(str(self._preset_key))
                    await self.call_linkplay_httpapi("setPlayerCmd:stop", None)
                else:
                    self._snap_spotify_volumeonly = True
                self._snap_spotify = True
                self._snap_volume = int(self._volume)
                # await asyncio.sleep(0.2)
                return

            elif self._state == STATE_IDLE:
                self._snap_volume = int(self._volume)

            elif switchinput and not self._playing_stream:
                value = await self.call_linkplay_httpapi("setPlayerCmd:switchmode:wifi", None)
                await asyncio.sleep(0.2)
                await self.call_linkplay_httpapi("setPlayerCmd:stop", None)
                if value == "OK":
                    await asyncio.sleep(2)  # have to wait for the sound fade-in of the unit when physical source is changed, otherwise volume value will be incorrect
                    await self.async_get_status()
                    if self._player_statdata is not None:
                        try:
                            self._snap_volume = int(self._player_statdata['vol'])
                        except ValueError:
                            _LOGGER.warning("Erroneous JSON during snapshot volume reading: %s, %s", self.entity_id, self._name)
                            self._snap_volume = 0
                    else:
                        self._snap_volume = 0
                else:
                    self._snap_volume = 0
            else:
                self._snap_volume = int(self._volume)
                if self._fwvercheck(self._fw_ver) >= self._fwvercheck(FW_SLOW_STREAMS):
                    await self.call_linkplay_httpapi("setPlayerCmd:pause", None)
                await self.call_linkplay_httpapi("setPlayerCmd:stop", None)
        else:
            return
            #await self._master.async_snapshot(switchinput)


    async def async_restore(self):
        """Restore the current input source and the volume level of it """
        if self._state == STATE_UNAVAILABLE:
            return

        if not self._slave_mode:
            _LOGGER.debug("Player %s current source: %s, restoring volume: %s, source: %s uri: %s, seek: %s, pos: %s", self.name, self._source, self._snap_volume, self._snap_source, self._snap_uri, self._snap_seek, self._snap_playhead_position)
            if self._snap_state != STATE_UNKNOWN:
                self._state = self._snap_state

            if self._snap_volume != 0:
                await self.call_linkplay_httpapi("setPlayerCmd:vol:{0}".format(str(self._snap_volume)), None)
                self._snap_volume = 0

                # await asyncio.sleep(.6)

            self._playing_tts = False
            self._playhead_position = self._snap_playhead_position

            if self._snap_spotify:
                self._snap_spotify = False
                if not self._snap_spotify_volumeonly:
                    await self.call_linkplay_httpapi("MCUKeyShortClick:{0}".format(str(self._preset_key)), None)
                self._snapshot_active = False
                self._snap_spotify_volumeonly = False
                # await self.async_schedule_update_ha_state(True)

            elif self._snap_source != "Network":
                self._snapshot_active = False
                await self.async_select_source(self._snap_source)
                self._snap_source = None
            
            elif self._snap_uri is not None:
                self._playing_mediabrowser = self._snap_playing_mediabrowser
                self._media_source_uri = self._snap_media_source_uri
                self._media_uri = self._snap_uri
                self._nometa = self._snap_nometa
                if self._snap_state in [STATE_PLAYING, STATE_PAUSED]:  # self._media_uri.find('tts_proxy') == -1
                    await self.async_play_media(MEDIA_TYPE_URL, self._media_uri)
                self._snapshot_active = False
                self._snap_uri = None

            if self._snap_state in [STATE_PLAYING, STATE_PAUSED]:
                await asyncio.sleep(0.5)
                if self._snap_seek and self._snap_playhead_position > 0:
                    _LOGGER.debug("Seekin'")
                    await self.call_linkplay_httpapi("setPlayerCmd:seek:{0}".format(str(self._snap_playhead_position)), None)
                    if self._snap_state == STATE_PAUSED:
                        await self.async_media_pause()

            self._snap_state = STATE_UNKNOWN
            self._snap_seek = False
            self._snap_playhead_position = 0

        else:
            return
            #await self._master.async_restore()

    async def async_play_track(self, track):
        """Play media track by name found in the tracks list."""
        if not len(self._trackq) > 0 or track is None:
            return

        track.hass = self.hass   # render template
        trackn = track.async_render()

        if not self._slave_mode:
            try:
                index = [idx for idx, s in enumerate(self._trackq) if trackn in s][0]
            except (IndexError):
                return

            if not index > 0:
                return

            value = await self.call_linkplay_httpapi("setPlayerCmd:playLocalList:{0}".format(index), None)
            if value != "OK":
                _LOGGER.warning("Failed to play media track by name. Device: %s, Got response: %s", self.entity_id, value)
                return False
            else:
                self._state = STATE_PLAYING
                self._playing_tts = False
                #self._wait_for_mcu = 0.4
                self._media_title = None
                self._media_artist = None
                self._media_album = None
                self._trackc = None
                self._icecast_name = None
                self._playhead_position = 0
                self._duration = 0
                self._position_updated_at = utcnow()
                self._media_image_url = None
                self._media_uri = None
                self._media_uri_final = None
                self._ice_skip_throt = False
                self._unav_throttle = False
                # await self.async_schedule_update_ha_state(True)
                return True
        else:
            await self._master.async_play_track(track)

    async def async_update_via_upnp(self):
        """Update track info via UPNP."""
        import validators
        radio = False

        if self._upnp_device is None:
            return

        self._service = self._upnp_device.service('urn:schemas-upnp-org:service:AVTransport:1')
        #_LOGGER.debug("GetMediaInfo for: %s, UPNP service:%s", self.entity_id, self._service)
        
        media_info = dict()
        media_metadata = None
        try:
            media_info = await self._service.action("GetMediaInfo").async_call(InstanceID=0)
            self._trackc = media_info.get('CurrentURI')
            self._media_uri_final = media_info.get('TrackSource')
            media_metadata = media_info.get('CurrentURIMetaData')
            #_LOGGER.debug("GetMediaInfo for: %s, UPNP media_metadata:%s", self.entity_id, media_info)
        except:
            _LOGGER.warning("GetMediaInfo/CurrentURIMetaData UPNP error: %s", self.entity_id)

        if media_metadata is None:
            return

        self._media_title = None
        self._media_album = None
        self._media_artist = None
        self._media_image_url = None

        xml_tree = ET.fromstring(media_metadata)

        xml_path = "{urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/}item/"
        title_xml_path = "{http://purl.org/dc/elements/1.1/}title"
        artist_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}artist"
        album_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}album"
        image_xml_path = "{urn:schemas-upnp-org:metadata-1-0/upnp/}albumArtURI"
        radiosub_xml_path = "{http://purl.org/dc/elements/1.1/}subtitle"

        if radio:
            title = xml_tree.find("{0}{1}".format(xml_path, radiosub_xml_path)).text
            if title.find(' - ') != -1:
                titles = title.split(' - ')
                self._media_artist = string.capwords(titles[0].strip())
                self._media_title = string.capwords(titles[1].strip())
            else:
                self._media_title = string.capwords(title.strip())
        else:
            self._media_title = xml_tree.find("{0}{1}".format(xml_path, title_xml_path)).text
            self._media_artist = xml_tree.find("{0}{1}".format(xml_path, artist_xml_path)).text
            self._media_album = xml_tree.find("{0}{1}".format(xml_path, album_xml_path)).text
 
        self._media_image_url = xml_tree.find("{0}{1}".format(xml_path, image_xml_path)).text

        if not validators.url(self._media_image_url):
            self._media_image_url = None

    async def async_tracklist_via_upnp(self, media):
        """Retrieve tracks list queue via UPNP."""
        if self._upnp_device is None:
            return

        if media == 'USB':
            queuename = 'USBDiskQueue'  # 'CurrentQueue'  # 'USBDiskQueue'
            rootdir = ROOTDIR_USB
        else:
            _LOGGER.debug("Tracklist retrieval: %s, %s is not supported. You can use only 'USB' for now.", self.entity_id, media)
            self._trackq = []
            return

        self._service = self._upnp_device.service('urn:schemas-wiimu-com:service:PlayQueue:1')
        #_LOGGER.debug("PlayQueue for: %s, UPNP service:%s", self.entity_id, self._service)
        
        media_info = dict()
        media_metadata = None
        try:
            media_info = await self._service.action("BrowseQueue").async_call(QueueName=queuename)
            media_metadata = media_info.get('QueueContext')
            #_LOGGER.debug("PlayQueue for: %s, UPNP media_metadata:%s", self.entity_id, media_info)
        except:
            _LOGGER.debug("PlayQueue/QueueContext UPNP error, media not present?: %s", self.entity_id)

        if media_metadata is None:
            return

        xml_tree = ET.fromstring(media_metadata)

        trackq = []
        for playlist in xml_tree:
           for tracks in playlist:
               for track in tracks:
                   if track.tag == 'URL':
                       if rootdir in track.text:
                           tracku = track.text.replace(rootdir, '')
                           trackq.append(tracku)

        if len(trackq) > 0:
            self._trackq = trackq

    async def async_preset_snap_via_upnp(self, presetnum):
        """Save current playlist to a preset via UPNP."""
        if self._upnp_device is None or not self._playing_spotify:
            return

        media_info = dict()

        self._service = self._upnp_device.service('urn:schemas-wiimu-com:service:PlayQueue:1')
        #_LOGGER.debug("PlayQueue for: %s, UPNP service:%s", self.entity_id, self._service)

        try:
            media_info = await self._service.action("SetSpotifyPreset").async_call(KeyIndex=int(presetnum))
            _LOGGER.debug("PlayQueue/SetSpotifyPreset for: %s, UPNP media_info:%s", self.entity_id, media_info)
            result = str(media_info.get('Result'))
        except:
            _LOGGER.debug("SetSpotifyPreset UPNP error for: %s, presetnum: %s, result: %s", self.entity_id, presetnum, result)
            return

        preset_map = dict()

        try:
            preset_map = await self._service.action("GetKeyMapping").async_call()
            preset_map = preset_map.get('QueueContext')
        except:
            _LOGGER.debug("GetKeyMapping UPNP error: %s", self.entity_id)
            return

        xml_tree = ET.fromstring(preset_map)
        
        if xml_tree.find('Key'+presetnum) is None:
            _LOGGER.error("Preset Map error: %s num: %s. Please create a Spotify preset first with the mobile app for this player. Tree: %s", self.entity_id, presetnum, preset_map)
            self.hass.components.persistent_notification.async_create("<b>Preset Map error:</b><br><br><br>This player can't store presets yet!<br>Please create a preset first manually with the mobile app for this player and then try again.", title=self.entity_id)
            return

        import time
        tme = time.strftime('%Y-%m-%d %H:%M:%S')

        try:
            xml_tree.find('Key'+presetnum+'/Name').text = "Snapshot set by Home Assistant ("+result+")_#~" + tme
        except:
            data=xml_tree.find('Key'+presetnum)
            snap=ET.SubElement(data,'Name')
            snap.text = "Snapshot set by Home Assistant ("+result+")_#~" + tme

        try:
            xml_tree.find('Key'+presetnum+'/Source').text = "SPOTIFY"
        except:
            data=xml_tree.find('Key'+presetnum)
            snap=ET.SubElement(data,'Source')
            snap.text = "SPOTIFY"

        try:
            xml_tree.find('Key'+presetnum+'/PicUrl').text = "https://brands.home-assistant.io/_/media_player/icon.png"
        except:
            data=xml_tree.find('Key'+presetnum)
            snap=ET.SubElement(data,'PicUrl')
            snap.text = "https://brands.home-assistant.io/_/media_player/icon.png"

        preset_map = ET.tostring(xml_tree, encoding='unicode')

        try:
            await self._service.action("SetKeyMapping").async_call(QueueContext=preset_map)
        except:
            _LOGGER.debug("SetKeyMapping UPNP error: %s, %s", self.entity_id, preset_map)
            return


    async def async_browse_media(self, media_content_type=None, media_content_id=None):
        """Implement the websocket media browsing helper."""
        return await media_source.async_browse_media(
            self.hass,
            media_content_id,
            content_filter=lambda item: item.media_content_type.startswith("audio/"),
        )
        
        #TODO: combide the BrowseMedia Media Sources above with the BrowseMedia Directory below
        #if "udisk" in self._source_list: 
        # if media_content_id not in (None, "root"):
            # raise BrowseError(
                # f"Media not found: {media_content_type} / {media_content_id}"
            # )

        # source_media_name = self._source_list.get("udisk", "USB Disk")

        # if len(self._trackq) > 0:
            # radio = [
                # BrowseMedia(
                    # title = preset,
                    # media_class = MEDIA_CLASS_MUSIC,
                    # media_content_id = index,
                    # media_content_type = MEDIA_TYPE_MUSIC,
                    # can_play = True,
                    # can_expand = False,
                # )
                # for index, preset in enumerate(self._trackq, start=1)
            # ]

            # root = BrowseMedia(
                # title=self._name + " " + source_media_name,
                # media_class = MEDIA_CLASS_DIRECTORY,
                # media_content_id = "root",
                # media_content_type = "listing",
                # can_play = False,
                # can_expand = True,
                # children = radio,
            # )

        # else:
            # root = BrowseMedia(
                # title=self._name + " " + source_media_name,
                # media_class = MEDIA_CLASS_DIRECTORY,
                # media_content_id = "root",
                # media_content_type = "listing",
                # can_play = False,
                # can_expand = False,
            # )

        # return root
#END
