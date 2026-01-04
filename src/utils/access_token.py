from typing import TypedDict, cast
import requests
import json
from requests.exceptions import RequestException, HTTPError
from models.exceptions import NetworkError, ApiError, TokenPollingError


# 1. Define the structure of the Auth0/OAuth Token Response
class AccessTokenDict(TypedDict, total=False):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    scope: str
    id_token: str


class AccessToken:
    def __init__(self, domain: str, client_id: str, device_code: str):
        self.url = f"https://{domain}/oauth/token"
        self.headers = {"Content-Type": "application/x-www-form-urlencoded"}
        self.data = {
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client_id,
            "device_code": device_code,
        }

    def request(self) -> AccessTokenDict:
        """
        Polls the token endpoint for an access token.
        - On success: returns the strictly typed token dictionary.
        - On failure: raises NetworkError, ApiError, or TokenPollingError.
        """
        try:
            response = requests.post(
                self.url, headers=self.headers, data=self.data, verify=False, timeout=10
            )

            # Check the response content first
            response_data = response.json()

            # The API can return a 200 OK with an error payload, or a 4xx.
            # We handle both by checking for an 'error' key.
            if "error" in response_data:
                error_code = response_data.get("error")
                # These are expected polling responses ("authorization_pending", etc.)
                # We raise a specific error so the caller can handle them.
                raise TokenPollingError(
                    error_code=error_code,
                    error_description=response_data.get("error_description", ""),
                )

            # If no 'error' key and status is OK, we have the token.
            if response.ok and "access_token" in response_data:
                # 2. Use cast to tell mypy this dict matches 
                # our AccessTokenDict structure
                return cast(AccessTokenDict, response_data)

            # For any other non-OK status, raise a generic ApiError.
            response.raise_for_status()

            # Fallback for unexpected successful responses without a token
            raise ApiError(
                response.status_code, "Resposta não possui o 'access_token'."
            )

        # Catch HTTPError BEFORE RequestException
        # HTTPError is a subclass of RequestException. 
        # If you catch RequestException first,
        # the specific HTTPError block is unreachable.
        except HTTPError as e:
            raise ApiError(
                status_code=e.response.status_code, response_text=e.response.text
            ) from e

        except RequestException as e:
            raise NetworkError(original_exception=e) from e

        except json.JSONDecodeError as e:
            # We use response.text safely here assuming response 
            # exists if we got to JSON decoding
            raise ApiError(
                status_code=response.status_code,
                response_text=f"Falha ao decodificar JSON: {response.text}",
            ) from e
