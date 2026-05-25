# Home Assistant — Shutter Automations

Automation package for smart shutter control: temperature-based opening,
sunrise-aware timing, holiday-aware waking times, staggered evening close,
and optional night ventilation.

## Prerequisites

Before installing the package, set up these integrations in HA:

1. **OpenWeatherMap** — Settings → Integrations → Add → OpenWeatherMap
   - Enter your API key
   - Set name to **owm** (so the entity is `weather.owm`)

2. **MQTT** — must already be configured (the Pico uses it for cover entities)

3. **Cover entities** — boot the Pico so it publishes MQTT Discovery and
   creates the cover entities. Find their entity IDs under:
   Settings → Devices & Services → Entities → filter by "shutter"

## Installation

1. **Copy the package file** into your HA config directory:

   ```
   <ha-config>/
   └── packages/
       └── shutters.yaml
   ```

2. **Enable packages** in `configuration.yaml` (add if not already present):

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

3. **Update the cover entity IDs** in `shutters.yaml`.
   Search for `# CONFIGURE` — there are four places:

   | Automation | Entities |
   |------------|----------|
   | Morning open | all shutters (0, 1, 2) |
   | Evening close (main) | shutters 0 and 1 |
   | Evening close (delayed) | shutter 2 |
   | Night ventilation | TBD — update when decided |

4. **Restart Home Assistant** — Settings → System → Restart

## First-time setup in the UI

After restart, configure the helpers under Settings → Helpers or directly
in the Dashboard:

| Helper | Suggested value |
|--------|----------------|
| Waking time (weekday) | 06:30 |
| Waking time (weekend/holiday) | 08:00 |
| Night vent min temperature threshold | 20.0 °C |

## How it works

### Morning open
Runs at 04:00. Fetches today's OWM daily forecast, calculates the opening
position from the maximum temperature, then waits until the later of
sunrise or the configured waking time before opening the shutters.

**Position formula** (linear between 25 °C and 35 °C):

| Max temp | Opening position |
|----------|-----------------|
| ≤ 25 °C | 100 % |
| 30 °C | 65 % |
| ≥ 35 °C | 30 % |

### Waking time selection
- Weekdays: `Waking time (weekday)`
- Weekends, Hungarian public holidays, or days with **Manual holiday today** turned on: `Waking time (weekend/holiday)`

Turn on **Manual holiday today** in the UI the evening before a day off.
It resets automatically at midnight.

### Evening close
- Shutters 0 and 1 close at **sunset**
- Shutter 2 closes at **sunset + 20 minutes**

### Night ventilation
At **22:30**, if tonight's forecast minimum temperature exceeds the
**Night vent min temperature threshold**, the designated shutters are
raised to **55%**.

The flag (`Night ventilation active`) is set each morning at 04:00 and
can also be toggled manually in the UI.

## Customisation

| What to change | Where |
|----------------|-------|
| Opening position formula | `shutters_morning_open` automation, `position` variable |
| Night ventilation time | `shutters_night_vent` automation trigger |
| Night ventilation position | `shutters_night_vent` automation, `position` value |
| Delayed close offset | `shutters_evening_close_delayed` trigger offset |
| Night vent threshold | `Night vent min temperature threshold` helper in UI |
