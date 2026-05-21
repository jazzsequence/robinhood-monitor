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
import subprocess
import sys

PICKLE_PATH = os.path.expanduser("~/.tokens/robinhood.robin_token.pickle")

# Extracts only the fields we need, keeping the clipboard payload small
JS_COMMAND = (
    "(function(){"
    "var s=JSON.parse(localStorage.getItem('web:auth_state'));"
    "copy(JSON.stringify({access_token:s.access_token,refresh_token:s.refresh_token,token_type:s.token_type}))"
    "})()"
)


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
    # Copy the JS command to clipboard so the user doesn't have to
    subprocess.run(["pbcopy"], input=JS_COMMAND.encode(), check=True)

    print("\nRobinhood session refresh")
    print("─" * 40)
    print("\nThe JS command has been copied to your clipboard.")
    print("\n1. Open https://robinhood.com and log in")
    print("2. Open DevTools (Cmd+Option+I) → Console tab")
    print("3. Paste and run the command — it will copy your token to the clipboard")
    print("\nPress Enter when done...")
    input()

    # Read the token the browser's copy() put in the clipboard
    result = subprocess.run(["pbpaste"], capture_output=True, text=True)
    raw = result.stdout.strip().strip("'")

    if not raw:
        print("Clipboard is empty. Make sure the JS command ran successfully.")
        sys.exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Could not parse token: {e}")
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

    print(f"Session saved to {PICKLE_PATH}")
    print("Run portfolio_monitor.py to verify.")


if __name__ == "__main__":
    main()
