# Copyright 2013-2023 Aerospike, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

#############################################################################################################
# Functions common to multiple modes (online cluster / offline cluster / collectinfo-analyser / log-analyser)
#############################################################################################################

import asyncio
import datetime
import json
import logging
import operator
import os
import platform
from typing import (
    Any,
    Literal,
    Optional,
    TypedDict,
    Union,
    Callable,
)
from typing_extensions import NotRequired
import distro
import socket
import time
import urllib.request
import urllib.error
import urllib.parse
import aiohttp
import zipfile
from collections import OrderedDict
from dateutil import parser as date_parser

from lib.utils import constants, file_size, util, version, data
from lib.utils.types import NamespaceDict, NodeDict
from lib.view import terminal

logger = logging.getLogger("asadm")

########## Feature ##########

comp_ops = {
    ">": operator.gt,
    "<": operator.lt,
    ">=": operator.ge,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

CompareValue = str | int | bool
CompareCallable = Callable[[Any, Any], bool]
CheckCallback = Callable[
    [dict[str, Any], tuple[str, ...], CompareCallable, str | int | bool], bool
]


def _check_value(
    data: dict[str, Any],
    keys: tuple[str],
    op: Callable[[Any, Any], bool],
    value: int | str | bool,
):
    """
    Function takes dictionary, and keys to compare.
    Returns boolean to indicate value for key is satisfying operation over value or not.
    """

    if not keys:
        return True

    if not data:
        return False

    for key in keys:
        k = key
        dv = 0
        type_check = int

        if isinstance(value, str):
            dv = None
            type_check = str
        if isinstance(value, bool):
            dv = False
            type_check = bool

        fetched_value = util.get_value_from_dict(data, k, dv, type_check)

        if fetched_value is None:
            continue

        if op(fetched_value, value):
            return True

    return False


def _check_feature_by_keys(
    service_data: dict[str, Any],
    service_keys: tuple[str, ...],
    op: CompareCallable,
    value: CompareValue,
) -> bool:
    """
    Function takes dictionary of service data, service keys, dictionary of namespace data and namespace keys.
    Returns boolean to indicate service key in service data or namespace key in namespace data has non-zero value or not.
    """

    if service_data and not isinstance(service_data, Exception) and service_keys:
        if _check_value(service_data, service_keys, op, value):
            return True

    return False


def _check_nested_feature_by_keys(
    ns_data: dict[str, Any],
    ns_keys: tuple[str, ...],
    op: CompareCallable,
    value: CompareValue,
) -> bool:
    if ns_data and ns_keys:
        for _, nsval in ns_data.items():
            if not nsval or isinstance(nsval, Exception):
                continue
            if _check_value(nsval, ns_keys, op, value):
                return True

    return False


class FieldCheck:
    def __init__(
        self,
        source: str,
        check_func: CheckCallback,
        fields: tuple[str, ...],
        compare_op: CompareCallable = operator.gt,
        compare_val: CompareValue = 0,
    ):
        """A class for describing the presence of a feature based on config and stat
        comparisons.

        Args:
            source (str): The key to use for the sources dict passed to check()
            check_func (CheckCallback): A callback that determines whether the feature is present.
            fields (tuple[str, ...]): All the fields to check.
            compare_op (CompareCallable, optional): A function that tells how we should compare the field value to compare_val. Defaults to operator.gt.
            compare_val (CompareValue, optional): The value to use in the comparison to each field value. Defaults to 0.
        """
        self._source = source
        self._fields: tuple[str, ...] = fields
        self._check_func = check_func
        self._compare_op = compare_op
        self._compare_val = compare_val

    def check(self, sources: dict[str, Any]) -> bool:
        if self._source not in sources:
            return False

        return self._check_func(
            sources[self._source], self._fields, self._compare_op, self._compare_val
        )


class GlobalFieldCheck(FieldCheck):
    def __init__(
        self,
        source: str,
        fields: tuple[str, ...],
        compare_op: CompareCallable = operator.gt,
        compare_val: CompareValue = 0,
    ):
        super().__init__(
            source, _check_feature_by_keys, fields, compare_op, compare_val
        )


class NestedFieldCheck(FieldCheck):
    def __init__(
        self,
        source: str,
        fields: tuple[str, ...],
        compare_op: CompareCallable = operator.gt,
        compare_val: CompareValue = 0,
    ):
        super().__init__(
            source, _check_nested_feature_by_keys, fields, compare_op, compare_val
        )


class ServiceFieldCheck(GlobalFieldCheck):
    pass


class SecurityFieldCheck(GlobalFieldCheck):
    pass


class NamespacesFieldCheck(NestedFieldCheck):
    pass


class XdrDcsFieldCheck(NestedFieldCheck):
    pass


class FeatureCheck:
    def __init__(self, display_key: str, *field_checks: FieldCheck) -> None:
        """A collection of checks that indicate a feature is in use.

        Args:
            display_key (str): The value to be displayed when a feature is determined to be in use.
            field_checks (tuple[FieldCheck, ...]): All the possible checks that would indicate that this
            feature is in use. Sometimes different metrics may indicate an feature is in use like
            errors, timeout, success. It also might be the case that a stat or config no longer exists
            and having multiple checks allows for backwards compatibility.
        """
        self.display_key = display_key
        self._field_checks = field_checks

    def check(self, sources: dict[str, Any]) -> bool:
        if not sources:
            return False

        for field_check in self._field_checks:
            if field_check.check(sources):
                return True

        return False

    def __str__(self):
        for field_check in self._field_checks:
            return True


# Please update the following page when feature checks are updated:
# https://aerospike.atlassian.net/wiki/spaces/PRODUCT/pages/2998730862/Feature+Definitions+for+summary+and+features+commands
feature_checks = (
    FeatureCheck(
        "KVS",
        ServiceFieldCheck("service", ("stat_read_reqs", "stat_write_reqs")),
        NamespacesFieldCheck(
            "namespaces",
            (
                "client_read_error",
                "client_read_success",
                "client_write_error",
                "client_write_success",
            ),
        ),
    ),
    FeatureCheck(
        "UDF",
        ServiceFieldCheck("service", ("udf_read_reqs", "udf_write_reqs")),
        NamespacesFieldCheck(
            "namespaces",
            ("client_udf_complete", "client_udf_error"),
        ),
    ),
    FeatureCheck(
        "Batch",
        ServiceFieldCheck(
            "service",
            ("batch_initiate", "batch_index_initiate"),
        ),
    ),
    FeatureCheck(
        "Scan",
        ServiceFieldCheck(
            "service",
            (
                "tscan_initiate",
                "basic_scans_succeeded",
                "basic_scans_failed",
                "aggr_scans_succeeded",
                "aggr_scans_failed",
                "udf_bg_scans_succeeded",
                "udf_bg_scans_failed",
            ),
        ),
        NamespacesFieldCheck(
            "namespaces",
            (
                "scan_basic_complete",
                "scan_basic_error",
                "scan_aggr_complete",
                "scan_aggr_error",
                "scan_udf_bg_complete",
                "scan_udf_bg_error",
            ),
        ),
    ),
    FeatureCheck(
        "SIndex",
        ServiceFieldCheck("service", ("sindex-used-bytes-memory",)),
        NamespacesFieldCheck("namespaces", ("memory_used_sindex_bytes",)),
    ),
    FeatureCheck(
        "Query",
        ServiceFieldCheck("service", ("query_reqs", "query_success")),
        NamespacesFieldCheck(
            "namespaces",
            ("query_reqs", "query_success"),
        ),
    ),
    FeatureCheck(
        "PIndex Query",
        NamespacesFieldCheck(
            "namespaces",
            (
                "pi_query_long_basic_complete",
                "pi_query_long_basic_error",
                "pi_query_short_basic_complete",
                "pi_query_short_basic_error",
                "pi_query_aggr_complete",
                "pi_query_aggr_error",
                "pi_query_udf_bg_complete",
                "pi_query_udf_bg_error",
                "pi_query_ops_bg_complete",
                "pi_query_ops_bg_error",
            ),
        ),
    ),
    FeatureCheck(
        "SIndex Query",
        NamespacesFieldCheck(
            "namespaces",
            (
                "si_query_long_basic_complete",
                "si_query_long_basic_error",
                "si_query_short_basic_complete",
                "si_query_short_basic_error",
                "si_query_aggr_complete",
                "si_query_aggr_error",
                "si_query_udf_bg_complete",
                "si_query_udf_bg_error",
                "si_query_ops_bg_complete",
                "si_query_ops_bg_error",
            ),
        ),
    ),
    FeatureCheck(
        "Aggregation",
        ServiceFieldCheck(
            "service",
            (
                "query_aggr_success",
                "query_aggr_error",
                "query_aggr_abort",
                "query_agg",
            ),
        ),
        NamespacesFieldCheck(
            "namespaces",
            (
                "query_aggr_success",
                "query_aggr_error",
                "query_aggr_abort",
                "query_agg",
            ),
        ),
    ),
    FeatureCheck(
        "LDT",
        ServiceFieldCheck(
            "service",
            (
                "sub-records",
                "ldt-writes",
                "ldt-reads",
                "ldt-deletes",
                "ldt_writes",
                "ldt_reads",
                "ldt_deletes",
                "sub_objects",
            ),
        ),
        NamespacesFieldCheck(
            "namespaces",
            (
                "ldt-writes",
                "ldt-reads",
                "ldt-deletes",
                "ldt_writes",
                "ldt_reads",
                "ldt_deletes",
            ),
        ),
    ),
    FeatureCheck(
        "XDR Source",
        ServiceFieldCheck(
            "service",
            ("stat_read_reqs_xdr", "xdr_read_success", "xdr_read_error"),
        ),
        XdrDcsFieldCheck("xdr_dc", ("success",)),
    ),
    FeatureCheck(
        "XDR Destination",
        ServiceFieldCheck("service", ("stat_write_reqs_xdr",)),
        NamespacesFieldCheck(
            "namespaces",
            ("xdr_write_success", "xdr_client_write_success"),
        ),
    ),
    FeatureCheck(
        "Rack-aware",
        ServiceFieldCheck("service", ("self-group-id",)),
        NamespacesFieldCheck("namespaces", ("rack-id",)),
    ),
    FeatureCheck(
        "Security",
        # Before asadm 2.7 the node.info_get_config call to get service configs also included security configs.
        # Security configs are now returned on their own.
        # This is here to handle the case where an older collectinfo is loaded and they are still included as one.
        ServiceFieldCheck(
            "service",
            ("enable-security",),
            comp_ops["=="],
            "true",
        ),
        SecurityFieldCheck(
            "security",
            ("enable-security",),
            comp_ops["=="],
            "true",
        ),
    ),
    FeatureCheck(
        "TLS (Heartbeat)",
        ServiceFieldCheck(
            "service",
            ("heartbeat.mesh-seed-address-port",),
        ),
    ),
    FeatureCheck(
        "TLS (Fabric)",
        ServiceFieldCheck(
            "service",
            ("fabric.tls-port",),
        ),
    ),
    FeatureCheck(
        "TLS (Service)",
        ServiceFieldCheck(
            "service",
            ("service.tls-port",),
        ),
    ),
    FeatureCheck(
        "SC",
        NamespacesFieldCheck(
            "namespaces",
            ("strong-consistency",),
            comp_ops["=="],
            "true",
        ),
    ),
    FeatureCheck(
        "Index-on-device",
        NamespacesFieldCheck("namespaces", ("index_flash_used_bytes",)),
    ),
    FeatureCheck(
        "Index-on-pmem",
        NamespacesFieldCheck(
            "namespaces",
            ("index-type",),
            comp_ops["=="],
            "pmem",
        ),
    ),
)


def _find_features_for_cluster(
    service_stats,
    ns_stats,
    xdr_dc_stats,
    service_configs={},
    ns_configs={},
    security_configs={},
):
    """
    Function takes service stats, namespace stats, service configs, namespace configs and dictionary cluster config.
    Returns list of active (used) features identifying by comparing respective keys for non-zero value.
    """

    features = []

    service_data = util.deep_merge_dicts(service_stats, service_configs)
    ns_data = util.deep_merge_dicts(ns_stats, ns_configs)
    nodes = list(service_data.keys())

    for fc in feature_checks:
        for node in nodes:
            ns_d = util.get_value_from_dict(ns_data, node, None, dict)
            service_d = util.get_value_from_dict(service_data, node, None, dict)
            xdr_d = util.get_value_from_dict(xdr_dc_stats, node, None, dict)
            security_d = util.get_value_from_dict(security_configs, node, None, dict)
            sources = {
                "service": service_d,
                "namespaces": ns_d,
                "xdr_dc": xdr_d,
                "security": security_d,
            }

            if fc.check(sources):
                features.append(fc.display_key)
                break

    return features


def find_nodewise_features(
    service_stats,
    ns_stats,
    xdr_dc_stats,
    service_configs={},
    ns_configs={},
    security_configs={},
):
    """
    Function takes service stats, namespace stats, service configs, namespace configs and dictionary cluster config.
    Returns map of active (used) features per node identifying by comparing respective keys for non-zero value.
    """

    features = {}
    service_data = util.deep_merge_dicts(service_stats, service_configs)
    ns_data = util.deep_merge_dicts(ns_stats, ns_configs)

    nodes = list(service_data.keys())

    for node in nodes:
        ns_d = util.get_value_from_dict(ns_data, node, None, dict)
        service_d = util.get_value_from_dict(service_data, node, None, dict)
        xdr_d = util.get_value_from_dict(xdr_dc_stats, node, None, dict)
        security_d = util.get_value_from_dict(security_configs, node, None, dict)
        sources = {
            "service": service_d,
            "namespaces": ns_d,
            "xdr_dc": xdr_d,
            "security": security_d,
        }

        for feature_check in feature_checks:
            if node not in features:
                features[node] = {}

            if feature_check.check(sources):
                features[node][feature_check.display_key] = "YES"
            else:
                features[node][feature_check.display_key] = "NO"

    return features


#############################

########## Summary ##########


class UDAEntryNamespaceDict(TypedDict):
    master_objects: int
    unique_data_bytes: int


UDAEntryNamespacesDict = NamespaceDict[UDAEntryNamespaceDict]


class UDAEntryDict(TypedDict):
    cluster_name: str
    cluster_generation: int
    node_count: int
    hours_since_start: int
    time: str
    level: Union[Literal["info"], Literal["error"]]
    master_objects: int
    unique_data_bytes: int
    namespaces: UDAEntryNamespacesDict
    cluster_stable: bool
    errors: list[str]


class UDAEntriesRespDict(TypedDict):
    count: int
    entries: list[UDAEntryDict]


class UDAResponsesRequiredDict(TypedDict):
    license_usage: UDAEntriesRespDict
    health: dict


class UDAResponsesOptionalDict(TypedDict, total=False):
    raw_store: str


class UDAResponsesDict(UDAResponsesRequiredDict, UDAResponsesOptionalDict):
    pass


async def _fetch_url(session, url, func, **kwargs):
    async with session.get(url, **kwargs) as resp:
        resp.raise_for_status()
        resp = await func(resp)
    return resp


async def _fetch_url_json(session, url, **kwargs):
    async def json_func(resp):
        return await resp.json()

    return await _fetch_url(session, url, json_func, **kwargs)


async def _fetch_url_text(session, url, **kwargs):
    async def text_func(resp):
        return await resp.text()

    return await _fetch_url(session, url, text_func, **kwargs)


async def _request_license_usage(
    agent_host: str, agent_port: str, get_store: bool = False
) -> UDAResponsesDict:
    json_data: UDAResponsesDict = {
        "license_usage": {"count": 0, "entries": []},
        "health": {},
    }

    a_year_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=365
    )
    a_year_ago = a_year_ago.isoformat()
    timeout = aiohttp.ClientTimeout(total=5)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            entries_params = {"start": a_year_ago}
            agent_req_base = "http://" + agent_host + ":" + str(agent_port) + "/v1/"
            requests = [
                _fetch_url_json(
                    session,
                    agent_req_base + "entries/range/time",
                    params=entries_params,
                ),
                _fetch_url_json(session, agent_req_base + "health"),
            ]

            entries_json: UDAEntriesRespDict
            health_json: dict
            store_txt: Optional[str] = None

            if get_store:
                requests.append(_fetch_url_text(session, agent_req_base + "raw-store"))
                (
                    entries_json,
                    health_json,
                    store_txt,
                ) = await asyncio.gather(  # pyright: ignore[reportGeneralTypeIssues]
                    *requests
                )
            else:
                (
                    entries_json,
                    health_json,
                ) = await asyncio.gather(  # pyright: ignore[reportGeneralTypeIssues]
                    *requests
                )

                json_data["health"] = health_json

                json_data["license_usage"] = entries_json

            if store_txt is not None:
                json_data["raw_store"] = store_txt

    except asyncio.TimeoutError as e:
        raise TimeoutError("Unable to connect to agent. Connection timed out.")
    except aiohttp.ClientConnectorError as e:
        raise OSError("Unable to connect to agent : {}".format(e.os_error))
    except aiohttp.ClientResponseError as e:
        raise OSError(
            "Incorrect response from agent : {} {}".format(e.status, e.message)
        )
    except Exception as e:
        raise OSError("Unknown error : {}".format(e))

    return json_data


