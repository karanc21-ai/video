"""Collection reordering extracted from orders-on-website, without order management.

Isolated Blueprint and scheduler, intended to run inside the existing one-worker
video booking Gunicorn service. No booking endpoint or booking function is modified.
"""
import re
import os
import json
import time
import datetime
import threading
import logging
import hmac
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple, Set

import requests
from flask import Blueprint, request, jsonify, abort

reorder_bp = Blueprint("collection_reorder", __name__)
log = logging.getLogger("collection_reorder")
SHOPIFY_API_VERSION = os.getenv("COLLECTION_REORDER_API_VERSION", "2025-10").strip() or "2025-10"
SHOPIFY_IN_STORE_DOMAIN = (os.getenv("SHOPIFY_IN_STORE_DOMAIN") or os.getenv("SHOPIFY_IN_SHOP_DOMAIN") or "").strip()
SHOPIFY_IN_ADMIN_TOKEN = (os.getenv("SHOPIFY_IN_ADMIN_TOKEN") or os.getenv("SHOPIFY_IN_ADMIN_ACCESS_TOKEN") or "").strip()
SHOPIFY_US_STORE_DOMAIN = (os.getenv("SHOPIFY_US_STORE_DOMAIN") or os.getenv("SHOPIFY_US_SHOP_DOMAIN") or "").strip()
SHOPIFY_US_ADMIN_TOKEN = (os.getenv("SHOPIFY_US_ADMIN_TOKEN") or os.getenv("SHOPIFY_US_ADMIN_ACCESS_TOKEN") or "").strip()
SNAPSHOT_COLLECTION_HANDLE = os.getenv("SNAPSHOT_COLLECTION_HANDLE", "current_orders").strip()
ADMIN_TOKEN = os.getenv("COLLECTION_REORDER_ADMIN_TOKEN", os.getenv("ADMIN_TOKEN", "")).strip()

def utc_now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)

def require_admin() -> None:
    token = request.args.get("admin_token", "") or request.args.get("token", "") or request.headers.get("X-Admin-Token", "")
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    if not ADMIN_TOKEN or not hmac.compare_digest(token, ADMIN_TOKEN):
        abort(401)

# Original Shopify reorder functions and ranking rules follow unchanged.

# =========================================================
# COLLECTION REORDER CONFIG + STATE
# =========================================================
def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def parse_csv_env(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


# Live by default for Render automation. Use COLLECTION_REORDER_DRY_RUN=1 for preview.
DRY_RUN = env_bool("COLLECTION_REORDER_DRY_RUN", False)

# Enable/disable the automatic scheduler without removing code.
COLLECTION_REORDER_ENABLED = env_bool("COLLECTION_REORDER_ENABLED", True)
COLLECTION_REORDER_RUN_ON_STARTUP = env_bool("COLLECTION_REORDER_RUN_ON_STARTUP", False)
COLLECTION_REORDER_FREQUENCY = os.getenv("COLLECTION_REORDER_FREQUENCY", "daily").strip().lower()
COLLECTION_REORDER_HOUR_IST = env_int("COLLECTION_REORDER_HOUR_IST", 3)
COLLECTION_REORDER_MINUTE_IST = env_int("COLLECTION_REORDER_MINUTE_IST", 0)
COLLECTION_REORDER_WEEKDAY_IST = env_int("COLLECTION_REORDER_WEEKDAY_IST", 0)  # Monday=0
COLLECTION_REORDER_SCHEDULER_CHECK_SECONDS = env_int("COLLECTION_REORDER_SCHEDULER_CHECK_SECONDS", 60)

# Store order. Accepts INDIA or IN for the India store.
STORES_TO_PROCESS = parse_csv_env("COLLECTION_REORDER_STORES", "US,INDIA")

INDIA_PRODUCT_STYLE_ORDER = [
    "Necklace Set",
    "Earrings",
    "Rings",
    "Bangles & Bracelet",
    "Maang Teeka & Jhoomer",
    "Pendant Set",
    "Bracelet",
    "Bangle",
    "Maang Teeka",
    "Jhoomer / Passa",
    "Pendant Only",
]

US_PRODUCT_CATEGORY_ORDER = [
    "Necklace Set",
    "Earrings",
    "Rings",
    "Bangles & Bracelets",
    "Mang Teeka & Jhoomer",
    "Pendant Set",
    "Bangle",
    "Bracelet",
    "Maang Teeka",
    "Jhoomer / Passa",
]

STORE_CONFIGS = {
    "INDIA": {
        "shop_domain": SHOPIFY_IN_STORE_DOMAIN,
        "admin_token": SHOPIFY_IN_ADMIN_TOKEN,
        "priority_mode": "INDIA_READY_BADGE",
        "variety_metafield_alias": "productStyleMetafield",
        "variety_metafield_label": "custom.product_style",
        "category_order": INDIA_PRODUCT_STYLE_ORDER,
    },
    "IN": {
        "shop_domain": SHOPIFY_IN_STORE_DOMAIN,
        "admin_token": SHOPIFY_IN_ADMIN_TOKEN,
        "priority_mode": "INDIA_READY_BADGE",
        "variety_metafield_alias": "productStyleMetafield",
        "variety_metafield_label": "custom.product_style",
        "category_order": INDIA_PRODUCT_STYLE_ORDER,
    },
    "US": {
        "shop_domain": SHOPIFY_US_STORE_DOMAIN,
        "admin_token": SHOPIFY_US_ADMIN_TOKEN,
        "priority_mode": "US_STATUS_IN_INDIA",
        "variety_metafield_alias": "productCategoryMetafield",
        "variety_metafield_label": "custom.product_category",
        "category_order": US_PRODUCT_CATEGORY_ORDER,
    },
}

USE_PROCESSED_LEDGER = False

PROCESS_MANUAL_COLLECTIONS = env_bool("COLLECTION_REORDER_PROCESS_MANUAL", True)
PROCESS_SMART_COLLECTIONS = env_bool("COLLECTION_REORDER_PROCESS_SMART", True)

# Empty = process all non-excluded collections.
ONLY_COLLECTION_HANDLES: List[str] = parse_csv_env("COLLECTION_REORDER_ONLY_HANDLES", "")

# Protect the order snapshot collection by default. Add more handles with commas if needed.
EXCLUDED_COLLECTION_HANDLES: Set[str] = set(parse_csv_env(
    "COLLECTION_REORDER_EXCLUDED_HANDLES",
    SNAPSHOT_COLLECTION_HANDLE,
))

VERIFY_AFTER_REORDER = env_bool("COLLECTION_REORDER_VERIFY_AFTER", False)

SLEEP_AFTER_SETTING_MANUAL_SEC = float(os.getenv("COLLECTION_REORDER_SLEEP_AFTER_MANUAL_SEC", "1.0"))
SLEEP_AFTER_REORDER_VERIFY_SEC = float(os.getenv("COLLECTION_REORDER_SLEEP_AFTER_VERIFY_SEC", "2.0"))
SLEEP_BETWEEN_COLLECTIONS_SEC = float(os.getenv("COLLECTION_REORDER_SLEEP_BETWEEN_COLLECTIONS_SEC", "0.75"))
SLEEP_BETWEEN_STORES_SEC = float(os.getenv("COLLECTION_REORDER_SLEEP_BETWEEN_STORES_SEC", "1.0"))

POLL_JOB_EVERY_SEC = float(os.getenv("COLLECTION_REORDER_POLL_JOB_EVERY_SEC", "1.5"))
POLL_JOB_TIMEOUT_SEC = env_int("COLLECTION_REORDER_POLL_JOB_TIMEOUT_SEC", 180)

MAX_MOVES_PER_CALL = env_int("COLLECTION_REORDER_MAX_MOVES_PER_CALL", 200)

INDIA_READY_BADGE_VALUE = "ready to ship"
US_READY_STATUS_VALUE = "2-5 Days Across India"

USE_VARIETY_RANKING = env_bool("COLLECTION_REORDER_USE_VARIETY_RANKING", True)
PRICE_TIER_COUNT = env_int("COLLECTION_REORDER_PRICE_TIER_COUNT", 4)
UNKNOWN_PRODUCT_CATEGORY = "uncategorized"
STOP_ON_STORE_ERROR = env_bool("COLLECTION_REORDER_STOP_ON_STORE_ERROR", False)

# Shopify GraphQL can return HTTP 200 with top-level {"errors": [{"message": "Throttled"}]}.
# Retry those inside the GraphQL client instead of letting one collection fail and get skipped.
GRAPHQL_THROTTLE_MAX_RETRIES = env_int("COLLECTION_REORDER_GRAPHQL_THROTTLE_MAX_RETRIES", 60)
GRAPHQL_THROTTLE_BASE_SLEEP_SEC = float(os.getenv("COLLECTION_REORDER_GRAPHQL_THROTTLE_BASE_SLEEP_SEC", "2.0"))
GRAPHQL_THROTTLE_MAX_SLEEP_SEC = float(os.getenv("COLLECTION_REORDER_GRAPHQL_THROTTLE_MAX_SLEEP_SEC", "30.0"))
GRAPHQL_MIN_AVAILABLE_POINTS = float(os.getenv("COLLECTION_REORDER_GRAPHQL_MIN_AVAILABLE_POINTS", "150"))

IST_TZ = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_collection_reorder_job_lock = threading.Lock()
_collection_reorder_state_guard = threading.Lock()
_collection_reorder_state: Dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_trigger": None,
    "last_status": "never_run",
    "last_error": None,
    "last_summary": None,
}
_collection_reorder_last_schedule_key: Optional[str] = None


