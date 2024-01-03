import concurrent.futures
import logging
import math
import pathlib

import geopandas
import pandas as pd
import requests
from pyproj import Transformer
from shapely.geometry import Polygon
from tqdm import tqdm

logger = logging.getLogger(__name__)


URL_LIZARD = "https://vitens.lizard.net/api/v4/"


def check_status_obs(metadata, timeseries):
    """
    checks if a monitoring tube is still active

    Parameters
    ----------
    metadata : dict
        metadata of the monitoring tube
    timeseries : pandas DataFrame
        timeseries of the monitoring well

    Returns
    -------
    metadata DataFrame including the status of the monitoring well

    """
    if timeseries.empty:
        metadata["status"] = "no timeseries available"
        return metadata

    last_measurement_date = timeseries.last_valid_index()
    today = pd.to_datetime("today").normalize()

    if today - last_measurement_date < pd.Timedelta(days=180):
        metadata["status"] = "active"

    else:
        metadata["status"] = "inactive"

    return metadata


def extent_to_wgs84_polygon(coordinates):
    """
    Translates a list of coordinates (xmin,xmax, ymin, ymax) to a polygon with
    coordinate system WGS84

    Parameters
    ----------
    coordinates : lst or tuple
        list of the modelextent in epsg 28992 within which the observations
        are collected.

    Returns
    -------
    polygon of the modelextent with coordinate system WGS84

    """
    transformer = Transformer.from_crs("EPSG:28992", "WGS84")

    lon_min, lat_min = transformer.transform(coordinates[0], coordinates[2])
    lon_max, lat_max = transformer.transform(coordinates[1], coordinates[3])

    poly_T = Polygon(
        [(lat_min, lon_min), (lat_max, lon_min), (lat_max, lon_max), (lat_min, lon_max)]
    )

    return poly_T


def translate_flag(timeseries):
    """
    Translates Vitens Lizard flags from interter to text

    Parameters
    ----------
    timeseries : pandas.DataFrame
    timeseries of a monitoring well with flags

    Returns
    -------
    timeseries : pandas.DataFrame
        timeseries with translated quality flags

    """
    translate_dic = {
        0: "betrouwbaar",
        1: "betrouwbaar",
        3: "onbeslist",
        4: "onbeslist",
        6: "onbetrouwbaar",
        7: "onbetrouwbaar",
        99: "onongevalideerd",
        -99: "verwijderd",
    }
    timeseries["flag"] = timeseries["flag"].replace(translate_dic)

    return timeseries


def get_metadata_mw_from_code(code):
    """
    extracts the Groundwater Station parameters from a monitoring well based
    on the code of the monitoring well

    Parameters
    ----------
    code : str
        code of the monitoring well

    Raises
    ------
    ValueError
        if code of the monitoring well is not known

    Returns
    -------
    groundwaterstation_metadata : dict
        dictionary with all available metadata of the monitoring well and its filters

    """
    lizard_GWS_endpoint = f"{URL_LIZARD}groundwaterstations/"
    url_groundwaterstation_code = f"{lizard_GWS_endpoint}?code={code}"

    try:
        groundwaterstation_metadata = requests.get(url_groundwaterstation_code).json()[
            "results"
        ][0]

    except IndexError:
        raise ValueError(f"Code {code} is invalid")

    return groundwaterstation_metadata


def _prepare_API_input(nr_pages, url_groundwater):
    """
    get API data pages within the defined extent

    Parameters
    ----------
    nr_pages : int
        number of the pages on which the information is stored
    url_groundwater : str
        location of the used API to extract the data

    Returns
    -------
    urls : list
        list of the page number and the corresponding url

    """
    urls = []
    for page in range(nr_pages):
        true_page = (
            page + 1
        )  # Het echte paginanummer wordt aan de import thread gekoppeld
        urls = [url_groundwater + "&page={}".format(true_page)]
    return urls


