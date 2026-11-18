# Roborock Custom Map

This allows you to use the core Roborock integration with the [Xiaomi Map Card](https://github.com/PiotrMachowski/lovelace-xiaomi-vacuum-map-card)

> [!NOTE]
> This is a fork of [Python-roborock/RoborockCustomMap](https://github.com/Python-roborock/RoborockCustomMap),
> originally created by [@Lash-L](https://github.com/Lash-L). The main changes in this fork:
>
> - **Works with Home Assistant 2026.7 and later.** The core Roborock integration now sets up devices
>   in the background, which caused the original to fail with
>   `Failed setup, will retry: No Roborock entries loaded. Cannot start.` and never create entities.
>   Devices (and maps) that finish setting up later are now picked up automatically.
> - **Support for B01 Q10 devices** (e.g. Roborock Q10 S5), including calibration points and room outlines.
> - **Support for B01 Q7 devices**, including calibration points.
> - Map rotation works for all supported device types.

## Supported devices

Roborock devices speak one of several protocols, and support depends on the protocol rather than on
the model name. The core integration picks the protocol from what the Roborock cloud reports for
your device, and this integration follows it.

| Device type | Examples | Supported | Notes |
|---|---|---|---|
| **V1** | Most Roborock robot vacuums: S4, S5, S6, S7, S8, Q5, Q7 / Q7 Max / Q7+, Q8 Max, Qrevo, Saros, E-series, and others | ✅ | One map entity per saved map. Calibration, rooms and zones. |
| **B01 Q10** | Q10 family (model codes containing `ss`, e.g. `roborock.vacuum.ss07`) | ✅ | Live map plus one preview entity per saved map. Calibration and rooms (as bounding boxes). |
| **B01 Q7** | Newer Q7 family (model codes starting with `sc`) | ✅ | One entity for the current map. Calibration only; the device does not report room shapes. |
| **A01** | Dyad wet/dry vacuums, Zeo washers/dryers | ➖ | Not applicable, these devices have no map. |

> [!IMPORTANT]
> "Q7" is used for two different product lines. The older Q7 / Q7 Max / Q7+
> (`roborock.vacuum.a40` / `a38`) are **V1** devices. Only models whose code starts with `sc` are **B01 Q7**.

To find your device's type, open the device in **Settings → Devices & services → Roborock** and check
the model, or download the Roborock integration diagnostics.

### Home Assistant version

- V1: Home Assistant 2025.12.1 or later.
- B01 Q7: Home Assistant 2026.2 or later.
- B01 Q10: Home Assistant 2026.4 or later.

Home Assistant 2026.7 or later is recommended. For B01 devices, the map entity is only created when
the Roborock library bundled with your Home Assistant version can read the device's map.

### Limitations of B01 devices

- Q10 devices push their live map and expose saved maps through read-only previews. This integration
  creates one image and rotation entity per saved map, plus a `Selected map` entity for switching the
  active map before cleaning. B01 Q7 devices still expose only their current map.
- Q10 rooms are reported as rectangular bounding boxes, not exact outlines.
- Q7 maps have no room geometry, so the map card cannot generate room configs for them.
- The core integration does not fetch Q7 maps, so this integration requests the map itself: once at
  startup and every 10 minutes while idle. During cleaning the device pushes live map updates.
- B01 Q7 devices do not support zone or point cleaning from the map card.
- B01 Q10 zone cleaning requires a recent Home Assistant/python-roborock version
  with Q10 zoned-cleaning support. The map entity exposes the common Roborock
  millimetre coordinates expected by that action.

## Setup

1. Install the [Roborock Core Integration](https://my.home-assistant.io/redirect/config_flow_start?domain=roborock) and set it up
2. It is recommended that you first disable the Image entities within the core integration. Open each image entity, hit the gear icon, then trigger the toggle by enabled.
3. Install this integration(See the installing via HACS section below)
4. This integration works by piggybacking off of the Core integration, so the Core integration will do all the data updating to help prevent rate-limits. But that means that the core integration must be setup and loaded first. If you run into any issues, make sure the Roborock integration is loaded first, and then reload this one.
5. Setup the map card like normal! An example configuration would look like
```yaml
type: custom:xiaomi-vacuum-map-card
vacuum_platform: Roborock
entity: vacuum.s7
map_source:
  camera: image.s7_downstairs_full_custom
calibration_source:
  camera: true
```
For B01 (Q7 / Q10) devices the map entity is named `image.<device>_map_custom`, e.g.
`image.roborock_q10_s5_map_custom`.

6. You can hit Edit on the card and then Generate Room Configs to allow for cleaning of rooms. It might generate extra keys, so check the yaml and make sure there are no extra 'predefined_sections'

### Map rotation

If your map is displayed sideways or upside down, you can rotate the map directly in Home Assistant.

This integration provides a **Select entity per map** to control rotation:
- `select.<...>_rotation`
- Options: `0°`, `90°`, `180°`, `270°` (labels depend on your HA language)

This rotates **both**:
- the map image
- and the calibration points used by the Xiaomi Vacuum Map Card  
  (so rooms/zones and interactions stay aligned after rotation)

**How to use**
1. Go to **Settings → Devices & services → Roborock Custom Map**
2. Open the device/entities list
3. Find the `… rotation` select entity for your map and choose the correct rotation

No reload is required; the map updates immediately.

## Installation

### Installing via HACS
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=msc5&repository=RoborockCustomMap&category=integration)

or

1. Go to HACS->Integrations
1. Add this repo(https://github.com/msc5/RoborockCustomMap) into your HACS custom repositories
1. Search for Roborock Custom Map and Download it
1. Restart your HomeAssistant
1. Go to Settings->Devices & Services
1. Add the Roborock Custom Map integration

If you already have the original integration installed through HACS, remove it first, then add this
repository.

### Alternative/optional

Once you set up this integration, you can generate a static config in the lovelace card, and theoretically, you should be able to use that code with your Roborock CORE integration. However, it wont stay up to date if the map calibrations change significantly, or rooms change. So I'd only do this when I was sure everything was good!

## Support the original author

If you would like to support the original author of this integration, you can do so here:

[![BuyMeCoffee][buymecoffeebadge]][buymecoffee]

[![PaypalMe][paypalmebadge]][paypalme]



[buymecoffee]: https://www.buymeacoffee.com/LashL
[buymecoffeebadge]: https://img.shields.io/badge/buy%20me%20a%20coffee-donate-yellow.svg?style=for-the-badge
[paypalme]: https://paypal.me/LLashley304
[paypalmebadge]: https://cdn.rawgit.com/twolfson/paypal-github-button/1.0.0/dist/button.svg