def processed_file_for_store(store_key: str) -> str:
    return f"processed_collections_{store_key.lower()}.json"


def load_processed(processed_file: str) -> Set[str]:
    path = Path(processed_file)
    if not path.exists():
        return set()

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(str(x) for x in data)
    except Exception:
        pass

    return set()


def save_processed(processed_file: str, processed: Set[str]) -> None:
    Path(processed_file).write_text(
        json.dumps(sorted(processed), indent=2),
        encoding="utf-8",
    )


def mark_processed_if_enabled(processed_file: str, processed: Set[str], collection_id: str) -> None:
    if not USE_PROCESSED_LEDGER:
        return

    processed.add(collection_id)
    save_processed(processed_file, processed)

# =========================================================
# SHOPIFY GRAPHQL CLIENT
# =========================================================

class ShopifyGraphQLClient:
    def __init__(self, shop_domain: str, token: str, api_version: str) -> None:
        self.shop_domain = shop_domain
        self.url = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        })

    def execute(self, query: str, variables: Optional[dict] = None) -> dict:
        payload = {
            "query": query,
            "variables": variables or {},
        }

        throttle_retries = 0

        while True:
            try:
                resp = self.session.post(self.url, json=payload, timeout=60)
            except requests.exceptions.RequestException as e:
                log.warning(f"[COLLECTION REORDER][RETRY] Network error on {self.shop_domain}: {e}. Waiting 2s")
                time.sleep(2.0)
                continue

            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait_s = float(retry_after) if retry_after else GRAPHQL_THROTTLE_BASE_SLEEP_SEC
                except Exception:
                    wait_s = GRAPHQL_THROTTLE_BASE_SLEEP_SEC
                wait_s = min(GRAPHQL_THROTTLE_MAX_SLEEP_SEC, max(1.0, wait_s))
                log.warning(f"[COLLECTION REORDER][RETRY] HTTP {resp.status_code} on {self.shop_domain}. Waiting {wait_s:.2f}s")
                time.sleep(wait_s)
                continue

            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                body_preview = resp.text[:1000]
                raise RuntimeError(
                    f"HTTP error on {self.shop_domain}: {e}\n"
                    f"Response body preview:\n{body_preview}"
                )

            data = resp.json()
            errors = data.get("errors") or []

            if errors:
                is_throttled = any(
                    str(err.get("message") or "").strip().lower() == "throttled"
                    or str((err.get("extensions") or {}).get("code") or "").strip().upper() == "THROTTLED"
                    for err in errors
                    if isinstance(err, dict)
                )

                if is_throttled:
                    throttle_retries += 1
                    if throttle_retries > GRAPHQL_THROTTLE_MAX_RETRIES:
                        raise RuntimeError(
                            f"GraphQL throttled too many times on {self.shop_domain} "
                            f"after {GRAPHQL_THROTTLE_MAX_RETRIES} retries:\n"
                            + json.dumps(errors, indent=2)
                        )

                    wait_s = GRAPHQL_THROTTLE_BASE_SLEEP_SEC
                    try:
                        cost = (data.get("extensions") or {}).get("cost") or {}
                        throttle_status = cost.get("throttleStatus") or {}
                        requested = float(cost.get("requestedQueryCost") or 0)
                        available = float(throttle_status.get("currentlyAvailable") or 0)
                        restore_rate = float(throttle_status.get("restoreRate") or 50)
                        if restore_rate > 0 and requested > available:
                            wait_s = ((requested - available) / restore_rate) + 1.0
                    except Exception:
                        # Fall back to a simple increasing delay.
                        wait_s = GRAPHQL_THROTTLE_BASE_SLEEP_SEC * min(throttle_retries, 5)

                    wait_s = min(GRAPHQL_THROTTLE_MAX_SLEEP_SEC, max(1.0, wait_s))
                    log.warning(
                        f"[COLLECTION REORDER][RETRY] GraphQL throttled on {self.shop_domain}. "
                        f"retry={throttle_retries}/{GRAPHQL_THROTTLE_MAX_RETRIES} waiting {wait_s:.2f}s"
                    )
                    time.sleep(wait_s)
                    continue

                raise RuntimeError(
                    f"Top-level GraphQL errors on {self.shop_domain}:\n"
                    + json.dumps(errors, indent=2)
                )

            # Optional soft pacing. Shopify often returns throttle status in successful responses.
            # If the bucket is getting low, pause briefly before the next request so we do not
            # hit a hard THROTTLED error on the next collection.
            try:
                cost = (data.get("extensions") or {}).get("cost") or {}
                throttle_status = cost.get("throttleStatus") or {}
                available = float(throttle_status.get("currentlyAvailable"))
                restore_rate = float(throttle_status.get("restoreRate") or 50)
                if restore_rate > 0 and available < GRAPHQL_MIN_AVAILABLE_POINTS:
                    wait_s = (GRAPHQL_MIN_AVAILABLE_POINTS - available) / restore_rate
                    wait_s = min(GRAPHQL_THROTTLE_MAX_SLEEP_SEC, max(0.25, wait_s))
                    log.info(
                        f"[COLLECTION REORDER][PACE] GraphQL bucket low on {self.shop_domain}: "
                        f"available={available:.0f}. Waiting {wait_s:.2f}s"
                    )
                    time.sleep(wait_s)
            except Exception:
                pass

            return data["data"]