def _download(url, timeout=1800):
    """
    Function to download the data from the API using the ThreadPoolExecutor

    Parameters
    ----------
    url : str
        url of an API page
    timeout : int, optional
        number of seconds to wait before terminating request

    Returns
    -------
    dictionary with timeseries data

    """
    data = requests.get(url=url, timeout=timeout)
    data = data.json()["results"]

    return data


def get_metadata_tube(metadata_mw, tube_nr):
    """
    extract the metadata for a specific tube from the monitoring well metadata

    Parameters
    ----------
    metadata_mw : dict
        dictionary with all available metadata of the monitoring well and all its
        filters
    tube_nr : int or None
        select metadata from a specific tube number

    Raises
    ------
    ValueError
        if code of the monitoring well is invalid.

    Returns
    -------
    dictionary with metadata of a specific tube
    """

    if tube_nr is None:
        tube_nr = 1

    metadata = {
        "monitoring_well": metadata_mw["name"],
        "ground_level": metadata_mw["surface_level"],
        "source": "lizard",
        "unit": "m NAP",
        "metadata_available": True,
        "status": None,
    }

    for metadata_tube in metadata_mw["filters"]:
        if metadata_tube["code"].endswith(str(tube_nr)):
            break
    else:
        raise ValueError(f"{metadata_mw['name']} doesn't have a tube number {tube_nr}")

    metadata.update(
        {
            "tube_nr": tube_nr,
            "name": metadata_tube["code"].replace("-", ""),
            "tube_top": metadata_tube["top_level"],
            "screen_top": metadata_tube["filter_top_level"],
            "screen_bottom": metadata_tube["filter_bottom_level"],
        }
    )

    lon, lat, _ = metadata_mw["geometry"]["coordinates"]
    transformer = Transformer.from_crs("WGS84", "EPSG:28992")
    metadata["x"], metadata["y"] = transformer.transform(lat, lon)

    if not metadata_tube["timeseries"]:
        metadata["timeseries_type"] = None
    else:
        for series in metadata_tube["timeseries"]:
            series_info = requests.get(series).json()
            if series_info["name"] == "WNS9040.hand":
                metadata["uuid_hand"] = series_info["uuid"]
                metadata["start_hand"] = series_info["start"]
            elif series_info["name"] == "WNS9040":
                metadata["uuid_diver"] = series_info["uuid"]
                metadata["start_diver"] = series_info["start"]

        if (metadata.get("start_hand") is None) and (
            metadata.get("start_diver") is None
        ):
            metadata["timeseries_type"] = None
        elif (metadata.get("start_hand") is not None) and (
            metadata.get("start_diver") is not None
        ):
            metadata["timeseries_type"] = "diver + hand"
        elif metadata.get("start_hand") is None:
            metadata["timeseries_type"] = "diver"
        elif metadata.get("start_diver") is None:
            metadata["timeseries_type"] = "hand"

    return metadata


def get_timeseries_uuid(uuid, code, tube_nr, tmin, tmax, page_size=100000):
    """
    Get the time series (hand or diver) using the uuid.

    ----------
    uuid : str
        Universally Unique Identifier of the tube and type of time series.
    code : str
        code or name of the monitoring well
    tube_nr : int
        select specific tube number
    tmin : str YYYY-m-d
        start of the observations, by default the entire serie is returned
    tmax : int YYYY-m-d
        end of the observations, by default the entire serie is returned
    page_size : int, optional
        Query parameter which can extend the response size. The default is 100000.

    Returns
    -------
    pandas DataFrame with the timeseries of the monitoring well

    """

    url_timeseries = URL_LIZARD + "timeseries/{}".format(uuid)

    if tmin is not None:
        tmin = pd.to_datetime(tmin).isoformat("T")

    if tmax is not None:
        tmax = pd.to_datetime(tmax).isoformat("T")

    params = {"start": tmin, "end": tmax, "page_size": page_size}
    url = url_timeseries + "/events/"

    time_series_events = requests.get(url=url, params=params).json()["results"]
    time_series_df = pd.DataFrame(time_series_events)

    if time_series_df.empty:
        return pd.DataFrame()

    else:
        time_series_df = translate_flag(time_series_df)

        timeseries_sel = time_series_df.loc[:, ["time", "value", "flag", "comment"]]
        timeseries_sel["time"] = pd.to_datetime(
            timeseries_sel["time"], format="%Y-%m-%dT%H:%M:%SZ", errors="coerce"
        ) + pd.DateOffset(hours=1)

        timeseries_sel = timeseries_sel[~timeseries_sel["time"].isnull()]

        timeseries_sel.set_index("time", inplace=True)
        timeseries_sel.index.rename("peil_datum_tijd", inplace=True)
        # timeseries_sel.dropna(inplace=True)

    return timeseries_sel


