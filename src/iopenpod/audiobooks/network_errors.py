"""Friendly audiobook metadata network error descriptions."""

from __future__ import annotations

from dataclasses import dataclass

import requests


@dataclass(frozen=True)
class AudiobookErrorInfo:
    title: str
    message: str
    code: str = ""


class AudiobookNetworkError(RuntimeError):
    """Network failure with a user-facing title/message/code."""

    def __init__(self, info: AudiobookErrorInfo):
        super().__init__(info.message)
        self.info = info


def describe_audiobook_error(
    error: BaseException,
    *,
    action: str = "look up audiobook details",
) -> AudiobookErrorInfo:
    """Return short, user-facing copy for metadata lookup failures."""
    if isinstance(error, AudiobookNetworkError):
        return error.info

    if isinstance(error, requests.Timeout):
        return AudiobookErrorInfo(
            title="The connection timed out",
            message="The audiobook service took too long to answer. Try again in a moment.",
        )

    if isinstance(error, requests.ConnectionError):
        return AudiobookErrorInfo(
            title="No internet connection",
            message="iOpenPod could not reach the audiobook service. Check your connection and try again.",
        )

    if isinstance(error, requests.HTTPError):
        response = getattr(error, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            code = f"HTTP {status_code}"
            if status_code == 429:
                return AudiobookErrorInfo(
                    title="Too many lookups at once",
                    message="The audiobook service is rate limiting requests. Wait a minute and try again.",
                    code=code,
                )
            if status_code == 404:
                return AudiobookErrorInfo(
                    title="No details for this edition",
                    message=("The service has no record for this title in the selected region. Try another region in Settings."),
                    code=code,
                )
            if 400 <= status_code < 500:
                return AudiobookErrorInfo(
                    title="The lookup was rejected",
                    message="The audiobook service refused the request. The code below can help identify the issue.",
                    code=code,
                )
            if 500 <= status_code < 600:
                return AudiobookErrorInfo(
                    title="The audiobook service is having trouble",
                    message="The server answered with an error. This usually clears up after a little while.",
                    code=code,
                )
            return AudiobookErrorInfo(
                title="The audiobook service could not finish the request",
                message="The code below can help identify what happened.",
                code=code,
            )

    if isinstance(error, requests.RequestException):
        return AudiobookErrorInfo(
            title=f"Could not {action}",
            message="iOpenPod could not reach the audiobook service. Check your connection and try again.",
        )

    if isinstance(error, ValueError):
        return AudiobookErrorInfo(
            title="The response could not be read",
            message="The audiobook service answered, but the reply was not in the expected format.",
        )

    return AudiobookErrorInfo(
        title=f"Could not {action}",
        message=str(error) or "Something went wrong while looking up audiobook details.",
    )


def audiobook_network_error(
    error: BaseException,
    *,
    action: str = "look up audiobook details",
) -> AudiobookNetworkError:
    return AudiobookNetworkError(describe_audiobook_error(error, action=action))
