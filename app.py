from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import hashlib
import os
import json
import jwt
import asyncio
import time
from contextlib import asynccontextmanager
import threading

# Firebase Admin SDK - Firestore
import firebase_admin
from firebase_admin import credentials, firestore
from google.api_core.exceptions import Aborted, FailedPrecondition, InvalidArgument, NotFound

# -------------------------------
# Simple in-memory cache with TTL
# -------------------------------
cache_data: Dict[str, Tuple[Any, float]] = {}
cache_lock = threading.Lock()
CACHE_TTL = {
    "products": 600,     # 10 minutes
    "categories": 1800,  # 30 minutes
    "users": 300,        # 5 minutes
    "counters": 60,      # 1 minute
    "default": 300,
}

def get_cache(key: str, cache_type: str = "default"):
    with cache_lock:
        if key in cache_data:
            data, timestamp = cache_data[key]
            ttl = CACHE_TTL.get(cache_type, CACHE_TTL["default"])
            if time.time() - timestamp < ttl:
                return data
        return None

def set_cache(key: str, data: Any, cache_type: str = "default"):
    with cache_lock:
        cache_data[key] = (data, time.time())

def clear_cache_pattern(pattern: str):
    with cache_lock:
        keys_to_delete = [k for k in cache_data.keys() if pattern in k]
        for k in keys_to_delete:
            del cache_data[k]

# -------------------------------
# Firebase initialization
# -------------------------------
def _load_firebase_credentials() -> credentials.Certificate:
    json_str = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if json_str:
        try:
            data = json.loads(json_str)
            return credentials.Certificate(data)
        except Exception:
            # If FIREBASE_CREDENTIALS_JSON is a path, fallback to file
            if os.path.exists(json_str):
                return credentials.Certificate(json_str)
            raise
    path = os.getenv("FIREBASE_CREDENTIALS", "credentials.json")
    if not os.path.exists(path):
        raise RuntimeError(
            "Firebase credentials not found. Provide FIREBASE_CREDENTIALS_JSON (JSON or path) "
            "or place credentials.json in the app directory."
        )
    return credentials.Certificate(path)

if not firebase_admin._apps:
    cred = _load_firebase_credentials()
    firebase_admin.initialize_app(cred)

db = firestore.client()

# -------------------------------
# ID sequences and helpers (Firestore)
# -------------------------------
def _now_ts() -> float:
    return time.time()

@firestore.transactional
def _increment_counter_txn(transaction: firestore.Transaction, col_name: str) -> int:
    ref = db.collection("counters").document(col_name)
    snapshot = ref.get(transaction=transaction)
    current = 0
    if snapshot.exists:
        current = int(snapshot.to_dict().get("value", 0))
    new_val = current + 1
    transaction.set(ref, {"value": new_val})
    return new_val

async def next_sequence(col_name: str) -> int:
    # Use cached counter for speed; still transactional increment to persist
    cache_key = f"counter_{col_name}"
    cached = get_cache(cache_key, "counters")
    if cached:
        new_id = int(cached) + 1
        set_cache(cache_key, new_id, "counters")

        # best-effort async sync to Firestore
        async def _sync():
            try:
                _ = _increment_counter_txn(db.transaction(), col_name)
            except Exception as e:
                print(f"[counter] async increment failed: {e}")
        asyncio.create_task(_sync())
        return new_id

    # First-time (or expired) - do transactional increment
    new_id = _increment_counter_txn(db.transaction(), col_name)
    set_cache(cache_key, new_id, "counters")
    return new_id