def _merge_timeseries(hand_measurements, diver_measurements):
    """
    merges the timeseries of the hand and diver measurements into one timeserie

    Parameters
    ----------
    hand_measurements : DataFrame
        DataFrame containing the hand measurements of the monitoring well
    diver_measurements : DataFrame
        DataFrame containing the Diver measurements of the monitoring well

    Returns
    -------
    DataFrame where hand and diver measurements are merged in one timeseries

    """
    if hand_measurements.empty and diver_measurements.empty:
        measurements = pd.DataFrame()

    elif diver_measurements.first_valid_index() is None:
        measurements = hand_measurements
        print(
            "no diver measuremets available for {}".format(
                hand_measurements.iloc[0]["name"]
            )
        )

    else:
        hand_measurements_sel = hand_measurements.loc[
            hand_measurements.index < diver_measurements.first_valid_index()
        ]
        measurements = pd.concat([hand_measurements_sel, diver_measurements], axis=0)

    return measurements


def _combine_timeseries(hand_measurements, diver_measurements):
    """
    combines the timeseries of the hand and diver measurements into one DataFrame

    Parameters
    ----------
    hand_measurements : DataFrame
        DataFrame containing the hand measurements of the monitoring well
    diver_measurements : DataFrame
        DataFrame containing the Diver measurements of the monitoring well

    Returns
    -------
    a combined DataFrame with both hand, and diver measurements
        DESCRIPTION.

    """
    hand_measurements.rename(
        columns={"value": "value_hand", "flag": "flag_hand"}, inplace=True
    )
    diver_measurements.rename(
        columns={"value": "value_diver", "flag": "flag_diver"}, inplace=True
    )

    measurements = pd.concat([hand_measurements, diver_measurements], axis=1)
    measurements = measurements.loc[
        :, ["value_hand", "value_diver", "flag_hand", "flag_diver"]
    ]
    measurements.loc[:, "name"] = hand_measurements.loc[:, "name"][0]
    measurements.loc[:, "filter_nr"] = hand_measurements.loc[:, "filter_nr"][0]

    return measurements


