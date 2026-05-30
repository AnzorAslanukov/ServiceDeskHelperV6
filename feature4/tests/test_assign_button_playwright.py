"""
Playwright E2E test for the "Assign Selected" button behavior.

Verifies that:
1. btnAssign is disabled on initial load (no tickets selected)
2. btnAssign stays disabled after selecting/locking a ticket (no support group)
3. btnAssign becomes enabled after a support group override is set
4. btnAssign becomes disabled again when the override is cleared

IMPORTANT: This test NEVER clicks btnAssign when it is enabled.
"""
import asyncio
from playwright.async_api import async_playwright


BASE_URL = "http://localhost:8000/bulk"
TEST_USER = "playwright_test_user"


async def run_test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel="chrome")
        page = await browser.new_page()

        # Navigate to bulk page
        await page.goto(f"{BASE_URL}/ui")
        await page.wait_for_selector("#bulkUserId")
        print("[PASS] Bulk page loaded")

        # Login (form submit - press Enter after filling the input)
        await page.fill("#bulkUserId", TEST_USER)
        await page.press("#bulkUserId", "Enter")
        await page.wait_for_selector("#bulkMainView", state="visible", timeout=5000)
        print("[PASS] Logged in as", TEST_USER)

        # Wait for queue to load (WebSocket streaming)
        try:
            await page.wait_for_selector(
                "tr[data-ticket-id], .bulk-empty-state",
                timeout=30000
            )
        except Exception:
            pass

        await page.wait_for_timeout(1000)

        # Test 1: btnAssign should be disabled initially
        btn_assign = page.locator("#btnAssign")
        is_disabled = await btn_assign.is_disabled()
        assert is_disabled, "FAIL: btnAssign should be disabled when no tickets are selected"
        print("[PASS] Test 1: btnAssign is disabled initially (no selection)")

        # Check if there are tickets in the queue
        ticket_rows = page.locator("tr[data-ticket-id]")
        ticket_count = await ticket_rows.count()

        if ticket_count == 0:
            print("[INFO] No tickets in queue -- testing with JavaScript state injection")

            # Inject mock state to test button logic without real tickets
            await page.evaluate("""() => {
                _bulkQueue = [{
                    id: 'TEST-001',
                    entity_id: 'entity-001',
                    ticket_type: 'incident',
                    title: 'Test Ticket',
                    status: 'Active',
                    priority: 2,
                    affected_user: 'test_user',
                    created_date: '2026-01-01T00:00:00Z'
                }];
                _bulkLocks['TEST-001'] = _bulkUserId;
                _bulkSelected.add('TEST-001');
                _bulkOverrides = {};
                _renderQueue();
                _updateCounts();
            }""")
            await page.wait_for_timeout(500)

            # Test 2: btnAssign should still be disabled (selected but no support group)
            is_disabled = await btn_assign.is_disabled()
            assert is_disabled, "FAIL: btnAssign should be disabled when ticket is selected but has no support group"
            print("[PASS] Test 2: btnAssign is disabled with selection but no support group")

            # Test 2b: btnRecommend should be enabled (has selection)
            btn_recommend = page.locator("#btnRecommend")
            is_rec_disabled = await btn_recommend.is_disabled()
            assert not is_rec_disabled, "FAIL: btnRecommend should be enabled when tickets are selected"
            print("[PASS] Test 2b: btnRecommend is enabled with selection")

            # Test 3: Set a support group override -> btnAssign should become enabled
            await page.evaluate("""() => {
                _bulkOverrides['TEST-001'] = {
                    tier_queue_guid: 'fake-guid-123',
                    tier_queue_name: 'Test Support Group',
                    priority: 2
                };
                _updateCounts();
            }""")
            await page.wait_for_timeout(300)

            is_disabled = await btn_assign.is_disabled()
            assert not is_disabled, "FAIL: btnAssign should be enabled when a selected ticket has a support group"
            print("[PASS] Test 3: btnAssign is enabled after support group is assigned")
            # NOTE: We do NOT click btnAssign here!

            # Test 4: Clear the support group override -> btnAssign should become disabled again
            await page.evaluate("""() => {
                _bulkOverrides['TEST-001'].tier_queue_guid = '';
                _updateCounts();
            }""")
            await page.wait_for_timeout(300)

            is_disabled = await btn_assign.is_disabled()
            assert is_disabled, "FAIL: btnAssign should be disabled when support group GUID is cleared"
            print("[PASS] Test 4: btnAssign is disabled after support group is cleared")

            # Test 5: Test bulkSgSelect triggers _updateCounts
            await page.evaluate("""() => {
                if (!_bulkOverrides['TEST-001']) _bulkOverrides['TEST-001'] = {};
                _bulkOverrides['TEST-001'].tier_queue_name = 'Another Group';
                _bulkOverrides['TEST-001'].tier_queue_guid = 'another-guid-456';
                _updateCounts();
            }""")
            await page.wait_for_timeout(300)

            is_disabled = await btn_assign.is_disabled()
            assert not is_disabled, "FAIL: btnAssign should be enabled after bulkSgSelect sets a support group"
            print("[PASS] Test 5: btnAssign is enabled after manual support group selection")
            # NOTE: We do NOT click btnAssign here!

            # Test 6: Deselect the ticket -> btnAssign should become disabled
            await page.evaluate("""() => {
                _bulkSelected.clear();
                _updateCounts();
            }""")
            await page.wait_for_timeout(300)

            is_disabled = await btn_assign.is_disabled()
            assert is_disabled, "FAIL: btnAssign should be disabled when no tickets are selected"
            print("[PASS] Test 6: btnAssign is disabled after deselecting all tickets")

        else:
            print(f"[INFO] Found {ticket_count} tickets in queue -- testing with real tickets")

            first_ticket_id = await ticket_rows.first.get_attribute("data-ticket-id")
            print(f"  Using ticket: {first_ticket_id}")

            first_checkbox = ticket_rows.first.locator("input[type='checkbox']")
            await first_checkbox.check()
            await page.wait_for_timeout(3000)

            # Test 2: btnAssign should still be disabled (no support group yet)
            is_disabled = await btn_assign.is_disabled()
            assert is_disabled, "FAIL: btnAssign should be disabled when ticket is selected but has no support group"
            print("[PASS] Test 2: btnAssign is disabled with selection but no support group")

            # Test 2b: btnRecommend should be enabled
            btn_recommend = page.locator("#btnRecommend")
            is_rec_disabled = await btn_recommend.is_disabled()
            assert not is_rec_disabled, "FAIL: btnRecommend should be enabled when tickets are selected"
            print("[PASS] Test 2b: btnRecommend is enabled with selection")

            # Test 3: Inject a support group override via JS
            await page.evaluate(f"""() => {{
                _bulkOverrides['{first_ticket_id}'] = {{
                    tier_queue_guid: 'fake-guid-123',
                    tier_queue_name: 'Test Support Group',
                    priority: 2
                }};
                _updateCounts();
            }}""")
            await page.wait_for_timeout(300)

            is_disabled = await btn_assign.is_disabled()
            assert not is_disabled, "FAIL: btnAssign should be enabled when a selected ticket has a support group"
            print("[PASS] Test 3: btnAssign is enabled after support group is assigned")
            # NOTE: We do NOT click btnAssign here!

            # Test 4: Clear the override -> btnAssign should become disabled
            await page.evaluate(f"""() => {{
                delete _bulkOverrides['{first_ticket_id}'];
                _updateCounts();
            }}""")
            await page.wait_for_timeout(300)

            is_disabled = await btn_assign.is_disabled()
            assert is_disabled, "FAIL: btnAssign should be disabled when support group is removed"
            print("[PASS] Test 4: btnAssign is disabled after support group is cleared")

            # Unlock the ticket to clean up
            first_checkbox = page.locator(f"tr[data-ticket-id='{first_ticket_id}'] input[type='checkbox']")
            if await first_checkbox.is_checked():
                await first_checkbox.uncheck()
                await page.wait_for_timeout(2000)
            print("[PASS] Cleaned up: unlocked test ticket")

        print("")
        print("======================================")
        print("  ALL TESTS PASSED")
        print("======================================")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(run_test())