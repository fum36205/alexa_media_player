"""Tests for playing Amazon Music tracks on an Echo by ASIN.

Amazon starts specific tracks on an Echo in two calls: AppSync ``InitiateQueue``
creates a queue holding the tracks with the Echo as target, then the Music Play
Queue Service ``transferQueue`` starts that queue on the Echo. Unlike voice
search, this plays the exact recording and the Echo does not announce it.
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.alexa_media.amazon_music_queue import (
    APPSYNC_URL,
    MPQS_URL,
    play_albums,
    play_asins,
)
from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.media_player import AlexaClient

# Real Amazon responses recorded on 2026-11-14/15. Account-specific IDs (queue
# and queue-entry IDs) are replaced; catalog data is kept.
INITIATE_QUEUE_RESPONSE = {
    "data": {
        "initiateQueue": {
            "queueMetadata": {
                "queueId": "9c1f3e52-6a1d-4c2b-8f0e-2d7b5a4c1e90",
                "title": "Hedwig's Theme",
            },
            "genericQueueEntity": {
                "entityReferenceId": "4b8e2a17-93c5-4f6d-a0b1-7e3c9d2f5a68",
                "identifier": "B001S0HA3G",
                "identifierType": "CATALOG_TRACK_ID",
                "title": "Hedwig's Theme",
            },
        }
    }
}
# Recorded 2026-11-16 when the Amazon Music app cast the album "Sun" (B09C46238P):
# Amazon starts the queue at the album's first track, "Before Time". Sending "Sun"
# and "Evolve" in one request on 2026-11-17 got the same answer.
INITIATE_QUEUE_ALBUM_RESPONSE = {
    "data": {
        "initiateQueue": {
            "queueMetadata": {
                "queueId": "5e0b7c21-3f4a-4d8e-9b16-c2a7f0d93e45",
                "title": "Sun",
            },
            "genericQueueEntity": {
                "entityReferenceId": "a3c9e1f7-2b5d-4e08-8f6a-71d4b0c2e953",
                "identifier": "B09C473JSN",
                "identifierType": "CATALOG_TRACK_ID",
                "title": "Before Time",
            },
        }
    }
}
# Sent with a required input field missing; AppSync answers HTTP 200 with errors.
INITIATE_QUEUE_GRAPHQL_ERROR = {
    "data": None,
    "errors": [
        {
            "path": None,
            "locations": [{"line": 1, "column": 24, "sourceName": None}],
            "message": "Variable 'input' has coerced Null value for NonNull type 'MetricsContextInput!'",
        }
    ],
}
TRANSFER_QUEUE_SUCCESS = {"status": "SUCCESS"}
# What transferQueue answers (HTTP 500) for a queue it does not know.
TRANSFER_QUEUE_INTERNAL_FAILURE = {"__type": "com.amazon.coral.service#InternalFailure"}

EMAIL = "test@example.com"


@pytest.fixture
async def login(aioclient_mock):
    """Return an alexapy login whose HTTP session is answered by aioclient_mock."""
    alexapy_login = MagicMock()
    alexapy_login.email = EMAIL
    alexapy_login.access_token = "test-access-token"
    alexapy_login.expires_in = time.time() + 3600
    alexapy_login.uuid = "0123456789abcdef0123456789abcdef"
    alexapy_login._oauth_lock = asyncio.Lock()
    alexapy_login.session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield alexapy_login
    await alexapy_login.session.close()


def _requests(aioclient_mock):
    """Return the recorded requests as (url, json body, headers)."""
    return [(str(url), body, headers) for _, url, body, headers in aioclient_mock.mock_calls]


async def test_transfers_the_queue_that_initiate_queue_created(login, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_RESPONSE)
    aioclient_mock.post(MPQS_URL, json=TRANSFER_QUEUE_SUCCESS)

    await play_asins(
        login, customer_id="CUSTOMER", serial="ECHO_SERIAL", device_type="ECHO_TYPE", asins=["B001S0HA3G"]
    )

    url, body, headers = _requests(aioclient_mock)[1]
    assert url == MPQS_URL
    assert headers["x-amz-target"].endswith("MusicPlayQueueServiceExternalVoiceEnabledClient.transferQueue")
    assert headers["x-amz-access-token"] == "test-access-token"
    assert body["targetId"] == "CUSTOMER-ECHO_SERIAL-ECHO_TYPE"
    assert body["queueId"] == "9c1f3e52-6a1d-4c2b-8f0e-2d7b5a4c1e90"
    assert body["startAt"] == {
        "trackIdentifier": "4b8e2a17-93c5-4f6d-a0b1-7e3c9d2f5a68",
        "trackIdentifierType": "ENTITY_REFERENCE_ID",
        "positionInMs": 0,
    }


async def test_initiate_queue_asks_for_the_asins_on_the_echo_in_order(login, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_RESPONSE)
    aioclient_mock.post(MPQS_URL, json=TRANSFER_QUEUE_SUCCESS)

    await play_asins(
        login,
        customer_id="CUSTOMER",
        serial="ECHO_SERIAL",
        device_type="ECHO_TYPE",
        asins=["B09C48YPXX", "B076NWZ659", "B079VRV49X"],
    )

    url, body, headers = _requests(aioclient_mock)[0]
    assert url == APPSYNC_URL
    assert json.loads(headers["authorization"]) == {
        "accessToken": "test-access-token",
        "customerId": "CUSTOMER",
        "deviceId": "0123456789abcdef0123456789abcdef",
        "deviceType": "A2IVLV5VM2W81",
    }
    queue_input = body["variables"]["input"]
    assert queue_input["targetDeviceContext"] == {
        "deviceType": "ECHO_TYPE",
        "deviceId": "CUSTOMER-ECHO_SERIAL-ECHO_TYPE",
    }
    assert queue_input["queueSeeds"][0]["trackList"]["tracks"] == [
        {"idType": "ASIN", "id": "B09C48YPXX"},
        {"idType": "ASIN", "id": "B076NWZ659"},
        {"idType": "ASIN", "id": "B079VRV49X"},
    ]
    assert queue_input["startAt"]["queueEntityId"] == "B09C48YPXX"


async def test_albums_are_seeded_one_entity_each_in_order_and_transferred_from_amazons_first_track(
    login, aioclient_mock
):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_ALBUM_RESPONSE)
    aioclient_mock.post(MPQS_URL, json=TRANSFER_QUEUE_SUCCESS)

    await play_albums(
        login,
        customer_id="CUSTOMER",
        serial="ECHO_SERIAL",
        device_type="ECHO_TYPE",
        album_asins=["B09C46238P", "B079VLSTG6"],
    )

    (_, initiate_body, _), (_, transfer_body, _) = _requests(aioclient_mock)
    queue_input = initiate_body["variables"]["input"]
    # The seed the Amazon Music app sent for one album on 2026-11-16, once per album.
    assert queue_input["queueSeeds"] == [
        {
            "seedContext": [],
            "playModeEnumValue": "ON_DEMAND",
            "queueSeedType": "QUEUE_ID_HISTORICAL_PLAYBACK",
            "queueSeedTypeEnumValue": "PLAYABLE_ENTITY_ID",
            "playableEntity": {"idType": "ASIN", "id": "B09C46238P", "metricsContext": {"metricTuple": []}},
        },
        {
            "seedContext": [],
            "playModeEnumValue": "ON_DEMAND",
            "queueSeedType": "QUEUE_ID_HISTORICAL_PLAYBACK",
            "queueSeedTypeEnumValue": "PLAYABLE_ENTITY_ID",
            "playableEntity": {"idType": "ASIN", "id": "B079VLSTG6", "metricsContext": {"metricTuple": []}},
        },
    ]
    assert "startAt" not in queue_input
    assert transfer_body["startAt"]["trackIdentifier"] == "a3c9e1f7-2b5d-4e08-8f6a-71d4b0c2e953"


async def test_graphql_errors_raise_without_transferring(login, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_GRAPHQL_ERROR)
    aioclient_mock.post(MPQS_URL, json=TRANSFER_QUEUE_SUCCESS)

    with pytest.raises(HomeAssistantError, match="coerced Null value"):
        await play_asins(
            login, customer_id="CUSTOMER", serial="ECHO_SERIAL", device_type="ECHO_TYPE", asins=["B001S0HA3G"]
        )

    assert [url for url, _, _ in _requests(aioclient_mock)] == [APPSYNC_URL]


async def test_failed_transfer_raises(login, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_RESPONSE)
    aioclient_mock.post(MPQS_URL, status=500, json=TRANSFER_QUEUE_INTERNAL_FAILURE)

    with pytest.raises(HomeAssistantError, match="InternalFailure"):
        await play_asins(
            login, customer_id="CUSTOMER", serial="ECHO_SERIAL", device_type="ECHO_TYPE", asins=["B001S0HA3G"]
        )


async def test_expired_token_is_refreshed_before_initiate_queue(login, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_RESPONSE)
    aioclient_mock.post(MPQS_URL, json=TRANSFER_QUEUE_SUCCESS)
    login.expires_in = time.time() - 60

    async def refresh_access_token():
        login.access_token = "test-access-token-refreshed"
        login.expires_in = time.time() + 3600
        return True

    login.refresh_access_token = AsyncMock(side_effect=refresh_access_token)
    login.exchange_token_for_cookies = AsyncMock(return_value=True)
    login.get_csrf = AsyncMock(return_value=True)
    login.finalize_login = AsyncMock()

    await play_asins(
        login, customer_id="CUSTOMER", serial="ECHO_SERIAL", device_type="ECHO_TYPE", asins=["B001S0HA3G"]
    )

    _, _, headers = _requests(aioclient_mock)[0]
    assert json.loads(headers["authorization"])["accessToken"] == "test-access-token-refreshed"


async def test_failed_token_refresh_raises_without_calling_amazon(login, aioclient_mock):
    login.expires_in = time.time() - 60
    login.refresh_access_token = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="refresh"):
        await play_asins(
            login, customer_id="CUSTOMER", serial="ECHO_SERIAL", device_type="ECHO_TYPE", asins=["B001S0HA3G"]
        )

    assert aioclient_mock.call_count == 0


# --- AlexaClient.async_play_media -------------------------------------------


@pytest.fixture
def hass():
    """Return a mock hass holding one Alexa account."""
    mock_hass = MagicMock()
    mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {EMAIL: {"options": {}, "entities": {"media_player": {}}}}}}
    mock_hass.async_create_task = MagicMock(side_effect=asyncio.ensure_future)
    return mock_hass


@pytest.fixture
def echo(hass, login):
    """Return the AlexaClient of one Echo, with its device identity set."""
    client = AlexaClient(None, login)
    client.hass = hass
    client._customer_id = "CUSTOMER"
    client._device_serial_number = "ECHO_SERIAL"
    client._device_type = "ECHO_TYPE"
    client.alexa_api = MagicMock()
    client.alexa_api.play_music = AsyncMock()
    client.async_update = AsyncMock()
    return client


async def test_play_media_with_an_asin_plays_on_this_echo(echo, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_RESPONSE)
    aioclient_mock.post(MPQS_URL, json=TRANSFER_QUEUE_SUCCESS)

    await echo.async_play_media("AMAZON_MUSIC_ASIN", "B001S0HA3G")

    _, body, _ = _requests(aioclient_mock)[1]
    assert body["targetId"] == "CUSTOMER-ECHO_SERIAL-ECHO_TYPE"
    echo.alexa_api.play_music.assert_not_called()


async def test_play_media_with_an_asin_raises_to_the_caller(echo, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_GRAPHQL_ERROR)

    with pytest.raises(HomeAssistantError):
        await echo.async_play_media("AMAZON_MUSIC_ASIN", "B001S0HA3G")


async def test_play_media_plays_comma_separated_asins_in_order(echo, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_RESPONSE)
    aioclient_mock.post(MPQS_URL, json=TRANSFER_QUEUE_SUCCESS)

    await echo.async_play_media("AMAZON_MUSIC_ASIN", " B09C48YPXX, B076NWZ659,,B079VRV49X ")

    _, body, _ = _requests(aioclient_mock)[0]
    assert body["variables"]["input"]["queueSeeds"][0]["trackList"]["tracks"] == [
        {"idType": "ASIN", "id": "B09C48YPXX"},
        {"idType": "ASIN", "id": "B076NWZ659"},
        {"idType": "ASIN", "id": "B079VRV49X"},
    ]


@pytest.mark.parametrize(
    ("media_id", "error"),
    [
        # A whole share link pasted instead of the ID after "trackAsin=".
        ("B09C48YPXX, https://music.amazon.de/albums/B09C46238P?trackAsin=B09C48YPXX", "music.amazon.de/albums"),
        (" , ", "No Amazon Music track ID"),
    ],
)
async def test_play_media_rejects_anything_but_asins_before_calling_amazon(echo, aioclient_mock, media_id, error):
    with pytest.raises(HomeAssistantError, match=error):
        await echo.async_play_media("AMAZON_MUSIC_ASIN", media_id)

    assert aioclient_mock.call_count == 0


async def test_play_media_plays_comma_separated_albums_in_order_on_this_echo(echo, aioclient_mock):
    aioclient_mock.post(APPSYNC_URL, json=INITIATE_QUEUE_ALBUM_RESPONSE)
    aioclient_mock.post(MPQS_URL, json=TRANSFER_QUEUE_SUCCESS)

    await echo.async_play_media("AMAZON_MUSIC_ALBUM", " B09C46238P, ,B079VLSTG6 ")

    (_, initiate_body, _), (_, transfer_body, _) = _requests(aioclient_mock)
    queue_seeds = initiate_body["variables"]["input"]["queueSeeds"]
    assert [seed["playableEntity"]["id"] for seed in queue_seeds] == ["B09C46238P", "B079VLSTG6"]
    assert transfer_body["targetId"] == "CUSTOMER-ECHO_SERIAL-ECHO_TYPE"
    echo.alexa_api.play_music.assert_not_called()


@pytest.mark.parametrize(
    ("media_id", "error"),
    [
        # A whole share link pasted instead of the ID after "albums/".
        ("B09C46238P, https://music.amazon.de/albums/B079VLSTG6", "music.amazon.de/albums"),
        (" , ", "No Amazon Music album ID"),
    ],
)
async def test_play_media_rejects_anything_but_album_ids_before_calling_amazon(echo, aioclient_mock, media_id, error):
    with pytest.raises(HomeAssistantError, match=error):
        await echo.async_play_media("AMAZON_MUSIC_ALBUM", media_id)

    assert aioclient_mock.call_count == 0


async def test_other_content_types_still_use_voice_search(echo, aioclient_mock):
    await echo.async_play_media("AMAZON_MUSIC", "Black Betty by Ram Jam")
    await asyncio.sleep(0)

    echo.alexa_api.play_music.assert_awaited_once()
    assert echo.alexa_api.play_music.await_args.args[:2] == ("AMAZON_MUSIC", "Black Betty by Ram Jam")
    assert aioclient_mock.call_count == 0
