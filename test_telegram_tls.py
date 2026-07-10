#!/usr/bin/env python
"""Quick diagnostic for Telegram API connectivity and TLS."""
import sys
import requests
import ssl

print("=== Telegram API Connectivity Test ===\n")

# Test 1: Raw socket SSL
print("Test 1: Raw TLS handshake to api.telegram.org:443")
try:
    import socket
    ctx = ssl.create_default_context()
    conn = socket.create_connection(('api.telegram.org', 443), timeout=10)
    sock = ctx.wrap_socket(conn, server_hostname='api.telegram.org')
    print("  ✓ TLS handshake successful")
    sock.close()
except Exception as e:
    print(f"  ✗ TLS handshake failed: {e}")

# Test 2: Requests GET
print("\nTest 2: HTTPS GET to api.telegram.org")
try:
    resp = requests.get('https://api.telegram.org/', timeout=10, allow_redirects=False)
    print(f"  ✓ GET successful: HTTP {resp.status_code}")
except Exception as e:
    print(f"  ✗ GET failed: {e}")

# Test 3: sendMessage POST (without auth)
print("\nTest 3: POST to /sendMessage endpoint (will fail auth, but tests connectivity)")
try:
    resp = requests.post(
        'https://api.telegram.org/bot000:XXX/sendMessage',
        data={"chat_id": "1", "text": "test"},
        timeout=10
    )
    print(f"  ✓ POST successful: HTTP {resp.status_code}")
except Exception as e:
    print(f"  ✗ POST failed: {e}")

print("\nDiagnostic complete.")
