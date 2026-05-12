with open("app/agents/team.py", "rb") as f:
    data = f.read()

# Fix: the last char before \r should be ' (single quote), not " (double quote)
# Find the problematic line
marker = b'"status": "valid|invalid|duplicate"'
idx = data.find(marker)
if idx >= 0:
    line_end = data.find(b"\r", idx)
    # The char just before \r
    print(f"Char before CR: {chr(data[line_end-1])} (hex {data[line_end-1]:02x})")
    # Change " to ' before the \r
    if data[line_end-1] == ord('"'):
        data = data[:line_end-1] + b"'" + data[line_end:]
        print("Fixed: changed trailing double-quote to single-quote")

with open("app/agents/team.py", "wb") as f:
    f.write(data)

# Verify it compiles
try:
    compile(data.decode("utf-8"), "team.py", "exec")
    print("Compiles OK")
except SyntaxError as e:
    print(f"Still broken: {e}")