request_license_usage = util.async_cached(_request_license_usage, ttl=30)


def _set_migration_status(namespace_stats, cluster_dict, ns_dict):
    """
    Function takes dictionary of namespace stats, cluster output dictionary and namespace output dictionary.
    Function finds migration status per namespace, and per cluster and updates output dictionaries.
    """

    if not namespace_stats:
        return

    for ns, ns_stats in namespace_stats.items():
        if not ns_stats or isinstance(ns_stats, Exception):
            continue

        migrations_in_progress = any(
            util.get_value_from_second_level_of_dict(
                ns_stats,
                ("migrate_tx_partitions_remaining", "migrate-tx-partitions-remaining"),
                default_value=0,
                return_type=int,
            ).values()
        )
        if migrations_in_progress:
            ns_dict[ns]["migrations_in_progress"] = True
            cluster_dict["migrations_in_progress"] = True


class SummaryClusterLicenseAggOptionalDict(TypedDict, total=False):
    min: int
    max: int
    avg: int
    latest_time: datetime.datetime


class SummaryClusterLicenseAggRequiredDict(TypedDict):
    latest: int


class SummaryClusterLicenseAggDict(
    SummaryClusterLicenseAggOptionalDict, SummaryClusterLicenseAggRequiredDict
):
    pass


class SummaryStorageUsageDict(TypedDict):
    total: int
    avail: int
    avail_pct: float
    used: int
    used_pct: float


class SummaryClusterOptionalDict(TypedDict, total=False):
    device: SummaryStorageUsageDict
    pmem: SummaryStorageUsageDict
    pmem_index: SummaryStorageUsageDict
    flash_index: SummaryStorageUsageDict


class SummaryClusterRequiredDict(TypedDict):
    server_version: list[str]
    os_version: list[str]
    cluster_size: list[int]
    cluster_name: list[str]
    device_count: int
    device_count_per_node: int
    device_count_same_across_nodes: bool
    active_features: list[str]
    migrations_in_progress: bool
    active_ns: int
    ns_count: int
    license_data: SummaryClusterLicenseAggDict
    memory: SummaryStorageUsageDict


class SummaryClusterDict(SummaryClusterOptionalDict, SummaryClusterRequiredDict):
    pass


class SummaryNamespaceOptionalDict(TypedDict, total=False):
    compression_ratio: float
    cache_read_pct: int
    device: SummaryStorageUsageDict
    pmem: SummaryStorageUsageDict
    pmem_index: SummaryStorageUsageDict
    flash_index: SummaryStorageUsageDict


class SummaryNamespaceRequiredDict(TypedDict):
    devices_total: int
    devices_per_node: int
    device_count_same_across_nodes: bool
    repl_factor: list[int]
    master_objects: int
    migrations_in_progress: bool
    index_type: str  # TODO: should be Union[Literal["pmem"], Literal["flash"], Literal["shmem"]]
    memory: SummaryStorageUsageDict
    rack_aware: bool
    license_data: SummaryClusterLicenseAggDict


class SummaryNamespaceDict(SummaryNamespaceOptionalDict, SummaryNamespaceRequiredDict):
    pass


SummaryNamespacesDict = NamespaceDict[SummaryNamespaceDict]


class SummaryDict(TypedDict):
    CLUSTER: SummaryClusterDict
    NAMESPACES: SummaryNamespacesDict


def _initialize_summary_output(ns_list) -> SummaryDict:
    """
    Function takes list of namespace names.
    Returns dictionary with summary fields set.
    """

    summary_dict: SummaryDict = {
        "CLUSTER": {
            "server_version": [],
            "os_version": [],
            "active_features": [],
            "cluster_size": [],
            "cluster_name": [],
            "migrations_in_progress": False,
            "device_count": 0,
            "device_count_per_node": 0,
            "device_count_same_across_nodes": True,
            "memory": {
                "total": 0,
                "used": 0,
                "used_pct": 0.0,
                "avail": 0,
                "avail_pct": 0.0,
            },
            "active_ns": 0,
            "ns_count": 0,
            "license_data": {"latest": 0},
        },
        "NAMESPACES": {},
    }

    for ns in ns_list:
        ns_dict: SummaryNamespaceDict = {
            "devices_total": 0,
            "devices_per_node": 0,
            "device_count_same_across_nodes": True,
            "repl_factor": [],
            "master_objects": 0,
            "migrations_in_progress": False,
            # Memory is always used regardless of configuration
            "memory": {
                "total": 0,
                "used": 0,
                "used_pct": 0.0,
                "avail": 0,
                "avail_pct": 0.0,
            },
            "index_type": "shmem",
            "rack_aware": False,
            "license_data": {"latest": 0},
        }
        summary_dict["NAMESPACES"][ns] = ns_dict

    return summary_dict


class AggregateLicenseUsage:
    """
    A helper object for calculating min, max, avg and storing latest and latest_time.
    It simply cleans up the code.  It is used to calculate total license usage for the
    cluster and for each namespace.
    """

    def __init__(self):
        """
        If val is None then the instance is init with defaults
        """
        self.initialized = False
        self.min = float("inf")
        self.max = 0
        self.avg = 0
        self.latest = 0
        self.latest_time = datetime.datetime.now()
        self.count = 0

    def update(self, val, time=None):
        self.min = min(self.min, val)
        self.max = max(self.max, val)
        self.count += 1
        self.avg = (
            (self.avg * (self.count - 1)) + val
        ) / self.count  # get old average first
        self.latest = val

        if time != None:
            self.latest_time = date_parser.isoparse(time)

    def __dict__(self) -> SummaryClusterLicenseAggDict:
        d: SummaryClusterLicenseAggDict = {"latest": self.latest}
        d["latest_time"] = self.latest_time
        d["min"] = self.min  # type: ignore
        d["max"] = self.max
        d["avg"] = round(self.avg)

        return d