# =========================================================
# GRAPHQL
# =========================================================

QUERY_SHOP = """
query GetShopIdentity {
  shop {
    name
    myshopifyDomain
  }
}
"""

QUERY_COLLECTIONS = """
query GetCollections($cursor: String) {
  collections(first: 100, after: $cursor) {
    edges {
      cursor
      node {
        id
        title
        handle
        sortOrder
        updatedAt
        ruleSet {
          appliedDisjunctively
        }
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

QUERY_COLLECTION_PRODUCTS = """
query GetCollectionProducts($id: ID!, $cursor: String) {
  collection(id: $id) {
    id
    title
    handle
    sortOrder
    ruleSet {
      appliedDisjunctively
    }
    products(first: 100, after: $cursor) {
      edges {
        cursor
        node {
          id
          legacyResourceId
          title
          handle
          status
          createdAt
          updatedAt

          badgesMetafield: metafield(namespace: "custom", key: "badges") {
            value
          }

          statusInIndiaMetafield: metafield(namespace: "custom", key: "status_in_india") {
            value
          }

          readySinceMetafield: metafield(namespace: "custom", key: "ready_to_ship_since") {
            value
          }

          productStyleMetafield: metafield(namespace: "custom", key: "product_style") {
            value
          }

          productCategoryMetafield: metafield(namespace: "custom", key: "product_category") {
            value
          }

          priceRangeV2 {
            minVariantPrice {
              amount
              currencyCode
            }
          }
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
"""

MUTATION_SET_MANUAL_SORT = """
mutation SetCollectionManualSort($input: CollectionInput!) {
  collectionUpdate(input: $input) {
    collection {
      id
      title
      sortOrder
    }
    userErrors {
      field
      message
    }
  }
}
"""

MUTATION_REORDER_PRODUCTS = """
mutation ReorderCollectionProducts($id: ID!, $moves: [MoveInput!]!) {
  collectionReorderProducts(id: $id, moves: $moves) {
    job {
      id
      done
    }
    userErrors {
      field
      message
    }
  }
}
"""

QUERY_JOB_STATUS = """
query GetJobStatus($id: ID!) {
  job(id: $id) {
    id
    done
  }
}
"""


# =========================================================
# HELPERS
# =========================================================

def normalize_text(s: Optional[str]) -> str:
    return (s or "").strip()


def normalize_for_compare(s: Optional[str]) -> str:
    value = normalize_text(s).casefold()
    value = re.sub(r"\s+", " ", value)
    return value


def is_manual_collection(collection_node: dict) -> bool:
    return collection_node.get("ruleSet") is None


def is_smart_collection(collection_node: dict) -> bool:
    return collection_node.get("ruleSet") is not None


def collection_kind(collection_node: dict) -> str:
    return "SMART" if is_smart_collection(collection_node) else "MANUAL"


def should_process_collection(collection_node: dict) -> bool:
    handle = normalize_text(collection_node.get("handle"))

    if handle in EXCLUDED_COLLECTION_HANDLES:
        return False

    if ONLY_COLLECTION_HANDLES and handle not in ONLY_COLLECTION_HANDLES:
        return False

    if is_manual_collection(collection_node) and PROCESS_MANUAL_COLLECTIONS:
        return True

    if is_smart_collection(collection_node) and PROCESS_SMART_COLLECTIONS:
        return True

    return False


def get_metafield_value(product_node: dict, alias_name: str) -> Optional[str]:
    mf = product_node.get(alias_name)
    if not mf:
        return None

    value = mf.get("value")
    if value is None:
        return None

    return str(value).strip()


def metafield_contains_list_or_text(value: Optional[str], needle: str) -> bool:
    if not value:
        return False

    needle_norm = normalize_for_compare(needle)

    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return any(needle_norm in normalize_for_compare(str(v)) for v in parsed)
    except (json.JSONDecodeError, TypeError):
        pass

    return needle_norm in normalize_for_compare(value)


def has_india_ready_badge(product_node: dict) -> bool:
    value = get_metafield_value(product_node, "badgesMetafield")
    return metafield_contains_list_or_text(value, INDIA_READY_BADGE_VALUE)


def has_us_ready_in_india_status(product_node: dict) -> bool:
    value = get_metafield_value(product_node, "statusInIndiaMetafield")
    return normalize_for_compare(value) == normalize_for_compare(US_READY_STATUS_VALUE)


def is_priority_product(product_node: dict, priority_mode: str) -> bool:
    if priority_mode == "INDIA_READY_BADGE":
        return has_india_ready_badge(product_node)

    if priority_mode == "US_STATUS_IN_INDIA":
        return has_us_ready_in_india_status(product_node)

    raise ValueError(f"Unknown priority_mode: {priority_mode}")


def parse_datetime_value(value: Optional[str]) -> Optional[datetime.datetime]:
    value = normalize_text(value)
    if not value:
        return None

    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"

        dt = datetime.datetime.fromisoformat(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)

        return dt
    except (ValueError, TypeError):
        return None


def get_legacy_resource_id(product_node: dict) -> int:
    raw = product_node.get("legacyResourceId")
    if raw is None:
        return 0

    try:
        return int(str(raw))
    except ValueError:
        return 0


def get_created_at_datetime(product_node: dict) -> Optional[datetime.datetime]:
    return parse_datetime_value(product_node.get("createdAt"))


def parse_possible_json_value(value: Optional[str]) -> Any:
    """
    Shopify metafield values are often raw strings, but list metafields arrive as
    JSON strings. Return parsed JSON when possible; otherwise return the string.
    """
    value = normalize_text(value)
    if not value:
        return None

    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


def first_text_from_metafield_value(value: Optional[str]) -> str:
    parsed = parse_possible_json_value(value)

    if parsed is None:
        return ""

    if isinstance(parsed, list):
        for item in parsed:
            text = normalize_text(str(item))
            if text:
                return text
        return ""

    if isinstance(parsed, dict):
        # Defensive fallback for reference-like values or unexpected structures.
        for key in ("name", "label", "title", "value"):
            if key in parsed:
                text = normalize_text(str(parsed.get(key)))
                if text:
                    return text
        return normalize_text(json.dumps(parsed, sort_keys=True))

    return normalize_text(str(parsed))


def normalize_category_key(value: Optional[str]) -> str:
    value = normalize_for_compare(value)
    value = re.sub(r"[^a-z0-9]+", " ", value).strip()
    return value or UNKNOWN_PRODUCT_CATEGORY


def get_product_category(product_node: dict, category_metafield_alias: str) -> str:
    value = get_metafield_value(product_node, category_metafield_alias)
    category = first_text_from_metafield_value(value)
    return normalize_category_key(category)


def build_category_order_map(category_order: List[str]) -> Dict[str, int]:
    return {
        normalize_category_key(category): idx
        for idx, category in enumerate(category_order)
    }


def get_product_min_price(product_node: dict) -> Optional[float]:
    price_range = product_node.get("priceRangeV2") or {}
    min_variant = price_range.get("minVariantPrice") or {}
    raw_amount = min_variant.get("amount")

    if raw_amount is None:
        return None

    try:
        return float(str(raw_amount))
    except ValueError:
        return None


def build_price_tier_map(products: List[dict]) -> Dict[str, int]:
    """
    Build collection-relative price tiers.

    Tier 0 = highest-priced priority products in this collection.
    Higher tier numbers = lower price tiers.

    This is better than fixed INR bands because the same script runs on India and
    US stores, where currencies and price scales differ.
    """
    priced: List[Tuple[str, float, int]] = []

    for idx, product in enumerate(products):
        price = get_product_min_price(product)
        if price is None:
            continue
        priced.append((product["id"], price, idx))

    if not priced:
        return {product["id"]: PRICE_TIER_COUNT for product in products}

    priced.sort(key=lambda item: (-item[1], item[2]))
    total = len(priced)
    tier_by_id: Dict[str, int] = {}

    for rank, (product_id, _price, _idx) in enumerate(priced):
        tier = int(rank * PRICE_TIER_COUNT / total)
        tier = min(max(tier, 0), PRICE_TIER_COUNT - 1)
        tier_by_id[product_id] = tier

    for product in products:
        tier_by_id.setdefault(product["id"], PRICE_TIER_COUNT)

    return tier_by_id


def newest_first_sort_key(product_node: dict, original_index: Dict[str, int]):
    created_dt = get_created_at_datetime(product_node)
    legacy_id = get_legacy_resource_id(product_node)

    created_key = -created_dt.timestamp() if created_dt else float("inf")
    legacy_key = -legacy_id if legacy_id else float("inf")

    return (created_key, legacy_key, original_index[product_node["id"]])


def build_newest_first_priority_products(products: List[dict], priority_mode: str) -> List[dict]:
    original_index = {p["id"]: idx for idx, p in enumerate(products)}
    priority_products = [p for p in products if is_priority_product(p, priority_mode)]
    return sorted(priority_products, key=lambda p: newest_first_sort_key(p, original_index))


def build_variety_ranked_priority_products(
    products: List[dict],
    priority_mode: str,
    category_metafield_alias: str,
    category_order: List[str],
) -> List[dict]:
    """
    Priority products:
      INDIA: custom.badges contains Ready To Ship
      US:    custom.status_in_india == 2-5 Days Across India

    Ranking inside priority group:
      1. Keep only priority/ready products.
      2. Split them into buckets by the store-specific category metafield + relative price tier.
         INDIA: custom.product_style
         US:    custom.product_category
      3. Round-robin through buckets so the top of the collection does not become
         ten nearly identical recent uploads.
      4. Inside each bucket, keep the newest products first.

    This means a recent batch still gets visibility, but it cannot completely
    hijack the first impression of the collection when other ready products exist.
    """
    priority_products = [p for p in products if is_priority_product(p, priority_mode)]

    if len(priority_products) <= 2:
        return build_newest_first_priority_products(products, priority_mode)

    original_index = {p["id"]: idx for idx, p in enumerate(products)}
    price_tier_by_id = build_price_tier_map(priority_products)
    category_order_map = build_category_order_map(category_order)

    buckets: Dict[Tuple[str, int], Dict[str, Any]] = {}

    for product in priority_products:
        category = get_product_category(product, category_metafield_alias)
        price_tier = price_tier_by_id.get(product["id"], PRICE_TIER_COUNT)
        bucket_key = (category, price_tier)

        if bucket_key not in buckets:
            buckets[bucket_key] = {
                "category": category,
                "price_tier": price_tier,
                "items": [],
                "picked": 0,
                "first_original_index": original_index[product["id"]],
            }

        buckets[bucket_key]["items"].append(product)
        buckets[bucket_key]["first_original_index"] = min(
            buckets[bucket_key]["first_original_index"],
            original_index[product["id"]],
        )

    for bucket in buckets.values():
        bucket["items"].sort(key=lambda p: newest_first_sort_key(p, original_index))

    ranked: List[dict] = []
    last_category: Optional[str] = None

    while True:
        non_empty_buckets = [b for b in buckets.values() if b["items"]]
        if not non_empty_buckets:
            break

        # Avoid repeating the same category back-to-back when there is another
        # category available. This is the main protection against the "small
        # jhumka shop" look.
        candidates = [b for b in non_empty_buckets if b["category"] != last_category]
        if not candidates:
            candidates = non_empty_buckets

        def bucket_sort_key(bucket: Dict[str, Any]):
            head = bucket["items"][0]
            created_dt = get_created_at_datetime(head)
            created_key = -created_dt.timestamp() if created_dt else float("inf")
            price = get_product_min_price(head)
            price_key = -price if price is not None else float("inf")

            category_rank = category_order_map.get(bucket["category"], len(category_order_map))

            # picked first = round-robin balance
            # price_tier second = premium products slightly earlier
            # price third = stronger first impression within the same tier
            # category_rank fourth = predictable store-specific category order
            # created fifth = newer product within equivalent bucket strength
            return (
                bucket["picked"],
                bucket["price_tier"],
                price_key,
                category_rank,
                created_key,
                bucket["first_original_index"],
            )

        chosen_bucket = sorted(candidates, key=bucket_sort_key)[0]
        chosen_product = chosen_bucket["items"].pop(0)
        chosen_bucket["picked"] += 1

        ranked.append(chosen_product)
        last_category = chosen_bucket["category"]

    return ranked


def build_ranked_priority_products(
    products: List[dict],
    priority_mode: str,
    category_metafield_alias: str,
    category_order: List[str],
) -> List[dict]:
    if USE_VARIETY_RANKING:
        return build_variety_ranked_priority_products(
            products=products,
            priority_mode=priority_mode,
            category_metafield_alias=category_metafield_alias,
            category_order=category_order,
        )

    return build_newest_first_priority_products(products, priority_mode)


def build_desired_products(
    products: List[dict],
    priority_mode: str,
    category_metafield_alias: str,
    category_order: List[str],
) -> List[dict]:
    priority_products = build_ranked_priority_products(
        products=products,
        priority_mode=priority_mode,
        category_metafield_alias=category_metafield_alias,
        category_order=category_order,
    )
    priority_ids = {p["id"] for p in priority_products}
    other_products = [p for p in products if p["id"] not in priority_ids]
    return priority_products + other_products


def build_prepend_priority_moves(priority_products: List[dict]) -> List[dict]:
    """
    Desired priority order:
      [Best mixed priority product, next mixed priority product, ...]

    Send moves in reverse desired order, always to position 0.
    Final result preserves the desired mixed order at the top.
    """
    moves: List[dict] = []
    seen = set()

    for product in reversed(priority_products):
        product_id = product["id"]

        if product_id in seen:
            continue

        seen.add(product_id)
        moves.append({
            "id": product_id,
            "newPosition": "0",
        })

    return moves


def chunked(items: List[dict], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def describe_priority_rule(priority_mode: str) -> str:
    if priority_mode == "INDIA_READY_BADGE":
        return 'custom.badges contains "Ready To Ship"'

    if priority_mode == "US_STATUS_IN_INDIA":
        return 'custom.status_in_india == "2-5 Days Across India"'

    return f"unknown priority mode: {priority_mode}"


def describe_variety_summary(priority_products: List[dict], category_metafield_alias: str) -> str:
    if not priority_products:
        return "categories=0 price_tiers=0"

    categories = {get_product_category(p, category_metafield_alias) for p in priority_products}
    price_tier_by_id = build_price_tier_map(priority_products)
    tiers = {price_tier_by_id.get(p["id"], PRICE_TIER_COUNT) for p in priority_products}

    return f"categories={len(categories)} price_tiers={len(tiers)}"


# =========================================================
# FETCHERS
# =========================================================

def verify_shop(client: ShopifyGraphQLClient) -> Tuple[str, str]:
    data = client.execute(QUERY_SHOP)
    shop = data.get("shop") or {}

    return (
        normalize_text(shop.get("name")),
        normalize_text(shop.get("myshopifyDomain")),
    )


def fetch_all_collections(client: ShopifyGraphQLClient) -> List[dict]:
    out: List[dict] = []
    cursor = None

    while True:
        data = client.execute(QUERY_COLLECTIONS, {"cursor": cursor})
        conn = data["collections"]

        for edge in conn["edges"]:
            out.append(edge["node"])

        if not conn["pageInfo"]["hasNextPage"]:
            break

        cursor = conn["pageInfo"]["endCursor"]

    return out


def fetch_all_products_in_collection(
    client: ShopifyGraphQLClient,
    collection_id: str,
) -> Tuple[dict, List[dict]]:
    products: List[dict] = []
    cursor = None
    collection_meta = None

    while True:
        data = client.execute(QUERY_COLLECTION_PRODUCTS, {
            "id": collection_id,
            "cursor": cursor,
        })

        collection = data.get("collection")
        if not collection:
            raise RuntimeError(f"Collection not found: {collection_id}")

        if collection_meta is None:
            collection_meta = {
                "id": collection["id"],
                "title": collection["title"],
                "handle": collection["handle"],
                "sortOrder": collection["sortOrder"],
                "ruleSet": collection["ruleSet"],
            }

        conn = collection["products"]

        for edge in conn["edges"]:
            products.append(edge["node"])

        if not conn["pageInfo"]["hasNextPage"]:
            break

        cursor = conn["pageInfo"]["endCursor"]

    if collection_meta is None:
        raise RuntimeError(f"Collection metadata missing: {collection_id}")

    return collection_meta, products


# =========================================================
# MUTATIONS
# =========================================================

def ensure_manual_sort(
    client: ShopifyGraphQLClient,
    collection_id: str,
    title: str,
    current_sort_order: str,
) -> bool:
    """
    Returns True if sortOrder was changed.
    """
    if current_sort_order == "MANUAL":
        return False

    if DRY_RUN:
        return False

    data = client.execute(MUTATION_SET_MANUAL_SORT, {
        "input": {
            "id": collection_id,
            "sortOrder": "MANUAL",
        }
    })

    payload = data["collectionUpdate"]
    user_errors = payload.get("userErrors", [])

    if user_errors:
        raise RuntimeError(
            f"collectionUpdate userErrors for '{title}':\n"
            + json.dumps(user_errors, indent=2)
        )

    return True


def poll_job_until_done(
    client: ShopifyGraphQLClient,
    job_id: str,
    timeout_sec: int = POLL_JOB_TIMEOUT_SEC,
) -> bool:
    start = time.time()

    while time.time() - start < timeout_sec:
        data = client.execute(QUERY_JOB_STATUS, {"id": job_id})
        job = data.get("job")

        if not job:
            raise RuntimeError(f"Job not found: {job_id}")

        if job.get("done") is True:
            return True

        time.sleep(POLL_JOB_EVERY_SEC)

    return False


def reorder_collection(
    client: ShopifyGraphQLClient,
    collection_id: str,
    title: str,
    moves: List[dict],
) -> None:
    if not moves:
        return

    if DRY_RUN:
        return

    for move_chunk in chunked(moves, MAX_MOVES_PER_CALL):
        data = client.execute(MUTATION_REORDER_PRODUCTS, {
            "id": collection_id,
            "moves": move_chunk,
        })

        payload = data["collectionReorderProducts"]
        user_errors = payload.get("userErrors", [])

        if user_errors:
            raise RuntimeError(
                f"collectionReorderProducts userErrors for '{title}':\n"
                + json.dumps(user_errors, indent=2)
            )

        job = payload.get("job")
        if not job:
            raise RuntimeError(f"No job returned for '{title}'")

        job_id = job["id"]
        done = poll_job_until_done(client, job_id)

        if not done:
            raise TimeoutError(f"Timed out waiting for reorder job: {job_id}")


# =========================================================
# PROCESSING
# =========================================================

def verify_after_reorder_if_enabled(
    client: ShopifyGraphQLClient,
    collection_id: str,
    priority_mode: str,
    category_metafield_alias: str,
    category_order: List[str],
) -> bool:
    if not VERIFY_AFTER_REORDER:
        return True

    time.sleep(SLEEP_AFTER_REORDER_VERIFY_SEC)

    _meta, verify_products = fetch_all_products_in_collection(client, collection_id)
    verify_desired_ids = [
        p["id"] for p in build_desired_products(
            products=verify_products,
            priority_mode=priority_mode,
            category_metafield_alias=category_metafield_alias,
            category_order=category_order,
        )
    ]
    actual_ids = [p["id"] for p in verify_products]

    return actual_ids == verify_desired_ids


def process_collection(
    client: ShopifyGraphQLClient,
    store_key: str,
    collection: dict,
    collection_index: int,
    total_collections: int,
    priority_mode: str,
    category_metafield_alias: str,
    category_order: List[str],
    processed_file: str,
    processed: Set[str],
) -> None:
    collection_id = collection["id"]
    collection_title = collection["title"]
    collection_handle = collection["handle"]

    prefix = f"[{store_key}] {collection_index}/{total_collections} {collection_handle}"

    if USE_PROCESSED_LEDGER and collection_id in processed:
        log.info(f"[COLLECTION REORDER] {prefix} | SKIP ledger")
        return

    meta, products = fetch_all_products_in_collection(client, collection_id)

    if len(products) <= 1:
        log.info(f"[COLLECTION REORDER] {prefix} | SKIP products={len(products)}")
        mark_processed_if_enabled(processed_file, processed, collection_id)
        return

    changed_to_manual = ensure_manual_sort(
        client=client,
        collection_id=collection_id,
        title=collection_title,
        current_sort_order=meta["sortOrder"],
    )

    if changed_to_manual:
        time.sleep(SLEEP_AFTER_SETTING_MANUAL_SEC)
        meta, products = fetch_all_products_in_collection(client, collection_id)

    current_ids = [p["id"] for p in products]

    priority_products = build_ranked_priority_products(
        products=products,
        priority_mode=priority_mode,
        category_metafield_alias=category_metafield_alias,
        category_order=category_order,
    )
    desired_products = build_desired_products(
        products=products,
        priority_mode=priority_mode,
        category_metafield_alias=category_metafield_alias,
        category_order=category_order,
    )
    desired_ids = [p["id"] for p in desired_products]

    priority_count = len(priority_products)
    other_count = len(products) - priority_count
    variety_summary = describe_variety_summary(priority_products, category_metafield_alias)

    if current_ids == desired_ids:
        log.info(f"[COLLECTION REORDER] {prefix} | OK already | products={len(products)} priority={priority_count} other={other_count} {variety_summary}")
        mark_processed_if_enabled(processed_file, processed, collection_id)
        return

    moves = build_prepend_priority_moves(priority_products)

    reorder_collection(
        client=client,
        collection_id=collection_id,
        title=collection_title,
        moves=moves,
    )

    verify_ok = verify_after_reorder_if_enabled(
        client=client,
        collection_id=collection_id,
        priority_mode=priority_mode,
        category_metafield_alias=category_metafield_alias,
        category_order=category_order,
    )

    if DRY_RUN:
        log.info(f"[COLLECTION REORDER] {prefix} | DRY_RUN would_reorder | products={len(products)} priority={priority_count} moves={len(moves)} {variety_summary}")
    else:
        verify_text = "verify=off" if not VERIFY_AFTER_REORDER else ("verify=ok" if verify_ok else "verify=warning")
        log.info(f"[COLLECTION REORDER] {prefix} | REORDERED | products={len(products)} priority={priority_count} moves={len(moves)} {verify_text} {variety_summary}")

    mark_processed_if_enabled(processed_file, processed, collection_id)




def process_store(store_key: str, store_config: Dict[str, Any]) -> Dict[str, Any]:
    shop_domain = normalize_text(store_config.get("shop_domain"))
    admin_token = normalize_text(store_config.get("admin_token"))
    priority_mode = store_config["priority_mode"]
    category_metafield_alias = store_config["variety_metafield_alias"]
    category_metafield_label = store_config["variety_metafield_label"]
    category_order = list(store_config["category_order"])
    processed_file = processed_file_for_store(store_key)

    summary: Dict[str, Any] = {
        "store": store_key,
        "domain": shop_domain,
        "collections_seen": 0,
        "collections_to_process": 0,
        "collections_done": 0,
        "collections_error": 0,
        "errors": [],
    }

    if not shop_domain or not admin_token:
        msg = f"missing Shopify domain/token for {store_key}"
        log.warning(f"[COLLECTION REORDER] {msg}")
        summary["errors"].append(msg)
        summary["collections_error"] += 1
        return summary

    log.info("#" * 100)
    log.info(f"[COLLECTION REORDER][STORE START] {store_key} | domain={shop_domain}")
    log.info(f"[COLLECTION REORDER][RULE] {describe_priority_rule(priority_mode)}")
    log.info(f"[COLLECTION REORDER][VARIETY] category_field={category_metafield_label} price_tiers={PRICE_TIER_COUNT}")
    log.info("#" * 100)

    client = ShopifyGraphQLClient(shop_domain, admin_token, SHOPIFY_API_VERSION)

    shop_name, myshopify_domain = verify_shop(client)
    log.info(f"[COLLECTION REORDER][VERIFY] {store_key} connected to {shop_name} / {myshopify_domain}")

    processed = load_processed(processed_file) if USE_PROCESSED_LEDGER else set()

    collections = fetch_all_collections(client)
    target_collections = [c for c in collections if should_process_collection(c)]

    summary["collections_seen"] = len(collections)
    summary["collections_to_process"] = len(target_collections)

    log.info(
        f"[COLLECTION REORDER][STORE INFO] {store_key} collections={len(collections)} "
        f"to_process={len(target_collections)} dry_run={DRY_RUN} excluded={sorted(EXCLUDED_COLLECTION_HANDLES)}"
    )

    for idx, collection in enumerate(target_collections, start=1):
        try:
            process_collection(
                client=client,
                store_key=store_key,
                collection=collection,
                collection_index=idx,
                total_collections=len(target_collections),
                priority_mode=priority_mode,
                category_metafield_alias=category_metafield_alias,
                category_order=category_order,
                processed_file=processed_file,
                processed=processed,
            )
            summary["collections_done"] += 1
        except KeyboardInterrupt:
            log.warning("[COLLECTION REORDER] stopped by KeyboardInterrupt")
            raise
        except Exception as e:
            msg = f"{collection.get('handle')} | {e}"
            log.exception(f"[COLLECTION REORDER][{store_key}] {idx}/{len(target_collections)} ERROR {msg}")
            summary["collections_error"] += 1
            summary["errors"].append(msg)

            if STOP_ON_STORE_ERROR:
                raise

        time.sleep(SLEEP_BETWEEN_COLLECTIONS_SEC)

    log.info(f"[COLLECTION REORDER][STORE DONE] {store_key}")
    return summary


def normalize_store_key_for_reorder(store_key: str) -> str:
    key = normalize_text(store_key).upper()
    if key == "IN":
        return "INDIA"
    return key


def run_collection_reorder_all_stores(trigger: str = "manual") -> Dict[str, Any]:
    log.info("[COLLECTION REORDER][START] trigger=%s api_version=%s dry_run=%s stores=%s", trigger, SHOPIFY_API_VERSION, DRY_RUN, STORES_TO_PROCESS)

    overall: Dict[str, Any] = {
        "trigger": trigger,
        "api_version": SHOPIFY_API_VERSION,
        "dry_run": DRY_RUN,
        "started_at": utc_now().isoformat(),
        "stores": [],
    }

    for raw_store_key in STORES_TO_PROCESS:
        store_key = normalize_store_key_for_reorder(raw_store_key)
        if store_key not in STORE_CONFIGS:
            msg = f"Unknown store key in COLLECTION_REORDER_STORES: {raw_store_key}. Valid values: US, INDIA, IN"
            log.warning(f"[COLLECTION REORDER] {msg}")
            overall["stores"].append({"store": raw_store_key, "error": msg})
            if STOP_ON_STORE_ERROR:
                raise ValueError(msg)
            continue

        try:
            summary = process_store(store_key, STORE_CONFIGS[store_key])
            overall["stores"].append(summary)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.exception(f"[COLLECTION REORDER][STORE ERROR] {store_key}: {e}")
            overall["stores"].append({"store": store_key, "error": str(e)})
            if STOP_ON_STORE_ERROR:
                raise

        time.sleep(SLEEP_BETWEEN_STORES_SEC)

    overall["finished_at"] = utc_now().isoformat()
    log.info("[COLLECTION REORDER][DONE] trigger=%s", trigger)
    return overall


def _set_collection_reorder_state(**updates: Any) -> None:
    with _collection_reorder_state_guard:
        _collection_reorder_state.update(updates)


def get_collection_reorder_state_snapshot() -> Dict[str, Any]:
    with _collection_reorder_state_guard:
        return dict(_collection_reorder_state)


def collection_reorder_job_runner(trigger: str = "manual") -> None:
    acquired = _collection_reorder_job_lock.acquire(blocking=False)
    if not acquired:
        log.info("[COLLECTION REORDER] job already running; trigger=%s ignored", trigger)
        return

    _set_collection_reorder_state(
        running=True,
        last_started_at=utc_now().isoformat(),
        last_finished_at=None,
        last_trigger=trigger,
        last_status="running",
        last_error=None,
    )

    try:
        summary = run_collection_reorder_all_stores(trigger=trigger)
        _set_collection_reorder_state(
            running=False,
            last_finished_at=utc_now().isoformat(),
            last_status="ok",
            last_error=None,
            last_summary=summary,
        )
    except Exception as e:
        log.exception("[COLLECTION REORDER] job failed")
        _set_collection_reorder_state(
            running=False,
            last_finished_at=utc_now().isoformat(),
            last_status="error",
            last_error=str(e),
        )
    finally:
        _collection_reorder_job_lock.release()


def start_collection_reorder_job(trigger: str = "manual") -> bool:
    if _collection_reorder_job_lock.locked():
        return False

    t = threading.Thread(target=collection_reorder_job_runner, kwargs={"trigger": trigger}, daemon=True)
    t.start()
    return True


def collection_reorder_schedule_key(now_ist: datetime.datetime) -> Optional[str]:
    scheduled_today = now_ist.replace(
        hour=COLLECTION_REORDER_HOUR_IST,
        minute=COLLECTION_REORDER_MINUTE_IST,
        second=0,
        microsecond=0,
    )

    if now_ist < scheduled_today:
        return None

    if COLLECTION_REORDER_FREQUENCY == "weekly":
        if now_ist.weekday() != COLLECTION_REORDER_WEEKDAY_IST:
            return None
        iso = now_ist.isocalendar()
        return f"weekly-{iso.year}-W{iso.week:02d}"

    return now_ist.strftime("daily-%Y-%m-%d")


def collection_reorder_scheduler_loop() -> None:
    global _collection_reorder_last_schedule_key

    if COLLECTION_REORDER_RUN_ON_STARTUP and COLLECTION_REORDER_ENABLED:
        log.info("[COLLECTION REORDER] startup run requested")
        start_collection_reorder_job(trigger="startup")

    while True:
        try:
            if COLLECTION_REORDER_ENABLED:
                now_ist = utc_now().astimezone(IST_TZ)
                key = collection_reorder_schedule_key(now_ist)

                if key and key != _collection_reorder_last_schedule_key:
                    started = start_collection_reorder_job(trigger=f"scheduled:{key}")
                    if started:
                        _collection_reorder_last_schedule_key = key
                        log.info("[COLLECTION REORDER] scheduled job started key=%s", key)
                    else:
                        log.info("[COLLECTION REORDER] scheduled job due but previous job still running key=%s", key)
        except Exception as e:
            log.warning(f"[COLLECTION REORDER] scheduler error: {e!r}")

        time.sleep(max(30, COLLECTION_REORDER_SCHEDULER_CHECK_SECONDS))


@reorder_bp.get("/admin/collection_reorder/status")
def admin_collection_reorder_status():
    require_admin()
    return jsonify(get_collection_reorder_state_snapshot())


@reorder_bp.post("/admin/collection_reorder/run")
def admin_collection_reorder_run():
    require_admin()
    sync = (request.args.get("sync", "") or "").strip().lower() in ("1", "true", "yes")

    if sync:
        if not _collection_reorder_job_lock.acquire(blocking=False):
            return jsonify({"status": "already_running", "state": get_collection_reorder_state_snapshot()}), 409
        try:
            _set_collection_reorder_state(
                running=True,
                last_started_at=utc_now().isoformat(),
                last_finished_at=None,
                last_trigger="manual_sync",
                last_status="running",
                last_error=None,
            )
            summary = run_collection_reorder_all_stores(trigger="manual_sync")
            _set_collection_reorder_state(
                running=False,
                last_finished_at=utc_now().isoformat(),
                last_status="ok",
                last_error=None,
                last_summary=summary,
            )
            return jsonify({"status": "ok", "summary": summary})
        except Exception as e:
            _set_collection_reorder_state(
                running=False,
                last_finished_at=utc_now().isoformat(),
                last_status="error",
                last_error=str(e),
            )
            raise
        finally:
            _collection_reorder_job_lock.release()

    started = start_collection_reorder_job(trigger="manual")
    if not started:
        return jsonify({"status": "already_running", "state": get_collection_reorder_state_snapshot()}), 409

    return jsonify({"status": "started", "state": get_collection_reorder_state_snapshot()})


# =========================================================

_scheduler_started = False
_scheduler_start_lock = threading.Lock()

def register_collection_reorder(app):
    """Register standalone routes and one scheduler; never alters booking handlers."""
    global _scheduler_started
    app.register_blueprint(reorder_bp)
    with _scheduler_start_lock:
        if not _scheduler_started and COLLECTION_REORDER_ENABLED:
            thread = threading.Thread(target=collection_reorder_scheduler_loop, name="collection-reorder-scheduler", daemon=True)
            thread.start()
            _scheduler_started = True
            log.info("[COLLECTION REORDER] scheduler started")