def get_timeseries_tube(tube_metadata, tmin, tmax, type_timeseries):
    """
    extracts multiple timeseries (hand and/or diver measurements) for a specific
    tube using the Lizard API.

    Parameters
    ----------
    tube_metadata : dict
        metadata of a tube
    tmin : str YYYY-m-d, optional
        start of the observations, by default the entire serie is returned
    tmax : Ttr YYYY-m-d, optional
        end of the observations, by default the entire serie is returned
    type_timeseries : str, optional
        type of timeseries to;
            hand: returns only hand measurements
            diver: returns only diver measurements
            merge: the hand and diver measurements into one time series (default)
            combine: keeps hand and diver measurements separeted
        The default is merge.

    Returns
    -------
    measurements : pandas DataFrame
        timeseries of the monitoring well
    metadata_df : dict
        metadata of the monitoring well

    """
    if tube_metadata["timeseries_type"] is None:
        return pd.DataFrame(), tube_metadata

    if type_timeseries in ["hand", "merge", "combine"]:
        if "hand" in tube_metadata["timeseries_type"]:
            hand_measurements = get_timeseries_uuid(
                tube_metadata.pop("uuid_hand"),
                tube_metadata["name"],
                tube_metadata["tube_nr"],
                tmin,
                tmax,
            )
        else:
            hand_measurements = None

    if type_timeseries in ["diver", "merge", "combine"]:
        if "diver" in tube_metadata["timeseries_type"]:
            diver_measurements = get_timeseries_uuid(
                tube_metadata.pop("uuid_diver"),
                tube_metadata["name"],
                tube_metadata["tube_nr"],
                tmin,
                tmax,
            )
        else:
            diver_measurements = None

    if type_timeseries == "hand" and hand_measurements is not None:
        measurements = hand_measurements
    elif type_timeseries == "diver" and diver_measurements is not None:
        measurements = diver_measurements
    elif type_timeseries in ["merge", "combine"]:
        if (hand_measurements is not None) and (diver_measurements is not None):
            if type_timeseries == "merge":
                measurements = _merge_timeseries(hand_measurements, diver_measurements)
            elif type_timeseries == "combine":
                measurements = _combine_timeseries(
                    hand_measurements, diver_measurements
                )
        elif hand_measurements is not None:
            measurements = hand_measurements
        elif diver_measurements is not None:
            measurements = diver_measurements

    return measurements, tube_metadata


def get_lizard_groundwater(
    code,
    tube_nr=None,
    tmin=None,
    tmax=None,
    type_timeseries="merge",
    only_metadata=False,
):
    """
    extracts the metadata and timeseries of an observation well from a
    LIZARD-API based on the code of a monitoring well

    Parameters
    ----------
    code : str
        code of the measuring well, e.g. '27B-0444'
    tube_nr : int, optional
        select specific tube top
        Default selects tube_nr = 1
    tmin : str YYYY-m-d, optional
        start of the observations, by default the entire serie is returned
    tmax : Ttr YYYY-m-d, optional
        end of the observations, by default the entire serie is returned
    type_timeseries : str, optional
        hand: returns only hand measurements
        diver: returns only diver measurements
        merge: the hand and diver measurements into one time series (merge; default) or
        combine: keeps hand and diver measurements separated
        The default is merge.
    only_metadata : bool, optional
        if True only metadata is returned and no time series data. The
        default is False.

    Returns
    -------
    returns a DataFrame with metadata and timeseries
    """

    groundwaterstation_metadata = get_metadata_mw_from_code(code)

    tube_metadata = get_metadata_tube(groundwaterstation_metadata, tube_nr)

    if only_metadata:
        return pd.DataFrame(), tube_metadata

    measurements, tube_metadata = get_timeseries_tube(
        tube_metadata, tmin, tmax, type_timeseries
    )
    tube_metadata = check_status_obs(tube_metadata, measurements)

    return measurements, tube_metadata


def get_obs_list_from_codes(
    codes,
    ObsClass,
    tube_nr="all",
    tmin=None,
    tmax=None,
    type_timeseries="merge",
    only_metadata=False,
):
    """
    get all observations from a list of codes of the monitoring wells and a
    list of tube numbers

    Parameters
    ----------
    codes : lst of str or str
        codes of the monitoring wells
    ObsClass : type
        class of the observations, e.g. GroundwaterObs
    tube_nr : lst of str
        list of tube numbers of the monitoring wells that should be selected.
        By default 'all' available tubes are selected.
    tmin : str YYYY-m-d, optional
        start of the observations, by default the entire serie is returned
    tmax : Ttr YYYY-m-d, optional
        end of the observations, by default the entire serie is returned
    type_timeseries : str, optional
        hand: returns only hand measurements
        diver: returns only diver measurements
        merge: the hand and diver measurements into one time series (merge; default) or
        combine: keeps hand and diver measurements separeted
        The default is merge.
    only_metadata : bool, optional
        if True only metadata is returned and no time series data. The
        default is False.


    Returns
    -------
    ObsCollection
        ObsCollection DataFrame with the 'obs' column

    """

    if isinstance(codes, str):
        codes = [codes]

    if not hasattr(codes, "__iter__"):
        raise TypeError("argument 'codes' should be an iterable")

    l = []
    for code in codes:
        groundwaterstation_metadata = get_metadata_mw_from_code(code)
        if tube_nr == "all":
            for metadata_tube in groundwaterstation_metadata["filters"]:
                tube_nr = int(metadata_tube["code"][-3:])
                o = ObsClass.from_lizard(
                    code,
                    tube_nr,
                    tmin,
                    tmax,
                    type_timeseries,
                    only_metadata=only_metadata,
                )
                l.append(o)
        else:
            o = ObsClass.from_lizard(
                code, tube_nr, tmin, tmax, type_timeseries, only_metadata=only_metadata
            )
            l.append(o)

    return l


