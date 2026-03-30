#!/bin/bash
set -e

cd "$(dirname "$0")"

echo "=== FreeWhisper Setup ==="
echo ""

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "Error: python3 not found. Install it via: brew install python3"
    exit 1
fi

# Create venv
if [ ! -d ".venv" ]; then
    echo "[1/3] Creating virtual environment..."
    python3 -m venv .venv
else
    echo "[1/3] Virtual environment already exists."
fi

# Install dependencies
echo "[2/3] Installing dependencies..."
.venv/bin/pip install -q -r requirements.txt

# Create config from template if missing
if [ ! -f "config.json" ]; then
    echo "[*] Creating config.json from template..."
    cp config.example.json config.json
fi

# Check API key
API_KEY=$(python3 -c "import json; print(json.load(open('config.json')).get('api_key',''))" 2>/dev/null || echo "")
if [ -z "$API_KEY" ]; then
    echo ""
    echo "[!] API key not configured."
    echo "    1. Get your free key at: https://app.gladia.io/account/api-keys"
    echo "    2. Edit config.json and paste it in the \"api_key\" field"
    echo ""
fi

# Create launcher script
cat > run.sh << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
.venv/bin/python free_whisper.py
EOF
chmod +x run.sh

echo "[3/3] Setup complete!"
echo ""
echo "Usage:"
echo "  open FreeWhisper.app      # Launch FreeWhisper"
echo "  ./run.sh                  # Dev launcher"
echo "  open config.json          # Edit settings"
echo ""
echo "Hotkeys (default — configurable via Settings):"
echo "  Hold Right Option -> record"
echo "  Release           -> transcribe & paste"
echo "  Escape            -> cancel recording"
echo ""
echo "Permissions required (macOS will prompt you):"
echo "  - Input Monitoring   (for global hotkey)"
echo "  - Accessibility      (for text paste simulation)"
echo "  - Microphone         (for audio capture)"
