# utils/access_token.py

import requests
import json
from requests.exceptions import RequestException
from models.exceptions import NetworkError, ApiError, TokenPollingError

class AccessToken():
    def __init__(self, domain: str, client_id: str, device_code: str):
        self.url = f'https://{domain}/oauth/token'
        self.headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        self.data = {
            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            'client_id': client_id,
            'device_code': device_code,
        }

    def request(self) -> dict:
        """
        Polls the token endpoint for an access token.
        - On success: returns the token dictionary.
        - On failure: raises NetworkError, ApiError, or TokenPollingError.
        """
        try:
            response = requests.post(self.url, headers=self.headers, data=self.data, verify=False, timeout=10)

            # Check the response content first
            response_data = response.json()

            # The API can return a 200 OK with an error payload, or a 4xx.
            # We handle both by checking for an 'error' key.
            if 'error' in response_data:
                error_code = response_data.get('error')
                # These are expected polling responses, not true exceptions.
                # We raise a specific error so the caller can handle them.
                raise TokenPollingError(
                    error_code=error_code,
                    error_description=response_data.get('error_description', '')
                )
            
            # If no 'error' key and status is OK, we have the token.
            if response.ok and 'access_token' in response_data:
                return response_data
            
            # For any other non-OK status, raise a generic ApiError.
            response.raise_for_status()

            # Fallback for unexpected successful responses without a token
            raise ApiError(response.status_code, "Resposta não possui o 'access_token'.")

        except RequestException as e:
            raise NetworkError(original_exception=e) from e
        
        except requests.HTTPError as e:
            # This catches non-2xx responses from raise_for_status()
            raise ApiError(status_code=e.response.status_code, response_text=e.response.text) from e
        
        except json.JSONDecodeError as e:
            raise ApiError(status_code=response.status_code, response_text=f"Falha ao decodificar JSON: {response.text}") from e