async def create_with_numeric_id(col_name: str, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    nid = await next_sequence(col_name)
    doc_id = str(nid)
    payload = {**payload, "id": nid, "created_at": _now_ts()}
    db.collection(col_name).document(doc_id).set(payload)
    set_cache(f"{col_name}_{nid}", payload, col_name)
    clear_cache_pattern(f"{col_name}_all")
    return doc_id, payload

async def get_doc_by_numeric_id(col_name: str, numeric_id: int) -> Optional[Tuple[str, Dict[str, Any]]]:
    cache_key = f"{col_name}_{numeric_id}"
    cached = get_cache(cache_key, col_name)
    if cached:
        return str(numeric_id), cached
    ref = db.collection(col_name).document(str(numeric_id))
    snap = ref.get()
    if snap.exists:
        data = snap.to_dict()
        set_cache(cache_key, data, col_name)
        return str(numeric_id), data
    return None

async def get_collection_cached(col_name: str, filters: Dict[str, Any] = None) -> List[Dict[str, Any]]:
    cache_key = f"{col_name}_all"
    if filters:
        try:
            filters_tuple = tuple(sorted(filters.items()))
            cache_key += f"_{hash(filters_tuple)}"
        except Exception:
            cache_key += f"_{hash(str(filters))}"

    cached = get_cache(cache_key, col_name)
    if cached:
        return cached

    # To avoid composite index issues at runtime, only use a single equality filter in Firestore.
    # If multiple filters are needed, fetch all and then filter in Python (safe fallback).
    items: List[Dict[str, Any]] = []
    try:
        if filters and len(filters) == 1:
            (k, v), = filters.items()
            query = db.collection(col_name).where(k, "==", v)
            docs = list(query.stream())
        else:
            docs = list(db.collection(col_name).stream())

        for d in docs:
            data = d.to_dict()
            if not isinstance(data, dict):
                continue
            if filters and len(filters) != 1:
                # Python-side filter
                if any(data.get(k) != v for k, v in filters.items()):
                    continue
            items.append(data)
    except Exception as e:
        # Fallback: if query failed (e.g., index), fetch all and filter locally
        docs = list(db.collection(col_name).stream())
        for d in docs:
            data = d.to_dict()
            if not isinstance(data, dict):
                continue
            if filters:
                if any(data.get(k) != v for k, v in filters.items()):
                    continue
            items.append(data)

    items.sort(key=lambda x: x.get("id", 0))
    set_cache(cache_key, items, col_name)
    return items

# -------------------------------
# Seed data
# -------------------------------
def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

async def seed_admin_user() -> None:
    users = await get_collection_cached("users", {"role": "admin"})
    if users:
        return
    await create_with_numeric_id(
        "users",
        {
            "name": "Admin User",
            "username": "admin",
            "email": "admin@shiptorockscollection.com",
            "password": sha256("admin123"),
            "phone": "",
            "address": "",
            "role": "admin",
        },
    )

async def seed_categories() -> None:
    categories = await get_collection_cached("categories")
    if categories:
        return

    sample_categories = [
        ("Traditional", "Timeless classics featuring traditional weaves and authentic craftsmanship", "/traditional-saree-category.png", 45),
        ("Designer", "Contemporary designs with modern aesthetics and innovative patterns", "/designer-saree-category.png", 32),
        ("Bridal", "Exquisite bridal sarees for your special day with intricate embellishments", "/bridal-saree-category.png", 28),
        ("Party Wear", "Glamorous sarees perfect for celebrations and special occasions", "/party-wear-saree-category.png", 38),
        ("Casual", "Comfortable and elegant sarees for everyday wear and office", "/casual-saree-category.png", 52),
        ("Regional", "Authentic regional sarees from different states of India", "/regional-saree-category.png", 41),
    ]
    batch = db.batch()
    for i, (name, description, image, product_count) in enumerate(sample_categories, 1):
        ref = db.collection("categories").document(str(i))
        batch.set(ref, {
            "id": i,
            "name": name,
            "description": description,
            "image": image,
            "product_count": product_count,
            "created_at": _now_ts(),
        })
    batch.set(db.collection("counters").document("categories"), {"value": len(sample_categories)})
    batch.commit()

async def seed_products() -> None:
    products = await get_collection_cached("products")
    if products:
        return

    sample_products = [
        ("Royal Banarasi Silk Saree", "Exquisite handwoven Banarasi silk saree with intricate gold zari work and traditional motifs. Perfect for weddings and special occasions.", 4500.00, ["/placeholder.svg?height=500&width=400"] * 3, "traditional", "Banarasi Silk", "Deep Red", 8, 4.8, 24, ["Free Size"], "Dry clean only. Store in cotton cloth.", True),
        ("Premium Kanjivaram Silk Saree", "Authentic Kanjivaram silk saree with temple border design and rich color combination. Handwoven by master craftsmen.", 5200.00, ["/placeholder.svg?height=500&width=400"] * 3, "traditional", "Kanjivaram Silk", "Royal Blue", 6, 4.9, 18, ["Free Size"], "Dry clean recommended. Avoid direct sunlight.", True),
        ("Designer Georgette Party Saree", "Contemporary georgette saree with sequin work perfect for evening parties and celebrations.", 2800.00, ["/placeholder.svg?height=500&width=400"] * 3, "party-wear", "Georgette", "Blush Pink", 12, 4.6, 31, ["Free Size"], "Hand wash or dry clean. Iron on low heat.", True),
        ("Handloom Cotton Saree", "Comfortable pure cotton handloom saree with traditional block prints. Perfect for daily wear.", 1200.00, ["/placeholder.svg?height=500&width=400"] * 3, "casual", "Cotton", "Forest Green", 15, 4.4, 42, ["Free Size"], "Machine wash cold. Line dry in shade.", True),
        ("Chiffon Designer Saree", "Trendy chiffon saree with modern digital prints and lightweight fabric. Ideal for office wear.", 2200.00, ["/placeholder.svg?height=500&width=400"] * 3, "designer", "Chiffon", "Lavender", 10, 4.5, 28, ["Free Size"], "Gentle hand wash. Do not wring.", True),
        ("Tussar Silk Heritage Saree", "Premium Tussar silk saree with hand-painted Madhubani motifs. A piece of art to wear.", 3800.00, ["/placeholder.svg?height=500&width=400"] * 3, "traditional", "Tussar Silk", "Golden Yellow", 5, 4.7, 15, ["Free Size"], "Dry clean only. Handle with care.", True),
        ("Net Embroidered Bridal Saree", "Luxurious net saree with heavy embroidery and stone work for special occasions and weddings.", 6500.00, ["/placeholder.svg?height=500&width=400"] * 3, "bridal", "Net", "Midnight Black", 4, 4.9, 12, ["Free Size"], "Professional dry clean only. Store flat.", True),
        ("Linen Casual Saree", "Eco-friendly linen saree perfect for daily wear with subtle border design. Breathable and comfortable.", 1800.00, ["/placeholder.svg?height=500&width=400"] * 3, "casual", "Linen", "Off White", 20, 4.3, 35, ["Free Size"], "Machine wash gentle cycle. Iron while damp.", True),
        ("Organza Festive Saree", "Shimmering organza saree with gold thread work ideal for festivals and celebrations.", 3200.00, ["/placeholder.svg?height=500&width=400"] * 3, "party-wear", "Organza", "Sunset Orange", 8, 4.6, 22, ["Free Size"], "Dry clean recommended. Store carefully.", True),
        ("Chanderi Silk Saree", "Traditional Chanderi silk saree with delicate zari border and lightweight feel. Perfect for formal occasions.", 2900.00, ["/placeholder.svg?height=500&width=400"] * 3, "traditional", "Chanderi Silk", "Teal Blue", 7, 4.8, 19, ["Free Size"], "Dry clean only. Avoid moisture.", True),
    ]
    batch = db.batch()
    for i, (name, description, price, images, category, fabric, color, stock, rating, reviews_count, sizes, care_instructions, is_active) in enumerate(sample_products, 1):
        ref = db.collection("products").document(str(i))
        batch.set(ref, {
            "id": i,
            "name": name,
            "description": description,
            "price": float(price),
            "image": images[0] if images else None,
            "images": images,
            "category": category,
            "fabric": fabric,
            "color": color,
            "stock": int(stock),
            "rating": float(rating),
            "reviews_count": int(reviews_count),
            "sizes": sizes,
            "care_instructions": care_instructions,
            "is_active": bool(is_active),
            "created_at": _now_ts(),
        })
    batch.set(db.collection("counters").document("products"), {"value": len(sample_products)})
    batch.commit()

async def bootstrap():
    await asyncio.gather(
        seed_admin_user(),
        seed_categories(),
        seed_products()
    )

# -------------------------------
# Pydantic models
# -------------------------------
class UserRegister(BaseModel):
    name: str
    email: EmailStr
    phone: Optional[str] = ""
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class OrderCreate(BaseModel):
    items: List[Dict[str, Any]]
    shipping_address: str
    phone: str
    payment_method: str = "cod"

class ReviewCreate(BaseModel):
    product_id: int
    rating: int
    comment: Optional[str] = ""

class ProductCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    price: float
    category: str
    stock: int = 0
    images: Optional[List[str]] = []
    is_active: bool = True
    fabric: Optional[str] = ""
    color: Optional[str] = ""
    sizes: Optional[List[str]] = ["Free Size"]
    care_instructions: Optional[str] = ""

class ProductUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    category: Optional[str] = None
    stock: Optional[int] = None
    images: Optional[List[str]] = None
    is_active: Optional[bool] = None
    fabric: Optional[str] = None
    color: Optional[str] = None
    sizes: Optional[List[str]] = None
    care_instructions: Optional[str] = None

class OrderStatusUpdate(BaseModel):
    status: str

# -------------------------------
# Auth utils (JWT)
# -------------------------------
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "53d1febbc7b724028cf112906ebe62bcfe05cbf16c3296871afe0359f773755a")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30 * 24 * 60  # 30 days

