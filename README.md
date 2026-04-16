# MeteoMemo

A desktop app that reads GPS + timestamp metadata from a drone photo and fetches historical weather data for the 24 hours before the flight.

## Features

- Select an image (`.jpg`, `.jpeg`, `.png`, `.tif`, `.tiff`)
- Extract EXIF GPS coordinates and `DateTimeOriginal`
- Convert local Brussels flight time to UTC for API requests
- Fetch hourly weather data from Open-Meteo
- View weather as:
  - one combined plot
  - individual subplots for each variable

## Requirements

- Python 3.10+
- Dependencies listed in `requirements.txt`

## Installation

```bash
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

## Notes

- The selected image must contain EXIF GPS data and `DateTimeOriginal`.
- Weather data comes from the Open-Meteo Archive API.
