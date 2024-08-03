# Linkplay-based speakers and sound devices

**NOTE: The `linkplay` custom component for Home Assistant is deprecated, and unsupported! Starting with Home Assistant 2024.08 release, Linkplay chipset based media players are officially supported without the need of any custom component.**

To switch to the official LinkPlay integration starting from **2024.08**, follow these steps:
* Remove the current `linkplay` configuration from your configuration.yaml.
* Restart Home-Assistant.
* Delete the custom component through HACS or manually by deleting the `custom_components/linkplay` folder.
* Restart Home-Assistant again. Your players will be automatically discovered, a notification popup will inform you on this.

For any bugs, feature requests, or PRs, please use the official Home Assistant channels.

---

**For Home Assistant versions before 2024.08:**

This component allows you to integrate control of audio devices based on Linkplay chipset into your [Home Assistant](http://www.home-assistant.io) smart home system. Originally developed by nicjo814, maintained by limych. This version mostly rewritten by nagyrobi. Read more about Linkplay at the bottom of this file.

Fully compatible with Mini Media Player card for Lovelace UI by kalkih, including speaker group management.

[Configuration details and documentation](https://github.com/nagyrobi/home-assistant-custom-components-linkplay#installation)


## Supported features:
- Configurable input sources list, to match choices in HA with the pyhsical inputs available on each device
- Configurable Icecast webradio streams as additional input sources
- Retrieval of current playing content metadata from Icecast webradio streams and filenames on directly attached USB sticks
- Retrieval of coverart from last.fm service based on current playing content metadata
- Multirooom in both WiFi-Direct and Router mode, using sonos-like standard 'join' and 'unjoin' service calls.
- Recall of music presets stored on the device
- Snapshot and restore state of the player for smooth usage with TTS
- Browsing and playing media files from the directly attached USB sticks through Lovelace UI
- Linkplay-chipset specific commands through HA service calls

## About Linkplay

Linkplay is a smart audio chipset and module manufacturer. Their various module types share the same functionality across the whole platform and alow for native audio content playback from lots of sources, including local inputs, local files, Bluetooth, DNLA, Airplay and also web-based services like Icecast, Spotify, Tune-In, Deezer, Tidal etc. They allow setting up multiroom listening environments using either self-created wireless connections or relying on existing network infrastructure, for longer distances coverage. For more information visit https://linkplay.com/.
There are quite a few manufacturers and devices that operate on the basis of Linkplay platform. For more information check out the [documentation](https://github.com/nagyrobi/home-assistant-custom-components-linkplay#about-linkplay)

## Component authors & contributors
    "@nicjo814",
    "@limych",
    "@nagyrobi"

[Support forum](https://community.home-assistant.io/t/linkplay-integration/33878/133)