security = HTTPBearer()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("user_id")
        if user_id is None:
            raise credentials_exception
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, Exception):
        raise credentials_exception

    user = await get_doc_by_numeric_id("users", user_id)
    if user is None:
        raise credentials_exception
    return user[1]

async def get_admin_user(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user

# -------------------------------
# Helpers
# -------------------------------
def product_to_view(p: Dict[str, Any]) -> Dict[str, Any]:
    images = p.get("images") or ([p.get("image")] if p.get("image") else [])
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "description": p.get("description"),
        "price": p.get("price"),
        "image": p.get("image"),
        "images": images,
        "category": p.get("category"),
        "fabric": p.get("fabric"),
        "color": p.get("color"),
        "stock": p.get("stock", 0),
        "rating": p.get("rating", 0),
        "reviews_count": p.get("reviews_count", 0),
        "sizes": p.get("sizes", ["Free Size"]),
        "care_instructions": p.get("care_instructions"),
        "inStock": int(p.get("stock", 0)) > 0,
        "is_active": bool(p.get("is_active", True)),
        "created_at": p.get("created_at"),
    }

def _ts_to_epoch(value: Any) -> float:
    try:
        if isinstance(value, (int, float)):
            return float(value)
        if hasattr(value, "timestamp"):
            return float(value.timestamp())
    except Exception:
        pass
    return 0.0

