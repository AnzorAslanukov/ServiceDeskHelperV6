"""Explore the correct filter for Validation queue tickets."""

import asyncio
import json

import httpx

from src.config import Settings


async def explore_validation_filter():
    settings = Settings()

    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        # Get token
        token_response = await client.post(
            settings.ATHENA_TOKEN_URL,
            data={
                "grant_type": "password",
                "username": settings.ATHENA_USERNAME,
                "password": settings.ATHENA_PASSWORD,
                "client_id": settings.ATHENA_CLIENT_ID,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token = token_response.json()["access_token"]
        headers = {
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
        }

        # Test IR with TierQueue
        print("=== IR: TierQueue eq 'Service Desk\\Validation' ===")
        ir_url = f"{settings.ATHENA_BASE_URL}v1/view/workitem?type=incident"
        ir_filter = [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": "TierQueue",
                        "operator": "eq",
                        "value": "Service Desk\\Validation",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Closed",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Resolved",
                    },
                ],
            }
        ]
        response = await client.post(ir_url, headers=headers, json=ir_filter)
        ir_data = response.json()
        ir_count = len(ir_data) if isinstance(ir_data, list) else 1 if ir_data else 0
        print(f"  IR count: {ir_count}")
        if isinstance(ir_data, list) and len(ir_data) > 0:
            sample = ir_data[0]
            print(f"  Sample IR: Id={sample.get('Id')}, Title={sample.get('Title', '')[:60]}, SupportGroup={sample.get('SupportGroup')}")

        # Test SR with TierQueue
        print("\n=== SR: TierQueue eq 'Service Desk\\Validation' ===")
        sr_url = f"{settings.ATHENA_BASE_URL}v1/view/workitem?type=servicerequest"
        sr_filter = [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": "TierQueue",
                        "operator": "eq",
                        "value": "Service Desk\\Validation",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Closed",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Completed",
                    },
                ],
            }
        ]
        response = await client.post(sr_url, headers=headers, json=sr_filter)
        sr_data = response.json()
        sr_count = len(sr_data) if isinstance(sr_data, list) else 1 if sr_data else 0
        print(f"  SR count: {sr_count}")
        if isinstance(sr_data, list) and len(sr_data) > 0:
            sample = sr_data[0]
            print(f"  Sample SR: Id={sample.get('Id')}, Title={sample.get('Title', '')[:60]}, SupportGroup={sample.get('SupportGroup')}")

        # Test SR with supportGroup eq 'Validation'
        print("\n=== SR: supportGroup eq 'Validation' ===")
        sr_filter2 = [
            {
                "condition": "and",
                "filters": [
                    {
                        "condition": "and",
                        "property": "supportGroup",
                        "operator": "eq",
                        "value": "Validation",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Closed",
                    },
                    {
                        "condition": "and",
                        "property": "Status",
                        "operator": "ne",
                        "value": "Completed",
                    },
                ],
            }
        ]
        response = await client.post(sr_url, headers=headers, json=sr_filter2)
        sr_data2 = response.json()
        sr_count2 = len(sr_data2) if isinstance(sr_data2, list) else 1 if sr_data2 else 0
        print(f"  SR count: {sr_count2}")

        print(f"\n=== TOTAL: {ir_count} IR + {sr_count} SR = {ir_count + sr_count} tickets ===")


asyncio.run(explore_validation_filter())