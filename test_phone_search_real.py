#!/usr/bin/env python
"""
Quick test of the real phone search against actual Athena API.
Reproduces the user's exact steps: Field=contactMethod, Value=2154856549, Operator=eq
"""

import asyncio
import sys
from src.config import Settings
from src.clients.athena_client import AthenaClient
from src.services.ticket_search import TicketSearchService
from src.services.local_vector_store import LocalVectorStore
from src.clients.databricks_client import DatabricksClient


async def main():
    settings = Settings()
    athena = AthenaClient(settings)
    databricks = DatabricksClient(settings)
    vector_store = LocalVectorStore()
    service = TicketSearchService(athena, databricks, vector_store)

    print("=" * 70)
    print("Testing REAL phone search against Athena API")
    print("=" * 70)
    print(f"\nField: contactMethod")
    print(f"Value: 2154856549")
    print(f"Operator: eq")
    print(f"Ticket Type: incident")
    print("\n" + "-" * 70)

    try:
        result = await service.search_by_field(
            field="contactMethod",
            value="2154856549",
            operator="eq",
            ticket_type="incident",
            page=1,
            page_size=50,
        )
        print(f"\n✓ SUCCESS!")
        print(f"  Total results: {result.total}")
        print(f"  Page: {result.page}")
        print(f"  Returned: {len(result.tickets)}")
        print(f"  Has more: {result.has_more}")
        if result.tickets:
            print(f"\n  First result:")
            t = result.tickets[0]
            print(f"    ID: {t.id}")
            print(f"    Title: {t.title}")
            print(f"    Status: {t.status}")
    except Exception as e:
        print(f"\n✗ ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return 1

    await athena.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