def _ts_to_iso(value: Any) -> str:
    try:
        if isinstance(value, (int, float)):
            return datetime.utcfromtimestamp(float(value)).isoformat() + "Z"
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if hasattr(value, "timestamp"):
            return datetime.utcfromtimestamp(float(value.timestamp())).isoformat() + "Z"
    except Exception:
        pass
    return ""

# -------------------------------
# FastAPI app + CORS + lifespan
# -------------------------------
app = FastAPI(
    title="Ship to Rocks Collection API - Firestore",
    description="FastAPI backend migrated to Firestore with atomic stock updates",
    version="4.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "https://shiptorockscollection.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bootstrap()
    yield
app.lifespan = lifespan

# -------------------------------
# Public routes
# -------------------------------
@app.get("/api/categories")
async def get_categories():
    cats = await get_collection_cached("categories")
    return cats

@app.get("/api/products")
async def get_products(category: Optional[str] = None, search: Optional[str] = None, sort: str = "name"):
    filters = {"is_active": True}
    # Avoid composite-index requirement by filtering in Python when both filters present
    products = await get_collection_cached("products")
    # Filter by active
    products = [p for p in products if p.get("is_active", True)]
    if category and category != "all":
        products = [p for p in products if p.get("category") == category]

    if search:
        s = search.lower()
        products = [
            p for p in products
            if (p.get("name") and s in p["name"].lower())
            or (p.get("description") and s in p["description"].lower())
        ]

    if sort == "price-low":
        products.sort(key=lambda x: float(x.get("price", 0)))
    elif sort == "price-high":
        products.sort(key=lambda x: float(x.get("price", 0)), reverse=True)
    elif sort == "rating":
        products.sort(key=lambda x: float(x.get("rating", 0)), reverse=True)
    else:
        products.sort(key=lambda x: (x.get("name") or ""))

    return [product_to_view(p) for p in products]

@app.get("/api/products/batch")
async def get_products_batch(ids: str):
    try:
        product_ids = [int(id.strip()) for id in ids.split(",") if id.strip()]
        if not product_ids:
            return []
        out = []
        for pid in product_ids:
            result = await get_doc_by_numeric_id("products", pid)
            if result:
                _, pdata = result
                if pdata.get("is_active", True):
                    out.append(product_to_view(pdata))
        return out
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid product IDs")

@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    found = await get_doc_by_numeric_id("products", product_id)
    if not found:
        raise HTTPException(status_code=404, detail="Product not found")
    _, pdata = found

    reviews_cache_key = f"reviews_product_{product_id}"
    reviews = get_cache(reviews_cache_key, "products")
    if reviews is None:
        # Query all, filter locally to avoid index issues
        all_reviews = await get_collection_cached("reviews")
        reviews = [r for r in all_reviews if r.get("product_id") == product_id]
        reviews.sort(key=lambda x: _ts_to_epoch(x.get("created_at", 0)), reverse=True)

        # Resolve user names (bulk fetch)
        user_ids = list({r.get("user_id") for r in reviews if r.get("user_id")})
        user_cache: Dict[int, str] = {}
        for uid in user_ids:
            user_result = await get_doc_by_numeric_id("users", int(uid))
            if user_result:
                _, udata = user_result
                user_cache[int(uid)] = udata.get("name")
        for r in reviews:
            r["user_name"] = user_cache.get(int(r.get("user_id", 0)))
        set_cache(reviews_cache_key, reviews, "products")

    pview = product_to_view(pdata)
    pview["reviews"] = reviews
    return pview

# -------------------------------
# Orders (atomic stock handling in Firestore)
# -------------------------------
@firestore.transactional
def _place_order_txn(
    transaction: firestore.Transaction,
    current_user_id: int,
    cart_items: List[Dict[str, Any]],
    shipping_address: str,
    phone: str,
    payment_method: str
) -> Tuple[int, str, Dict[str, Any], List[Dict[str, Any]]]:
    product_refs = [db.collection("products").document(str(it["id"])) for it in cart_items]

    # Read product snapshots inside the transaction (READS)
    product_snaps = [ref.get(transaction=transaction) for ref in product_refs]

    # Read counters before any writes (READS)
    counters_ref = db.collection("counters").document("orders")
    counters_snap = counters_ref.get(transaction=transaction)
    curr_val = int(counters_snap.to_dict().get("value", 0)) if counters_snap.exists else 0
    order_id = curr_val + 1

    # Build normalized list using authoritative values (still just using data from reads)
    normalized_items: List[Dict[str, Any]] = []
    for it, snap in zip(cart_items, product_snaps):
        if not snap.exists:
            raise ValueError(f"Product {it['id']} unavailable")
        pdata = snap.to_dict() or {}
        if not pdata.get("is_active", True):
            raise ValueError(f"Product {it['id']} unavailable")
        stock = int(pdata.get("stock", 0))
        qty = int(it["quantity"])
        if stock < qty:
            raise ValueError(f"Only {stock} left for product {it['id']}")
        price = float(pdata.get("price", 0.0))
        normalized_items.append({
            "id": int(it["id"]),
            "name": pdata.get("name", f"Product {it['id']}"),
            "image": pdata.get("image"),
            "quantity": qty,
            "price": price,
            "size": it.get("size", "Free Size"),
            "color": it.get("color", ""),
            "ref": db.collection("products").document(str(it["id"])),
            "current_stock": stock,
        })

    # Compute derived values (no Firestore reads)
    total_amount = sum(float(n["price"]) * int(n["quantity"]) for n in normalized_items)
    tracking_number = f"SRC{int(time.time())}{int(current_user_id):04d}"
    now_ts = _now_ts()
    order_payload = {
        "id": order_id,
        "user_id": int(current_user_id),
        "total_amount": float(total_amount),
        "status": "pending",
        "shipping_address": shipping_address,
        "phone": phone,
        "customer_phone": phone,
        "payment_method": payment_method,
        "tracking_number": tracking_number,
        "created_at": now_ts,
        "updated_at": now_ts,
    }

    # Perform all writes after reads (WRITES)
    # 1) Persist the incremented counter
    transaction.set(counters_ref, {"value": order_id})

    # 2) Create order
    order_ref = db.collection("orders").document(str(order_id))
    transaction.set(order_ref, order_payload)

    # 3) Create order items
    for i, n in enumerate(normalized_items):
        item_id = int(f"{order_id}{i+1:02d}")
        item_ref = db.collection("order_items").document(str(item_id))
        transaction.set(item_ref, {
            "id": item_id,
            "order_id": order_id,
            "product_id": int(n["id"]),
            "quantity": int(n["quantity"]),
            "price": float(n["price"]),
            "size": n.get("size", "Free Size"),
            "color": n.get("color", ""),
            "created_at": _now_ts(),
        })

    # 4) Update product stocks
    for n in normalized_items:
        new_stock = int(n["current_stock"]) - int(n["quantity"])
        transaction.update(n["ref"], {"stock": new_stock})

    # Build a response-friendly items array (no references)
    items_for_response: List[Dict[str, Any]] = []
    for n in normalized_items:
        items_for_response.append({
            "product_id": int(n["id"]),
            "name": n.get("name"),
            "image": n.get("image"),
            "price": float(n["price"]),
            "quantity": int(n["quantity"]),
            "size": n.get("size", "Free Size"),
            "color": n.get("color", ""),
            "subtotal": float(n["price"]) * int(n["quantity"]),
        })

    return order_id, tracking_number, order_payload, items_for_response

@app.post("/api/orders")
async def place_order(order_data: OrderCreate, current_user: dict = Depends(get_current_user)):
    if not order_data.items:
        raise HTTPException(status_code=400, detail="No items in cart")

    cart_items: List[Dict[str, Any]] = []
    try:
        for raw in order_data.items:
            pid_raw = raw.get("id") or raw.get("product_id")
            qty_raw = raw.get("quantity")
            if pid_raw is None or qty_raw is None:
                raise ValueError("Missing id or quantity")
            pid = int(pid_raw)
            qty = int(qty_raw)
            if qty <= 0:
                raise ValueError("Quantity must be positive")
            cart_items.append({
                "id": pid,
                "quantity": qty,
                "size": raw.get("size", "Free Size"),
                "color": raw.get("color", ""),
            })
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid cart item payload")

    try:
        order_id, tracking, order_doc, items_detail = _place_order_txn(
            db.transaction(),
            int(current_user["id"]),
            cart_items,
            order_data.shipping_address,
            order_data.phone,
            order_data.payment_method,
        )
    except ValueError as e:
        # stock or availability error
        raise HTTPException(status_code=409, detail=str(e))
    except (Aborted, FailedPrecondition) as e:
        print("[v0] Firestore transaction conflict:", repr(e))
        raise HTTPException(status_code=409, detail="Order could not be placed due to concurrent stock updates. Please retry.")
    except (InvalidArgument, NotFound) as e:
        print("[v0] Firestore invalid argument/not found:", repr(e))
        raise HTTPException(status_code=400, detail="Invalid request data")
    except Exception as e:
        print("[v0] Unexpected order placement error:", repr(e))
        raise HTTPException(status_code=500, detail="Failed to place order due to stock update error")

    clear_cache_pattern("orders")
    clear_cache_pattern("products")

    # Build client/device-friendly response including full order + items
    created_at_iso = _ts_to_iso(order_doc.get("created_at"))
    updated_at_iso = _ts_to_iso(order_doc.get("updated_at"))

    return {
        "success": True,
        "order_id": order_id,
        "tracking_number": tracking,
        "order": {
            "id": order_id,
            "user_id": int(current_user["id"]),
            "status": order_doc.get("status", "pending"),
            "total_amount": float(order_doc.get("total_amount", 0.0)),
            "shipping_address": order_doc.get("shipping_address", ""),
            "phone": order_doc.get("phone") or order_doc.get("customer_phone", ""),
            "payment_method": order_doc.get("payment_method", "cod"),
            "tracking_number": tracking,
            "created_at": order_doc.get("created_at"),
            "updated_at": order_doc.get("updated_at"),
            "created_at_iso": created_at_iso,
            "updated_at_iso": updated_at_iso,
            "items": items_detail,
            "item_count": len(items_detail),
        },
    }

# -------------------------------
# Reviews
# -------------------------------
@app.post("/api/reviews")
async def add_review(review_data: ReviewCreate, current_user: dict = Depends(get_current_user)):
    uid = int(current_user["id"])
    # Ensure one review per user per product (simple check)
    existing = await get_collection_cached("reviews")
    existing = [r for r in existing if int(r.get("user_id", 0)) == uid and int(r.get("product_id", 0)) == int(review_data.product_id)]
    if existing:
        raise HTTPException(status_code=400, detail="You have already reviewed this product")

    await create_with_numeric_id(
        "reviews",
        {
            "product_id": int(review_data.product_id),
            "user_id": uid,
            "rating": int(review_data.rating),
            "comment": review_data.comment or "",
        },
    )

    # Recalculate rating
    reviews = await get_collection_cached("reviews")
    reviews = [r for r in reviews if int(r.get("product_id", 0)) == int(review_data.product_id)]
    total = len(reviews)
    avg = 0.0
    if total:
        s = sum(int(r.get("rating", 0)) for r in reviews)
        avg = float(s) / float(total)

    db.collection("products").document(str(review_data.product_id)).update({
        "rating": avg,
        "reviews_count": total
    })

    clear_cache_pattern(f"products_{review_data.product_id}")
    clear_cache_pattern("reviews_product")
    return {"success": True}

# -------------------------------
# Auth routes
# -------------------------------
@app.post("/api/auth/register")
async def register(user_data: UserRegister):
    users = await get_collection_cached("users")
    if any(u.get("email") == user_data.email for u in users):
        raise HTTPException(status_code=400, detail="Email already exists")

    username = user_data.email.split("@")[0].strip()
    hashed = sha256(user_data.password)

    _, user_payload = await create_with_numeric_id(
        "users",
        {
            "name": user_data.name,
            "username": username,
            "email": user_data.email,
            "password": hashed,
            "phone": user_data.phone,
            "address": "",
            "role": "customer",
        },
    )

    access_token = create_access_token(data={"user_id": user_payload["id"]})
    return {
        "success": True,
        "message": "Registration successful",
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user_payload["id"],
            "name": user_payload["name"],
            "email": user_payload["email"],
            "role": user_payload["role"],
        },
    }

