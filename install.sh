#!/bin/bash
# install.sh — Skapar venv och installerar beroenden för cellar-eu MCP-server

set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
echo "=== Installerar cellar-eu MCP-server i $DIR ==="

if [ ! -f "$DIR/.env" ]; then
    echo "Fel: $DIR/.env saknas — kopiera config.example.env och fyll i DATABASE_URL"
    exit 1
fi

echo "Skapar virtuell miljö..."
python3 -m venv "$DIR/.venv"

echo "Installerar beroenden (detta kan ta ett par minuter — sentence-transformers är stor)..."
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

echo ""
echo "=== Klar! ==="
echo "Servern startas med:"
echo "  $DIR/.venv/bin/python3 $DIR/mcp_server.py"
echo ""
echo "Lägg till i claude_desktop_config.json:"
echo "  \"cellar-eu\": {"
echo "    \"command\": \"$DIR/.venv/bin/python3\","
echo "    \"args\": [\"$DIR/mcp_server.py\"]"
echo "  }"
