# Auto Equalizer

A small Windows-friendly Python app for making a personal Equalizer APO / Peace preset from a simple hearing-threshold test.

It plays one frequency at a time, gradually increasing level until you press Space or click **I can hear it**. The app turns those thresholds into a conservative 14-band EQ curve, lets you adjust it, and exports an Equalizer APO text preset that Peace can import.

The 14 bands are: `40`, `63`, `100`, `160`, `250`, `400`, `630`, `1k`, `1.6k`, `2.5k`, `4k`, `6.3k`, `10k`, and `16k`.

The graph also shows a realtime 14-band output meter using Windows WASAPI loopback capture. If Equalizer APO is active on the playback device, this meter reflects the equalized audio being sent to that output.

## Important safety note

This is not a medical hearing test and it does not diagnose hearing loss. Keep your Windows volume comfortable before starting, use headphones/speakers you normally use, and stop immediately if a tone feels loud, painful, or irritating. The app caps generated boosts by default, but you are still in charge of listening level.

## Install

Python 3.10 or newer is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Run

```powershell
python auto_equalizer.py
```

## Using The Preset In Peace

1. Run the hearing test in the app.
2. Click **Export APO Preset**.
3. Save the preset somewhere convenient, or directly in:

   `C:\Program Files\EqualizerAPO\config`

4. In Peace, use its import option for Equalizer APO filters and choose the exported `.txt` file.

The exported file is standard Equalizer APO syntax:

```text
Preamp: -6.0 dB
GraphicEQ: 40 2.0; 63 1.5; 100 0.0; 160 0.0
```

Peace manages its own `peace.txt`, so this app exports a separate preset instead of modifying Peace's live file.

## Applying To Equalizer APO

Click **Apply To APO** when you are happy with the current curve. The app backs up and then rewrites:

`C:\Program Files\EqualizerAPO\config\config.txt`

The active `config.txt` becomes the generated Auto Equalizer preset:

```text
Preamp: -6.0 dB
GraphicEQ: 40 2.0; 63 1.5; 100 0.0; 160 0.0
```

Hearing-test results stay local until you click **Apply To APO**. This keeps the hearing test from changing the APO curve while it is measuring.

Manual slider changes update Equalizer APO automatically after a short debounce, using the same `GraphicEQ` preset file.

If Windows blocks the write, run the app as Administrator or use **Export APO Preset** and import it from Peace.

Peace may overwrite `config.txt` later if you change settings inside Peace.

## Saved Configuration

The app saves the last thresholds and slider curve in `auto_equalizer_state.json` and loads them on startup.

Use **Save Settings** and **Load Settings** to manage named profiles. Profiles are stored as JSON files in the `profiles` folder beside the app.
