"""
Databricks Serving Endpoints Explorer
Tests the LLM (Claude Sonnet 4.5) and embedding (GTE-Large-EN) endpoints.
"""

import os
import json
import requests
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

DATABRICKS_API_KEY = os.getenv('DATABRICKS_API_KEY')
DATABRICKS_SONNET_URL = os.getenv('DATABRICKS_SONNET_4.5_URL')
DATABRICKS_EMBEDDING_URL = os.getenv('DATABRICKS_EMBEDDING_URL')
DATABRICKS_SERVER_HOSTNAME = os.getenv('DATABRICKS_SERVER_HOSTNAME')


def get_headers():
    """Get authorization headers for Databricks API calls."""
    return {
        'Authorization': f'Bearer {DATABRICKS_API_KEY}',
        'Content-Type': 'application/json'
    }


def test_llm_endpoint():
    """Test the Claude Sonnet 4.5 LLM endpoint."""
    print("\n" + "="*60)
    print("  Testing LLM Endpoint: Claude Sonnet 4.5")
    print("="*60)
    print(f"  URL: {DATABRICKS_SONNET_URL}")

    payload = {
        "messages": [
            {
                "role": "user",
                "content": "What is a service desk incident ticket? Answer in one sentence."
            }
        ],
        "max_tokens": 100
    }

    print(f"  Payload: {json.dumps(payload, indent=2)}")
    print("-"*60)

    try:
        response = requests.post(
            DATABRICKS_SONNET_URL,
            headers=get_headers(),
            json=payload,
            timeout=30
        )

        print(f"  Status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"  Response keys: {list(data.keys())}")
            print(f"  Model: {data.get('model', 'N/A')}")

            if 'choices' in data:
                for i, choice in enumerate(data['choices']):
                    message = choice.get('message', {})
                    print(f"  Choice [{i}] role: {message.get('role', 'N/A')}")
                    print(f"  Choice [{i}] content: {message.get('content', 'N/A')}")

            if 'usage' in data:
                usage = data['usage']
                print(f"  Usage - prompt_tokens: {usage.get('prompt_tokens', 'N/A')}")
                print(f"  Usage - completion_tokens: {usage.get('completion_tokens', 'N/A')}")
                print(f"  Usage - total_tokens: {usage.get('total_tokens', 'N/A')}")

            print(f"\n  Full response:\n{json.dumps(data, indent=2)}")
        else:
            print(f"  Error response: {response.text}")

    except Exception as e:
        print(f"  ERROR: {e}")


def test_embedding_endpoint():
    """Test the GTE-Large-EN embedding endpoint."""
    print("\n" + "="*60)
    print("  Testing Embedding Endpoint: GTE-Large-EN")
    print("="*60)
    print(f"  URL: {DATABRICKS_EMBEDDING_URL}")

    payload = {
        "input": ["My computer is not turning on and I need help"]
    }

    print(f"  Payload: {json.dumps(payload, indent=2)}")
    print("-"*60)

    try:
        response = requests.post(
            DATABRICKS_EMBEDDING_URL,
            headers=get_headers(),
            json=payload,
            timeout=30
        )

        print(f"  Status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"  Response keys: {list(data.keys())}")
            print(f"  Model: {data.get('model', 'N/A')}")
            print(f"  Object type: {data.get('object', 'N/A')}")

            if 'data' in data:
                for i, item in enumerate(data['data']):
                    embedding = item.get('embedding', [])
                    print(f"  Embedding [{i}] dimensions: {len(embedding)}")
                    print(f"  Embedding [{i}] first 5 values: {embedding[:5]}")
                    print(f"  Embedding [{i}] last 5 values: {embedding[-5:]}")

            if 'usage' in data:
                usage = data['usage']
                print(f"  Usage - prompt_tokens: {usage.get('prompt_tokens', 'N/A')}")
                print(f"  Usage - total_tokens: {usage.get('total_tokens', 'N/A')}")

            # Print response without the full embedding array
            summary = {k: v for k, v in data.items() if k != 'data'}
            if 'data' in data:
                summary['data'] = [
                    {
                        'index': item.get('index'),
                        'object': item.get('object'),
                        'embedding_dimensions': len(item.get('embedding', [])),
                        'embedding_preview': item.get('embedding', [])[:5]
                    }
                    for item in data['data']
                ]
            print(f"\n  Response summary:\n{json.dumps(summary, indent=2)}")
        else:
            print(f"  Error response: {response.text}")

    except Exception as e:
        print(f"  ERROR: {e}")


def test_embedding_multiple_inputs():
    """Test embedding endpoint with multiple inputs to verify batch support."""
    print("\n" + "="*60)
    print("  Testing Embedding Endpoint: Multiple Inputs (Batch)")
    print("="*60)

    payload = {
        "input": [
            "My computer is not turning on",
            "I need access to the shared drive",
            "PennChart is showing an error when I try to log in"
        ]
    }

    print(f"  Sending {len(payload['input'])} texts for embedding...")
    print("-"*60)

    try:
        response = requests.post(
            DATABRICKS_EMBEDDING_URL,
            headers=get_headers(),
            json=payload,
            timeout=30
        )

        print(f"  Status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            if 'data' in data:
                print(f"  Embeddings returned: {len(data['data'])}")
                for i, item in enumerate(data['data']):
                    embedding = item.get('embedding', [])
                    print(f"  Input [{i}] -> {len(embedding)} dimensions")
        else:
            print(f"  Error: {response.text}")

    except Exception as e:
        print(f"  ERROR: {e}")


def list_serving_endpoints():
    """List all available serving endpoints."""
    print("\n" + "="*60)
    print("  Listing All Serving Endpoints")
    print("="*60)

    url = f"https://{DATABRICKS_SERVER_HOSTNAME}/api/2.0/serving-endpoints"
    print(f"  URL: {url}")
    print("-"*60)

    try:
        response = requests.get(
            url,
            headers=get_headers(),
            timeout=30
        )

        print(f"  Status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            endpoints = data.get('endpoints', [])
            print(f"  Total endpoints: {len(endpoints)}")

            for i, ep in enumerate(endpoints):
                name = ep.get('name', 'N/A')
                state = ep.get('state', {}).get('ready', 'N/A')
                creator = ep.get('creator', 'N/A')
                endpoint_type = ep.get('config', {}).get('served_entities', [{}])[0].get('external_model', {}).get('provider', 'N/A') if ep.get('config', {}).get('served_entities') else 'N/A'
                print(f"  [{i}] {name} | Ready: {state} | Creator: {creator} | Provider: {endpoint_type}")
        else:
            print(f"  Error: {response.text[:500]}")

    except Exception as e:
        print(f"  ERROR: {e}")


if __name__ == '__main__':
    print("Databricks Serving Endpoints Explorer")
    print(f"Server: {DATABRICKS_SERVER_HOSTNAME}")

    # 1. List all serving endpoints
    list_serving_endpoints()

    # 2. Test LLM endpoint
    test_llm_endpoint()

    # 3. Test embedding endpoint
    test_embedding_endpoint()

    # 4. Test batch embedding
    test_embedding_multiple_inputs()

    print("\n" + "="*60)
    print("  Exploration complete!")
    print("="*60)