def _parse_agent_response(
    license_usage: UDAEntriesRespDict,
    summary_dict: SummaryDict,
    allow_unstable: bool,
) -> bool:
    """
    license_usage - a combination of responses from the unique-data-agent.
    cluster_dict - A dictionary in which to store the result.
    filter_cluster_stable - Ignore entries where the cluster is unstable because
                              the computation may not be accurate. Default=True
    """
    entries = license_usage["entries"]
    cluster_result = AggregateLicenseUsage()
    namespaces_result: dict[str, AggregateLicenseUsage] = {}

    for entry in entries:
        if entry["level"] == "info":
            # Pre-release v. of uda did not have cluster-stable
            if not allow_unstable and not entry["cluster_stable"]:
                continue

            time_ = entry["time"]

            total_data_bytes = entry["unique_data_bytes"]
            cluster_result.update(total_data_bytes, time_)

            if "namespaces" in entry:
                for ns, usage in entry["namespaces"].items():
                    ns_data_bytes = usage["unique_data_bytes"]
                    if ns not in namespaces_result:
                        namespaces_result[ns] = AggregateLicenseUsage()

                    namespaces_result[ns].update(ns_data_bytes, time_)

    if cluster_result.count != 0:
        summary_dict["CLUSTER"][
            "license_data"
        ] = (
            cluster_result.__dict__()
        )  # allows type checker to view type rather than generic dict
    else:
        return False

    for ns, ns_result in namespaces_result.items():
        if ns_result.count != 0:
            if ns in summary_dict["NAMESPACES"]:
                summary_dict["NAMESPACES"][ns][
                    "license_data"
                ] = (
                    ns_result.__dict__()
                )  # allows type checker to view type rather than generic dict
            else:
                logger.warning(
                    "Namespace %s found in UDA response but not in current cluster.", ns
                )

    return True


def _manually_compute_license_data_size(
    namespace_stats, server_builds, summary_dict: SummaryDict
):
    """
    Function takes dictionary of set stats, dictionary of namespace stats, cluster output dictionary and namespace output dictionary.
    Function finds license data size per namespace, and per cluster and updates output dictionaries.
    Please check formulae at https://aerospike.atlassian.net/wiki/spaces/SUP/pages/198344706/License+Data+Formulae.
    For more detail please see https://www.aerospike.com/docs/operations/plan/capacity/index.html.
    """

    if not namespace_stats:
        return

    cl_unique_data = 0.0

    for ns, ns_stats in namespace_stats.items():
        if not ns_stats or isinstance(ns_stats, Exception):
            continue

        ns_unique_data = 0.0
        ns_master_objects = 0
        ns_repl_factor = 1
        ns_record_overhead = 0.0

        for host_id, host_stats in ns_stats.items():
            host_memory_bytes = 0.0
            host_device_bytes = 0.0
            host_pmem_bytes = 0.0
            host_master_objects = 0

            if not host_stats or isinstance(host_stats, Exception):
                continue

            repl_factor = util.get_value_from_dict(
                host_stats,
                ("effective_replication_factor", "replication-factor", "repl-factor"),
                default_value=-1,
                return_type=int,
            )

            if repl_factor == 0:
                continue

            if repl_factor == -1:
                raise Exception("unable to determine cluster replication factor")

            if ns_repl_factor != 1 and repl_factor != ns_repl_factor:
                raise Exception(
                    "different replication factor found across nodes for namespace %s"
                    % ns
                )

            ns_repl_factor = repl_factor

            host_master_objects += util.get_value_from_dict(
                host_stats,
                ("master_objects", "master-objects"),
                default_value=0,
                return_type=int,
            )

            host_device_compression_ratio = util.get_value_from_dict(
                host_stats,
                "device_compression_ratio",
                default_value=1.0,
                return_type=float,
            )

            host_pmem_compression_ratio = util.get_value_from_dict(
                host_stats,
                "pmem_compression_ratio",
                default_value=1.0,
                return_type=float,
            )

            host_device_bytes = util.get_value_from_dict(
                host_stats,
                "device_used_bytes",
                default_value=0.0,
                return_type=float,
            )

            host_device_bytes /= host_device_compression_ratio

            host_pmem_bytes = util.get_value_from_dict(
                host_stats,
                "pmem_used_bytes",
                default_value=0.0,
                return_type=float,
            )

            host_pmem_bytes /= host_pmem_compression_ratio

            if host_pmem_bytes == 0.0 and host_device_bytes == 0.0:
                host_memory_bytes += util.get_value_from_dict(
                    host_stats,
                    "memory_used_index_bytes",
                    default_value=0.0,
                    return_type=float,
                )

                host_memory_bytes += util.get_value_from_dict(
                    host_stats,
                    "memory_used_data_bytes",
                    default_value=0.0,
                    return_type=float,
                )

            host_build_version = util.get_value_from_dict(
                server_builds,
                host_id,
                default_value=None,
                return_type=str,
            )

            if host_build_version is None:
                raise Exception("could not find host %s in build responses" % host_id)

            host_record_overhead = 35

            if version.LooseVersion(
                constants.SERVER_39_BYTE_OVERHEAD_FIRST_VERSION
            ) <= version.LooseVersion(host_build_version):
                host_record_overhead = 39

            host_unique_data = host_memory_bytes + host_pmem_bytes + host_device_bytes
            ns_unique_data += host_unique_data
            ns_record_overhead += host_master_objects * host_record_overhead
            ns_master_objects += host_master_objects

        ns_unique_data = round((ns_unique_data / ns_repl_factor) - ns_record_overhead)
        summary_dict["NAMESPACES"][ns]["license_data"]["latest"] = int(
            round(ns_unique_data)
        )
        cl_unique_data += ns_unique_data

    summary_dict["CLUSTER"]["license_data"]["latest"] = int(round(cl_unique_data))


def compute_license_data_size(
    namespace_stats,
    license_data_usage: Optional[UDAResponsesDict],
    server_builds: dict[str, str],
    allow_unstable: bool,
    summary_dict: SummaryDict,
):
    if not license_data_usage:
        _manually_compute_license_data_size(
            namespace_stats, server_builds, summary_dict
        )
    else:
        try:
            license_usage = license_data_usage["license_usage"]
            if not _parse_agent_response(license_usage, summary_dict, allow_unstable):
                logger.warning("Zero entries found in uda response")
                _manually_compute_license_data_size(
                    namespace_stats, server_builds, summary_dict
                )

        #  an error was returned from request
        except (TypeError, ValueError, KeyError) as e:
            logger.error("Issue parsing agent response: %s", e)
            _manually_compute_license_data_size(
                namespace_stats, server_builds, summary_dict
            )
            return


