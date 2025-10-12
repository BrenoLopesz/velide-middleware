from datetime import datetime
from utils.enviroment_variables import VELIDE_SERVER, TARGET_SOFTWARE, USE_NEIGHBOURHOOD
import httpx

class Velide():
    def __init__(self, access_token):
        self.url = VELIDE_SERVER
        self.headers = {
            'Content-Type': 'application/json',
            'Authorization': access_token
        }

    async def getDeliverymen(self):
        query = """
            query {
                deliverymen {
                    id
                    name
                }
            }
        """
        payload = {
            'query': query,
        }

         # Sending the GraphQL mutation request asynchronously
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(self.url, json=payload, headers=self.headers)

         # Checking for errors
        if response.status_code != 200:
            raise Exception(f"GraphQL mutation failed with status code {response.status_code}: {response.text}")
        
        print(response.json())

        # Parsing and returning the JSON response
        return response.json()["data"]["deliverymen"]
    
    async def getDeliveries(self, start_period_unix_seconds, end_period_unix_seconds):
        query = """
            query GetDeliveries($startPeriod: Int, $endPeriod: Int) {
                deliveries(startPeriod: $startPeriod, endPeriod: $endPeriod) {
                    id
                    route {
                        deliverymanId
                        startedAt
                    }
                    location {
                        properties {
                            name
                        }
                    }
                    endedAt
                }
            }
        """
        payload = {
            'query': query,
            'variables': {
                'startPeriod': int(start_period_unix_seconds),
                'endPeriod': int(end_period_unix_seconds)
            }
        }

         # Sending the GraphQL mutation request asynchronously
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(self.url, json=payload, headers=self.headers)

         # Checking for errors
        if response.status_code != 200:
            raise Exception(f"GraphQL mutation failed with status code {response.status_code}: {response.text}")
        
        print(response.json())

        # Parsing and returning the JSON response
        return response.json()["data"]["deliveries"]

    async def addDelivery(self, sale_info):

        # GraphQL mutation with variables
        mutation = """
            mutation AddDeliveryFromIntegration($metadata: MetadataInput!, $address: String, $address2: String, $neighbourhood: String, $reference: String, $offset: Int) {
                addDeliveryFromIntegration(
                    metadata: $metadata
                    address: $address
                    address2: $address2
                    neighbourhood: $neighbourhood
                    reference: $reference
                    offset: $offset
                ) {
                    id
                    routeId
                    endedAt
                    createdAt
                    location {
                        properties {
                            name
                            housenumber
                            street
                            neighbourhood
                        }
                    }
                }
            }
        """

        today_date = datetime.now().date()
        # sale_info["created_at"] is a datetime.time object
        # We have to combine it to today's date
        datetime_combined = datetime.combine(today_date, sale_info["created_at"])

        # Offset should be an integer
        unix_timestamp = int(datetime_combined.timestamp())
        now = int(datetime.now().timestamp()) 
        offset_in_seconds = now - unix_timestamp
        # Offset should be in milliseconds
        offset = round(offset_in_seconds * 1000)

        # GraphQL request payload
        payload = {
            'query': mutation,
            'variables': { 
                'metadata': {
                    'integrationName': TARGET_SOFTWARE,
                    'customerName': sale_info["name"]
                    # 'contact': sale_info["contact"] TODO
                 },
                'address': sale_info["address"],
                # To avoid unneccessary offsets, only set it if is bigger than a minute ago
                'offset': offset if offset > 60000 else 0,
                
                # Conditionally adds reference if existing
                **({"reference": sale_info["reference"]} if "reference" in sale_info else {}),
                # Conditionally adds address2 if existing
                **({"address2": sale_info["address2"]} if "address2" in sale_info else {}),
                # Conditionally use neighbourhood when adding delivery
                **({"neighbourhood": sale_info["neighbourhood"]} if USE_NEIGHBOURHOOD and "neighbourhood" in sale_info else {}),
            }
        }

        timeout = httpx.Timeout(10.0, read=None)

         # Sending the GraphQL mutation request asynchronously
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(self.url, json=payload, headers=self.headers, timeout=timeout)

         # Checking for errors
        if response.status_code != 200:
            raise Exception(f"GraphQL mutation failed with status code {response.status_code}: {response.text}")
        
        try:
            response_json = response.json()
        except: 
            raise Exception(f"Could not parse response as JSON.", response)
        
        print(response_json)

        if response_json is None or "data" not in response_json or "addDeliveryFromIntegration" not in response_json["data"]:
            raise Exception(f"Unexpected response structure: {response_json}")

        # Parsing and returning the JSON response
        return response_json["data"]["addDeliveryFromIntegration"]
    
    async def deleteDelivery(self, id):

        # GraphQL mutation with variables
        mutation = """
            mutation DeleteDelivery($deliveryId: String!) {
                deleteDelivery(
                    deliveryId: $deliveryId
                )
            }
        """

        # GraphQL request payload
        payload = {
            'query': mutation,
            'variables': { 
                'deliveryId': id,
            }
        }

        timeout = httpx.Timeout(10.0, read=None)

         # Sending the GraphQL mutation request asynchronously
        async with httpx.AsyncClient(verify=False) as client:
            response = await client.post(self.url, json=payload, headers=self.headers, timeout=timeout)

         # Checking for errors
        if response.status_code != 200:
            raise Exception(f"GraphQL mutation failed with status code {response.status_code}: {response.text}")
        
        print(response.json())

        # Parsing and returning the JSON response
        return response.json()["data"]["deleteDelivery"]