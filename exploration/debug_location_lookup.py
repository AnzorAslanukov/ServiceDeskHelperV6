"""Debug script to trace location resolution for a specific ticket."""
import asyncio
import json
from src.clients.athena_client import AthenaClient
from src.services.assignment import LOCATION_GUID_TO_FULLNAME
from src.services.ticket_search import TicketSearchService


async def main():
    print(f"LOCATION_GUID_TO_FULLNAME size: {len(LOCATION_GUID_TO_FULLNAME)}")

    from src.config import get_settings
    settings = get_settings()
    client = AthenaClient(settings)

    ticket = await client.get_ticket("IR9984536")
    loc = ticket.get("location")
    loc_value = ticket.get("locationValue")

    print(f"\n--- Raw ticket data for IR9984536 ---")
    print(f"location field: {json.dumps(loc, indent=2) if isinstance(loc, dict) else loc}")
    print(f"locationValue field: {loc_value}")

    # Check GUID resolution
    if isinstance(loc, dict):
        guid = loc.get("id")
        name = loc.get("name")
        print(f"\nGUID from dict: {guid}")
        print(f"Name from dict: {name}")
        resolved = LOCATION_GUID_TO_FULLNAME.get(guid)
        print(f"Lookup result: {resolved}")
    elif isinstance(loc, str):
        print(f"\nLocation is a string: '{loc}'")
        from src.services.ticket_search import TicketSearchService
        is_guid = TicketSearchService._is_guid(loc)
        print(f"Is GUID: {is_guid}")
        if is_guid:
            resolved = LOCATION_GUID_TO_FULLNAME.get(loc)
            print(f"Lookup result: {resolved}")

    # Test _resolve_location
    print(f"\n--- _resolve_location result ---")
    result = TicketSearchService._resolve_location(ticket)
    print(f"Result: {result}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())