def create_summary(
    service_stats,
    namespace_stats,
    xdr_dc_stats,
    metadata,
    license_allow_unstable: bool,
    service_configs={},
    ns_configs={},
    security_configs={},
    license_data_usage: UDAResponsesDict | None = None,
):
    """
    Function takes four dictionaries service stats, namespace stats, set stats and metadata.
    Returns dictionary with summary information.
    """

    features = _find_features_for_cluster(
        service_stats,
        namespace_stats,
        xdr_dc_stats,
        service_configs=service_configs,
        ns_configs=ns_configs,
        security_configs=security_configs,
    )

    namespace_stats = util.flip_keys(namespace_stats)
    ns_configs = util.flip_keys(ns_configs)

    summary_dict = _initialize_summary_output(namespace_stats.keys())

    total_nodes = len(service_stats.keys())

    cl_memory_size_total = 0
    cl_memory_size_avail = 0
    cl_pmem_index_size_total = 0
    cl_pmem_index_size_avail = 0
    cl_flash_index_size_total = 0
    cl_flash_index_size_avail = 0

    cl_nodewise_device_counts = {}

    cl_nodewise_device_size = {}
    cl_nodewise_device_used = {}
    cl_nodewise_device_avail = {}

    cl_nodewise_pmem_size = {}
    cl_nodewise_pmem_used = {}
    cl_nodewise_pmem_avail = {}

    compute_license_data_size(
        namespace_stats,
        license_data_usage,
        metadata["server_build"],
        license_allow_unstable,
        summary_dict,
    )
    _set_migration_status(
        namespace_stats, summary_dict["CLUSTER"], summary_dict["NAMESPACES"]
    )

    summary_dict["CLUSTER"]["active_features"] = features
    summary_dict["CLUSTER"]["cluster_size"] = list(
        set(
            util.get_value_from_second_level_of_dict(
                service_stats, ("cluster_size",), default_value=0, return_type=int
            ).values()
        )
    )

    if "cluster_name" in metadata and metadata["cluster_name"]:
        summary_dict["CLUSTER"]["cluster_name"] = list(
            set(metadata["cluster_name"].values()).difference(set(["null"]))
        )

    if "server_version" in metadata and metadata["server_version"]:
        summary_dict["CLUSTER"]["server_version"] = list(
            set(metadata["server_version"].values())
        )

    if "os_version" in metadata and metadata["os_version"]:
        summary_dict["CLUSTER"]["os_version"] = list(
            set(
                util.get_value_from_second_level_of_dict(
                    metadata["os_version"],
                    ("description",),
                    default_value="",
                    return_type=str,
                ).values()
            )
        )

    for ns, ns_stats in namespace_stats.items():
        if not ns_stats or isinstance(ns_stats, Exception):
            continue

        device_name_list = util.get_values_from_second_level_of_dict(
            ns_stats,
            (
                r"^storage-engine.device$",
                r"^device$",
                r"^storage-engine.file$",
                r"^file$",
                r"^dev$",
                r"^storage-engine.device\[[0-9]+\]$",
                r"^storage-engine.file\[[0-9]+\]$",
            ),
            return_type=str,
        )

        device_counts = dict(
            [
                (k, sum(len(i.split(",")) for i in v) if v else 0)
                for k, v in device_name_list.items()
            ]
        )
        cl_nodewise_device_counts = util.add_dicts(
            cl_nodewise_device_counts, device_counts
        )
        ns_total_devices = sum(device_counts.values())
        ns_total_nodes = len(ns_stats.keys())

        if ns_total_devices:
            summary_dict["NAMESPACES"][ns]["devices_total"] = ns_total_devices
            summary_dict["NAMESPACES"][ns]["devices_per_node"] = round(
                ns_total_devices / ns_total_nodes
            )
            if len(set(device_counts.values())) > 1:
                summary_dict["NAMESPACES"][ns]["device_count_same_across_nodes"] = False

        # Memory
        mem_size: int = sum(
            util.get_value_from_second_level_of_dict(
                ns_stats, ("memory-size",), default_value=0, return_type=int
            ).values()
        )
        mem_used: int = sum(
            util.get_value_from_second_level_of_dict(
                ns_stats, ("memory_used_bytes",), default_value=0, return_type=int
            ).values()
        )
        mem_avail = mem_size - mem_used
        mem_avail_pct = (mem_avail / mem_size) * 100.0
        mem_used_pct = 100.00 - mem_avail_pct
        cl_memory_size_total += mem_size
        cl_memory_size_avail += mem_avail

        ns_mem_usage: SummaryStorageUsageDict = {
            "total": mem_size,
            "used": mem_used,
            "used_pct": mem_used_pct,
            "avail": mem_avail,
            "avail_pct": mem_avail_pct,
        }
        summary_dict["NAMESPACES"][ns]["memory"] = ns_mem_usage

        index_type = summary_dict["NAMESPACES"][ns]["index_type"] = list(
            util.get_value_from_second_level_of_dict(
                ns_stats, ("index-type",), default_value="shmem", return_type=str
            ).values()
        )[0]

        # Pmem Index
        if index_type == "pmem":
            pmem_index_size = sum(
                util.get_value_from_second_level_of_dict(
                    ns_configs[ns],
                    ("index-type.mounts-size-limit",),
                    default_value=0,
                    return_type=int,
                ).values()
            )
            pmem_index_used = sum(
                util.get_value_from_second_level_of_dict(
                    ns_stats,
                    ("index_pmem_used_bytes",),
                    default_value=0,
                    return_type=int,
                ).values()
            )

            if pmem_index_size > 0:
                pmem_index_avail = pmem_index_size - pmem_index_used
                pmem_index_avail_pct = (pmem_index_avail / pmem_index_size) * 100.0
                pmem_index_used_pct = 100.00 - pmem_index_avail_pct
                cl_pmem_index_size_total += pmem_index_size
                cl_pmem_index_size_avail += pmem_index_avail

                ns_pmem_index_usage: SummaryStorageUsageDict = {
                    "total": pmem_index_size,
                    "used": pmem_index_used,
                    "used_pct": pmem_index_used_pct,
                    "avail": pmem_index_avail,
                    "avail_pct": pmem_index_avail_pct,
                }
                summary_dict["NAMESPACES"][ns]["pmem_index"] = ns_pmem_index_usage

        # Flash Index
        elif index_type == "flash":
            flash_index_size = sum(
                util.get_value_from_second_level_of_dict(
                    ns_configs[ns],
                    ("index-type.mounts-size-limit",),
                    default_value=0,
                    return_type=int,
                ).values()
            )
            flash_index_used = sum(
                util.get_value_from_second_level_of_dict(
                    ns_stats,
                    ("index_flash_used_bytes",),
                    default_value=0,
                    return_type=int,
                ).values()
            )

            if flash_index_size > 0:
                flash_index_avail = flash_index_size - flash_index_used
                flash_index_avail_pct = (flash_index_avail / flash_index_size) * 100.0
                flash_index_used_pct = 100.00 - flash_index_avail_pct
                cl_flash_index_size_total += flash_index_size
                cl_flash_index_size_avail += flash_index_avail

                ns_flash_index_usage: SummaryStorageUsageDict = {
                    "total": flash_index_size,
                    "used": flash_index_used,
                    "used_pct": flash_index_used_pct,
                    "avail": flash_index_avail,
                    "avail_pct": flash_index_avail_pct,
                }
                summary_dict["NAMESPACES"][ns]["flash_index"] = ns_flash_index_usage

        storage_engine_type = list(
            util.get_value_from_second_level_of_dict(
                ns_stats, ("storage-engine",), default_value="", return_type=str
            ).values()
        )[0]

        if storage_engine_type == "device":
            device_size = util.get_value_from_second_level_of_dict(
                ns_stats,
                ("device_total_bytes", "total-bytes-disk"),
                default_value=0,
                return_type=int,
            )
            device_used = util.get_value_from_second_level_of_dict(
                ns_stats,
                ("device_used_bytes", "used-bytes-disk"),
                default_value=0,
                return_type=int,
            )
            device_avail_pct = util.get_value_from_second_level_of_dict(
                ns_stats,
                ("device_available_pct", "available_pct"),
                default_value=0,
                return_type=int,
            )
            device_avail = util.pct_to_value(device_size, device_avail_pct)
            cl_nodewise_device_size = util.add_dicts(
                cl_nodewise_device_size, device_size
            )
            cl_nodewise_device_used = util.add_dicts(
                cl_nodewise_device_used, device_used
            )
            cl_nodewise_device_avail = util.add_dicts(
                cl_nodewise_device_avail, device_avail
            )
            device_size_total = sum(device_size.values())

            if device_size_total > 0:
                device_size_used = sum(device_used.values())
                device_size_avail = sum(device_avail.values())
                device_size_avail_pct = (device_size_avail / device_size_total) * 100.0
                device_size_used_pct = (device_size_used / device_size_total) * 100.0

                ns_device_usage: SummaryStorageUsageDict = {
                    "total": device_size_total,
                    "used": device_size_used,
                    "used_pct": device_size_used_pct,
                    "avail": device_size_avail,
                    "avail_pct": device_size_avail_pct,
                }
                summary_dict["NAMESPACES"][ns]["device"] = ns_device_usage

        elif storage_engine_type == "pmem":
            pmem_size = util.get_value_from_second_level_of_dict(
                ns_stats,
                ("pmem_total_bytes",),
                default_value=0,
                return_type=int,
            )
            pmem_used = util.get_value_from_second_level_of_dict(
                ns_stats,
                ("pmem_used_bytes"),
                default_value=0,
                return_type=int,
            )
            pmem_avail_pct = util.get_value_from_second_level_of_dict(
                ns_stats,
                ("pmem_available_pct"),
                default_value=0,
                return_type=int,
            )
            pmem_avail = util.pct_to_value(pmem_size, pmem_avail_pct)
            cl_nodewise_pmem_size = util.add_dicts(cl_nodewise_pmem_size, pmem_size)
            cl_nodewise_pmem_used = util.add_dicts(cl_nodewise_pmem_used, pmem_used)
            cl_nodewise_pmem_avail = util.add_dicts(cl_nodewise_pmem_avail, pmem_avail)
            pmem_size_total = sum(pmem_size.values())

            if pmem_size_total > 0:
                pmem_size_used = sum(pmem_used.values())
                pmem_size_avail = sum(pmem_avail.values())
                pmem_size_avail_pct = (pmem_size_avail / pmem_size_total) * 100.0
                pmem_size_used_pct = (pmem_size_used / pmem_size_total) * 100.0

                ns_pmem_usage: SummaryStorageUsageDict = {
                    "total": pmem_size_total,
                    "used": pmem_size_used,
                    "used_pct": pmem_size_used_pct,
                    "avail": pmem_size_avail,
                    "avail_pct": pmem_size_avail_pct,
                }
                summary_dict["NAMESPACES"][ns]["pmem"] = ns_pmem_usage

        compression_ratio = max(
            util.get_value_from_second_level_of_dict(
                ns_stats,
                ("device_compression_ratio", "pmem_compression_ratio"),
                default_value=0.0,
                return_type=float,
            ).values()
        )

        if compression_ratio > 0:
            summary_dict["NAMESPACES"][ns]["compression_ratio"] = compression_ratio

        summary_dict["NAMESPACES"][ns]["repl_factor"] = list(
            set(
                util.get_value_from_second_level_of_dict(
                    ns_stats,
                    ("repl-factor", "replication-factor"),
                    default_value=0,
                    return_type=int,
                ).values()
            )
        )

        data_in_memory = list(
            util.get_value_from_second_level_of_dict(
                ns_stats,
                ("storage-engine.data-in-memory", "data-in-memory"),
                default_value=False,
                return_type=bool,
            ).values()
        )[0]

        if data_in_memory:
            cache_read_pcts = list(
                util.get_value_from_second_level_of_dict(
                    ns_stats,
                    ("cache_read_pct", "cache-read-pct"),
                    default_value=None,
                    return_type=int,
                ).values()
            )
            if cache_read_pcts:
                try:
                    summary_dict["NAMESPACES"][ns]["cache_read_pct"] = sum(
                        cache_read_pcts
                    ) // len(cache_read_pcts)
                except Exception:
                    pass
        master_objects = sum(
            util.get_value_from_second_level_of_dict(
                ns_stats,
                ("master_objects", "master-objects"),
                default_value=0,
                return_type=int,
            ).values()
        )
        summary_dict["CLUSTER"]["ns_count"] += 1

        if master_objects > 0:
            summary_dict["NAMESPACES"][ns]["master_objects"] = master_objects
            summary_dict["CLUSTER"]["active_ns"] += 1

        rack_ids = util.get_value_from_second_level_of_dict(
            ns_stats, ("rack-id",), default_value=None, return_type=int
        )

        if rack_ids:
            rack_ids = list(set(rack_ids.values()))
            if len(rack_ids) > 1 or rack_ids[0] is not None:
                if any((i is not None and i > 0) for i in rack_ids):
                    summary_dict["NAMESPACES"][ns]["rack_aware"] = True

    cl_device_counts = sum(cl_nodewise_device_counts.values())
    if cl_device_counts:
        summary_dict["CLUSTER"]["device_count"] = cl_device_counts
        summary_dict["CLUSTER"]["device_count_per_node"] = round(
            cl_device_counts / total_nodes
        )
        if len(set(cl_nodewise_device_counts.values())) > 1:
            summary_dict["CLUSTER"]["device_count_same_across_nodes"] = False

    if cl_memory_size_total > 0:
        memory_avail_pct = (cl_memory_size_avail / cl_memory_size_total) * 100.0
        cluster_memory: SummaryStorageUsageDict = {
            "total": cl_memory_size_total,
            "avail": cl_memory_size_avail,
            "avail_pct": memory_avail_pct,
            "used": cl_memory_size_total - cl_memory_size_avail,
            "used_pct": 100.0 - memory_avail_pct,
        }
        summary_dict["CLUSTER"]["memory"] = cluster_memory

    if cl_pmem_index_size_total > 0:
        cl_pmem_index_size_avail_pct = (
            cl_pmem_index_size_avail / cl_pmem_index_size_total
        ) * 100.0
        cluster_pmem_index: SummaryStorageUsageDict = {
            "total": cl_pmem_index_size_total,
            "avail": cl_pmem_index_size_avail,
            "avail_pct": cl_pmem_index_size_avail_pct,
            "used": cl_pmem_index_size_total - cl_pmem_index_size_avail,
            "used_pct": 100.0 - cl_pmem_index_size_avail_pct,
        }
        summary_dict["CLUSTER"]["pmem_index"] = cluster_pmem_index

    if cl_flash_index_size_total > 0:
        cl_flash_index_size_avail_pct = (
            cl_flash_index_size_avail / cl_flash_index_size_total
        ) * 100.0
        cluster_flash_index: SummaryStorageUsageDict = {
            "total": cl_flash_index_size_total,
            "avail": cl_flash_index_size_avail,
            "avail_pct": cl_flash_index_size_avail_pct,
            "used": cl_flash_index_size_total - cl_flash_index_size_avail,
            "used_pct": 100.0 - cl_flash_index_size_avail_pct,
        }
        summary_dict["CLUSTER"]["flash_index"] = cluster_flash_index

    cl_device_size_total = sum(cl_nodewise_device_size.values())
    if cl_device_size_total > 0:
        cluster_device_used = sum(cl_nodewise_device_used.values())
        cluster_device_avail = sum(cl_nodewise_device_avail.values())
        cluster_device_index: SummaryStorageUsageDict = {
            "total": cl_device_size_total,
            "avail": cluster_device_avail,
            "avail_pct": (cluster_device_avail / cl_device_size_total) * 100.0,
            "used": cluster_device_used,
            "used_pct": (cluster_device_used / cl_device_size_total) * 100.0,
        }
        summary_dict["CLUSTER"]["device"] = cluster_device_index

    cl_pmem_size_total = sum(cl_nodewise_pmem_size.values())
    if cl_pmem_size_total > 0:
        cluster_pmem_used = sum(cl_nodewise_pmem_used.values())
        cluster_pmem_avail = sum(cl_nodewise_pmem_avail.values())
        cluster_pmem_index: SummaryStorageUsageDict = {
            "total": cl_pmem_size_total,
            "avail": cluster_pmem_avail,
            "avail_pct": (cluster_pmem_avail / cl_pmem_size_total) * 100.0,
            "used": cluster_pmem_used,
            "used_pct": (cluster_pmem_used / cl_pmem_size_total) * 100.0,
        }
        summary_dict["CLUSTER"]["pmem"] = cluster_pmem_index

    return summary_dict


