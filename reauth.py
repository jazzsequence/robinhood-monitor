#!/usr/bin/env python3
"""
Recreate the Robinhood session pickle from a browser token.

Run this when the session expires and automatic re-authentication fails:
    .venv/bin/python reauth.py
"""
import json
import os
import pickle
import secrets
import sys

PICKLE_PATH = os.path.expanduser("~/.tokens/robinhood.robin_token.pickle")
JS_COMMAND = "JSON.stringify(JSON.parse(localStorage.getItem('web:auth_state')))"


def generate_device_token():
    rands = [secrets.randbelow(256) for _ in range(16)]
    hexa = [str(hex(i + 256)).lstrip("0x")[1:] for i in range(256)]
    token = ""
    for i, r in enumerate(rands):
        token += hexa[r]
        if i in [3, 5, 7, 9]:
            token += "-"
    return token


def main():
    print("\nRobinhood session refresh")
    print("─" * 40)
    print("\n1. Open https://robinhood.com and log in")
    print("2. Open DevTools  (Cmd+Option+I on Mac)")
    print("3. Go to the Console tab")
    print("4. Paste and run this command:\n")
    print(f"   {JS_COMMAND}\n")
    print("5. Copy the output, paste it below, then press Enter twice:\n")

    lines = []
    try:
        while True:
            line = input()
            if not line and lines:
                break
            lines.append(line)
    except EOFError:
        pass

    raw = "\n".join(lines).strip()
    if not raw:
        print("No input received.")
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Could not parse JSON: {e}")
        print("Make sure you used JSON.stringify() in the console command.")
        sys.exit(1)

    for key in ("access_token", "refresh_token", "token_type"):
        if key not in data:
            print(f"Missing field: {key}")
            sys.exit(1)

    os.makedirs(os.path.dirname(PICKLE_PATH), exist_ok=True)
    with open(PICKLE_PATH, "wb") as f:
        pickle.dump({
            "token_type": data["token_type"],
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "device_token": generate_device_token(),
        }, f)

    print(f"\nSession saved to {PICKLE_PATH}")
    print("Run portfolio_monitor.py to verify.")


if __name__ == "__main__":
    main()