def get_obs_list_from_extent(
    extent,
    ObsClass,
    tube_nr="all",
    tmin=None,
    tmax=None,
    type_timeseries="merge",
    only_metadata=False,
    page_size=100,
    nr_threads=10,
):
    """
    get all observations within a specified extent
    Parameters
    ----------
    extent : list or a shapefile
        get groundwater monitoring wells wihtin this extent [xmin, xmax, ymin, ymax]
        or within a predefined Polygon from a shapefile
    ObsClass : type
        class of the observations, e.g. GroundwaterObs
    tube_nr : lst of str
        list of tube numbers of the monitoring wells that should be selected.
        By default 'all' available tubes are selected.
    tmin : str YYYY-m-d, optional
        start of the observations, by default the entire serie is returned
    tmax : Ttr YYYY-m-d, optional
        end of the observations, by default the entire serie is returned
    type_timeseries : str, optional
        merge: the hand and diver measurements into one time series (merge; default) or
        combine: keeps hand and diver measurements separeted
        The default is merge.
    only_metadata : bool, optional
        if True only metadata is returned and no time series data. The
        default is False.


    Returns
    -------
    obs_col : TYPE
        ObsCollection DataFrame with the 'obs' column

    """

    if isinstance(extent, (list, tuple)):
        polygon_T = extent_to_wgs84_polygon(extent)

    elif isinstance(extent, str) or isinstance(extent, pathlib.PurePath):
        polygon = geopandas.read_file(extent)
        polygon_T = polygon.to_crs("WGS84", "EPSG:28992").loc[0, "geometry"]
    else:
        raise TypeError("Extent should be a shapefile or a list of coordinates")

    lizard_GWS_endpoint = f"{URL_LIZARD}groundwaterstations/"
    url_groundwaterstation_extent = (
        f"{lizard_GWS_endpoint}?geometry__within={polygon_T}&page_size={page_size}"
    )

    groundwaterstation_data = requests.get(url_groundwaterstation_extent).json()
    nr_results = groundwaterstation_data["count"]
    nr_pages = math.ceil(nr_results / page_size)

    print("Number of monitoring wells: {}".format(nr_results))
    print("Number of pages: {}".format(nr_pages))

    if nr_threads > nr_pages:
        nr_threads = nr_pages

    urls = _prepare_API_input(nr_pages, url_groundwaterstation_extent)

    arg_tuple = (ObsClass, tube_nr, tmin, tmax, type_timeseries, only_metadata)
    codes = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=nr_threads) as executor:
        for result in tqdm(executor.map(_download, urls), total=nr_pages, desc="Page"):
            codes += [(d["code"],) + arg_tuple for d in result]

    obs_list = []
    with concurrent.futures.ThreadPoolExecutor() as executor:
        for obs_list_mw in tqdm(
            executor.map(lambda args: get_obs_list_from_codes(*args), codes),
            total=len(codes),
            desc="monitoring well",
        ):
            obs_list += obs_list_mw

    return obs_list
