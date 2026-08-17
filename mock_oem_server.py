"""
Mock Maruti OEM API Server for testing the enrollment workflow.

This simulates the Maruti backend API:
- validate_vin: Returns true for all VINs except negative numbers
- capabilities: Returns a predefined list of capabilities
- enroll: Returns request_id immediately, then sends pub/sub messages
         for each product with 2s delay between each

Usage (without real Redis):
    USE_FAKE_REDIS=true python mock_oem_server.py

Usage (with real Redis):
    python mock_oem_server.py
"""

import os
import sys
import time
import json
import random
import threading
from flask import Flask, request, jsonify

# Use real Redis by default
USE_FAKE_REDIS = os.environ.get('USE_FAKE_REDIS', 'false').lower() == 'true'

# Shared fakeredis server instance for pub/sub to work across modules
_shared_fake_server = None

def get_redis_client():
    """Get a shared Redis client for pub/sub to work across processes."""
    global _shared_fake_server

    if USE_FAKE_REDIS:
        import fakeredis
        # IMPORTANT: Use a shared fakeredis server instance so that
        # the mock server's publish() calls are visible to Django's pub/sub listener.
        # Without this, each module gets its own isolated in-memory Redis
        # and pub/sub messages never reach the subscriber.
        if _shared_fake_server is None:
            _shared_fake_server = fakeredis.FakeServer()
            print("[INFO] Created shared fakeredis server for pub/sub")
        return fakeredis.FakeRedis(server=_shared_fake_server, decode_responses=True)
    else:
        import redis
        REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
        try:
            client = redis.from_url(REDIS_URL, decode_responses=True)
            client.ping()
            print(f"[INFO] Connected to real Redis at {REDIS_URL}")
            return client
        except Exception as e:
            print(f"[ERROR] Cannot connect to Redis: {e}")
            raise

redis_client = get_redis_client()

app = Flask(__name__)

# Pub/sub channel
PUBSUB_CHANNEL = 'enrollment:maruti'

# Default capabilities (used as fallback)
MARUTI_CAPABILITIES = ['gps', 'obd', 'immobilizer', 'speed_alert', 'fuel_monitor']


@app.route('/api/oem/maruti/validate-vin', methods=['POST'])
def validate_vin():
    """Validate VIN - returns true for all VINs except negative numbers."""
    data = request.json or {}
    vin = data.get('vin', '')

    print(f"[Maruti API] validate_vin called with VIN: {vin}")

    # Check for negative VIN (error case)
    try:
        if vin.replace('-', '').isdigit() and int(vin) < 0:
            return jsonify({
                'valid': False,
                'error': 'Invalid VIN format',
                'error_code': '5001'
            }), 400
    except (ValueError, AttributeError):
        pass

    time.sleep(0.2)

    return jsonify({
        'valid': True,
        'make': 'maruti',
        'model': 'swift',
        'year': 2024,
        'vin': vin
    })


@app.route('/api/oem/maruti/capabilities', methods=['POST'])
def capabilities():
    """Return all supported capabilities for Maruti."""
    data = request.json or {}
    vin = data.get('vin', '')

    print(f"[Maruti API] capabilities called for VIN: {vin}")
    time.sleep(0.3)

    return jsonify({
        'capabilities': MARUTI_CAPABILITIES,
        'supported': True,
        'vin': vin,
        'model': 'swift'
    })


@app.route('/api/oem/maruti/enroll', methods=['POST'])
def enroll():
    """Submit enrollment request."""
    data = request.json or {}
    vin = data.get('vin', '')
    products = data.get('products', [])

    print(f"[Maruti API] enroll called for VIN: {vin}, products: {products}")

    # Validate products
    if not products or not isinstance(products, list):
        print(f"[Maruti API] WARNING: No products for VIN={vin}, using default")
        products = list(MARUTI_CAPABILITIES)
    else:
        products = list(products)

    request_id = f"MARUTI-ENR-{vin}-{random.randint(10000, 99999)}"

    response = {
        'request_id': request_id,
        'status': 'processing',
        'vin': vin,
        'products': products,
        'message': 'Enrollment request accepted'
    }

    thread = threading.Thread(target=send_completion_messages, args=(request_id, vin, products))
    thread.daemon = True
    thread.start()

    return jsonify(response)


def send_completion_messages(request_id, vin, products):
    """Send completion messages to Redis pub/sub."""
    try:
        print(f"[Maruti API] Thread started: {vin}, products={products}")

        if not products:
            print(f"[Maruti API] ERROR: No products for {vin}")
            return

        total = len(products)

        for i, product in enumerate(products):
            product_name = str(product).strip()
            if not product_name:
                print(f"[Maruti API] WARNING: Empty product at index {i}")
                continue

            # Delay before each message to ensure subscriber is ready
            # First message needs longer delay for subscription setup
            if i == 0:
                print(f"[Maruti API] Waiting 500ms for subscriber setup...")
                time.sleep(0.5)
            else:
                print(f"[Maruti API] Waiting 2s... ({i+1}/{total})")
                time.sleep(2)

            message = {
                'request_id': request_id,
                'product_id': f'PROD-{product_name.upper()}',
                'vin': vin,
                'status': 'success',
                'error_code': '6700',
                'capability': product_name.lower(),
                'enrolled_at': time.strftime('%Y-%m-%dT%H:%M:%SZ')
            }

            redis_client.publish(PUBSUB_CHANNEL, json.dumps(message))
            print(f"[Maruti API] Published: {vin} -> {product_name} ({i+1}/{total})")

        print(f"[Maruti API] Thread completed: All {total} messages sent for {vin}")

    except Exception as e:
        print(f"[Maruti API] ERROR in thread for {vin}: {e}")
        import traceback
        traceback.print_exc()


@app.route('/api/oem/maruti/register-ready', methods=['POST'])
def register_ready():
    data = request.json or {}
    vin = data.get('vin', '')
    print(f"[Maruti API] register_ready called for VIN: {vin}")
    time.sleep(0.5)

    return jsonify({
        'registered': True,
        'status': 'active',
        'vin': vin,
        'vehicle_id': f'VEH-MARUTI-{vin}',
        'activated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ')
    })


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'healthy', 'service': 'maruti-oem-api'})


@app.route('/api/oem/maruti/status/<request_id>', methods=['GET'])
def get_status(request_id):
    print(f"[Maruti API] Status check for: {request_id}")
    return jsonify({
        'request_id': request_id,
        'status': 'completed',
        'completed_capabilities': MARUTI_CAPABILITIES
    })


if __name__ == '__main__':
    print("=" * 60)
    print("  Mock Maruti OEM API Server")
    print("=" * 60)
    print(f"  Redis: {'fakeredis' if USE_FAKE_REDIS else os.environ.get('REDIS_URL', 'redis://localhost:6379/0')}")
    print(f"  Pub/Sub Channel: {PUBSUB_CHANNEL}")
    print(f"  Capabilities: {MARUTI_CAPABILITIES}")
    print("=" * 60)
    print()
    print("[OK] Server ready on http://localhost:8001")
    app.run(host='0.0.0.0', port=8001, debug=False)
