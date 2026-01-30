import pandas as pd
from sqlalchemy import text
from datetime import datetime
from database import get_engine

# ---------- STOCK MANAGEMENT ----------
# ---------- STOCK MANAGEMENT ----------
def upload_parts_stock(df_parts: pd.DataFrame, stock_type: str):
    df = df_parts.copy()
    df.columns = df.columns.str.strip().str.lower()
    
    column_mapping = {
        'part_number': 'part_number',
        'description': 'description',
        'stock': 'free_stock',
        'price($)': 'price'
    }

    # Rename mapped columns
    actual_map = {}
    for col in df.columns:
        if col in column_mapping:
            actual_map[col] = column_mapping[col]
            
    df = df.rename(columns=actual_map)
    
    # Sanitization: Ensure Price is numeric
    if 'price' in df.columns:
        # Convert to string, clean, then to numeric
        df['price'] = df['price'].astype(str).str.replace('$', '').str.replace(',', '').str.strip()
        df['price'] = pd.to_numeric(df['price'], errors='coerce').fillna(0)

    
    # Sanitization
    if 'part_number' in df.columns:
        df['part_number'] = df['part_number'].astype(str).str.strip()
        
    # Add metadata
    df['stock_type'] = stock_type
    df['is_active'] = True
    
    engine = get_engine()
    with engine.begin() as conn:
        # Soft Delete: Mark existing active items of this stock_type as inactive
        conn.execute(
            text("UPDATE parts_stock SET is_active = FALSE WHERE stock_type = :st AND is_active = TRUE"),
            {"st": stock_type}
        )
        
        # Insert New
        # Filter to allowed columns. Removed legacy delivery prices.
        allowed_cols = ['part_number', 'description', 'free_stock', 'price', 'stock_type', 'is_active']
        df_final = df[[c for c in allowed_cols if c in df.columns]]
        
        df_final.to_sql("parts_stock", con=conn, if_exists="append", index=False)

def reset_stock(stock_type: str):
    engine = get_engine()
    with engine.begin() as conn:
        # Hard Delete
        conn.execute(
            text("DELETE FROM parts_stock WHERE stock_type = :st"),
            {"st": stock_type}
        )