@app.post("/api/auth/login")
async def login(user_data: UserLogin):
    users = await get_collection_cached("users")
    hashed = sha256(user_data.password)
    match = [u for u in users if u.get("email") == user_data.email and u.get("password") == hashed]
    if not match:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    u = match[0]
    access_token = create_access_token(data={"user_id": u.get("id")})
    return {
        "success": True,
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": u.get("id"),
            "name": u.get("name"),
            "email": u.get("email"),
            "role": u.get("role"),
        },
    }

@app.get("/api/auth/me")
async def get_me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user.get("id"),
        "name": current_user.get("name"),
        "email": current_user.get("email"),
        "phone": current_user.get("phone"),
        "role": current_user.get("role"),
    }

# -------------------------------
# Admin routes
# -------------------------------
@app.get("/api/admin/orders")
async def get_admin_orders(current_user: dict = Depends(get_admin_user)):
    orders = await get_collection_cached("orders")
    users = await get_collection_cached("users")
    user_name = {u["id"]: u.get("name") for u in users}
    user_email = {u["id"]: u.get("email") for u in users}
    user_phone = {u["id"]: u.get("phone") for u in users}

    products = await get_collection_cached("products")
    product_map = {int(p["id"]): p for p in products if p.get("id")}

    all_items = await get_collection_cached("order_items")

    result = []
    for o in orders:
        items = [i for i in all_items if int(i.get("order_id", 0)) == int(o["id"])]
        # normalize/enrich items
        norm_items = []
        for i in items:
            pid = int(i.get("product_id") or i.get("id") or 0)
            p = product_map.get(pid) or {}
            qty = int(i.get("quantity") or 0)
            price = float(i.get("price") or p.get("price") or 0.0)
            norm_items.append({
                "product_id": pid,
                "name": i.get("name") or p.get("name"),
                "image": i.get("image") or (p.get("images", [p.get("image")])[0] if p else None),
                "quantity": qty,
                "price": price,
                "size": i.get("size") or "Free Size",
                "color": i.get("color") or (p.get("color") or ""),
                "subtotal": price * qty,
            })

        result.append({
            "id": o["id"],
            "user_id": o.get("user_id"),
            "customer_name": user_name.get(int(o.get("user_id", 0))),
            "customer_email": user_email.get(int(o.get("user_id", 0))),
            "customer_phone": o.get("phone") or o.get("customer_phone") or user_phone.get(int(o.get("user_id", 0))),
            "total_amount": o.get("total_amount", sum((it["subtotal"] for it in norm_items), 0.0)),
            "status": o.get("status", "pending"),
            "shipping_address": o.get("shipping_address", ""),
            "payment_method": o.get("payment_method", "cod"),
            "tracking_number": o.get("tracking_number", ""),
            "created_at": _ts_to_iso(o.get("created_at")),
            "updated_at": _ts_to_iso(o.get("updated_at")),
            "items": norm_items,
            "item_count": len(norm_items),
        })
    result.sort(key=lambda x: int(x.get("id", 0)), reverse=True)
    return result

