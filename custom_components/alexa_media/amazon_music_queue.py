"""Play Amazon Music tracks or albums on an Echo by ASIN.

Amazon starts specific tracks on an Echo in two calls, the same ones the Amazon
Music app makes when it casts to an Echo: AppSync ``InitiateQueue`` creates a
queue that holds the tracks (or the albums) and targets the Echo, then the Music Play Queue
Service ``transferQueue`` starts that queue on the Echo. Unlike voice search this
plays the exact recording, and the Echo does not announce it. Both endpoints are
unofficial and may change without notice.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError

_LOGGER = logging.getLogger(__name__)

APPSYNC_URL = "https://l3fxpyai3bhcni5phkv5l5n5nm.appsync-api.eu-south-2.amazonaws.com/graphql"
MPQS_URL = "https://music.amazon.com/ZAZ/api/mpqs/voiceenabled/"

# alexapy registers its login as the Alexa iOS app (alexalogin.get_tokens). The
# queue API wants the device registration the access token belongs to.
ALEXAPY_DEVICE_TYPE = "A2IVLV5VM2W81"

# Amazon catalog IDs of tracks and albums, such as B09C48YPXX.
ASIN_PATTERN = re.compile(r"[A-Z0-9]{10}")

TRANSFER_QUEUE_TARGET = (
    "com.amazon.musicplayqueueservice.model.client.external.voiceenabled."
    "MusicPlayQueueServiceExternalVoiceEnabledClient.transferQueue"
)

INITIATE_QUEUE_MUTATION = """mutation InitiateQueue($input: InitiateQueueInput!) {
  initiateQueue(input: $input) {
    queueMetadata { queueId }
    genericQueueEntity { entityReferenceId }
  }
}"""


async def play_asins(login, *, customer_id: str, serial: str, device_type: str, asins: list[str]) -> None:
    """Start the tracks ``asins`` on the Echo ``serial``, in this order.

    Raises HomeAssistantError when the token cannot be refreshed or Amazon refuses
    either call, so the caller learns that nothing is playing.
    """
    if not asins:
        raise HomeAssistantError("No Amazon Music track ID given")
    malformed = [asin for asin in asins if not ASIN_PATTERN.fullmatch(asin)]
    if malformed:
        # Checked before Amazon sees it: a pasted share link would otherwise fail with an unclear Amazon error.
        raise HomeAssistantError(f"Not Amazon Music track IDs: {', '.join(malformed)}")
    queue_seed = {
        "playModeEnumValue": "ON_DEMAND",
        # Required; the Amazon Music app sends this legacy value next to the real enum value.
        "queueSeedType": "QUEUE_ID_HISTORICAL_PLAYBACK",
        "queueSeedTypeEnumValue": "TRACK_LIST",
        # metricsContext is required; the app fills it with analytics tuples, Amazon accepts it empty.
        "trackList": {
            "metricsContext": {"metricTuple": []},
            "tracks": [{"idType": "ASIN", "id": asin} for asin in asins],
        },
    }
    start_at = {"offsetInMs": 0, "queueEntityId": asins[0], "queueEntityIdType": "CATALOG_TRACK_ID"}
    await _start_queue(
        login, customer_id, serial, device_type, [queue_seed], start_at, content=f"ASINs {', '.join(asins)}"
    )


async def play_albums(login, *, customer_id: str, serial: str, device_type: str, album_asins: list[str]) -> None:
    """Start the albums ``album_asins`` on the Echo ``serial``, one after the other, from the first one's first track.

    Raises HomeAssistantError like ``play_asins``.
    """
    if not album_asins:
        raise HomeAssistantError("No Amazon Music album ID given")
    malformed = [album_asin for album_asin in album_asins if not ASIN_PATTERN.fullmatch(album_asin)]
    if malformed:
        raise HomeAssistantError(f"Not Amazon Music album IDs: {', '.join(malformed)}")
    # The seed the Amazon Music app sends when it casts an album (captured 2026-11-16). Sending
    # the album ID as a track instead fails with a DependencyException. The app sends one album
    # per request; Amazon also queues several, in order (tested 2026-11-17 with two).
    queue_seeds = [
        {
            "seedContext": [],
            "playModeEnumValue": "ON_DEMAND",
            "queueSeedType": "QUEUE_ID_HISTORICAL_PLAYBACK",
            "queueSeedTypeEnumValue": "PLAYABLE_ENTITY_ID",
            "playableEntity": {"idType": "ASIN", "id": album_asin, "metricsContext": {"metricTuple": []}},
        }
        for album_asin in album_asins
    ]
    # No start position, like the app: Amazon starts at the first album's first track.
    await _start_queue(
        login, customer_id, serial, device_type, queue_seeds, None, content=f"albums {', '.join(album_asins)}"
    )


async def _start_queue(
    login,
    customer_id: str,
    serial: str,
    device_type: str,
    queue_seeds: list[dict[str, Any]],
    start_at: dict[str, Any] | None,
    *,
    content: str,
) -> None:
    await _ensure_fresh_access_token(login)
    target_id = f"{customer_id}-{serial}-{device_type}"
    queue_id, entity_reference_id = await _initiate_queue(
        login, customer_id, target_id, device_type, queue_seeds, start_at, content=content
    )
    await _transfer_queue(login, target_id, queue_id, entity_reference_id)
    _LOGGER.debug("Started %s on %s", content, serial)


async def _ensure_fresh_access_token(login) -> None:
    # The same check and refresh sequence alexapy's AlexaAPI runs before each of its own requests.
    async with login._oauth_lock:
        if not (login.expires_in and login.expires_in - time.time() < 0):
            return
        refreshed = (
            await login.refresh_access_token()
            and await login.exchange_token_for_cookies()
            and await login.get_csrf()
        )
        if not refreshed:
            # Clearing the stored tokens and starting reauthentication stays with alexapy's own requests.
            raise HomeAssistantError("Amazon access token expired and could not be refreshed")
        await login.finalize_login()


async def _initiate_queue(
    login,
    customer_id: str,
    target_id: str,
    device_type: str,
    queue_seeds: list[dict[str, Any]],
    start_at: dict[str, Any] | None,
    *,
    content: str,
) -> tuple[str, str]:
    authorization = json.dumps(
        {
            "accessToken": login.access_token,
            "customerId": customer_id,
            "deviceId": login.uuid,
            "deviceType": ALEXAPY_DEVICE_TYPE,
        },
        separators=(",", ":"),
    )
    queue_input = {
        "targetDeviceContext": {"deviceType": device_type, "deviceId": target_id},
        "deviceContext": {"deviceType": ALEXAPY_DEVICE_TYPE, "deviceId": login.uuid},
        "playbackOptions": {"shuffleMode": "SHUFFLE_OFF", "sequencingRuleEnumValue": "SHUFFLE_OFF", "loopMode": "LOOP_OFF"},
        "queueSeeds": queue_seeds,
        "allowedParentalControls": {"hasExplicitLanguage": True},
        "customerId": customer_id,
    }
    if start_at is not None:
        queue_input["startAt"] = start_at
    headers = {
        "authorization": authorization,
        "x-amz-date": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
    }
    status, response = await _post_json(
        login, APPSYNC_URL, {"query": INITIATE_QUEUE_MUTATION, "variables": {"input": queue_input}}, headers
    )
    initiated = ((response.get("data") or {}).get("initiateQueue") if isinstance(response, dict) else None)
    if status != 200 or not initiated:
        raise HomeAssistantError(
            f"Amazon InitiateQueue failed for {content}: HTTP {status} {_error_summary(response)}"
        )
    return initiated["queueMetadata"]["queueId"], initiated["genericQueueEntity"]["entityReferenceId"]


async def _transfer_queue(login, target_id: str, queue_id: str, entity_reference_id: str) -> None:
    request = {
        "__type": "com.amazon.musicplayqueueservice.client.common#TransferQueueRequest",
        "targetId": target_id,
        "queueId": queue_id,
        "startAt": {
            "trackIdentifier": entity_reference_id,
            "trackIdentifierType": "ENTITY_REFERENCE_ID",
            "positionInMs": 0,
        },
        "playbackPreference": "IMMERSIVE",
        "allowedParentalControls": {"hasExplicitLanguage": True},
    }
    headers = {
        "Accept": "application/json",
        "Content-Encoding": "amz-1.0",
        "x-amz-target": TRANSFER_QUEUE_TARGET,
        "x-amz-access-token": login.access_token,
    }
    status, response = await _post_json(login, MPQS_URL, request, headers)
    if status != 200 or not isinstance(response, dict) or response.get("status") != "SUCCESS":
        raise HomeAssistantError(f"Amazon transferQueue failed: HTTP {status} {_error_summary(response)}")


async def _post_json(login, url: str, payload: dict[str, Any], headers: dict[str, str]) -> tuple[int, Any]:
    async with login.session.post(url, json=payload, headers=headers) as response:
        status = response.status
        text = await response.text()
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def _error_summary(response: Any) -> str:
    """Amazon's own error wording: GraphQL error messages or the Coral exception type."""
    if isinstance(response, dict):
        if response.get("errors"):
            return "; ".join(str(error.get("message")) for error in response["errors"])
        if response.get("__type"):
            return str(response["__type"])
    return str(response)[:200]