#############################

####### Stop-Writes #########


class StopWritesEntry(TypedDict):
    metric: str
    metric_usage: int | float
    stop_writes: bool
    metric_threshold: NotRequired[int | float]
    config: NotRequired[str]
    namespace: NotRequired[str]
    set: NotRequired[str]


StopWritesEntryKey = tuple[str | None, str | None, str]
StopWritesDict = NodeDict[dict[StopWritesEntryKey, StopWritesEntry]]


def _create_stop_writes_entry(
    node_sw_metrics: dict[StopWritesEntryKey, StopWritesEntry],
    metric: str,
    metric_usage: float | int,
    stop_writes: bool,
    metric_threshold: float | int | None,
    config: str | None = None,
    namespace: str | None = None,
    set_: str | None = None,
):
    entry: StopWritesEntry = {
        "stop_writes": stop_writes,
        "metric": metric,
        "metric_usage": metric_usage,
    }

    if config:
        entry["config"] = config
    if metric_threshold:
        entry["metric_threshold"] = metric_threshold
    if namespace:
        entry["namespace"] = namespace
    if set_:
        entry["set"] = set_

    node_sw_metrics[(namespace, set_, metric)] = entry


@staticmethod
def _is_stop_writes_cause(
    usage: int | float, threshold: int | float, stop_writes: str | None = None
):
    if threshold == 0:
        return False

    return (
        True
        if usage >= threshold and (stop_writes is None or stop_writes.lower() == "true")
        else False
    )


@staticmethod
def _format_ns_stop_writes_metrics(
    stop_writes_metrics: StopWritesDict,
    service_stats,
    ns_stats,
):
    for node in service_stats:
        cluster_clock_skew_ms = service_stats[node].get("cluster_clock_skew_ms", None)
        cluster_clock_skew_stop_writes_sec = service_stats[node].get(
            "cluster_clock_skew_stop_writes_sec", None
        )
        system_free_mem_pct = service_stats[node].get("system_free_mem_pct", None)

        for ns, stats in ns_stats.get(node, {}).items():
            # There is no config for this trigger
            strong_consistency: str | None = stats.get("strong-consistency", None)
            nsup_period: str | None = stats.get("nsup-period", None)
            stop_writes: str | None = stats.get("clock_skew_stop_writes", None)
            metric: str = "cluster_clock_skew_ms"
            usage = cluster_clock_skew_ms
            threshold = cluster_clock_skew_stop_writes_sec

            """
            For Available mode (AP) namespaces running versions 4.5.1 or above and where 
            NSUP is enabled (i.e. nsup-period not zero), will be true if the cluster 
            clock skew exceeds 40 seconds.
            """
            if usage is not None and threshold is not None and stop_writes is not None:
                if (
                    strong_consistency == "false"
                    and nsup_period is not None  # nsup-period was added in 4.5.1.
                    and nsup_period != "0"
                ):
                    thresh = 40000
                else:
                    thresh = (
                        int(cluster_clock_skew_stop_writes_sec) * 1000
                    )  # convert to ms
                use = int(usage)
                sw = _is_stop_writes_cause(use, thresh, stop_writes)
                _create_stop_writes_entry(
                    stop_writes_metrics[node],
                    metric,
                    use,
                    sw,
                    thresh,
                    namespace=ns,
                )

            stop_writes: str | None = stats.get("stop_writes", None)
            metric = "system_free_mem_pct"
            config = "stop-writes-sys-memory-pct"
            threshold: str | None = stats.get(config, None)

            if (
                threshold is not None
                and system_free_mem_pct is not None
                and stop_writes is not None
            ):
                thresh = int(threshold)
                use = 100 - int(system_free_mem_pct)
                sw = _is_stop_writes_cause(use, thresh, stop_writes)
                _create_stop_writes_entry(
                    stop_writes_metrics[node],
                    metric,
                    use,
                    sw,
                    thresh,
                    config=config,
                    namespace=ns,
                )

    for node in ns_stats:
        for ns, stats in ns_stats[node].items():
            stop_writes: str | None = stats.get("stop_writes", None)

            if stop_writes is None:
                continue

            metric = "device_avail_pct"
            config = "min-avail-pct"
            usage: str | None = stats.get(metric, None)
            threshold: str | None = stats.get(config, None)

            if usage is None:
                metric = "pmem_avail_pct"
                usage = stats.get(metric, None)

            if usage is not None and threshold is not None:
                use = int(usage)
                thresh = int(threshold)
                sw = _is_stop_writes_cause(use, thresh, stop_writes)
                _create_stop_writes_entry(
                    stop_writes_metrics[node],
                    metric,
                    use,
                    sw,
                    thresh,
                    config=config,
                    namespace=ns,
                )

            metric = "device_used_bytes"
            config = "max-used-pct"
            usage: str | None = stats.get(metric, None)
            bytes_total: str | None = stats.get("device_total_bytes", None)
            threshold: str | None = stats.get(config, None)

            if usage is None:
                metric = "pmem_used_bytes"
                usage = stats.get(metric, None)
                bytes_total = stats.get("pmem_total_bytes", None)

            if usage is not None and threshold is not None and bytes_total is not None:
                use = int(usage)
                thresh = int(bytes_total) * (int(threshold) / 100)
                sw = _is_stop_writes_cause(use, thresh, stop_writes)
                _create_stop_writes_entry(
                    stop_writes_metrics[node],
                    metric,
                    use,
                    sw,
                    thresh,
                    config=config,
                    namespace=ns,
                )

            metric = "memory_used_bytes"
            config = "stop-writes-pct"
            usage: str | None = stats.get(metric, None)
            bytes_total: str | None = stats.get("memory-size", None)
            threshold: str | None = stats.get(config, None)

            if usage is not None and threshold is not None and bytes_total is not None:
                use = int(usage)
                thresh = int(bytes_total) * (int(threshold) / 100)
                sw = _is_stop_writes_cause(use, thresh, stop_writes)
                _create_stop_writes_entry(
                    stop_writes_metrics[node],
                    metric,
                    use,
                    sw,
                    thresh,
                    config=config,
                    namespace=ns,
                )


def _format_set_stop_writes_metrics(
    stop_writes_metrics: StopWritesDict,
    set_stats,
):
    for node in set_stats:
        for (ns, set_), stats in set_stats[node].items():
            metric1 = "memory_data_bytes"
            metric2 = "device_data_bytes"
            config = "stop-writes-size"
            usage = None
            metric = None
            usage1: str | None = stats.get(metric1, None)
            usage2: str | None = stats.get(metric2, None)
            threshold: str | None = stats.get(config, None)

            if usage1 is None or usage1 == "0":
                metric = metric2
                usage = usage2
            else:
                metric = metric1
                usage = usage1

            if usage is not None and threshold is not None:
                use = int(usage)
                thresh = int(threshold)
                sw = _is_stop_writes_cause(use, thresh)
                _create_stop_writes_entry(
                    stop_writes_metrics[node],
                    metric,
                    use,
                    sw,
                    thresh,
                    config=config,
                    namespace=ns,
                    set_=set_,
                )

            metric = "objects"
            config = "stop-writes-count"
            usage: str | None = stats.get(metric, None)
            threshold: str | None = stats.get(config, None)

            if threshold is None:
                config = "set-stop-writes-count"
                threshold = stats.get(config, None)

            if usage is not None and threshold is not None:
                use = int(usage)
                thresh = int(threshold)
                sw = _is_stop_writes_cause(use, thresh)
                _create_stop_writes_entry(
                    stop_writes_metrics[node],
                    metric,
                    use,
                    sw,
                    thresh,
                    config=config,
                    namespace=ns,
                    set_=set_,
                )


def create_stop_writes_summary(
    service_stats, ns_stats, ns_config, set_stats, set_config
) -> StopWritesDict:
    stop_writes_metrics: StopWritesDict = {}
    node_keys = (
        set(service_stats.keys())
        .union(ns_stats.keys())
        .union(ns_config.keys())
        .union(set_stats.keys())
        .union(set_config.keys())
    )
    for key in node_keys:
        stop_writes_metrics[key] = {}

    ns_stats = util.deep_merge_dicts(ns_stats, ns_config)
    set_stats = util.deep_merge_dicts(set_stats, set_config)

    _format_ns_stop_writes_metrics(stop_writes_metrics, service_stats, ns_stats)
    _format_set_stop_writes_metrics(stop_writes_metrics, set_stats)
    return stop_writes_metrics


#############################

########## Histogram ##########


def _create_histogram_percentiles_output(histogram_name, histogram_data):
    histogram_data = util.flip_keys(histogram_data)

    for namespace, host_data in histogram_data.items():
        if not host_data or isinstance(host_data, Exception):
            continue

        for host_id, data_ in host_data.items():
            if not data_ or isinstance(data_, Exception):
                continue

            hist = data_["data"]
            width = data_["width"]

            cum_total = 0
            total = sum(hist)
            percentile = 0.1
            result = []

            for i, v in enumerate(hist):
                cum_total += float(v)

                if total > 0:
                    portion = cum_total / total
                else:
                    portion = 0.0

                while portion >= percentile:
                    percentile += 0.1
                    result.append(i + 1)

                if percentile > 1.0:
                    break

            if result == []:
                result = [0] * 10

            if histogram_name == "objsz":
                data_["percentiles"] = [(r * width) - 1 if r > 0 else r for r in result]
            else:
                data_["percentiles"] = [r * width for r in result]

    return histogram_data


