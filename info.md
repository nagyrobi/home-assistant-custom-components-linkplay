# Linkplay-based speakers and sound devices

**The `linkplay` custom component for Home Assistant is deprecated, and unsupported! Starting with Home Assistant 2024.08 release, Linkplay chipset based media players are officially supported without the need of any custom component.**

To switch to the official LinkPlay integration starting from **2024.08**, follow these steps:
* Remove the current `linkplay` configuration from your configuration.yaml.
* Restart Home-Assistant.
* Delete the custom component through HACS or manually by deleting the `custom_components/linkplay` folder.
* Restart Home-Assistant again. Your players will be automatically discovered, a notification popup will inform you on this.

For any bugs, feature requests, or PRs, please use the official Home Assistant channels.
