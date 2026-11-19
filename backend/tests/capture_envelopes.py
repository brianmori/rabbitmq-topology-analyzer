"""Shared real-SDK inputs for upload and static-catalogue integration tests."""

import pytest

CAPTURE_PRIVATE_MARKER = "SYNTHETIC_CAPTURE_SECRET_DO_NOT_EXPOSE"

INVALID_CAPTURE_ENVELOPES = (
    pytest.param({}, id="empty-object"),
    pytest.param(
        {"error": CAPTURE_PRIVATE_MARKER, "reason": CAPTURE_PRIVATE_MARKER, "vhosts": []},
        id="error-response-with-definitions-section",
    ),
    pytest.param(
        {
            "original_vhost_name": CAPTURE_PRIVATE_MARKER,
            "queues": [],
            "exchanges": [],
            "bindings": [],
        },
        id="single-vhost-without-format",
    ),
    pytest.param(
        {
            "rabbitmq_definition_format": "single_virtual_host",
            "original_vhost_name": CAPTURE_PRIVATE_MARKER,
            "queues": [],
            "exchanges": [],
            "bindings": [],
        },
        id="single-vhost-with-format",
    ),
    pytest.param(
        {"rabbitmq_definition_format": "single_virtual_host", "vhosts": []},
        id="single-vhost-format-with-cluster-section",
    ),
    pytest.param(
        {"rabbitmq_definition_format": CAPTURE_PRIVATE_MARKER, "vhosts": []},
        id="incompatible-format",
    ),
)

VALID_CAPTURE_ENVELOPES = (
    pytest.param({"vhosts": []}, 0, id="empty-cluster-without-metadata"),
    pytest.param(
        {"rabbitmq_definition_format": "cluster", "vhosts": []},
        0,
        id="explicit-empty-cluster",
    ),
    pytest.param(
        {
            "rabbit_version": "3.8.34",
            "vhosts": [{"name": "/"}],
            "queues": [],
            "exchanges": [],
            "bindings": [],
        },
        1,
        id="older-cluster-without-format",
    ),
)