def _create_bytewise_histogram_percentiles_output(histogram_data, bucket_count, builds):
    histogram_data = util.flip_keys(histogram_data)

    for namespace, host_data in histogram_data.items():
        result = []
        rblock_size_bytes = 128
        width = 1

        for host_id, data_ in host_data.items():
            try:
                as_version = builds[host_id]
                if version.LooseVersion(as_version) < version.LooseVersion("2.7.0") or (
                    version.LooseVersion(as_version) >= version.LooseVersion("3.0.0")
                    and version.LooseVersion(as_version) < version.LooseVersion("3.1.3")
                ):
                    rblock_size_bytes = 512

            except Exception:
                pass

            hist = data_["data"]
            width = data_["width"]

            for i, v in enumerate(hist):
                if v and v > 0:
                    result.append(i)

        result = list(set(result))
        result.sort()
        start_buckets = []

        if len(result) <= bucket_count:
            # if asinfo buckets with values>0 are less than
            # show_bucket_count then we can show all single buckets as it
            # is, no need to merge to show big range
            for res in result:
                start_buckets.append(res)
                start_buckets.append(res + 1)

        else:
            # dividing volume buckets (from min possible bucket with
            # value>0 to max possible bucket with value>0) into same range
            start_bucket = result[0]
            size = result[len(result) - 1] - result[0] + 1

            bucket_width = size // bucket_count
            additional_bucket_index = bucket_count - (size % bucket_count)

            bucket_index = 0

            while bucket_index < bucket_count:
                start_buckets.append(start_bucket)

                if bucket_index == additional_bucket_index:
                    bucket_width += 1

                start_bucket += bucket_width
                bucket_index += 1

            start_buckets.append(start_bucket)

        columns = []
        need_to_show = {}

        for i, bucket in enumerate(start_buckets):
            if i == len(start_buckets) - 1:
                break

            key = _get_bucket_range(
                bucket, start_buckets[i + 1], width, rblock_size_bytes
            )
            need_to_show[key] = False
            columns.append(key)

        for host_id, data_ in host_data.items():
            rblock_size_bytes = 128

            try:
                as_version = builds[host_id]

                if version.LooseVersion(as_version) < version.LooseVersion("2.7.0") or (
                    version.LooseVersion(as_version) >= version.LooseVersion("3.0.0")
                    and version.LooseVersion(as_version) < version.LooseVersion("3.1.3")
                ):
                    rblock_size_bytes = 512

            except Exception:
                pass

            hist = data_["data"]
            width = data_["width"]
            data_["values"] = {}

            for i, s in enumerate(start_buckets):
                if i == len(start_buckets) - 1:
                    break

                b_index = s

                key = _get_bucket_range(
                    s, start_buckets[i + 1], width, rblock_size_bytes
                )

                if key not in columns:
                    columns.append(key)

                if key not in data_["values"]:
                    data_["values"][key] = 0

                while b_index < start_buckets[i + 1]:
                    data_["values"][key] += hist[b_index]
                    b_index += 1

                if data_["values"][key] > 0:
                    need_to_show[key] = True

                else:
                    if key not in need_to_show:
                        need_to_show[key] = False

        host_data["columns"] = []

        for column in columns:
            if need_to_show[column]:
                host_data["columns"].append(column)

    return histogram_data


def _get_bucket_range(current_bucket, next_bucket, width, rblock_size_bytes):
    s_b = "0 B"
    if current_bucket > 0:
        last_bucket_last_rblock_end = ((current_bucket * width) - 1) * rblock_size_bytes

        if last_bucket_last_rblock_end < 1:
            last_bucket_last_rblock_end = 0

        else:
            last_bucket_last_rblock_end += 1

        s_b = file_size.size(last_bucket_last_rblock_end, file_size.byte)

        if current_bucket == 99 or next_bucket > 99:
            return ">%s" % (s_b.replace(" ", ""))

    bucket_last_rblock_end = ((next_bucket * width) - 1) * rblock_size_bytes
    e_b = file_size.size(bucket_last_rblock_end, file_size.byte)
    return _create_range_key(s_b.replace(" ", ""), e_b.replace(" ", ""))


def _create_range_key(s, e):
    return "%s to %s" % (s, e)


def _string_to_bytes(k):
    k = k.split(" to ")
    s = k[0]
    b = {
        "K": 1024**1,
        "M": 1024**2,
        "G": 1024**3,
        "T": 1024**4,
        "P": 1024**5,
        "E": 1024**6,
    }

    for suffix, val in b.items():
        if s.endswith(suffix):
            s = s[: -1 * len(suffix)]
            return int(s) * val

    return int(s)


def _restructure_new_log_histogram(histogram_data):
    histogram_data = util.flip_keys(histogram_data)

    for namespace, ns_data in histogram_data.items():
        if not ns_data or isinstance(ns_data, Exception):
            continue

        columns = []

        for host_id, host_data in ns_data.items():
            if not host_data or isinstance(host_data, Exception):
                continue

            hist = host_data["data"]
            host_data["values"] = {}

            for k, v in hist.items():
                try:
                    kl = k.split("-")
                    s, e = kl[0], kl[1]
                    key = _create_range_key(s, e)
                    host_data["values"][key] = v
                    if key not in columns:
                        columns.append(key)

                except Exception:
                    continue

        for host_id, host_data in ns_data.items():
            if not host_data or isinstance(host_data, Exception):
                continue

            for k in columns:
                if k not in host_data["values"].keys():
                    host_data["values"][k] = 0

        ns_data["columns"] = sorted(columns, key=_string_to_bytes)

    return histogram_data


def _parse_old_histogram(histogram, histogram_data):
    datum = histogram_data.split(",")
    datum.pop(0)  # don't care about ns, hist_name, or length
    width = int(datum.pop(0))
    datum[-1] = datum[-1].split(";")[0]
    datum = [int(data) for data in datum]
    return {"histogram": histogram, "width": width, "data": datum}


def _parse_new_linear_histogram(histogram, histogram_data):
    datum = histogram_data.split(":")
    key_map = {"units": "units", "bucket-width": "width", "buckets": "data"}

    result = {}
    for d in datum:
        k = None
        v = None
        try:
            _d = d.split("=")
            k, v = _d[0], _d[1]

        except Exception:
            continue

        if k is None:
            continue

        if k in key_map:
            result[key_map[k]] = v

    if result:
        buckets = result["data"]
        buckets = buckets.split(",")
        result["data"] = [int(bucket) for bucket in buckets]
        result["width"] = int(result["width"])
        result["histogram"] = histogram

    return result


def _parse_new_log_histogram(histogram, histogram_data):
    datum = histogram_data.split(":")

    field = datum.pop(0)
    split = field.split("=")
    k, v = split[0], split[1]

    if k != "units":
        # wrong format
        return {}

    result = {}
    result[k] = v
    result["data"] = OrderedDict()
    result["histogram"] = histogram

    for d in datum:
        k = None
        v = None
        try:
            _d = d.split("=")
            k, v = _d[0], _d[1]
            if k.endswith(")"):
                k = k[:-1]
            if k.startswith("["):
                k = k[1:]

            result["data"][k] = v

        except Exception:
            continue

    return result


def create_histogram_output(histogram_name, histogram_data, **params):
    if "byte_distribution" not in params or not params["byte_distribution"]:
        return _create_histogram_percentiles_output(histogram_name, histogram_data)

    try:
        units = get_histogram_units(histogram_data)

        if units is not None:
            return _restructure_new_log_histogram(histogram_data)

    except Exception as e:
        raise e

    if "bucket_count" not in params or "builds" not in params:
        return {}

    return _create_bytewise_histogram_percentiles_output(
        histogram_data, params["bucket_count"], params["builds"]
    )


def get_histogram_units(histogram_data):
    """
    Function takes dictionary of histogram data.
    Checks for units key which indicates it is newer format or older and return unit.
    """

    units = None
    units_present = False
    units_absent = False

    for k1, v1 in histogram_data.items():
        if not v1 or isinstance(v1, Exception):
            continue

        for k2, v2 in v1.items():
            if not v2 or isinstance(v2, Exception):
                continue

            if "units" in v2:
                units_present = True
                units = v2["units"]

            else:
                units_absent = True

    if units_absent and units_present:
        raise Exception("Different histogram formats on different nodes")

    return units


def parse_raw_histogram(
    histogram, histogram_data, logarithmic=False, new_histogram_version=False
):
    if not histogram_data or isinstance(histogram_data, Exception):
        return {}

    if not new_histogram_version:
        return _parse_old_histogram(histogram, histogram_data)

    if logarithmic:
        return _parse_new_log_histogram(histogram, histogram_data)

    return _parse_new_linear_histogram(histogram, histogram_data)


def is_new_histogram_version(version_):
    """
    Function takes version to check

    It returns true if version is supporting new histogram command else returns
    false
    """

    if not version_:
        return False

    if version.LooseVersion(version_) >= version.LooseVersion(
        constants.SERVER_NEW_HISTOGRAM_FIRST_VERSION
    ):
        return True

    return False


#################################


########## Latencies ##########
def is_new_latencies_version(version_):
    """
    Function takes a version to check

    It returns true if the version is supporting the new latencies command else
     returns false
    """

    if not version_:
        return False

    if version.LooseVersion(version_) >= version.LooseVersion(
        constants.SERVER_NEW_LATENCIES_CMD_FIRST_VERSION
    ):
        return True

    return False


#################################

########## System Collectinfo ##########


def _create_fail_string(cloud_provider):
    return "\nCould not determine if node is in {0}, check lsb_release, kernel name and dmesg manually".format(
        cloud_provider
    )


def _get_aws_metadata(response_str, prefix="", old_response=""):
    aws_c = ""
    aws_metadata_base_url = "http://169.254.169.254/latest/meta-data"

    # set of values which will give same old_response, so no need to go further
    last_values = []
    for rsp in response_str.split("\n"):
        if "credential" in rsp:
            # ignore credentials
            continue

        if rsp[-1:] == "/":
            rsp_p = rsp.strip("/")
            aws_c += _get_aws_metadata(rsp_p, prefix, old_response=old_response)
        else:
            urls_to_join = [aws_metadata_base_url, prefix, rsp]
            meta_url = "/".join(urls_to_join)
            req = urllib.request.Request(meta_url)
            r = urllib.request.urlopen(req)
            if r.code != 404:
                response = r.read().strip().decode("utf-8")
                if response == old_response:
                    last_values.append(rsp.strip())
                    continue
                try:
                    aws_c += _get_aws_metadata(
                        response, prefix + rsp + "/", old_response=response
                    )
                except Exception:
                    aws_c += (prefix + rsp).strip("/") + "\n" + response + "\n\n"

    if last_values:
        aws_c += prefix.strip("/") + "\n" + "\n".join(last_values) + "\n\n"

    return aws_c


def _check_cmds_for_str(cmds, strings):
    for cmd in cmds:
        try:
            output, _ = util.shell_command([cmd])

            for string in strings:
                if string in output:
                    return True

        except Exception:
            continue

    return False


def _collect_aws_data(cmd=""):
    aws_rsp = ""
    aws_timeout = 1
    socket.setdefaulttimeout(aws_timeout)
    aws_metadata_base_url = "http://169.254.169.254/latest/meta-data"
    cloud_provider = "AWS"
    out = "['" + cloud_provider + "']"
    grep_for = "Amazon"
    extra_cmds_to_check = [
        "lsb_release -a",
        "ls /etc|grep release|xargs -I f cat /etc/f",
    ]
    try:
        out += "\nRequesting . . . {0}".format(aws_metadata_base_url)
        req = urllib.request.Request(aws_metadata_base_url)
        r = urllib.request.urlopen(req)
        if r.code == 200:
            rsp = r.read().decode("utf-8")
            aws_rsp += _get_aws_metadata(rsp, "/")
            out += "\nSuccess! Resp: {0}".format(aws_rsp)
        else:
            out += "\nFailed! Response Code: {0}".format(r.code)
            out += "\nChecking {0} for '{1}'".format(extra_cmds_to_check, grep_for)
            if _check_cmds_for_str(extra_cmds_to_check, [grep_for]):
                out += "\nSuccess!"
            else:
                out += "\nFailed!"
                out += _create_fail_string(cloud_provider)

    except Exception as e:
        out += "\nFailed! Exception: {0}".format(e)
        out += "\nChecking [{0}] for {1}".format(extra_cmds_to_check, grep_for)
        if _check_cmds_for_str(extra_cmds_to_check, [grep_for]):
            out += "\nSuccess!"
        else:
            out += "\nFailed!"
            out += _create_fail_string(cloud_provider)

    return out, None


