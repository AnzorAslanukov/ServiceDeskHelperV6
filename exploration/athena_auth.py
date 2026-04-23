"""
Athena API Authentication Helper
Handles OAuth2 token retrieval for the Athena ticketing system.
"""

import os
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ATHENA_AUTH_URL = os.getenv('ATHENA_AUTH_URL')
ATHENA_USERNAME = os.getenv('ATHENA_USERNAME')
ATHENA_PASSWORD = os.getenv('ATHENA_PASSWORD')
ATHENA_CLIENT_ID = os.getenv('ATHENA_CLIENT_ID')
ATHENA_BASE_URL = os.getenv('ATHENA_BASE_URL')


def get_auth_token():
    """
    Authenticate with the Athena API and return a JWT token.
    Uses OAuth2 password grant type.
    """
    payload = {
        'username': ATHENA_USERNAME,
        'password': ATHENA_PASSWORD,
        'grant_type': 'password',
        'client_id': ATHENA_CLIENT_ID
    }
    
    headers = {
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    
    print(f"Authenticating as '{ATHENA_USERNAME}' against {ATHENA_AUTH_URL}...")
    
    response = requests.post(ATHENA_AUTH_URL, data=payload, headers=headers)
    
    if response.status_code == 200:
        token_data = response.json()
        token = token_data.get('access_token')
        print(f"Authentication successful. Token type: {token_data.get('token_type', 'N/A')}")
        return token
    else:
        print(f"Authentication failed. Status: {response.status_code}")
        print(f"Response: {response.text}")
        return None


def get_auth_headers():
    """
    Get headers with authorization token for API calls.
    """
    token = get_auth_token()
    if token:
        return {
            'Authorization': f'bearer {token}',
            'Content-Type': 'application/json'
        }
    return None


if __name__ == '__main__':
    token = get_auth_token()
    if token:
        print(f"\nToken (first 50 chars): {token[:50]}...")
    else:
        print("\nFailed to obtain token.")