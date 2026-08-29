"""B2-1: application match evidence rules (auto mode, no default regex)."""

from __future__ import annotations

import pytest

from arthas_mcp_proxy.application_resolver import (
    find_java_application,
    match_application,
    normalize_application_name,
)
from arthas_mcp_proxy.errors import DomainError
from arthas_mcp_proxy.models import ErrorCode
from arthas_mcp_proxy.process_inventory import ProcessRecord


@pytest.mark.unit
def test_b2_1_a_unique_inventory_service_jar() -> None:
    """B2-1-a: unique inventory-service.jar → match, evidence=jar_basename."""
    command = "java -jar /opt/apps/inventory-service.jar --server.port=8080"
    matched, evidence = match_application(command, "inventory-service.jar")
    assert matched is True
    assert evidence == "jar_basename"

    result = find_java_application(
        [ProcessRecord(pid=4242, command=command)],
        "inventory-service.jar",
    )
    assert result.pid == 4242
    assert result.match_evidence == "jar_basename"


@pytest.mark.unit
def test_b2_1_b_spring_application_name() -> None:
    """B2-1-b: -Dspring.application.name=billing-service → spring_application_name."""
    command = "java -Dspring.application.name=billing-service -jar app.jar"
    matched, evidence = match_application(command, "billing-service")
    assert matched is True
    assert evidence == "spring_application_name"

    # VALUE must be exact: not a prefix of a longer value (start/space/end bounds).
    prefix_cmd = "java -Dspring.application.name=billing-service-extra -jar app.jar"
    prefix_matched, prefix_evidence = match_application(prefix_cmd, "billing-service")
    assert prefix_matched is False
    assert prefix_evidence is None


@pytest.mark.unit
def test_b2_1_c_main_simple_order_app() -> None:
    """B2-1-c: main com.foo.OrderApp query OrderApp → evidence=main_simple."""
    matched, evidence = match_application("java com.foo.OrderApp", "OrderApp")
    assert matched is True
    assert evidence == "main_simple"


@pytest.mark.unit
def test_b2_1_d_query_order_does_not_match_order_service_jar() -> None:
    """B2-1-d: query Order vs OrderService.jar → not found (no prefix fuzzy)."""
    matched, evidence = match_application("java -jar OrderService.jar", "Order")
    assert matched is False
    assert evidence is None

    with pytest.raises(DomainError) as exc_info:
        find_java_application(
            [ProcessRecord(pid=7, command="java -jar OrderService.jar")],
            "Order",
        )
    assert exc_info.value.code is ErrorCode.JVM_NOT_FOUND


@pytest.mark.unit
def test_b2_1_e_normalize_hyphen_underscore_jar() -> None:
    """B2-1-e: Order-Service vs order_service.jar after locked normalize."""
    # Lock: lowercase, treat '-' and '_' as equivalent.
    assert normalize_application_name("Order-Service") == "order_service"
    assert normalize_application_name("order_service") == "order_service"
    assert normalize_application_name("ORDER_SERVICE") == "order_service"
    assert normalize_application_name("Order-Service") == normalize_application_name(
        "order_service"
    )

    matched, evidence = match_application("java -jar order_service.jar", "Order-Service")
    assert matched is True
    assert evidence == "jar_basename"


@pytest.mark.unit
def test_b2_1_f_default_mode_is_not_regex() -> None:
    """B2-1-f: default match_mode is not regex; foo.* must not match com.foo.Bar."""
    matched, evidence = match_application("java com.foo.Bar", "foo.*")
    assert matched is False
    assert evidence is None

    explicit_auto, _ = match_application("java com.foo.Bar", "foo.*", match_mode="auto")
    assert explicit_auto is False


@pytest.mark.unit
def test_b2_1_g_regex_mode_rejected() -> None:
    """B2-1-g: match_mode=regex → INVALID_ARGUMENT (regex not implemented this cell)."""
    with pytest.raises(DomainError) as exc_info:
        match_application("java com.foo.Bar", "foo.*", match_mode="regex")
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT

    with pytest.raises(DomainError) as exc_info:
        find_java_application(
            [ProcessRecord(pid=1, command="java com.foo.Bar")],
            "foo.*",
            match_mode="regex",
        )
    assert exc_info.value.code is ErrorCode.INVALID_ARGUMENT


@pytest.mark.unit
def test_b2_1_first_hit_prefers_jar_over_spring() -> None:
    """Priority lock: jar_basename wins when the same query also matches Spring."""
    command = (
        "java -Dspring.application.name=inventory-service "
        "-jar /opt/apps/inventory-service.jar --server.port=8080"
    )
    matched, evidence = match_application(command, "inventory-service")
    assert matched is True
    assert evidence == "jar_basename"