def _get_gce_metadata(response_str, fields_to_ignore=[], prefix=""):
    res_str = ""
    gce_metadata_base_url = "http://169.254.169.254/computeMetadata/v1/instance"

    for rsp in response_str.split("\n"):
        rsp = rsp.strip()
        if not rsp or rsp in fields_to_ignore:
            continue

        urls_to_join = [gce_metadata_base_url, prefix, rsp]
        meta_url = "/".join(urls_to_join)

        try:
            req = urllib.request.Request(
                meta_url, headers={"Metadata-Flavor": "Google"}
            )
            r = urllib.request.urlopen(req)

            if r.code != 404:
                response = r.read().strip().decode("utf-8")

                if rsp[-1:] == "/":
                    res_str += _get_gce_metadata(
                        response, fields_to_ignore=fields_to_ignore, prefix=prefix + rsp
                    )
                else:
                    res_str += prefix + rsp + "\n" + response + "\n\n"
        except Exception:
            pass

    return res_str


def _collect_gce_data(cmd=""):
    gce_timeout = 1
    socket.setdefaulttimeout(gce_timeout)
    gce_metadata_base_url = "http://169.254.169.254/computeMetadata/v1/instance"
    cloud_provider = "GCE"
    out = "['" + cloud_provider + "']"
    fields_to_ignore = ["attributes/"]

    try:
        out += "\nRequesting . . . {0}".format(gce_metadata_base_url)
        req = urllib.request.Request(
            gce_metadata_base_url, headers={"Metadata-Flavor": "Google"}
        )
        r = urllib.request.urlopen(req)

        if r.code == 200:
            rsp = r.read().decode("utf-8")
            gce_rsp = _get_gce_metadata(rsp, fields_to_ignore=fields_to_ignore)
            out += "\nSuccess! Resp: {0}".format(gce_rsp)
        else:
            out += "\nFailed! Resp Code: {0}".format(r.code)
            out += _create_fail_string(cloud_provider)

    except Exception as e:
        out += "\nFailed! Exception: {0}".format(e)
        out += _create_fail_string(cloud_provider)

    return out, None


def _collect_azure_data(cmd=""):
    azure_timeout = 1
    socket.setdefaulttimeout(azure_timeout)
    azure_metadata_base_url = (
        "http://169.254.169.254/metadata/instance?api-version=2017-04-02"
    )
    cloud_provider = "Azure"
    out = "['" + cloud_provider + "']"

    try:
        out += "\nRequesting . . . {0}".format(azure_metadata_base_url)
        req = urllib.request.Request(
            azure_metadata_base_url, headers={"Metadata": "true"}
        )
        r = urllib.request.urlopen(req)

        if r.code == 200:
            rsp = r.read().decode("utf-8")
            jsonObj = json.loads(rsp)
            out += "\nSuccess! Resp: {0}".format(
                json.dumps(jsonObj, sort_keys=True, indent=4, separators=(",", ": "))
            )
        else:
            out += "\nFailed! Response Code: {0}".format(r.code)
            out += _create_fail_string(cloud_provider)

    except Exception as e:
        out += "\nFailed! Exception: {0}".format(e)
        out += _create_fail_string(cloud_provider)

    return out, None


def _collect_cpuinfo(cmd=""):
    out = "['cpuinfo']"

    cpu_info_cmd = 'cat /proc/cpuinfo | grep "vendor_id"'
    o, e = util.shell_command([cpu_info_cmd])

    if o:
        o = o.strip().split("\n")
        cpu_info = {}

        for item in o:
            items = item.strip().split(":")

            if len(items) == 2:
                key = items[1].strip()
                if key in cpu_info.keys():
                    cpu_info[key] = cpu_info[key] + 1
                else:
                    cpu_info[key] = 1
        out += "\nvendor_id\tprocessor count"

        for key in cpu_info.keys():
            out += "\n" + key + "\t" + str(cpu_info[key])

    return out, None


def _collect_lsof(verbose=False):
    # Collect lsof data
    # If verbose true then returns whole output
    # If verbose false then returns count and type of fds for aerospike process

    out = "['lsof']"

    pids = get_asd_pids()

    o_dict = {}
    unidentified_protocol_count = 0
    type_ljust = 20
    desc_ljust = 20

    for pid in pids:
        cmd = "sudo lsof -n -p %s" % str(pid)
        o, e = util.shell_command([cmd])

        if e or not o:
            continue

        if verbose:
            out += "\n" + str(o)
            continue

        o_rows = o.strip().split("\n")

        # first line is header, so ignore it
        if "asd" not in o_rows[0]:
            o_rows = o_rows[1:]

        for row in o_rows:
            try:
                if "can't identify protocol" in row:
                    unidentified_protocol_count += 1

            except Exception:
                pass

            try:
                t = row.strip().split()[4]
                if t not in o_dict:
                    if len(t) > type_ljust:
                        type_ljust = len(t)

                    if (
                        t in data.lsof_file_type_desc
                        and len(data.lsof_file_type_desc[t]) > desc_ljust
                    ):
                        desc_ljust = len(data.lsof_file_type_desc[t])

                    o_dict[t] = 1
                else:
                    o_dict[t] += 1

            except Exception:
                continue

    if verbose:
        # sending actual output, no need to compute counts
        return out, None

    out += (
        "\n"
        + "FileType".ljust(type_ljust)
        + "Description".ljust(desc_ljust)
        + "fd count"
    )

    for ftype in sorted(o_dict.keys()):
        desc = "Unknown"
        if ftype in data.lsof_file_type_desc:
            desc = data.lsof_file_type_desc[ftype]

        out += (
            "\n" + ftype.ljust(type_ljust) + desc.ljust(desc_ljust) + str(o_dict[ftype])
        )

    out += "\n\n" + "Unidentified Protocols = " + str(unidentified_protocol_count)

    return out, None


def _collect_env_variables(cmd=""):
    # collets environment variables

    out = "['env_variables']"

    variables = [
        "ENTITLEMENT",
        "SERVICE_THREADS",
        "TRANSACTION_QUEUES",
        "TRANSACTION_THREADS_PER_QUEUE",
        "LOGFILE",
        "SERVICE_ADDRESS",
        "SERVICE_PORT",
        "HB_ADDRESS",
        "HB_PORT",
        "FABRIC_ADDRESS",
        "FABRIC_PORT",
        "INFO_ADDRESS",
        "INFO_PORT",
        "NAMESPACE",
        "REPL_FACTOR",
        "MEM_GB",
        "DEFAULT_TTL",
        "STORAGE_GB",
    ]

    for v in variables:
        out += "\n" + v + "=" + str(os.environ.get(v))

    return out, None


def _collect_ip_link_details(cmd=""):
    out = "['ip -s link']"

    cmd = "ip -s link"
    loop_count = 3
    sleep_seconds = 5

    for i in range(0, loop_count):
        o, e = util.shell_command([cmd])

        if o:
            out += "\n" + str(o) + "\n"
        time.sleep(sleep_seconds)

    return out, None


def _collectinfo_content(func, cmd=None, alt_cmds=[]):
    if cmd is None:
        cmd = []

    fname = ""
    try:
        fname = func.__name__
    except Exception:
        pass

    info_line = constants.COLLECTINFO_PROGRESS_MSG % (
        fname,
        (" %s" % (str(cmd)) if cmd else ""),
    )
    logger.info(info_line)

    o_line = constants.COLLECTINFO_SEPERATOR

    o, e = None, None

    if cmd:
        o_line += str(cmd) + "\n"

    failed_cmds = []

    try:
        o, e = func(cmd)
    except Exception as e:
        return o_line + str(e), failed_cmds

    if e:
        logger.warning(str(e))
        if func == util.shell_command:
            failed_cmds += cmd

        if alt_cmds:
            success = False
            for alt_cmd in alt_cmds:
                if not alt_cmd:
                    continue

                alt_cmd = [alt_cmd]
                info_line = (
                    "Data collection for alternative command %s %s  in progress..."
                    % (fname, str(alt_cmd))
                )
                logger.info(info_line)
                o_line += str(alt_cmd) + "\n"
                o_alt, e_alt = util.shell_command(alt_cmd)

                if e_alt:
                    e = e_alt

                else:
                    failed_cmds = []
                    success = True

                    if o_alt:
                        o = o_alt
                    break

            if not success:
                if alt_cmds:
                    failed_cmds += alt_cmds

    if o:
        o_line += str(o) + "\n"

    return o_line, failed_cmds


def _zip_files(dir_path, _size=1):
    """
    If file size is greater then given _size, create zip of file on same location and
    remove original one. Won't zip If zlib module is not available.
    """
    for root, dirs, files in os.walk(dir_path):
        for _file in files:
            file_path = os.path.join(root, _file)
            size_mb = os.path.getsize(file_path) // (1024 * 1024)
            if size_mb >= _size:
                os.chdir(root)
                try:
                    newzip = zipfile.ZipFile(_file + ".zip", "w", zipfile.ZIP_DEFLATED)
                    newzip.write(_file)
                    newzip.close()
                    os.remove(_file)
                except Exception as e:
                    print(e)


