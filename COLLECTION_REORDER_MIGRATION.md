# Collection reorder migration from orders-on-website to video-main

This archive contains the original video/store-visit booking service plus `collection_reorder.py`, extracted from the old order service. No order creation, order dashboards, order webhooks or snapshot automation were copied. The video-call and store-visit routes, templates and booking functions were not edited.

## Deployment

1. Back up the current `video-main` repo, then replace its files with this archive's `video-main/` contents (preserving your existing live secrets in Render). Push to the existing **video-main** repository and deploy the existing Render web service.
2. Keep its existing Render start command from `render.yaml`: `gunicorn app:app --worker-class gthread --workers 1 --threads 8 --timeout 90 --access-logfile - --error-logfile -`. **Do not use multiple Gunicorn workers**: the in-memory scheduler would run in each process.
3. `collection_reorder.py` can use existing video-main Shopify credentials `SHOPIFY_IN_SHOP_DOMAIN`, `SHOPIFY_IN_ADMIN_ACCESS_TOKEN`, `SHOPIFY_US_SHOP_DOMAIN`, `SHOPIFY_US_ADMIN_ACCESS_TOKEN`. It also supports the older orders-service names `SHOPIFY_IN_STORE_DOMAIN`, `SHOPIFY_IN_ADMIN_TOKEN`, `SHOPIFY_US_STORE_DOMAIN`, `SHOPIFY_US_ADMIN_TOKEN`. Set credentials for both stores in **video-main** Render Environment before enabling the scheduler. If both sets are present, the older order-service names take priority.
4. Set `COLLECTION_REORDER_ADMIN_TOKEN` to a long private token (recommended). As a fallback, `ADMIN_TOKEN` is accepted. **This is separate from `ADMIN_PASSWORD` used by the booking admin UI.** Never add the token to source control.
5. `COLLECTION_REORDER_API_VERSION` defaults to `2025-10`, preserving the old reorder service's Shopify GraphQL version. The video booking service retains its own `SHOPIFY_API_VERSION` value (`2025-07` by default). They do not interfere.
6. First test with `COLLECTION_REORDER_DRY_RUN=1`, `COLLECTION_REORDER_ENABLED=0`; deploy and POST to `/admin/collection_reorder/run`. Read `/admin/collection_reorder/status` and Render logs. To avoid testing all collections, optionally set `COLLECTION_REORDER_ONLY_HANDLES=some-handle`. The default includes both US and India stores.
7. For live nightly runs, set `COLLECTION_REORDER_DRY_RUN=0`, `COLLECTION_REORDER_ENABLED=1`, and clear `COLLECTION_REORDER_ONLY_HANDLES` if it was used. Redeploy. Defaults: every day at **03:00 Asia/Kolkata**, no startup run, US then India. Schedule is in-memory and can run once shortly after a redeploy if Render starts after 03:00 IST, as in the source service.
8. Only after confirming the new webservice completes a successful live reorder and checking bookings, stop **orders-on-website** in Render, remove any old Shopify order webhook targets pointing to it (if still configured), and then delete the old webservice. Do not disable or delete the old service before checking the migration. Order functionality will no longer be accessible from the retired service.

## Manual controls

Substitute your existing video-main Render URL:

```bash
BASE_URL='https://YOUR-VIDEO-MAIN.onrender.com'
REORDER_TOKEN='YOUR_COLLECTION_REORDER_ADMIN_TOKEN'

# Inspect status
curl -H "Authorization: Bearer $REORDER_TOKEN" "$BASE_URL/admin/collection_reorder/status"

# Trigger asynchronous collection reordering
curl -X POST -H "Authorization: Bearer $REORDER_TOKEN" "$BASE_URL/admin/collection_reorder/run"

# Check progress/completion
curl -H "Authorization: Bearer $REORDER_TOKEN" "$BASE_URL/admin/collection_reorder/status"

# Check the original booking health route
curl "$BASE_URL/healthz"
```

The synchronous `POST /admin/collection_reorder/run?sync=1` method still exists but can exceed the Render request timeout. Prefer the asynchronous method.

## Other supported configuration

- `COLLECTION_REORDER_FREQUENCY=daily` (`weekly` also supported)
- `COLLECTION_REORDER_HOUR_IST=3`
- `COLLECTION_REORDER_MINUTE_IST=0`
- `COLLECTION_REORDER_WEEKDAY_IST=0` (Monday, when weekly)
- `COLLECTION_REORDER_STORES=US,INDIA`
- `COLLECTION_REORDER_RUN_ON_STARTUP=0`
- `COLLECTION_REORDER_PROCESS_MANUAL=1`
- `COLLECTION_REORDER_PROCESS_SMART=1`
- `COLLECTION_REORDER_EXCLUDED_HANDLES=current_orders`
- `COLLECTION_REORDER_ONLY_HANDLES=`
- `COLLECTION_REORDER_USE_VARIETY_RANKING=1`
- `COLLECTION_REORDER_PRICE_TIER_COUNT=4`

## Behavior preserved

The original algorithms prioritize `custom.badges` containing `Ready To Ship` for India and `custom.status_in_india` equal to `2-5 Days Across India` for US; then vary by style/category, collection-relative price tier and newness. They use Shopify collection reorder mutations and can switch a collection to manual sort. Shopify reordering uses API/network traffic and happens in a daemon background thread; it does not hold open a booking request.

## Limitations

This code has been syntax/static checked but cannot be run against your live Shopify credentials in this environment. Run the dry-run and live smoke checks before retiring the old Render service. If Render scales to more than one instance, duplicate schedulers are possible; keep one instance and one worker or migrate scheduling to an external job runner.
