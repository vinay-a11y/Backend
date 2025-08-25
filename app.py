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
from contextlib import asynccontextmanager
# Firebase Admin (Firestore)
import firebase_admin
from firebase_admin import credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter


# Pydantic Models
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

# Firebase Setup
def _load_firebase_credentials() -> credentials.Certificate:
    """Load Firebase service account credentials"""
    json_str = os.getenv("FIREBASE_CREDENTIALS_JSON")
    if json_str:
        data = json.loads(json_str)
        return credentials.Certificate(data)

    path = os.getenv("FIREBASE_CREDENTIALS", "credentials.json")
    if not os.path.exists(path):
        raise RuntimeError(
            "Firebase credentials not found. Set FIREBASE_CREDENTIALS_JSON or place credentials.json in app directory."
        )
    return credentials.Certificate(path)

# Initialize Firebase app once
if not firebase_admin._apps:
    cred = _load_firebase_credentials()
    firebase_admin.initialize_app(cred)

db = firestore.client()

# JWT Configuration
SECRET_KEY = os.getenv("JWT_SECRET_KEY", "53d1febbc7b724028cf112906ebe62bcfe05cbf16c3296871afe0359f773755a")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30 * 24 * 60  # 30 days

security = HTTPBearer()

# Utility Functions
def now_ts() -> datetime:
    return datetime.now()

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

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
    except jwt.PyJWTError:
        raise credentials_exception
    
    user = await get_doc_by_numeric_id("users", user_id)
    if user is None:
        raise credentials_exception
    return user[1]  # Return user data

async def get_admin_user(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

# Database Operations
@firestore.transactional
def _txn_inc(transaction, col_name):
    counter_ref = db.collection("counters").document(col_name)
    snapshot = counter_ref.get(transaction=transaction)

    if snapshot.exists:
        current = snapshot.get("value")
        if current is None:
            current = 0
    else:
        current = 0

    transaction.set(counter_ref, {"value": current + 1})
    return current + 1

async def next_sequence(col_name):
    transaction = db.transaction()
    return _txn_inc(transaction, col_name)

async def create_with_numeric_id(col_name: str, payload: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Create a document with numeric incremental ID"""
    nid = await next_sequence(col_name)
    doc_id = str(nid)
    payload = {**payload, "id": nid}
    db.collection(col_name).document(doc_id).set(payload)
    return doc_id, payload

async def get_doc_by_numeric_id(col_name: str, numeric_id: int) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Retrieve document by numeric ID"""
    doc_ref = db.collection(col_name).document(str(numeric_id))
    snap = doc_ref.get()
    if snap.exists:
        return snap.id, snap.to_dict()

    # Fallback query
    q = db.collection(col_name).where("id", "==", numeric_id).limit(1).stream()
    for doc in q:
        return doc.id, doc.to_dict()
    return None

# Bootstrap Functions
async def seed_admin_user() -> None:
    users_ref = db.collection("users")
    admin_q = users_ref.where("role", "==", "admin").limit(1).stream()
    has_admin = any(True for _ in admin_q)
    if not has_admin:
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
                "created_at": now_ts(),
            },
        )

async def seed_categories() -> None:
    cats = list(db.collection("categories").limit(1000).stream())
    if cats:
        return
    
    sample_categories = [
        ("Traditional", "Timeless classics featuring traditional weaves and authentic craftsmanship", "/traditional-saree-category.png", 45),
        ("Designer", "Contemporary designs with modern aesthetics and innovative patterns", "/designer-saree-category.png", 32),
        ("Bridal", "Exquisite bridal sarees for your special day with intricate embellishments", "/bridal-saree-category.png", 28),
        ("Party Wear", "Glamorous sarees perfect for celebrations and special occasions", "/party-wear-saree-category.png", 38),
        ("Casual", "Comfortable and elegant sarees for everyday wear and office", "/casual-saree-category.png", 52),
        ("Regional", "Authentic regional sarees from different states of India", "/regional-saree-category.png", 41),
    ]
    
    for (name, description, image, product_count) in sample_categories:
        await create_with_numeric_id(
            "categories",
            {
                "name": name,
                "description": description,
                "image": image,
                "product_count": product_count,
            },
        )

