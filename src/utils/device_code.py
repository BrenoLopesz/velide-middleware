from typing import TypedDict, cast
import requests
import json
from models.exceptions import ApiError, NetworkError

class DeviceCodeDict(TypedDict):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int
    verification_uri_complete: str

class DeviceCode():
    def __init__(self, domain: str, client_id: str, scope: str, audience: str):
        # Added type hints to init arguments for completeness
        self.url = 'https://{_domain}/oauth/device/code'.format(_domain=domain)
        self.headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        self.data = {
            'client_id': client_id,
            'scope': scope,
            'audience': audience
        }
    
    def request(self) -> DeviceCodeDict:
        """
        Performs the device code request.
        Returns the response JSON on success.
        Raises NetworkError or ApiError on failure.
        """
        try:
            response = requests.post(self.url, headers=self.headers, data=self.data, verify=False, timeout=15)

            # Raise an exception for bad status codes (4xx or 5xx)
            response.raise_for_status()

            # FIX: Explicitly cast the generic JSON result to your specific TypedDict
            return cast(DeviceCodeDict, response.json())

        except requests.HTTPError as e:
            # FIX: Catch HTTPError BEFORE RequestException, because HTTPError inherits from it.
            # If you catch RequestException first, this block is never reached.
            raise ApiError(status_code=e.response.status_code, response_text=e.response.text) from e

        except requests.RequestException as e:
            # Catches connection errors, timeouts, etc.
            raise NetworkError(original_exception=e) from e
        
        except requests.HTTPError as e:
            # Catches non-2xx responses after raise_for_status() is called
            raise ApiError(status_code=e.response.status_code, response_text=e.response.text) from e

        except json.JSONDecodeError as e:
            # Catches errors if the server response is not valid JSON
            # Note: We use e.doc or generic text because 'response' might not be fully available if crash happened earlier, 
            # but usually safely accessible here.
            raise ApiError(status_code=response.status_code, response_text=f"Falha ao decodificar JSON: {response.text}") from e