def get_parts_like(prefix, stock_type, adjustment_percent=0):
    # Search Logic: Strip hyphens from input
    cleaned_prefix = str(prefix).replace("-", "").strip()
    
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
            SELECT part_number, description, free_stock,
                   price, stock_type
            FROM parts_stock
            WHERE (part_number ILIKE :prefix OR description ILIKE :prefix)
              AND stock_type = :st
              AND is_active = TRUE
            ORDER BY 
                CASE 
                    WHEN part_number ILIKE :exact_start THEN 1 
                    ELSE 2 
                END, 
                part_number
            LIMIT 50
            """),
            {
                "prefix": f"%{cleaned_prefix}%",
                "exact_start": f"{cleaned_prefix}%",
                "st": stock_type
            },
        ).fetchall()
        
    results = []
    for row in rows:
        d = dict(row._mapping)
        # Apply Adjustment: base * (1 + pct/100)
        base = float(d['price'] or 0)
        d['price'] = round(base * (1 + adjustment_percent / 100.0), 2)
        results.append(d)
        
    return [type('obj', (object,), r) for r in results] # Return as objects to match existing usage (r.price) or just dicts if main uses dicts. 
    # WAIT: main.py uses dot notation for results (r.part_number).
    # The original returned rows which are Row objects (support dot notation).
    # Since I converted to dict to modify price, I need to wrap them back or return simple objects.
    # Simple object class:
    class PartObj:
        def __init__(self, **entries):
            self.__dict__.update(entries)
            
    return [PartObj(**r) for r in results]

def get_part_by_number(part_number, stock_type):
    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            text("""
            SELECT part_number, description, free_stock, price
            FROM parts_stock
            WHERE part_number = :part_number 
              AND stock_type = :st
              AND is_active = TRUE
            """),
            {"part_number": part_number, "st": stock_type},
        ).fetchone()
    return row

# ---------- CART MANAGEMENT ----------
def add_to_cart_db(user_id, part_number, description, qty, price):
    # Sanitize
    part_number = str(part_number).strip()
    
    engine = get_engine()
    with engine.begin() as conn:
        # Check if exists
        curr = conn.execute(
            text("SELECT id, qty FROM cart WHERE user_id = :uid AND part_number = :pn"),
            {"uid": user_id, "pn": part_number}
        ).fetchone()
        
        if curr:
            # UPSERT: Update existing
            new_qty = curr.qty + qty
            conn.execute(
                text("UPDATE cart SET qty = :qty WHERE id = :id"),
                {"qty": new_qty, "id": curr.id}
            )
        else:
            # Insert New
            conn.execute(
                text("""
                INSERT INTO cart (user_id, part_number, description, qty, price)
                VALUES (:user_id, :part_number, :description, :qty, :price)
                """),
                {
                    "user_id": user_id,
                    "part_number": part_number,
                    "description": description,
                    "qty": qty,
                    "price": price
                }
            )

def get_user_cart(user_id):
    engine = get_engine()
    with engine.begin() as conn:
        # Join with stock to get real-time availability
        rows = conn.execute(
            text("""
            SELECT c.id, c.part_number, c.description, c.qty, c.price,
                   p.free_stock as available_qty
            FROM cart c
            LEFT JOIN parts_stock p ON c.part_number = p.part_number AND p.stock_type = 'parts_stock' AND p.is_active = TRUE
            WHERE c.user_id = :user_id
            ORDER BY c.timestamp DESC
            """),
            {"user_id": user_id}
        ).fetchall()
        
    results = []
    for row in rows:
        d = dict(row._mapping)
        req = d['qty']
        avail = d['available_qty'] or 0
        
        # Logic: Allocated = min(Req, Stock)
        if avail >= req:
            d['allocated_qty'] = req
            d['status'] = "Fully Allocated"
        elif avail > 0:
            d['allocated_qty'] = avail
            d['status'] = "Partial Fulfillment"
        else:
            d['allocated_qty'] = 0
            d['status'] = "Out of Stock"
            
        d['no_record'] = False # existed in cart means valid
        results.append(d)
        
    return results

def remove_from_cart_db(cart_id):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM cart WHERE id = :id"),
            {"id": cart_id}
        )

def update_cart_item_db(cart_id, new_qty):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE cart SET qty = :qty WHERE id = :id"),
            {"qty": new_qty, "id": cart_id}
        )

def clear_cart_db(user_id):
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM cart WHERE user_id = :user_id"),
            {"user_id": user_id}
        )

# ---------- ORDER PROCESSING ----------
def create_order(user_id, items, stock_type):
    # items is list of dict: {part_number, description, qty (This is REQUESTED), price, ...}
    
    total_alloc_price = 0
    engine = get_engine()
    
    try:
        with engine.begin() as conn:
            # 1. Create Order Header (Initial total 0, update later)
            result = conn.execute(
                text("INSERT INTO orders (user_id, total_price, stock_type) VALUES (:uid, 0, :stype) RETURNING order_id"),
                {"uid": user_id, "stype": stock_type}
            )
            order_id = result.fetchone()[0]
            
            for item in items:
                # 2. Check Live Stock
                # Lock row if possible, or just select
                row = conn.execute(
                    text("SELECT free_stock FROM parts_stock WHERE part_number = :pn AND stock_type = :stype"),
                    {"pn": item['part_number'], "stype": stock_type}
                ).fetchone()
                
                current_stock = row.free_stock if row else 0
                requested_qty = item['qty'] # The user's input
                
                # 3. Cap Allocation
                allocated_qty = min(requested_qty, current_stock)
                
                # 4. Deduct Stock (Only if allocated > 0)
                if allocated_qty > 0:
                    conn.execute(
                        text("UPDATE parts_stock SET free_stock = free_stock - :qty WHERE part_number = :pn AND stock_type = :stype"),
                        {"qty": allocated_qty, "pn": item['part_number'], "stype": stock_type}
                    )
                
                # 5. Insert Line Item
                # qty -> allocated_qty
                # requested_qty -> requested_qty
                
                conn.execute(
                    text("""
                    INSERT INTO order_items 
                    (order_id, part_number, description, qty, requested_qty, available_qty, price)
                    VALUES (:oid, :pn, :desc, :qty, :req, :avail, :price)
                    """),
                    {
                        "oid": order_id,
                        "pn": item['part_number'],
                        "desc": item['description'],
                        "qty": allocated_qty, # SAVED AS ALLOCATED
                        "req": requested_qty, # SAVED AS REQUESTED
                        "avail": current_stock, # Snapshot of stock at time of order
                        "price": item['price']
                    }
                )
                
                total_alloc_price += (allocated_qty * item['price'])
                
            # 6. Update Header Total
            conn.execute(
                text("UPDATE orders SET total_price = :tot WHERE order_id = :oid"),
                {"tot": total_alloc_price, "oid": order_id}
            )
            
            # 7. Clear Cart
            conn.execute(text("DELETE FROM cart WHERE user_id = :uid"), {"uid": user_id})
            
        return True, order_id
    except Exception as e:
        return False, str(e)

# ---------- BULK PROCESSING ----------
def process_bulk_enquiry(df_bulk, stock_type, adjustment_percent=0):
    df = df_bulk.copy()
    
    # Normalize headers
    df.columns = [str(c).strip().lower() for c in df.columns]

    # New parts_order file contains only: part_number, qty
    col_map = {}
    for c in df.columns:
        if 'part' in c or 'number' in c:
            col_map[c] = 'part_number'
        elif 'qty' in c or 'quantity' in c:
            col_map[c] = 'qty'
    
    df = df.rename(columns=col_map)
    
    # Keep only target columns
    if 'part_number' not in df.columns:
         if len(df.columns) >= 2:
            df.columns.values[0] = 'part_number'
            df.columns.values[1] = 'qty'

    # Filter df to only part_number and qty
    wanted_cols = ['part_number', 'qty']
    df = df[[c for c in wanted_cols if c in df.columns]]
            
    # Clean Part Number (Sanitization)
    if 'part_number' in df.columns:
        df['part_number'] = df['part_number'].astype(str).str.replace("-", "").str.strip()
    
    # Aggregation: Group by part_number to merge duplicates
    if 'part_number' in df.columns and 'qty' in df.columns:
        df = df.groupby('part_number', as_index=False)['qty'].sum()
        
    engine = get_engine()
    with engine.begin() as conn:
        stock = pd.read_sql(
            text("SELECT part_number, description, free_stock, price FROM parts_stock WHERE stock_type = :st AND is_active = TRUE"),
            conn,
            params={"st": stock_type}
        )
    
    # Apply Price Adjustment to Stock Lookup Table BEFORE merging
    if adjustment_percent != 0:
        stock['price'] = stock['price'].fillna(0) * (1 + adjustment_percent / 100.0)
        stock['price'] = stock['price'].round(2)
    
    # Merge
    merged = df.merge(stock, on="part_number", how="left")
    merged["no_record"] = merged["description"].isna()
    merged["available_qty"] = merged["free_stock"].fillna(0).astype(int)
    
    # Allocation Logic
    def calculate_allocation(row):
        if pd.isna(row['description']):
            return 0, "Invalid Part"
            
        req = row['qty']
        avail = row['available_qty']
        
        if avail >= req:
            return req, "Fully Allocated"
        elif avail > 0:
            return avail, "Partial Fulfillment"
        else:
            return 0, "Out of Stock"

    # Apply Logic
    allocation_results = merged.apply(calculate_allocation, axis=1, result_type='expand')
    merged[['allocated_qty', 'status']] = allocation_results
    
    # Remove free_stock to avoid duplicate/confusing columns if available_qty is used
    if 'free_stock' in merged.columns:
        merged = merged.drop(columns=['free_stock'])
        
    return merged

# ---------- ADMIN FUNCTIONS ----------
def get_all_orders():
    engine = get_engine()
    with engine.begin() as conn:
        # Get Headers
        headers = conn.execute(
            text("SELECT order_id, user_id, total_price, order_status, stock_type, timestamp FROM orders ORDER BY timestamp DESC")
        ).fetchall()
        
    return [dict(row._mapping) for row in headers]

def get_order_details(order_id):
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, order_id, part_number, description, qty, requested_qty, available_qty, price, no_record_flag FROM order_items WHERE order_id = :oid"),
            {"oid": order_id}
        ).fetchall()
    return [dict(row._mapping) for row in rows]

def restore_stock_from_order(conn, order_id):
    """
    Adds back the ALLOCATED quantity (qty) from order_items to parts_stock.
    MUST be called within an active transaction (conn).
    """
    # 1. Get items to restore
    items = conn.execute(
        text("SELECT part_number, qty FROM order_items WHERE order_id = :oid"),
        {"oid": order_id}
    ).fetchall()
    
    # 2. Identify stock type from header
    header = conn.execute(
        text("SELECT stock_type FROM orders WHERE order_id = :oid"),
        {"oid": order_id}
    ).fetchone()
    
    if not header:
        return # Orphaned items?
        
    stype = header.stock_type
    
    for item in items:
        # Restore only if something was allocated
        if item.qty > 0:
            conn.execute(
                text("UPDATE parts_stock SET free_stock = free_stock + :qty WHERE part_number = :pn AND stock_type = :stype"),
                {"qty": item.qty, "pn": item.part_number, "stype": stype}
            )

def update_order_status(order_id, status):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            # Check current status first to prevent double-restoration?
            # Ideally yes, but simplified here. 
            # If transitioning FROM Pending/Accepted TO Rejected -> Restore.
            # If transitioning FROM Rejected TO Accepted -> Deduct again? (Not implemented yet, assumed one-way or careful admin)
            
            # Simple Rule: If New Status is Rejected, Restore Stock.
            # WARNING: If Admin clicks Reject twice, it duplicates stock? 
            # Guard: Only restore if current status is NOT Rejected.
            
            curr = conn.execute(text("SELECT order_status FROM orders WHERE order_id = :oid"), {"oid": order_id}).fetchone()
            if curr and curr.order_status != 'Rejected' and status == 'Rejected':
                restore_stock_from_order(conn, order_id)
            
            conn.execute(
                text("UPDATE orders SET order_status = :status WHERE order_id = :oid"),
                {"status": status, "oid": order_id}
            )
        return True, "Updated"
    except Exception as e:
        return False, str(e)

def delete_order(order_id):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            # RESTORE STOCK BEFORE DELETE
            restore_stock_from_order(conn, order_id)
            
            conn.execute(text("DELETE FROM order_items WHERE order_id = :oid"), {"oid": order_id})
            conn.execute(text("DELETE FROM orders WHERE order_id = :oid"), {"oid": order_id})
        return True, "Deleted"
    except Exception as e:
        return False, str(e)

def delete_all_users_history():
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM order_items"))
            conn.execute(text("DELETE FROM orders"))
        return True, "All history deleted"
    except Exception as e:
        return False, str(e)

def delete_all_orders(stock_type):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            # Delete Items first (Foreign Key would cascade usually, but manual safety is good)
            # We need to find order_ids for this stock_type first, or use a join delete.
            # Postgres supports USING for delete joins, but standard SQL is subquery.
            
            conn.execute(
                text("""
                DELETE FROM order_items 
                WHERE order_id IN (SELECT order_id FROM orders WHERE stock_type = :st)
                """),
                {"st": stock_type}
            )
            
            # Delete Orders
            conn.execute(
                text("DELETE FROM orders WHERE stock_type = :st"),
                {"st": stock_type}
            )
            return True, "All orders deleted"
    except Exception as e:
        return False, str(e)

# ---------- USER MANAGEMENT (ADMIN) ----------
def get_all_users():
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT user_id, user_name, mail_id, phone_number, is_active, role, assigned_stock_type, price_adjustment_percent FROM customer_details ORDER BY user_id")
        ).fetchall()
    results = [dict(row._mapping) for row in rows]
    for r in results:
        r['price_adjustment_percent'] = float(r['price_adjustment_percent'] or 0)
    return results

def update_user_status(user_id, is_active):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE customer_details SET is_active = :status WHERE user_id = :uid"),
                {"status": is_active, "uid": user_id}
            )
        return True, "Updated"
    except Exception as e:
        return False, str(e)

def update_user_role(user_id, role):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE customer_details SET role = :role WHERE user_id = :uid"),
                {"role": role, "uid": user_id}
            )
        return True, "Updated"
    except Exception as e:
        return False, str(e)

def update_user_stock_assignment(user_id, stock_type):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE customer_details SET assigned_stock_type = :st WHERE user_id = :uid"),
                {"st": stock_type, "uid": user_id}
            )
        return True, "Updated"
    except Exception as e:
        return False, str(e)

def update_user_price_adjustment(user_id, percent):
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE customer_details SET price_adjustment_percent = :pct WHERE user_id = :uid"),
                {"pct": percent, "uid": user_id}
            )
        return True, "Updated"
    except Exception as e:
        return False, str(e)

def force_schema_cleanup():
    engine = get_engine()
    log = []
    try:
        with engine.begin() as conn:
            try:
                conn.execute(text("ALTER TABLE cart DROP COLUMN IF EXISTS delivery_area"))
                log.append("Dropped delivery_area from cart.")
            except Exception as e:
                log.append(f"Cart Error: {e}")
                
            try:
                conn.execute(text("ALTER TABLE order_items DROP COLUMN IF EXISTS delivery_area"))
                log.append("Dropped delivery_area from order_items.")
            except Exception as e:
                log.append(f"Items Error: {e}")
                
        return True, " | ".join(log)
    except Exception as e:
        return False, str(e)

# ---------- PROFILE & HISTORY ----------
def get_stock_csv(stock_type):
    engine = get_engine()
    # Read active parts for the assigned stock type with aliases
    # df = pd.read_sql(
    #     text("""
    #     SELECT part_number, description, free_stock as stock, price as "price($)" 
    #     FROM parts_stock 
    #     WHERE stock_type = :st AND is_active = TRUE
    #     """), 
    #     engine,
    #     params={"st": stock_type}
    # )
     df = pd.read_sql(
        text("""
        SELECT part_number, description, free_stock as stock" 
        FROM parts_stock 
        WHERE stock_type = :st AND is_active = TRUE
        """), 
        engine,
        params={"st": stock_type}
    )
    return df.to_csv(index=False).encode('utf-8')

def get_user_orders(user_id):
    engine = get_engine()
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT order_id, total_price, order_status, timestamp FROM orders WHERE user_id = :uid ORDER BY timestamp DESC"),
            {"uid": user_id}
        ).fetchall()
    return [dict(row._mapping) for row in rows]
