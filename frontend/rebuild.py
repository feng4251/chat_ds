#!/usr/bin/env python3
"""Write all frontend source files from embedded strings."""
import os

ROOT = "/nfs/yangbb/codes/chat_ds/frontend/src"

def w(path, content):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    print(f"WROTE: {path} ({len(content)} bytes)")

w("App.jsx", open("TEMPLATES/App.jsx").read())
w("pages/Login.jsx", open("TEMPLATES/Login.jsx").read())
w("pages/Register.jsx", open("TEMPLATES/Register.jsx").read())
w("pages/Chat.jsx", open("TEMPLATES/Chat.jsx").read())
w("components/Sidebar.jsx", open("TEMPLATES/Sidebar.jsx").read())
w("components/ChatArea.jsx", open("TEMPLATES/ChatArea.jsx").read())
w("components/Settings.jsx", open("TEMPLATES/Settings.jsx").read())
print("ALL DONE")