async def seed_products() -> None:
    snaps = list(db.collection("products").limit(1000).stream())
    if snaps:
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

    for (name, description, price, images, category, fabric, color, stock, rating, reviews_count, sizes, care_instructions, is_active) in sample_products:
        await create_with_numeric_id(
            "products",
            {
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
                "created_at": now_ts(),
            },
        )

async def bootstrap():
    await seed_admin_user()
    await seed_categories()
    await seed_products()

# Lifespan manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await bootstrap()
    yield
    # Shutdown
    pass

# FastAPI App
app = FastAPI(
    title="Ship to Rocks Collection API",
    description="FastAPI backend for saree e-commerce website",
    version="2.0.0",
    lifespan=lifespan
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Utility Functions
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

# API Routes

# Products
@app.get("/api/products")
async def get_products(category: Optional[str] = None, search: Optional[str] = None, sort: str = "name"):
    products_snap = db.collection("products").where("is_active", "==", True).stream()
    products = [product_to_view(doc.to_dict()) for doc in products_snap]

    # Filter by category
    if category and category != "all":
        products = [p for p in products if p.get("category") == category]

    # Search
    if search:
        s = search.lower()
        products = [
            p for p in products
            if (p.get("name") and s in p["name"].lower())
            or (p.get("description") and s in p["description"].lower())
        ]

    # Sort
    if sort == "price-low":
        products.sort(key=lambda x: float(x.get("price", 0)))
    elif sort == "price-high":
        products.sort(key=lambda x: float(x.get("price", 0)), reverse=True)
    elif sort == "rating":
        products.sort(key=lambda x: float(x.get("rating", 0)), reverse=True)
    else:
        products.sort(key=lambda x: (x.get("name") or ""))

    return products

@app.get("/api/products/{product_id}")
async def get_product(product_id: int):
    found = await get_doc_by_numeric_id("products", product_id)
    if not found:
        raise HTTPException(status_code=404, detail="Product not found")
    
    _doc_id, pdata = found

    # Fetch reviews with user names
    reviews_snap = (
        db.collection("reviews")
        .where("product_id", "==", product_id)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .stream()
    )
    
    reviews = []
    for rdoc in reviews_snap:
        r = rdoc.to_dict()
        # Join user
        uname = None
        u = await get_doc_by_numeric_id("users", int(r.get("user_id", 0))) if r.get("user_id") else None
        if u:
            _, udata = u
            uname = udata.get("name")
        r["user_name"] = uname
        reviews.append(r)

    pview = product_to_view(pdata)
    pview["reviews"] = reviews
    return pview

# Categories
@app.get("/api/categories")
async def get_categories():
    snaps = db.collection("categories").order_by("name").stream()
    categories = [doc.to_dict() for doc in snaps]
    return categories

# Authentication
@app.post("/api/auth/register")
async def register(user_data: UserRegister):
    # Check existing email
    exists = list(db.collection("users").where("email", "==", user_data.email).limit(1).stream())
    if exists:
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
            "created_at": now_ts(),
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
    hashed = sha256(user_data.password)
    snaps = (
        db.collection("users")
        .where("email", "==", user_data.email)
        .where("password", "==", hashed)
        .limit(1)
        .stream()
    )
    
    user_doc = None
    for d in snaps:
        user_doc = d
        break

    if not user_doc:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    u = user_doc.to_dict()
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
# Synchronous helper to get document by numeric ID
def get_doc_by_numeric_id_sync(collection: str, numeric_id: int):
    docs = db.collection(collection).where("id", "==", numeric_id).stream()
    for doc in docs:
        return doc.id, doc.to_dict()
    return None


# Transactional function to decrement stock
@firestore.transactional
def _dec_stock(transaction, pid: int, quantity: int):
    p_result = get_doc_by_numeric_id_sync("products", pid)
    if not p_result:
        return
    pdoc_id, pdata = p_result
    new_stock = max(0, int(pdata.get("stock", 0)) - quantity)
    product_ref = db.collection("products").document(pdoc_id)
    transaction.update(product_ref, {"stock": new_stock})


# Place order endpoint
@app.post("/api/orders")
async def place_order(order_data: OrderCreate, current_user: dict = Depends(get_current_user)):
    print(f"[DEBUG] Order placement attempt by user: {current_user.get('email')}")

    if not order_data.items:
        raise HTTPException(status_code=400, detail="No items in cart")

    total_amount = sum(float(i["price"]) * int(i["quantity"]) for i in order_data.items)
    tracking_number = f"SRC{datetime.now().strftime('%Y%m%d')}{int(current_user['id']):04d}"

    order_payload = {
        "user_id": int(current_user["id"]),
        "total_amount": float(total_amount),
        "status": "pending",
        "shipping_address": order_data.shipping_address,
        "phone": order_data.phone,
        "customer_phone": order_data.phone,
        "payment_method": order_data.payment_method,
        "tracking_number": tracking_number,
        "created_at": now_ts(),
        "updated_at": now_ts(),
    }

    # Create order
    order_doc_id, order_doc = await create_with_numeric_id("orders", order_payload)
    print(f"[DEBUG] Order created successfully: {order_doc['id']}")

    # Create order items and update stock inside transactions
    for item in order_data.items:
        await create_with_numeric_id(
            "order_items",
            {
                "order_id": int(order_doc["id"]),
                "product_id": int(item["id"]),
                "quantity": int(item["quantity"]),
                "price": float(item["price"]),
                "size": item.get("size", "Free Size"),
                "color": item.get("color", ""),
            },
        )

        # Firestore transaction for stock update
        transaction = db.transaction()
        _dec_stock(transaction, int(item["id"]), int(item["quantity"]))

    return {
        "success": True,
        "order_id": order_doc["id"],
        "tracking_number": tracking_number
    }


@app.get("/api/orders")
async def get_orders(current_user: dict = Depends(get_current_user)):
    uid = int(current_user["id"])

    orders_snap = (
        db.collection("orders")
        .where("user_id", "==", uid)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .stream()
    )

    orders_list = []
    for doc in orders_snap:
        o = doc.to_dict()
        # Get order items
        items_snap = db.collection("order_items").where("order_id", "==", int(o.get("id"))).stream()
        names = []
        count = 0
        for it in items_snap:
            oi = it.to_dict()
            p = await get_doc_by_numeric_id("products", int(oi.get("product_id")))
            pname = None
            if p:
                _, pdata = p
                pname = pdata.get("name")
            q = int(oi.get("quantity", 0))
            count += 1
            names.append(f"{pname or 'Product'} (x{q})")

        orders_list.append({
            "id": o.get("id"),
            "total_amount": o.get("total_amount"),
            "status": o.get("status"),
            "created_at": o.get("created_at"),
            "tracking_number": o.get("tracking_number"),
            "payment_method": o.get("payment_method"),
            "items": ", ".join(names) if names else "No items",
            "item_count": count,
        })

    return orders_list

# Reviews
@app.post("/api/reviews")
async def add_review(review_data: ReviewCreate, current_user: dict = Depends(get_current_user)):
    uid = int(current_user["id"])

    # Check existing review
    existing = list(
        db.collection("reviews")
        .where("product_id", "==", review_data.product_id)
        .where("user_id", "==", uid)
        .limit(1)
        .stream()
    )
    if existing:
        raise HTTPException(status_code=400, detail="You have already reviewed this product")

    # Create review
    await create_with_numeric_id(
        "reviews",
        {
            "product_id": review_data.product_id,
            "user_id": uid,
            "rating": review_data.rating,
            "comment": review_data.comment,
            "created_at": now_ts(),
        },
    )

    # Update product rating
    revs = list(db.collection("reviews").where("product_id", "==", review_data.product_id).stream())
    total = len(revs)
    avg = 0.0
    if total:
        s = sum(int(r.to_dict().get("rating", 0)) for r in revs)
        avg = float(s) / float(total)

    db.collection("products").document(str(review_data.product_id)).update(
        {"rating": avg, "reviews_count": total}
    )

    return {"success": True}

# Admin Routes
@app.get("/api/admin/orders")
async def get_admin_orders(current_user: dict = Depends(get_admin_user)):
    orders_snap = db.collection("orders").order_by("created_at", direction=firestore.Query.DESCENDING).stream()
    orders_list = []

    for doc in orders_snap:
        o = doc.to_dict()
        # Join user
        cust_name = cust_email = cust_phone = None
        u = await get_doc_by_numeric_id("users", int(o.get("user_id", 0)))
        if u:
            _, udata = u
            cust_name = udata.get("name")
            cust_email = udata.get("email")
            cust_phone = udata.get("phone")

        # Items summary
        items_snap = db.collection("order_items").where("order_id", "==", int(o.get("id"))).stream()
        names = []
        count = 0
        for it in items_snap:
            oi = it.to_dict()
            p = await get_doc_by_numeric_id("products", int(oi.get("product_id")))
            pname = None
            if p:
                _, pdata = p
                pname = pdata.get("name")
            q = int(oi.get("quantity", 0))
            count += 1
            names.append(f"{pname or 'Product'} (x{q})")

        orders_list.append({
            "id": o.get("id"),
            "customer_name": cust_name,
            "customer_email": cust_email,
            "customer_phone": cust_phone or o.get("phone"),
            "total_amount": o.get("total_amount"),
            "status": o.get("status"),
            "created_at": o.get("created_at"),
            "shipping_address": o.get("shipping_address"),
            "phone": o.get("phone"),
            "payment_method": o.get("payment_method"),
            "tracking_number": o.get("tracking_number"),
            "items": ", ".join(names) if names else "No items",
            "item_count": count,
        })

    return orders_list

@app.put("/api/admin/orders/{order_id}/status")
async def update_order_status(order_id: int, status_data: OrderStatusUpdate, current_user: dict = Depends(get_admin_user)):
    found = await get_doc_by_numeric_id("orders", order_id)
    if not found:
        raise HTTPException(status_code=404, detail="Order not found")

    db.collection("orders").document(str(order_id)).update({
        "status": status_data.status,
        "updated_at": now_ts(),
    })

    return {"success": True}

@app.get("/api/admin/stats")
async def get_admin_stats(current_user: dict = Depends(get_admin_user)):
    # Total orders
    total_orders = len(list(db.collection("orders").stream()))

    # Total revenue
    revenue = 0.0
    for o in db.collection("orders").stream():
        od = o.to_dict()
        if od.get("status") != "cancelled":
            revenue += float(od.get("total_amount", 0))

    # Total customers
    total_customers = len(list(db.collection("users").where("role", "==", "customer").stream()))

    # Total products
    total_products = len(list(db.collection("products").stream()))

    # Recent orders
    recents = []
    snaps = (
        db.collection("orders")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(5)
        .stream()
    )
    for s in snaps:
        od = s.to_dict()
        customer_name = None
        u = await get_doc_by_numeric_id("users", int(od.get("user_id", 0)))
        if u:
            _, udata = u
            customer_name = udata.get("name")
        recents.append({
            "id": od.get("id"),
            "customer_name": customer_name,
            "total_amount": od.get("total_amount"),
            "status": od.get("status"),
            "created_at": od.get("created_at"),
        })

    # Status distribution
    status_count = {}
    for o in db.collection("orders").stream():
        st = (o.to_dict().get("status") or "pending").lower()
        status_count[st] = status_count.get(st, 0) + 1

    status_distribution = [{"status": k, "count": v} for k, v in status_count.items()]

    return {
        "total_orders": total_orders,
        "total_revenue": revenue,
        "total_customers": total_customers,
        "total_products": total_products,
        "recent_orders": recents,
        "status_distribution": status_distribution,
    }

@app.post("/api/admin/products")
async def add_product(product_data: ProductCreate, current_user: dict = Depends(get_admin_user)):
    main_image = product_data.images[0] if product_data.images else "/placeholder.svg?height=500&width=400"

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
            "created_at": now_ts(),
        },
    )

    return product_to_view(payload)

@app.put("/api/admin/products/{product_id}")
async def update_product(product_id: int, product_data: ProductUpdate, current_user: dict = Depends(get_admin_user)):
    found = await get_doc_by_numeric_id("products", product_id)
    if not found:
        raise HTTPException(status_code=404, detail="Product not found")

    # Build update data
    update_data = {}
    for field, value in product_data.dict(exclude_unset=True).items():
        if value is not None:
            update_data[field] = value

    if "images" in update_data and update_data["images"]:
        update_data["image"] = update_data["images"][0]

    db.collection("products").document(str(product_id)).update(update_data)

    # Return updated product
    doc = db.collection("products").document(str(product_id)).get()
    pdata = doc.to_dict() if doc.exists else None
    if not pdata:
        raise HTTPException(status_code=404, detail="Product not found")

    return product_to_view(pdata)

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=True)
