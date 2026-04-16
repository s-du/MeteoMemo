
import requests_cache
import pandas as pd
from retry_requests import retry
from datetime import datetime, timedelta
import openmeteo_requests


def fetch_weather_data(self, latitude, longitude, datetime_utc):
    # Setup the Open-Meteo API client with cache and retry on error
    cache_session = requests_cache.CachedSession('.cache', expire_after=-1)
    retry_session = retry(cache_session, retries=5, backoff_factor=0.2)
    openmeteo = openmeteo_requests.Client(session=retry_session)

    # Convert UTC datetime to a date string for the API request
    flight_date_utc = datetime_utc.strftime("%Y-%m-%d")

    # Get the date 24 hours before the flight in UTC
    start_date_utc = (datetime_utc - timedelta(days=1)).strftime("%Y-%m-%d")

    # Define the API parameters (customize to include the needed weather variables)
    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": start_date_utc,
        "end_date": flight_date_utc,
        "hourly": ["temperature_2m", "relative_humidity_2m", "precipitation", "cloud_cover", "wind_speed_10m",
                   "wind_speed_100m"]
    }

    # Make the API request
    responses = openmeteo.weather_api(url, params=params)

    # Process the first response
    response = responses[0]

    # Extract hourly weather data
    hourly = response.Hourly()
    hourly_temperature_2m = hourly.Variables(0).ValuesAsNumpy()
    hourly_relative_humidity_2m = hourly.Variables(1).ValuesAsNumpy()
    hourly_precipitation = hourly.Variables(2).ValuesAsNumpy()
    hourly_cloud_cover = hourly.Variables(3).ValuesAsNumpy()
    hourly_wind = hourly.Variables(4).ValuesAsNumpy()

    # Create a DataFrame with the extracted data
    hourly_data = {
        "date": pd.date_range(
            start=pd.to_datetime(hourly.Time(), unit="s", utc=True),
            end=pd.to_datetime(hourly.TimeEnd(), unit="s", utc=True),
            freq=pd.Timedelta(seconds=hourly.Interval()),
            inclusive="left"
        ),
        "temperature_2m": hourly_temperature_2m,
        "relative_humidity_2m": hourly_relative_humidity_2m,
        "precipitation": hourly_precipitation,
        "cloud_cover": hourly_cloud_cover,
        "wind": hourly_wind
    }

    # Convert to Pandas DataFrame
    self.hourly_dataframe = pd.DataFrame(data=hourly_data)