@app.put("/api/admin/orders/{order_id}/status")
async def update_order_status(order_id: int, status_data: OrderStatusUpdate, current_user: dict = Depends(get_admin_user)):
    found = await get_doc_by_numeric_id("orders", order_id)
    if not found:
        raise HTTPException(status_code=404, detail="Order not found")

    db.collection("orders").document(str(order_id)).update({
        "status": status_data.status,
        "updated_at": _now_ts(),
    })
    clear_cache_pattern("orders")
    return {"success": True}

@app.get("/api/admin/stats")
async def get_admin_stats(current_user: dict = Depends(get_admin_user)):
    cache_key = "admin_stats"
    cached = get_cache(cache_key, "default")
    if cached:
        return cached

    orders = await get_collection_cached("orders")
    users = await get_collection_cached("users")
    products = await get_collection_cached("products")

    customers = [u for u in users if (u.get("role") == "customer")]
    total_orders = len(orders)
    total_revenue = sum(float(o.get("total_amount", 0)) for o in orders if (o.get("status") or "").lower() != "cancelled")
    total_customers = len(customers)
    total_products = len(products)

    recent_orders = sorted(orders, key=lambda x: _ts_to_epoch(x.get("created_at", 0)), reverse=True)[:5]
    user_cache = {u.get("id"): u.get("name") for u in users}
    recents = [{
        "id": o.get("id"),
        "customer_name": user_cache.get(int(o.get("user_id", 0))),
        "total_amount": o.get("total_amount"),
        "status": o.get("status"),
        "created_at": o.get("created_at"),
    } for o in recent_orders]

    status_count: Dict[str, int] = {}
    for o in orders:
        st = (o.get("status") or "pending").lower()
        status_count[st] = status_count.get(st, 0) + 1
    status_distribution = [{"status": k, "count": v} for k, v in status_count.items()]

    stats = {
        "total_orders": total_orders,
        "total_revenue": total_revenue,
        "total_customers": total_customers,
        "total_products": total_products,
        "recent_orders": recents,
        "status_distribution": status_distribution,
    }
    set_cache(cache_key, stats, "default")
    return stats

