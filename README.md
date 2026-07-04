# Clalit Appointment Scraper

Search for available specialist appointments on the Clalit healthcare system (Israel).

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# Edit .env with your Clalit credentials
```

## Usage

```bash
# Default: dermatology near Kiryat Tivon
python clalit.py

# Custom specialty and city
python clalit.py --specialty אורתופדיה --city חיפה

# Headless mode (no browser window)
python clalit.py --headless

# JSON output
python clalit.py --json

# List available specialties
python clalit.py --list-specialties
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `CLALIT_USER_ID` | Your Clalit user ID (teudat zehut number) |
| `CLALIT_USERNAME` | Your Clalit username |
| `CLALIT_PASSWORD` | Your Clalit password |

## How It Works

1. Logs into Clalit e-services
2. Opens the Tamuz appointment system (inside an iframe)
3. Selects the medical specialty
4. Types the city and selects from autocomplete
5. Enables "include nearby settlements"
6. Searches and parses the results