def get_system_commands(port=3000) -> list[list[str]]:
    """
    Returns list of system commands and their alternatives to gather output from. The
    unparsed output is then stored in a sysinfo.log. This list of commands is separate
    from what is found in class Node which are system commands which are parsed and stored
    as json to later be used in collectinfo mode's summary and health commands.
    """

    # Unfortunately timestamp cannot be printed in Centos with dmesg,
    # storing dmesg logs without timestamp for this particular OS.
    if "centos" == (distro.linux_distribution()[0]).lower():
        dmesg_cmds = ["sudo dmesg"]
    else:
        dmesg_cmds = ["sudo dmesg -T", "sudo dmesg"]

    if "darwin" in platform.system().lower():
        netstat_cmds = ["netstat -rn"]
    else:
        netstat_cmds = ["netstat"]

    # cmd and alternative cmds are stored in list of list instead of dic to
    # maintain proper order for output

    sys_shell_cmds: list[list[str]] = [
        ["hostname -I", "hostname"],
        ["top -n3 -b", "top -l 3"],
        ["lsb_release -a", "ls /etc|grep release|xargs -I f cat /etc/f"],
        ["sudo lshw -class system"],
        ["cat /proc/meminfo", "vmstat -s"],
        ["cat /proc/interrupts"],
        ["iostat -y -x 5 4"],
        dmesg_cmds,
        ['sudo pgrep asd | xargs -I f sh -c "cat /proc/f/limits"'],
        ["lscpu"],
        ['sudo sysctl -a | grep -E "shmmax|file-max|maxfiles"'],
        ["sudo iptables -L -vn"],
        [
            'sudo fdisk -l |grep Disk |grep dev | cut -d " " -f 2 | cut -d ":" -f 1 | xargs sudo hdparm -I 2>/dev/null'
        ],
        ["df -h"],
        ["mount"],
        ["lsblk"],
        ["free -m"],
        ["uname -a"],
        [
            'sudo netstat -i | tr -s [:blank:] | cut -d" " -f1 | tail -n +3 | grep -v -E "lo|docker" | xargs --max-lines=1 -i{} sh -c "echo ethtool -S {}; ethtool -S {}"'
        ],
        # Only in Pretty Print
        ["dmidecode -s system-product-name"],
        ["systemd-detect-virt"],
        ["cat /sys/class/dmi/id/product_name"],
        ["cat /sys/class/dmi/id/sys_vendor"],
        ["cat /sys/kernel/mm/*transparent_hugepage/enabled"],
        ["cat /sys/kernel/mm/*transparent_hugepage/defrag"],
        ["cat /sys/kernel/mm/*transparent_hugepage/khugepaged/defrag"],
        ["sysctl vm.min_free_kbytes"],
        ["ps -eo rss,vsz,comm |grep asd"],
        ["cat /proc/partitions", "fdisk -l"],
        [
            'ls /sys/block/{sd*,xvd*,nvme*}/queue/rotational |xargs -I f sh -c "echo f; cat f;"'
        ],
        [
            'ls /sys/block/{sd*,xvd*,nvme*}/device/model |xargs -I f sh -c "echo f; cat f;"'
        ],
        [
            'ls /sys/block/{sd*,xvd*,nvme*}/queue/scheduler |xargs -I f sh -c "echo f; cat f;"'
        ],
        ['rpm -qa|grep -E "citrus|aero"', 'dpkg -l|grep -E "citrus|aero"'],
        ["ip addr"],
        ["sar -n DEV"],
        ["sar -n EDEV"],
        ["mpstat -P ALL 2 3"],
        ["uptime"],
        netstat_cmds,
        [
            "ss -ant state time-wait sport = :%d or dport = :%d | wc -l" % (port, port),
            "netstat -ant | grep %d | grep TIME_WAIT | wc -l" % (port),
        ],
        [
            "ss -ant state close-wait sport = :%d or dport = :%d | wc -l"
            % (port, port),
            "netstat -ant | grep %d | grep CLOSE_WAIT | wc -l" % (port),
        ],
        [
            "ss -ant state established sport = :%d or dport = :%d | wc -l"
            % (port, port),
            "netstat -ant | grep %d | grep ESTABLISHED | wc -l" % (port),
        ],
        [
            "ss -ant state listen sport = :%d or dport = :%d |  wc -l" % (port, port),
            "netstat -ant | grep %d | grep LISTEN | wc -l" % (port),
        ],
        ['arp -n|grep ether|tr -s [:blank:] | cut -d" " -f5 |sort|uniq -c'],
        [
            r'find /proc/sys/net/ipv4/neigh/default/ -name "gc_thresh*" -print -exec cat {} \;'
        ],
    ]

    uid = os.getuid()

    """
    Some distros and most containers do not have sudo installed by default. If running
    as root don't use it. 
    """
    if uid == 0:
        for cmd_list in sys_shell_cmds:
            for idx, cmd in enumerate(cmd_list):
                if "sudo " in cmd:
                    cmd_list[idx] = cmd.replace("sudo ", "")

    return sys_shell_cmds


def get_asd_pids():
    pids = []
    ps_cmd = 'sudo ps aux|grep -v grep|grep -E "asd|cld"'
    ps_o, ps_e = util.shell_command([ps_cmd])
    if ps_o:
        ps_o = ps_o.strip().split("\n")
        pids = []
        for item in ps_o:
            vals = item.strip().split()
            if len(vals) >= 2:
                pids.append(vals[1])
    return pids


def set_collectinfo_path(timestamp, output_prefix=""):
    output_time = time.strftime("%Y%m%d_%H%M%S", timestamp)

    if output_prefix:
        output_prefix = str(output_prefix).strip()

    aslogdir_prefix = ""
    if output_prefix:
        aslogdir_prefix = "%s%s" % (
            str(output_prefix),
            "_"
            if output_prefix
            and not output_prefix.endswith("-")
            and not output_prefix.endswith("_")
            else "",
        )

    aslogdir = "/tmp/%scollect_info_" % (aslogdir_prefix) + output_time
    as_logfile_prefix = aslogdir + "/" + output_time + "_"

    os.makedirs(aslogdir)

    return aslogdir, as_logfile_prefix


def archive_log(logdir):
    _zip_files(logdir)
    util.shell_command(["tar -czvf " + logdir + ".tgz " + logdir])
    print("\n\n\n")
    logger.info("Files in " + logdir + " and " + logdir + ".tgz saved.")


def print_collectinfo_summary(logdir, failed_cmds):
    if failed_cmds:
        logger.warning(
            "Following commands are either unavailable or giving runtime error..."
        )
        logger.warning(list(set(failed_cmds)))

    print("\n")
    logger.info("Please provide file " + logdir + ".tgz to Aerospike Support.")
    logger.info("END OF ASCOLLECTINFO")

    # If multiple commands are given in execute_only mode then we might need coloring for next commands
    terminal.enable_color(True)


def collect_sys_info(port=3000, timestamp="", outfile=""):
    failed_cmds = []

    cluster_online = True
    aslogdir = ""

    if not timestamp:
        cluster_online = False
        ts = time.gmtime()
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S UTC\n", ts)
        aslogdir, as_logfile_prefix = set_collectinfo_path(ts)
        outfile = as_logfile_prefix + "sysinfo.log"

    util.write_to_file(outfile, timestamp)

    try:
        for cmds in get_system_commands(port=port):
            o, f_cmds = _collectinfo_content(
                func=util.shell_command,
                cmd=cmds[0:1],
                alt_cmds=cmds[1:] if len(cmds) > 1 else [],
            )
            failed_cmds += f_cmds
            util.write_to_file(outfile, o)
    except Exception as e:
        print(e)
        util.write_to_file(outfile, str(e))

    try:
        o, f_cmds = _collectinfo_content(func=_collect_cpuinfo)
        util.write_to_file(outfile, o)
    except Exception as e:
        util.write_to_file(outfile, str(e))

    try:
        o, f_cmds = _collectinfo_content(func=_collect_aws_data)
        util.write_to_file(outfile, o)
    except Exception as e:
        util.write_to_file(outfile, str(e))

    try:
        o, f_cmds = _collectinfo_content(func=_collect_gce_data)
        util.write_to_file(outfile, o)
    except Exception as e:
        util.write_to_file(outfile, str(e))

    try:
        o, f_cmds = _collectinfo_content(func=_collect_azure_data)
        util.write_to_file(outfile, o)
    except Exception as e:
        util.write_to_file(outfile, str(e))

    try:
        o, f_cmds = _collectinfo_content(func=_collect_lsof)
        util.write_to_file(outfile, o)
    except Exception as e:
        util.write_to_file(outfile, str(e))

    try:
        o, f_cmds = _collectinfo_content(func=_collect_env_variables)
        util.write_to_file(outfile, o)
    except Exception as e:
        util.write_to_file(outfile, str(e))

    try:
        o, f_cmds = _collectinfo_content(func=_collect_ip_link_details)
        util.write_to_file(outfile, o)
    except Exception as e:
        util.write_to_file(outfile, str(e))

    if not cluster_online:
        # Cluster is offline so collecting only system info and archiving files
        archive_log(aslogdir)
        print_collectinfo_summary(aslogdir, failed_cmds=failed_cmds)

    return failed_cmds


########################################


def format_xdr5_configs(xdr_configs, for_mods=[]):
    """Needed in both collectinfoanalyzer and basiccontroller.  This would not
    be needed if collectinfo could load this format but it cannot since the "node"
    is not the top level key

    Sample Input:
    {
        '192.168.173.203:3000': {
            'dc_configs': {
                'DC1': {
                    'node-address-port': '',
                    . . .
                },
                'DC2': {
                    'node-address-port': '',
                    . . .
                }
            },
            'ns_configs': {
                'DC1': {
                    'test': {
                        'enabled': 'true',
                        . . .
                    }
                },
                'DC2': {
                    'bar': {
                        'enabled': 'true',
                        . . .
                    }
                }
            },
            'xdr_configs': {
                'dcs': 'DC1,DC2',
                'trace-fraction': '0'
            }
        }
    }
    Sample Output:
    {
        'xdr_configs': {
            '192.168.173.203:3000': {
                'dcs': 'DC1,DC2', 'trace-fraction': '0'
            }
        },
        'dc_configs': {
            'DC1': {
                '192.168.173.203:3000': {
                    'node-address-port': '',
                     . . .
                }
            },
            'DC2': {
                '192.168.173.203:3000': {
                    'node-address-port': '',
                     . . .
                }
            }
        },
        'ns_configs': {
            'DC1': {
                '192.168.173.203:3000': {
                    'test': {
                        'enabled': 'true',
                         . . .
                    }
                }
            },
            'DC2': {
                '192.168.173.203:3000': {
                    'bar': {
                        'enabled': 'true',
                         . . .
                    }
                }
            }
        }
    }
    """
    # Filter configs for data-center
    if for_mods:
        xdr_dc = for_mods[0]

        for config in xdr_configs.values():
            # There is only one dc config per dc
            try:
                dc_configs_matches = util.filter_list(config["dc_configs"], [xdr_dc])
            except KeyError:
                dc_configs_matches = []

            try:
                ns_configs_matches = util.filter_list(config["ns_configs"], [xdr_dc])
            except KeyError:
                ns_configs_matches = []

            config["dc_configs"] = {
                dc: config["dc_configs"][dc] for dc in dc_configs_matches
            }
            config["ns_configs"] = {
                dc: config["ns_configs"][dc] for dc in ns_configs_matches
            }

            # There can be multiple namespace configs per dc
            if len(for_mods) >= 2:
                xdr_ns = for_mods[1]
                for dc in config["ns_configs"]:
                    try:
                        ns_matches = util.filter_list(
                            config["ns_configs"][dc], [xdr_ns]
                        )
                    except KeyError:
                        ns_matches = []

                    config["ns_configs"][dc] = {
                        ns: config["ns_configs"][dc][ns] for ns in ns_matches
                    }

    formatted_xdr_configs = {}

    try:
        for node in xdr_configs:
            formatted_xdr_configs[node] = xdr_configs[node]["xdr_configs"]

        formatted_dc_configs = {}

        for node in xdr_configs:
            for dc in xdr_configs[node]["dc_configs"]:
                if dc not in formatted_dc_configs:
                    formatted_dc_configs[dc] = {}

                formatted_dc_configs[dc][node] = xdr_configs[node]["dc_configs"][dc]

        formatted_ns_configs = {}

        for node in xdr_configs:
            for dc in xdr_configs[node]["ns_configs"]:
                if dc not in formatted_ns_configs:
                    formatted_ns_configs[dc] = {}

                if node not in formatted_ns_configs[dc]:
                    formatted_ns_configs[dc][node] = {}

                for ns in xdr_configs[node]["ns_configs"][dc]:
                    formatted_ns_configs[dc][node][ns] = xdr_configs[node][
                        "ns_configs"
                    ][dc][ns]

    # A Key error is possible if the incomming data has the wrong schema.
    # This can happen on asadm < 1.0.2 on server >= 5.0
    except KeyError:
        return {}

    formatted_configs = {
        "xdr_configs": formatted_xdr_configs,
        "dc_configs": formatted_dc_configs,
        "ns_configs": formatted_ns_configs,
    }

    return formatted_configs