@app.post("/api/admin/products")
async def add_product(product_data: ProductCreate, current_user: dict = Depends(get_admin_user)):
    main_image = (product_data.images or ["/placeholder.svg?height=500&width=400"])[0]
    _id, payload = await create_with_numeric_id(
        "products",
        {
            "name": product_data.name,
            "description": product_data.description,
            "price": float(product_data.price),
            "image": main_image,
            "images": product_data.images,
            "category": product_data.category,
            "fabric": product_data.fabric,
            "color": product_data.color,
            "stock": int(product_data.stock),
            "sizes": product_data.sizes,
            "care_instructions": product_data.care_instructions,
            "is_active": product_data.is_active,
            "rating": 0.0,
            "reviews_count": 0,
        },
    )
    return product_to_view(payload)

@app.put("/api/admin/products/{product_id}")
async def update_product(product_id: int, product_data: ProductUpdate, current_user: dict = Depends(get_admin_user)):
    found = await get_doc_by_numeric_id("products", product_id)
    if not found:
        raise HTTPException(status_code=404, detail="Product not found")

    update_data: Dict[str, Any] = {}
    for field, value in product_data.dict(exclude_unset=True).items():
        if value is not None:
            update_data[field] = value

    if "images" in update_data and update_data["images"]:
        update_data["image"] = update_data["images"][0]

    db.collection("products").document(str(product_id)).update(update_data)
    clear_cache_pattern(f"products_{product_id}")
    clear_cache_pattern("products_all")

    updated_result = await get_doc_by_numeric_id("products", product_id)
    if not updated_result:
        raise HTTPException(status_code=404, detail="Product not found")
    _, pdata = updated_result
    return product_to_view(pdata)

