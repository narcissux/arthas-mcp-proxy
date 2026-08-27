"""B1-1 remote JVM inventory collection."""

from __future__ import annotations

import pytest

from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.process_inventory import (
    collect_inventory,
    parse_cmdline,
    parse_stat_start_time,
    parse_status_uid,
)

STAT_TEMPLATE = (
    "4242 (java) S 1 4242 4242 0 0 0 0 0 0 0 0 0 0 0 20 0 1 0 17000 "
    "0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0"
)


def test_b1_1_a_cmdline_nuls_become_spaces() -> None:
    assert parse_cmdline("java\x00-jar\x00app.jar\x00") == "java -jar app.jar"


def test_b1_1_b_stat_field_22_kept_as_ticks() -> None:
    assert parse_stat_start_time(STAT_TEMPLATE) == "17000"


def test_b1_1_c_boot_id_is_exact_uuid() -> None:
    boot_id = "2f4c1b6a-9d3e-4a10-8c2b-77e0d1a2b3c4"
    records = collect_inventory(
        proc_available=True,
        boot_id=boot_id,
        proc_processes=[
            (4242, "java\x00-jar\x00app.jar", STAT_TEMPLATE, "Uid:\t1000\t1000\t1000\t1000\n")
        ],
        passwd="appuser:x:1000:1000::/home/appuser:/bin/sh\n",
    )
    assert records[0].boot_id == boot_id


def test_b1_1_d_owner_from_status_uid_and_passwd() -> None:
    records = collect_inventory(
        proc_available=True,
        boot_id="boot",
        proc_processes=[
            (4242, "java\x00-jar\x00app.jar", STAT_TEMPLATE, "Uid:\t1000\t1000\t1000\t1000\n")
        ],
        passwd="appuser:x:1000:1000::/home/appuser:/bin/sh\n",
    )
    assert records[0].owner == "appuser"
    assert records[0].owner


def test_b1_1_e_jps_fallback_does_not_invent_identity() -> None:
    records = collect_inventory(
        proc_available=False,
        jps_available=True,
        jps_lines=["4242 com.example.OrderService"],
    )
    assert len(records) == 1
    assert records[0].pid == 4242
    assert records[0].command == "com.example.OrderService"
    assert records[0].start_time is None
    assert records[0].boot_id is None


def test_b1_1_f_both_sources_fail_is_structured_error() -> None:
    with pytest.raises(DomainError) as exc:
        collect_inventory(proc_available=False, jps_available=False)
    assert exc.value.code in {ErrorCode.JVM_NOT_FOUND, ErrorCode.SSH_COMMAND_TIMEOUT}


def test_b1_1_g_jps_and_arthas_processes_are_filtered() -> None:
    records = collect_inventory(
        proc_available=False,
        jps_available=True,
        jps_lines=[
            "100 sun.tools.jps.Jps -l -m",
            "101 arthas-boot.jar",
            "4242 com.example.OrderService",
        ],
    )
    assert [record.pid for record in records] == [4242]


def test_parse_status_uid() -> None:
    assert parse_status_uid("Uid:\t1000\t1000\t1000\t1000\nGid:\t1000\n") == 1000