# -------------------------------
# User-facing routes
# -------------------------------
@app.get("/api/orders")
async def get_user_orders(current_user: dict = Depends(get_current_user)):
    uid = int(current_user["id"])

    orders = await get_collection_cached("orders")
    my_orders = [o for o in orders if int(o.get("user_id", 0)) == uid]

    products = await get_collection_cached("products")
    product_map = {int(p.get("id")): p for p in products if p.get("id")}

    all_items = await get_collection_cached("order_items")

    result: List[Dict[str, Any]] = []
    for o in my_orders:
        # items for this order
        raw_items = [i for i in all_items if int(i.get("order_id", 0)) == int(o["id"])]
        items_enriched: List[Dict[str, Any]] = []
        total = 0.0
        for i in raw_items:
            pid = int(i.get("product_id") or i.get("id") or 0)
            p = product_map.get(pid) or {}
            qty = int(i.get("quantity") or 0)
            price = float(i.get("price") or p.get("price") or 0.0)
            subtotal = price * qty
            total += subtotal
            items_enriched.append({
                "product_id": pid,
                "name": i.get("name") or p.get("name") or f"Product {pid}",
                "image": i.get("image") or (p.get("images", [p.get("image")])[0] if p else None),
                "quantity": qty,
                "price": price,
                "size": i.get("size") or "Free Size",
                "color": i.get("color") or (p.get("color") or ""),
                "subtotal": subtotal,
            })

        result.append({
            "id": o["id"],
            "user_id": o.get("user_id"),
            "total_amount": o.get("total_amount", total),
            "status": o.get("status", "pending"),
            "shipping_address": o.get("shipping_address", ""),
            "phone": o.get("phone") or o.get("customer_phone", ""),
            "payment_method": o.get("payment_method", "cod"),
            "tracking_number": o.get("tracking_number", ""),
            "created_at": _ts_to_iso(o.get("created_at")),
            "updated_at": _ts_to_iso(o.get("updated_at")),
            "items": items_enriched,
            "item_count": len(items_enriched),
        })

    result.sort(key=lambda x: int(x.get("id", 0)), reverse=True)
    result = result[:15]
    return result

# -------------------------------
# Entrypoint (local dev)
# -------------------------------
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
